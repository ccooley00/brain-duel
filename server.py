"""
Brain Duel - A 2-player networked trivia & logic challenge game.
Run: python server.py
Then open http://localhost:8080 in two browser tabs (or on two different machines).
For remote play, use your public IP or set up port forwarding on port 8080.
"""

import collections
import http.server
import json
import os
import random
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid

# Force unbuffered stdout so print() shows in Render logs immediately
sys.stdout.reconfigure(line_buffering=True)

# ── Question Banks ───────────────────────────────────────────────────────────

from questions_fun import QUESTIONS as FUN_QUESTIONS
from questions_nyc import QUESTIONS as NYC_QUESTIONS
from questions_gaming import QUESTIONS as GAMING_QUESTIONS
from questions_colgate import QUESTIONS as COLGATE_QUESTIONS
from questions_cooleys import QUESTIONS as COOLEYS_QUESTIONS
from questions_buildings import QUESTIONS as BUILDINGS_QUESTIONS
from questions_logic import QUESTIONS as LOGIC_QUESTIONS
from questions_awh import QUESTIONS as AWH_QUESTIONS

QUESTION_BANKS = {
    "fun": FUN_QUESTIONS,
    "nyc": NYC_QUESTIONS,
    "gaming": GAMING_QUESTIONS,
    "colgate": COLGATE_QUESTIONS,
    "cooleys": COOLEYS_QUESTIONS,
    "buildings": BUILDINGS_QUESTIONS,
    "logic": LOGIC_QUESTIONS,
    "awh": AWH_QUESTIONS,
}

MODE_NAMES = {
    "fun": "Fun",
    "nyc": "NYC History",
    "gaming": "Gaming",
    "colgate": "Colgate University",
    "cooleys": "History of Cooleys",
    "buildings": "Famous Buildings",
    "logic": "Logic",
    "awh": "History of AWH",
}

TOTAL_ROUNDS = 10
POINTS_CORRECT = 10
POINTS_SPEED_BONUS = 5  # bonus for answering first AND correctly
ROUND_TIMEOUT = 15      # seconds before auto-submitting wrong answer

COMPUTER_OPPONENTS = {
    "hal": {"name": "HAL 9000", "accuracy": 0.30, "min_time": 8.0, "max_time": 15.0},
    "terminator": {"name": "Terminator", "accuracy": 0.65, "min_time": 4.0, "max_time": 8.0},
    "claude": {"name": "Claude", "accuracy": 0.92, "min_time": 1.5, "max_time": 4.0},
}

# ── Game State ───────────────────────────────────────────────────────────────

games = {}       # game_id -> game dict
game_lock = threading.Lock()  # protects all shared state

# ── Team Mode ──────────────────────────────────────────────────────────────

team_game_state = {"queue": None}  # game_id of active team game being set up
TEAM_DISCONNECT_TIMEOUT = 10       # seconds before player considered disconnected
TEAM_RESULT_DISPLAY_TIME = 8       # seconds to show round results in team mode

# ── Matchmaking Pool ─────────────────────────────────────────────────────────

matchmaking_pool = {}   # player_id -> {name, selected, last_poll, joined_at}
pending_matches = {}    # match_id -> {players: {pid: {name, category}}, created_at, game_id}
POOL_STALE_TIMEOUT = 15


def cleanup_stale_pool():
    """Remove stale pool entries and pending matches."""
    now = time.time()
    stale = [pid for pid, e in matchmaking_pool.items()
             if now - e["last_poll"] > POOL_STALE_TIMEOUT]
    for pid in stale:
        del matchmaking_pool[pid]
    # Clear dangling selections
    for entry in matchmaking_pool.values():
        if entry["selected"] and entry["selected"] not in matchmaking_pool:
            entry["selected"] = None
    # Clean up old pending matches
    stale_m = [mid for mid, m in pending_matches.items()
               if now - m["created_at"] > 120]
    for mid in stale_m:
        del pending_matches[mid]

# ── Supabase Leaderboard ────────────────────────────────────────────────────

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")


def _supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def _load_leaderboard(mode="fun"):
    """Load top 10 from Supabase for a specific mode, sorted by score desc then time asc."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        print(f"[LEADERBOARD] Skipping load — SUPABASE_URL={'set' if SUPABASE_URL else 'MISSING'}, SUPABASE_KEY={'set' if SUPABASE_KEY else 'MISSING'}")
        return []
    try:
        url = (
            f"{SUPABASE_URL}/rest/v1/leaderboard"
            f"?select=name,score,total_time"
            f"&mode=eq.{mode}"
            f"&order=score.desc,total_time.asc"
            f"&limit=10"
        )
        req = urllib.request.Request(url, headers=_supabase_headers())
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            print(f"[LEADERBOARD] Loaded {len(data)} entries for mode={mode}")
            return data
    except Exception as e:
        print(f"[LEADERBOARD] Load FAILED for mode={mode}: {e}")
        return []


def _insert_leaderboard_entry(name, score, total_time, mode="fun"):
    """Insert a single entry into the Supabase leaderboard."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        print(f"[LEADERBOARD] Skipping insert — SUPABASE_URL={'set' if SUPABASE_URL else 'MISSING'}, SUPABASE_KEY={'set' if SUPABASE_KEY else 'MISSING'}")
        return
    try:
        url = f"{SUPABASE_URL}/rest/v1/leaderboard"
        payload = {"name": name, "score": score, "total_time": total_time, "mode": mode}
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers=_supabase_headers(), method="POST")
        resp = urllib.request.urlopen(req, timeout=5)
        print(f"[LEADERBOARD] Inserted: {name} score={score} time={total_time} mode={mode} (HTTP {resp.status})")
    except Exception as e:
        print(f"[LEADERBOARD] Insert FAILED for {name}: {e}")


recent_question_indices = collections.deque(maxlen=2)  # last 2 games' question index sets



def new_game(mode="fun", mode2=None):
    game_id = str(uuid.uuid4())[:8]
    mixed = bool(mode2 and mode2 != mode)

    # Exclude questions used in the last 2 games
    excluded = set()
    for s in recent_question_indices:
        excluded |= s

    if mixed:
        # Draw 5 from each category
        pool1 = QUESTION_BANKS.get(mode, QUESTION_BANKS["fun"])
        pool2 = QUESTION_BANKS.get(mode2, QUESTION_BANKS["fun"])
        eligible1 = [(i, q) for i, q in enumerate(pool1) if f"{mode}:{i}" not in excluded]
        eligible2 = [(i, q) for i, q in enumerate(pool2) if f"{mode2}:{i}" not in excluded]
        if len(eligible1) < 5:
            eligible1 = list(enumerate(pool1))
        if len(eligible2) < 5:
            eligible2 = list(enumerate(pool2))
        chosen1 = random.sample(eligible1, min(5, len(eligible1)))
        chosen2 = random.sample(eligible2, min(5, len(eligible2)))
        recent_question_indices.append(
            {f"{mode}:{i}" for i, q in chosen1} | {f"{mode2}:{i}" for i, q in chosen2}
        )
        questions = [q for i, q in chosen1] + [q for i, q in chosen2]
        random.shuffle(questions)
    else:
        questions_pool = QUESTION_BANKS.get(mode, QUESTION_BANKS["fun"])
        eligible = [(i, q) for i, q in enumerate(questions_pool) if f"{mode}:{i}" not in excluded]
        if len(eligible) < TOTAL_ROUNDS:
            eligible = list(enumerate(questions_pool))
        chosen = _select_with_image_guarantee(mode, questions_pool, eligible, TOTAL_ROUNDS, excluded)
        recent_question_indices.append({f"{mode}:{i}" for i, q in chosen})
        questions = [q for i, q in chosen]

    # Shuffle choices for each question
    for q in questions:
        choices_key = "choices" if "choices" in q else "options"
        paired = list(zip(q[choices_key], [c == q["answer"] for c in q[choices_key]]))
        random.shuffle(paired)
        q[choices_key] = [p[0] for p in paired]
        q["answer"] = next(p[0] for p in paired if p[1])
    game = {
        "id": game_id,
        "players": {},          # player_id -> {name, score}
        "player_order": [],     # [player_id, player_id]
        "questions": questions,
        "round": 0,
        "round_start": None,
        "answers": {},          # round -> {player_id: {choice, time, correct}}
        "ready": {},             # player_id -> True for players who clicked Begin
        "state": "waiting",     # waiting | lobby | active | round_result | finished
        "result_shown_at": None,
        "round_results": [],    # list of per-round summaries
        "last_poll": {},        # player_id -> timestamp of last poll
        "created_at": time.time(),
        "mode": mode,
        "mode2": mode2 if mixed else None,
        "mixed": mixed,
    }
    games[game_id] = game
    return game


def _get_player_team(game, player_id):
    """Return 'A' or 'B' for the player's team, or None."""
    for team_key in ("A", "B"):
        if player_id in game["teams"][team_key]["players"]:
            return team_key
    return None


MIN_IMAGE_QUESTIONS = 3  # minimum photo questions in AWH mode


def _select_with_image_guarantee(mode, pool, eligible, count, excluded):
    """Select questions ensuring at least MIN_IMAGE_QUESTIONS have images (for AWH mode).
    Photo questions are spread evenly throughout the game, not clustered together."""
    if mode != "awh":
        return random.sample(eligible, min(count, len(eligible)))
    # Separate image and non-image eligible questions
    img_eligible = [(i, q) for i, q in eligible if "image" in q]
    non_img_eligible = [(i, q) for i, q in eligible if "image" not in q]
    # Guarantee at least MIN_IMAGE_QUESTIONS image questions
    img_needed = min(MIN_IMAGE_QUESTIONS, len(img_eligible))
    img_chosen = random.sample(img_eligible, img_needed)
    remaining_count = count - img_needed
    remaining_pool = non_img_eligible + [x for x in img_eligible if x not in img_chosen]
    rest_chosen = random.sample(remaining_pool, min(remaining_count, len(remaining_pool)))
    # Spread photo questions evenly: place them in distributed slots
    all_chosen = rest_chosen[:]  # start with non-image (mostly)
    random.shuffle(all_chosen)
    img_list = list(img_chosen)
    random.shuffle(img_list)
    if img_list and all_chosen:
        spacing = len(all_chosen) // (len(img_list) + 1)
        for idx, img_q in enumerate(img_list):
            insert_pos = min((idx + 1) * spacing + idx, len(all_chosen))
            all_chosen.insert(insert_pos, img_q)
    else:
        all_chosen.extend(img_list)
    return all_chosen


def _generate_questions_for_modes(modes):
    """Generate TOTAL_ROUNDS questions from the given mode(s)."""
    excluded = set()
    for s in recent_question_indices:
        excluded |= s
    all_questions = []
    tracking = set()
    if len(modes) == 1:
        mode = modes[0]
        pool = QUESTION_BANKS.get(mode, QUESTION_BANKS["fun"])
        eligible = [(i, q) for i, q in enumerate(pool) if f"{mode}:{i}" not in excluded]
        if len(eligible) < TOTAL_ROUNDS:
            eligible = list(enumerate(pool))
        chosen = _select_with_image_guarantee(mode, pool, eligible, TOTAL_ROUNDS, excluded)
        tracking = {f"{mode}:{i}" for i, q in chosen}
        all_questions = [q for i, q in chosen]
    else:
        per_cat = TOTAL_ROUNDS // len(modes)
        remainder = TOTAL_ROUNDS % len(modes)
        for i, mode in enumerate(modes):
            count = per_cat + (1 if i < remainder else 0)
            pool = QUESTION_BANKS.get(mode, QUESTION_BANKS["fun"])
            eligible = [(j, q) for j, q in enumerate(pool) if f"{mode}:{j}" not in excluded]
            if len(eligible) < count:
                eligible = list(enumerate(pool))
            chosen = random.sample(eligible, min(count, len(eligible)))
            tracking |= {f"{mode}:{j}" for j, q in chosen}
            all_questions.extend([q for j, q in chosen])
        random.shuffle(all_questions)
    recent_question_indices.append(tracking)
    for q in all_questions:
        choices_key = "choices" if "choices" in q else "options"
        paired = list(zip(q[choices_key], [c == q["answer"] for c in q[choices_key]]))
        random.shuffle(paired)
        q[choices_key] = [p[0] for p in paired]
        q["answer"] = next(p[0] for p in paired if p[1])
    return all_questions


def new_team_game(host_id, host_name):
    """Create a new team game (questions generated after category vote)."""
    game_id = str(uuid.uuid4())[:8]
    game = {
        "id": game_id,
        "team_mode": True,
        "host_id": host_id,
        "players": {
            host_id: {"name": host_name, "score": 0, "total_time": 0.0, "correct_count": 0},
        },
        "player_order": [host_id],
        "questions": [],
        "round": 0,
        "round_start": None,
        "answers": {},
        "ready": {},
        "state": "team_lobby",
        "result_shown_at": None,
        "round_results": [],
        "last_poll": {host_id: time.time()},
        "created_at": time.time(),
        "mode": None,
        "teams": {
            "A": {"name": "Team A", "players": []},
            "B": {"name": "Team B", "players": []},
        },
        "team_finalized": False,
        "category_votes": {},
        "disconnected": [],
        "team_scores": {"A": 0, "B": 0},
    }
    games[game_id] = game
    return game


def get_safe_state(game, player_id):
    """Return game state safe to send to a specific player."""
    g = game
    p = g["players"].get(player_id, {})
    opponent_id = None
    for pid in g["player_order"]:
        if pid != player_id:
            opponent_id = pid

    opponent = g["players"].get(opponent_id, {}) if opponent_id else {}

    state = {
        "game_id": g["id"],
        "state": g["state"],
        "round": g["round"] + 1,
        "total_rounds": TOTAL_ROUNDS,
        "your_name": p.get("name", ""),
        "your_score": p.get("score", 0),
        "opponent_name": opponent.get("name", "Waiting..."),
        "opponent_score": opponent.get("score", 0),
        "round_results": g["round_results"],
        "mode": g.get("mode", "fun"),
        "mode_name": (
            f"{MODE_NAMES.get(g['mode'], 'Fun')} + {MODE_NAMES.get(g.get('mode2', ''), '')}"
            if g.get("mixed")
            else MODE_NAMES.get(g.get("mode", "fun"), "Fun")
        ),
        "computer": bool(g.get("computer_player_id")),
    }

    if g["state"] == "lobby":
        state["your_ready"] = player_id in g["ready"]
        state["opponent_ready"] = opponent_id in g["ready"] if opponent_id else False
        # Include per-player category picks for dual-player games
        pcats = g.get("player_categories")
        if pcats:
            state["your_category"] = MODE_NAMES.get(pcats.get(player_id, ""), "")
            state["opponent_category"] = MODE_NAMES.get(pcats.get(opponent_id, ""), "") if opponent_id else ""
            state["categories_match"] = (pcats.get(player_id) == pcats.get(opponent_id)) if opponent_id else True

    if g["state"] == "active":
        q = g["questions"][g["round"]]
        choices_key = "choices" if "choices" in q else "options"
        state["question"] = {
            "category": q.get("category", ""),
            "text": q["question"],
            "choices": q[choices_key],
        }
        if "image" in q:
            state["question"]["image"] = q["image"]
        # Countdown timer
        round_start = g.get("round_start")
        if round_start:
            state["time_remaining"] = max(0, round(ROUND_TIMEOUT - (time.time() - round_start), 1))
        # Has this player already answered this round?
        rd_answers = g["answers"].get(g["round"], {})
        if player_id in rd_answers:
            state["already_answered"] = True
            state["your_choice"] = rd_answers[player_id]["choice"]

    if g["state"] == "round_result":
        state["last_result"] = g["round_results"][-1] if g["round_results"] else None

    if g["state"] == "finished":
        if p.get("score", 0) > opponent.get("score", 0):
            state["outcome"] = "win"
        elif p.get("score", 0) < opponent.get("score", 0):
            state["outcome"] = "lose"
        else:
            state["outcome"] = "tie"
        state["your_total_time"] = round(p.get("total_time", 0), 1)
        state["opponent_total_time"] = round(opponent.get("total_time", 0), 1)
        # Include leaderboard rank if available
        ranks = g.get("leaderboard_ranks", {})
        if player_id in ranks:
            state["leaderboard_rank"] = ranks[player_id]

    return state


def get_team_safe_state(game, player_id):
    """Return team game state safe to send to a specific player."""
    g = game
    p = g["players"].get(player_id, {})
    my_team = _get_player_team(g, player_id)
    state = {
        "game_id": g["id"],
        "state": g["state"],
        "team_mode": True,
        "your_name": p.get("name", ""),
        "your_team": my_team,
        "is_host": player_id == g.get("host_id"),
        "mode": g.get("mode"),
        "teams": {
            "A": {
                "name": g["teams"]["A"]["name"],
                "players": [g["players"][pid]["name"] for pid in g["teams"]["A"]["players"]
                            if pid in g["players"]],
                "player_count": len(g["teams"]["A"]["players"]),
                "score": g.get("team_scores", {}).get("A", 0),
            },
            "B": {
                "name": g["teams"]["B"]["name"],
                "players": [g["players"][pid]["name"] for pid in g["teams"]["B"]["players"]
                            if pid in g["players"]],
                "player_count": len(g["teams"]["B"]["players"]),
                "score": g.get("team_scores", {}).get("B", 0),
            },
        },
    }
    if g["state"] == "team_lobby":
        assigned = set(g["teams"]["A"]["players"]) | set(g["teams"]["B"]["players"])
        unassigned = [{"player_id": pid, "name": g["players"][pid]["name"]}
                      for pid in g["player_order"] if pid not in assigned]
        state["unassigned_players"] = unassigned
        state["team_finalized"] = g.get("team_finalized", False)
        if player_id == g.get("host_id"):
            state["all_players"] = [
                {"player_id": pid, "name": g["players"][pid]["name"],
                 "team": _get_player_team(g, pid)}
                for pid in g["player_order"]
            ]
    if g["state"] == "team_preview":
        state["is_host"] = player_id == g.get("host_id")
    if g["state"] == "team_vote":
        total_players = len(g["player_order"])
        votes_in = len(g.get("category_votes", {}))
        state["vote_progress"] = {"voted": votes_in, "total": total_players}
        state["has_voted"] = player_id in g.get("category_votes", {})
    if g["state"] in ("active", "round_result", "finished"):
        state["round"] = g["round"] + 1
        state["total_rounds"] = TOTAL_ROUNDS
        state["mode_name"] = g.get("voted_category_name", "")
    if g["state"] == "active":
        q = g["questions"][g["round"]]
        choices_key = "choices" if "choices" in q else "options"
        state["question"] = {
            "category": q.get("category", ""),
            "text": q["question"],
            "choices": q[choices_key],
        }
        if "image" in q:
            state["question"]["image"] = q["image"]
        round_start = g.get("round_start")
        if round_start:
            state["time_remaining"] = max(0, round(ROUND_TIMEOUT - (time.time() - round_start), 1))
        rd_answers = g["answers"].get(g["round"], {})
        if player_id in rd_answers:
            state["already_answered"] = True
            state["your_choice"] = rd_answers[player_id]["choice"]
        if my_team:
            teammates = g["teams"][my_team]["players"]
            active_teammates = [pid for pid in teammates if pid not in g.get("disconnected", [])]
            answered_teammates = sum(1 for pid in active_teammates if pid in rd_answers)
            state["teammate_progress"] = {"answered": answered_teammates, "total": len(active_teammates)}
    if g["state"] == "round_result":
        state["last_result"] = g["round_results"][-1] if g["round_results"] else None
    if g["state"] == "finished":
        a_score = g.get("team_scores", {}).get("A", 0)
        b_score = g.get("team_scores", {}).get("B", 0)
        if a_score > b_score:
            state["winning_team"] = "A"
        elif b_score > a_score:
            state["winning_team"] = "B"
        else:
            state["winning_team"] = "tie"
        state["round_results"] = g["round_results"]
        state["mvp"] = g.get("mvp")
        if g.get("rematch_game_id"):
            state["rematch_game_id"] = g["rematch_game_id"]
        if g.get("newgame_id"):
            state["newgame_id"] = g["newgame_id"]
    return state


def advance_round(game):
    """Check if round is complete and advance."""
    rd = game["round"]
    rd_answers = game["answers"].get(rd, {})

    if len(rd_answers) < 2:
        return  # still waiting for both players

    q = game["questions"][rd]
    correct_answer = q["answer"]

    # Determine who answered first
    times = [(pid, a["time"]) for pid, a in rd_answers.items()]
    times.sort(key=lambda x: x[1])
    first_pid = times[0][0]

    result = {"round": rd + 1, "category": q.get("category", ""), "question": q["question"],
              "correct_answer": correct_answer, "players": {}}

    round_start = game.get("round_start") or game["created_at"]

    for pid, a in rd_answers.items():
        is_correct = a["choice"] == correct_answer
        points = 0
        if is_correct:
            points += POINTS_CORRECT
            if pid == first_pid:
                points += POINTS_SPEED_BONUS
        game["players"][pid]["score"] += points
        time_taken = round(a["time"] - round_start, 2)
        game["players"][pid]["total_time"] += time_taken
        pname = game["players"][pid]["name"]
        result["players"][pname] = {
            "choice": a["choice"] if a["choice"] is not None else "No answer",
            "correct": is_correct,
            "points": points,
            "was_first": pid == first_pid,
            "time_taken": time_taken,
            "timed_out": a["choice"] is None,
        }

    game["round_results"].append(result)
    game["state"] = "round_result"
    game["result_shown_at"] = time.time()

    # After a short display period the client will request next round
    if rd + 1 >= TOTAL_ROUNDS:
        game["state"] = "finished"
        _update_leaderboard(game)
    else:
        game["round"] += 1


def advance_round_team(game):
    """Check if team round is complete and advance."""
    rd = game["round"]
    rd_answers = game["answers"].get(rd, {})
    active_players = [pid for pid in game["player_order"]
                      if pid not in game.get("disconnected", [])]
    for pid in active_players:
        if pid not in rd_answers:
            return  # Still waiting

    q = game["questions"][rd]
    correct_answer = q["answer"]
    round_start = game.get("round_start") or game["created_at"]
    team_correct = {"A": 0, "B": 0}
    team_time = {"A": 0.0, "B": 0.0}
    team_player_count = {"A": 0, "B": 0}

    for pid, a in rd_answers.items():
        team = _get_player_team(game, pid)
        if not team:
            continue
        is_correct = a["choice"] == correct_answer
        time_taken = round(a["time"] - round_start, 2)
        game["players"][pid]["total_time"] += time_taken
        team_player_count[team] += 1
        team_time[team] += time_taken
        if is_correct:
            team_correct[team] += 1
            game["players"][pid]["correct_count"] = game["players"][pid].get("correct_count", 0) + 1

    a_raw = team_correct["A"]
    b_raw = team_correct["B"]
    a_time = round(team_time["A"], 1)
    b_time = round(team_time["B"], 1)
    a_count = team_player_count["A"]
    b_count = team_player_count["B"]
    a_has = a_count > 0
    b_has = b_count > 0

    # Normalize scores: (correct / team_size) * 10 so both teams on same scale
    a_base = round((a_raw / a_count) * 10, 2) if a_has else 0
    b_base = round((b_raw / b_count) * 10, 2) if b_has else 0
    # Accuracy percentages for bonus comparison
    a_pct = a_raw / a_count if a_has else 0
    b_pct = b_raw / b_count if b_has else 0

    # Determine bonus (compare accuracy percentages, not raw counts)
    bonus = None
    if a_pct > b_pct and a_has and b_has and a_time < b_time:
        bonus = {"team": "A", "type": "exceeds_expectations",
                 "name": "Exceeds Expectations Bonus", "multiplier": 1.5}
    elif b_pct > a_pct and a_has and b_has and b_time < a_time:
        bonus = {"team": "B", "type": "exceeds_expectations",
                 "name": "Exceeds Expectations Bonus", "multiplier": 1.5}
    elif abs(a_pct - b_pct) < 0.001 and a_pct > 0 and a_has and b_has:
        if a_time < b_time:
            bonus = {"team": "A", "type": "quick_response",
                     "name": "Quick Response Bonus", "multiplier": 1.25}
        elif b_time < a_time:
            bonus = {"team": "B", "type": "quick_response",
                     "name": "Quick Response Bonus", "multiplier": 1.25}

    a_final = a_base
    b_final = b_base
    if bonus:
        if bonus["team"] == "A":
            a_final = round(a_base * bonus["multiplier"], 2)
        else:
            b_final = round(b_base * bonus["multiplier"], 2)

    game["team_scores"]["A"] += a_final
    game["team_scores"]["B"] += b_final

    result = {
        "round": rd + 1,
        "category": q.get("category", ""),
        "question": q["question"],
        "correct_answer": correct_answer,
        "team_results": {
            "A": {"correct": a_raw, "total_players": a_count, "time": a_time,
                   "base_score": a_base, "final_score": a_final,
                   "name": game["teams"]["A"]["name"]},
            "B": {"correct": b_raw, "total_players": b_count, "time": b_time,
                   "base_score": b_base, "final_score": b_final,
                   "name": game["teams"]["B"]["name"]},
        },
        "bonus": bonus,
        "team_a_cumulative": game["team_scores"]["A"],
        "team_b_cumulative": game["team_scores"]["B"],
    }

    game["round_results"].append(result)
    game["state"] = "round_result"
    game["result_shown_at"] = time.time()

    if rd + 1 >= TOTAL_ROUNDS:
        game["state"] = "finished"
        _calculate_mvp(game)
        _update_team_leaderboard(game)
    else:
        game["round"] += 1


def _get_leaderboard_rank(name, score, total_time, mode):
    """Get a player's rank on the leaderboard (1-indexed), or None if not in top 10."""
    board = _load_leaderboard(mode)
    for i, entry in enumerate(board):
        if (entry["name"] == name
                and entry["score"] == score
                and abs(entry["total_time"] - total_time) < 0.5):
            return i + 1
    return None


def _update_leaderboard(game):
    """Add players from a finished game to the Supabase leaderboard."""
    if game.get("mixed"):
        return
    mode = game.get("mode", "fun")
    cpu_id = game.get("computer_player_id")
    for pid in game["player_order"]:
        if pid == cpu_id:
            continue
        p = game["players"][pid]
        _insert_leaderboard_entry(p["name"], p["score"], round(p["total_time"], 1), mode)
    # Compute leaderboard ranks after all inserts
    game["leaderboard_ranks"] = {}
    for pid in game["player_order"]:
        if pid == cpu_id:
            continue
        p = game["players"][pid]
        rank = _get_leaderboard_rank(p["name"], p["score"], round(p["total_time"], 1), mode)
        if rank:
            game["leaderboard_ranks"][pid] = rank


def _update_team_leaderboard(game):
    """Add winning team to the team leaderboard."""
    a_score = game.get("team_scores", {}).get("A", 0)
    b_score = game.get("team_scores", {}).get("B", 0)
    if a_score == b_score:
        return
    winner = game["teams"]["A"] if a_score > b_score else game["teams"]["B"]
    score = max(a_score, b_score)
    _insert_leaderboard_entry(winner["name"], score, 0, "team")


def _resolve_category_vote(game):
    """Tally votes and generate questions for the winning category/categories."""
    votes = game.get("category_votes", {})
    if not votes:
        return
    vote_counts = {}
    for cat in votes.values():
        vote_counts[cat] = vote_counts.get(cat, 0) + 1
    max_votes = max(vote_counts.values())
    winning_cats = sorted([cat for cat, count in vote_counts.items() if count == max_votes])
    game["questions"] = _generate_questions_for_modes(winning_cats)
    if len(winning_cats) == 1:
        game["mode"] = winning_cats[0]
        game["voted_category_name"] = MODE_NAMES.get(winning_cats[0], winning_cats[0])
    else:
        game["mode"] = "mixed"
        game["voted_category_name"] = " + ".join(MODE_NAMES.get(c, c) for c in winning_cats)


def _check_team_disconnects(game):
    """Mark team players as disconnected if they haven't polled recently."""
    if not game.get("team_mode") or game["state"] not in ("active", "round_result"):
        return
    now = time.time()
    disconnected = game.get("disconnected", [])
    for pid in game["player_order"]:
        if pid in disconnected:
            continue
        last_poll = game["last_poll"].get(pid, 0)
        if now - last_poll > TEAM_DISCONNECT_TIMEOUT:
            disconnected.append(pid)
            if game["state"] == "active":
                rd = game["round"]
                if rd not in game["answers"]:
                    game["answers"][rd] = {}
                if pid not in game["answers"][rd]:
                    round_start = game.get("round_start") or game["created_at"]
                    game["answers"][rd][pid] = {
                        "choice": None,
                        "time": now,
                        "correct": False,
                    }
    game["disconnected"] = disconnected


def _calculate_mvp(game):
    """Calculate MVP: most correct answers, fastest avg time as tiebreaker."""
    best_pid = None
    best_correct = -1
    best_avg_time = float('inf')
    disconnected = game.get("disconnected", [])
    for pid in game["player_order"]:
        if pid in disconnected:
            continue
        p = game["players"][pid]
        correct_count = p.get("correct_count", 0)
        avg_time = p.get("total_time", 0) / max(TOTAL_ROUNDS, 1)
        if (correct_count > best_correct or
                (correct_count == best_correct and avg_time < best_avg_time)):
            best_correct = correct_count
            best_avg_time = avg_time
            best_pid = pid
    if best_pid:
        game["mvp"] = {
            "name": game["players"][best_pid]["name"],
            "team": _get_player_team(game, best_pid),
            "correct_count": best_correct,
            "avg_time": round(best_avg_time, 1),
        }


def _cleanup_team_lobby(game):
    """Remove stale players from team lobby."""
    if game["state"] != "team_lobby":
        return
    now = time.time()
    host_id = game.get("host_id")
    stale = [pid for pid in game["player_order"]
             if pid != host_id and now - game["last_poll"].get(pid, 0) > POOL_STALE_TIMEOUT]
    for pid in stale:
        game["player_order"].remove(pid)
        if pid in game["players"]:
            del game["players"][pid]
        for tk in ("A", "B"):
            if pid in game["teams"][tk]["players"]:
                game["teams"][tk]["players"].remove(pid)


def _generate_computer_answers(game, difficulty):
    """Pre-roll all computer answers at game creation time."""
    config = COMPUTER_OPPONENTS[difficulty]
    answers = []
    for q in game["questions"]:
        correct = random.random() < config["accuracy"]
        delay = random.uniform(config["min_time"], config["max_time"])
        choices_key = "choices" if "choices" in q else "options"
        if correct:
            choice = q["answer"]
        else:
            wrong = [c for c in q[choices_key] if c != q["answer"]]
            choice = random.choice(wrong) if wrong else q["answer"]
        answers.append({"choice": choice, "delay": delay})
    game["computer_answers"] = answers


def _maybe_computer_answer(game):
    """Check if the computer's simulated answer time has elapsed and record it."""
    cpu_id = game.get("computer_player_id")
    if not cpu_id or game["state"] != "active":
        return
    rd = game["round"]
    rd_answers = game["answers"].get(rd, {})
    if cpu_id in rd_answers:
        return
    round_start = game.get("round_start")
    if not round_start:
        return
    ca = game["computer_answers"][rd]
    elapsed = time.time() - round_start
    if elapsed >= ca["delay"]:
        if rd not in game["answers"]:
            game["answers"][rd] = {}
        game["answers"][rd][cpu_id] = {
            "choice": ca["choice"],
            "time": round_start + ca["delay"],
            "correct": ca["choice"] == game["questions"][rd]["answer"],
        }
        advance_round(game)


def _check_round_timeout(game):
    """Auto-submit wrong answers for players who haven't answered within the timeout."""
    if game["state"] != "active":
        return
    round_start = game.get("round_start")
    if not round_start:
        return
    if time.time() - round_start < ROUND_TIMEOUT:
        return
    rd = game["round"]
    if rd not in game["answers"]:
        game["answers"][rd] = {}
    if game.get("team_mode"):
        active_players = [pid for pid in game["player_order"]
                          if pid not in game.get("disconnected", [])]
    else:
        active_players = game["player_order"]
    for pid in active_players:
        if pid not in game["answers"][rd]:
            game["answers"][rd][pid] = {
                "choice": None,
                "time": round_start + ROUND_TIMEOUT,
                "correct": False,
            }
    if game.get("team_mode"):
        advance_round_team(game)
    else:
        advance_round(game)


def next_round_if_ready(game):
    """Move from round_result to active if enough time has passed."""
    if game["state"] == "round_result" and game["result_shown_at"]:
        delay = TEAM_RESULT_DISPLAY_TIME if game.get("team_mode") else 8
        if time.time() - game["result_shown_at"] > delay:
            game["state"] = "active"
            game["round_start"] = time.time()


# ── HTTP Server ──────────────────────────────────────────────────────────────

PUBLIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")


class GameHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        # Quieter logging
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json_response(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, filepath, content_type):
        try:
            with open(filepath, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self._cors()
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self._serve_file(os.path.join(PUBLIC_DIR, "index.html"), "text/html; charset=utf-8")
            return

        if path == "/api/leaderboard":
            params = urllib.parse.parse_qs(parsed.query)
            mode = params.get("mode", ["fun"])[0]
            if mode not in QUESTION_BANKS and mode != "team":
                mode = "fun"
            self._json_response(_load_leaderboard(mode))
            return

        if path == "/api/debug":
            # Diagnostic endpoint to check Supabase connectivity
            info = {
                "supabase_url_set": bool(SUPABASE_URL),
                "supabase_key_set": bool(SUPABASE_KEY),
                "supabase_url_prefix": SUPABASE_URL[:30] + "..." if SUPABASE_URL else "",
                "active_games": len(games),
                "pool_players": len(matchmaking_pool),
                "pending_matches": len(pending_matches),
            }
            # Try a test read
            if SUPABASE_URL and SUPABASE_KEY:
                try:
                    url = f"{SUPABASE_URL}/rest/v1/leaderboard?select=name&limit=1"
                    req = urllib.request.Request(url, headers=_supabase_headers())
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        info["supabase_read_test"] = "OK"
                except Exception as e:
                    info["supabase_read_test"] = f"FAILED: {e}"
            self._json_response(info)
            return

        if path == "/api/state":
            params = urllib.parse.parse_qs(parsed.query)
            game_id = params.get("game_id", [None])[0]
            player_id = params.get("player_id", [None])[0]
            if not game_id or not player_id or game_id not in games:
                self._json_response({"error": "Invalid game or player"}, 400)
                return
            with game_lock:
                game = games[game_id]
                game["last_poll"][player_id] = time.time()
                next_round_if_ready(game)
                _maybe_computer_answer(game)
                _check_round_timeout(game)
                self._json_response(get_safe_state(game, player_id))
            return

        if path == "/api/pool/state":
            params = urllib.parse.parse_qs(parsed.query)
            player_id = params.get("player_id", [None])[0]
            if not player_id:
                self._json_response({"error": "Missing player_id"}, 400)
                return
            with game_lock:
                cleanup_stale_pool()

                # Check if player is in the pool
                if player_id in matchmaking_pool:
                    entry = matchmaking_pool[player_id]
                    entry["last_poll"] = time.time()

                    # Check for mutual selection
                    my_sel = entry["selected"]
                    if my_sel and my_sel in matchmaking_pool and matchmaking_pool[my_sel]["selected"] == player_id:
                        # Mutual match! Create pending match
                        other = matchmaking_pool[my_sel]
                        match_id = "m_" + str(uuid.uuid4())[:8]
                        pending_matches[match_id] = {
                            "players": {
                                player_id: {"name": entry["name"], "category": None},
                                my_sel: {"name": other["name"], "category": None},
                            },
                            "created_at": time.time(),
                            "game_id": None,
                        }
                        del matchmaking_pool[player_id]
                        del matchmaking_pool[my_sel]
                        self._json_response({
                            "status": "matched",
                            "match_id": match_id,
                            "opponent_name": other["name"],
                        })
                        return

                    # Build player list
                    players = []
                    for pid, e in matchmaking_pool.items():
                        if pid == player_id:
                            continue
                        players.append({
                            "player_id": pid,
                            "name": e["name"],
                            "selected_you": e["selected"] == player_id,
                        })
                    self._json_response({
                        "status": "selecting",
                        "players": players,
                        "your_selection": entry["selected"],
                    })
                    return

                # Check if player is in a pending match
                for mid, match in pending_matches.items():
                    if player_id in match["players"]:
                        match["players"][player_id]["last_poll"] = time.time()
                        # Game already created?
                        if match.get("game_id"):
                            self._json_response({
                                "status": "game_ready",
                                "game_id": match["game_id"],
                                "player_id": player_id,
                            })
                            return
                        # Get opponent info
                        opp_id = [p for p in match["players"] if p != player_id][0]
                        opp = match["players"][opp_id]
                        my_cat = match["players"][player_id].get("category")
                        opp_ready = opp.get("category") is not None
                        self._json_response({
                            "status": "category_select",
                            "match_id": mid,
                            "opponent_name": opp["name"],
                            "your_category": my_cat,
                            "opponent_ready": opp_ready,
                        })
                        return

                self._json_response({"error": "Not in pool"}, 404)
            return

        if path == "/api/team/state":
            params = urllib.parse.parse_qs(parsed.query)
            game_id = params.get("game_id", [None])[0]
            player_id = params.get("player_id", [None])[0]
            if not game_id or not player_id or game_id not in games:
                self._json_response({"error": "Invalid game or player"}, 400)
                return
            with game_lock:
                game = games[game_id]
                game["last_poll"][player_id] = time.time()
                _cleanup_team_lobby(game)
                next_round_if_ready(game)
                _check_team_disconnects(game)
                _check_round_timeout(game)
                if game["state"] == "active" and game.get("team_mode"):
                    advance_round_team(game)
                self._json_response(get_team_safe_state(game, player_id))
            return

        # Serve static files
        safe = path.lstrip("/")
        filepath = os.path.join(PUBLIC_DIR, safe)
        if os.path.isfile(filepath):
            ct = "text/html"
            if filepath.endswith(".js"):
                ct = "application/javascript"
            elif filepath.endswith(".css"):
                ct = "text/css"
            self._serve_file(filepath, ct)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len else b"{}"
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {}

        if path == "/api/pool/join":
            name = data.get("name", "Player")[:20].strip() or "Player"
            player_id = str(uuid.uuid4())[:8]
            with game_lock:
                cleanup_stale_pool()
                matchmaking_pool[player_id] = {
                    "name": name,
                    "selected": None,
                    "last_poll": time.time(),
                    "joined_at": time.time(),
                }
            self._json_response({"player_id": player_id})
            return

        if path == "/api/pool/select":
            player_id = data.get("player_id")
            selected_id = data.get("selected_id")
            with game_lock:
                if player_id not in matchmaking_pool:
                    self._json_response({"error": "Not in pool"}, 404)
                    return
                if selected_id and (selected_id == player_id or selected_id not in matchmaking_pool):
                    self._json_response({"error": "Invalid selection"}, 400)
                    return
                matchmaking_pool[player_id]["selected"] = selected_id
            self._json_response({"ok": True})
            return

        if path == "/api/pool/category":
            player_id = data.get("player_id")
            match_id = data.get("match_id")
            category = data.get("category", "fun").strip().lower()
            if category not in QUESTION_BANKS:
                category = "fun"
            with game_lock:
                if match_id not in pending_matches:
                    self._json_response({"error": "Match not found"}, 404)
                    return
                match = pending_matches[match_id]
                if player_id not in match["players"]:
                    self._json_response({"error": "Not in this match"}, 400)
                    return
                match["players"][player_id]["category"] = category
                # Check if both categories submitted
                cats = [p["category"] for p in match["players"].values()]
                if all(cats):
                    pids = list(match["players"].keys())
                    game = new_game(cats[0], cats[1])
                    game["player_categories"] = {}
                    for i, pid in enumerate(pids):
                        pname = match["players"][pid]["name"]
                        game["players"][pid] = {"name": pname, "score": 0, "total_time": 0.0}
                        game["player_order"].append(pid)
                        game["player_categories"][pid] = cats[i]
                    game["state"] = "lobby"
                    match["game_id"] = game["id"]
            self._json_response({"ok": True})
            return

        if path == "/api/team/create":
            name = data.get("name", "Player")[:20].strip() or "Player"
            player_id = str(uuid.uuid4())[:8]
            with game_lock:
                game = new_team_game(player_id, name)
                team_game_state["queue"] = game["id"]
            self._json_response({"game_id": game["id"], "player_id": player_id})
            return

        if path == "/api/team/join":
            name = data.get("name", "Player")[:20].strip() or "Player"
            player_id = str(uuid.uuid4())[:8]
            with game_lock:
                qid = team_game_state.get("queue")
                if not qid or qid not in games:
                    self._json_response({"error": "No team game available"}, 404)
                    return
                game = games[qid]
                if game["state"] != "team_lobby":
                    self._json_response({"error": "Game already started"}, 400)
                    return
                game["players"][player_id] = {"name": name, "score": 0, "total_time": 0.0, "correct_count": 0}
                game["player_order"].append(player_id)
                game["last_poll"][player_id] = time.time()
            self._json_response({"game_id": game["id"], "player_id": player_id})
            return

        if path == "/api/team/assign":
            game_id = data.get("game_id")
            host_id = data.get("host_id")
            target_id = data.get("target_player_id")
            team = data.get("team", "A")
            if team not in ("A", "B"):
                self._json_response({"error": "Invalid team"}, 400)
                return
            with game_lock:
                if game_id not in games:
                    self._json_response({"error": "Game not found"}, 404)
                    return
                game = games[game_id]
                if game.get("host_id") != host_id:
                    self._json_response({"error": "Not the host"}, 403)
                    return
                if target_id not in game["players"]:
                    self._json_response({"error": "Player not found"}, 404)
                    return
                for tk in ("A", "B"):
                    if target_id in game["teams"][tk]["players"]:
                        game["teams"][tk]["players"].remove(target_id)
                game["teams"][team]["players"].append(target_id)
            self._json_response({"ok": True})
            return

        if path == "/api/team/name":
            game_id = data.get("game_id")
            host_id = data.get("host_id")
            team = data.get("team")
            name = data.get("name", "").strip()[:30]
            if team not in ("A", "B") or not name:
                self._json_response({"error": "Invalid request"}, 400)
                return
            with game_lock:
                if game_id not in games:
                    self._json_response({"error": "Game not found"}, 404)
                    return
                game = games[game_id]
                if game.get("host_id") != host_id:
                    self._json_response({"error": "Not the host"}, 403)
                    return
                game["teams"][team]["name"] = name
            self._json_response({"ok": True})
            return

        if path == "/api/team/finalize":
            game_id = data.get("game_id")
            host_id = data.get("host_id")
            with game_lock:
                if game_id not in games:
                    self._json_response({"error": "Game not found"}, 404)
                    return
                game = games[game_id]
                if game.get("host_id") != host_id:
                    self._json_response({"error": "Not the host"}, 403)
                    return
                game["team_finalized"] = True
                game["state"] = "team_preview"
            self._json_response({"ok": True})
            return

        if path == "/api/team/start":
            game_id = data.get("game_id")
            host_id = data.get("host_id")
            host_category = data.get("category")  # optional: host picks category directly
            with game_lock:
                if game_id not in games:
                    self._json_response({"error": "Game not found"}, 404)
                    return
                game = games[game_id]
                if game.get("host_id") != host_id:
                    self._json_response({"error": "Not the host"}, 403)
                    return
                if game["state"] != "team_preview":
                    self._json_response({"error": "Not in preview"}, 400)
                    return
                if host_category and host_category in QUESTION_BANKS:
                    # Host chose category directly — skip voting
                    game["category_votes"] = {host_id: host_category}
                    _resolve_category_vote(game)
                    game["state"] = "active"
                    game["round_start"] = time.time()
                    team_game_state["queue"] = None
                else:
                    game["state"] = "team_vote"
            self._json_response({"ok": True})
            return

        if path == "/api/team/vote":
            game_id = data.get("game_id")
            player_id = data.get("player_id")
            category = data.get("category", "fun").strip().lower()
            if category not in QUESTION_BANKS:
                category = "fun"
            with game_lock:
                if game_id not in games:
                    self._json_response({"error": "Game not found"}, 404)
                    return
                game = games[game_id]
                if player_id not in game["players"]:
                    self._json_response({"error": "Not in game"}, 400)
                    return
                if game["state"] != "team_vote":
                    self._json_response({"error": "Not in voting phase"}, 400)
                    return
                game["category_votes"][player_id] = category
                game["last_poll"][player_id] = time.time()
                if len(game["category_votes"]) >= len(game["player_order"]):
                    _resolve_category_vote(game)
                    game["state"] = "active"
                    game["round_start"] = time.time()
                    team_game_state["queue"] = None
            self._json_response({"ok": True})
            return

        if path == "/api/team/rematch":
            game_id = data.get("game_id")
            player_id = data.get("player_id")
            with game_lock:
                if game_id not in games:
                    self._json_response({"error": "Game not found"}, 404)
                    return
                old_game = games[game_id]
                if not old_game.get("team_mode") or old_game["state"] != "finished":
                    self._json_response({"error": "Game not finished"}, 400)
                    return
                # Check if rematch already created
                if old_game.get("rematch_game_id"):
                    self._json_response({"game_id": old_game["rematch_game_id"]})
                    return
                # Create new game with same teams
                host_id = old_game.get("host_id")
                host_name = old_game["players"].get(host_id, {}).get("name", "Host")
                new_g = new_team_game(host_id, host_name)
                # Copy team structure (only players still connected)
                disconnected = old_game.get("disconnected", [])
                for tk in ("A", "B"):
                    new_g["teams"][tk]["name"] = old_game["teams"][tk]["name"]
                    for pid in old_game["teams"][tk]["players"]:
                        if pid in old_game["players"] and pid not in disconnected:
                            if pid != host_id:
                                pname = old_game["players"][pid]["name"]
                                new_g["players"][pid] = {"name": pname, "score": 0, "total_time": 0.0, "correct_count": 0}
                                new_g["player_order"].append(pid)
                                new_g["last_poll"][pid] = time.time()
                            new_g["teams"][tk]["players"].append(pid)
                new_g["team_finalized"] = True
                new_g["state"] = "team_vote"
                old_game["rematch_game_id"] = new_g["id"]
                team_game_state["queue"] = None  # rematch doesn't need queue
            self._json_response({"game_id": new_g["id"]})
            return

        if path == "/api/team/newgame":
            game_id = data.get("game_id")
            player_id = data.get("player_id")
            with game_lock:
                if game_id not in games:
                    self._json_response({"error": "Game not found"}, 404)
                    return
                old_game = games[game_id]
                if not old_game.get("team_mode") or old_game["state"] != "finished":
                    self._json_response({"error": "Game not finished"}, 400)
                    return
                if old_game.get("newgame_id"):
                    self._json_response({"game_id": old_game["newgame_id"]})
                    return
                host_id = old_game.get("host_id")
                host_name = old_game["players"].get(host_id, {}).get("name", "Host")
                new_g = new_team_game(host_id, host_name)
                # Add all non-disconnected players back to lobby (unassigned)
                disconnected = old_game.get("disconnected", [])
                for pid in old_game["player_order"]:
                    if pid != host_id and pid in old_game["players"] and pid not in disconnected:
                        pname = old_game["players"][pid]["name"]
                        new_g["players"][pid] = {"name": pname, "score": 0, "total_time": 0.0, "correct_count": 0}
                        new_g["player_order"].append(pid)
                        new_g["last_poll"][pid] = time.time()
                old_game["newgame_id"] = new_g["id"]
                team_game_state["queue"] = new_g["id"]
            self._json_response({"game_id": new_g["id"]})
            return

        if path == "/api/join":
            name = data.get("name", "Player")[:20].strip() or "Player"
            mode = data.get("mode", "fun").strip().lower()
            if mode not in QUESTION_BANKS:
                mode = "fun"
            player_id = str(uuid.uuid4())[:8]
            computer = data.get("computer", "").strip().lower()

            if not computer or computer not in COMPUTER_OPPONENTS:
                self._json_response({"error": "Invalid request"}, 400)
                return

            with game_lock:
                config = COMPUTER_OPPONENTS[computer]
                game = new_game(mode)
                game["players"][player_id] = {"name": name, "score": 0, "total_time": 0.0}
                game["player_order"].append(player_id)
                cpu_id = "cpu_" + str(uuid.uuid4())[:8]
                game["players"][cpu_id] = {"name": config["name"], "score": 0, "total_time": 0.0}
                game["player_order"].append(cpu_id)
                game["computer_player_id"] = cpu_id
                _generate_computer_answers(game, computer)
                game["state"] = "lobby"
                game["ready"][cpu_id] = True
                self._json_response({
                    "game_id": game["id"],
                    "player_id": player_id,
                    "mode": mode,
                    "computer": True,
                })
            return

        if path == "/api/rejoin":
            # Allow a client to reconnect to an existing game
            game_id = data.get("game_id")
            player_id = data.get("player_id")
            if game_id and player_id and game_id in games:
                game = games[game_id]
                if player_id in game["players"] and game["state"] != "finished":
                    game["last_poll"][player_id] = time.time()
                    self._json_response({"ok": True, "game_id": game_id, "player_id": player_id})
                    return
            self._json_response({"error": "Game not found or ended"}, 404)
            return

        if path == "/api/ready":
            game_id = data.get("game_id")
            player_id = data.get("player_id")
            if not game_id or not player_id or game_id not in games:
                self._json_response({"error": "Invalid game"}, 400)
                return

            with game_lock:
                game = games[game_id]
                if game["state"] != "lobby":
                    self._json_response({"error": "Not in lobby"}, 400)
                    return
                game["ready"][player_id] = True
                if len(game["ready"]) == 2:
                    game["state"] = "active"
                    game["round_start"] = time.time()
                    _maybe_computer_answer(game)
            self._json_response({"ok": True})
            return

        if path == "/api/answer":
            game_id = data.get("game_id")
            player_id = data.get("player_id")
            choice = data.get("choice")

            if not game_id or not player_id or game_id not in games:
                self._json_response({"error": "Invalid game"}, 400)
                return

            with game_lock:
                game = games[game_id]
                if game["state"] != "active":
                    self._json_response({"error": "Not in active round"}, 400)
                    return

                rd = game["round"]
                if rd not in game["answers"]:
                    game["answers"][rd] = {}

                if player_id in game["answers"][rd]:
                    self._json_response({"error": "Already answered"}, 400)
                    return

                game["last_poll"][player_id] = time.time()
                game["answers"][rd][player_id] = {
                    "choice": choice,
                    "time": time.time(),
                    "correct": choice == game["questions"][rd]["answer"],
                }

                if game.get("team_mode"):
                    advance_round_team(game)
                else:
                    advance_round(game)
                    _maybe_computer_answer(game)
            self._json_response({"ok": True})
            return

        if path == "/api/next":
            # Client signals ready for next round
            game_id = data.get("game_id")
            if game_id and game_id in games:
                with game_lock:
                    game = games[game_id]
                    next_round_if_ready(game)
            self._json_response({"ok": True})
            return

        self.send_response(404)
        self.end_headers()


def run(port=8080):
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), GameHandler)
    print(f"=== Brain Duel Server ===")
    print(f"Game running at: http://localhost:{port}")
    print(f"Share your IP address for remote play (port {port})")
    print(f"SUPABASE_URL: {'configured' if SUPABASE_URL else 'NOT SET'}")
    print(f"SUPABASE_KEY: {'configured' if SUPABASE_KEY else 'NOT SET'}")
    print(f"Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        server.server_close()


if __name__ == "__main__":
    run(port=int(os.environ.get("PORT", 8080)))
