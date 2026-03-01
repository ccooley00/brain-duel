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

QUESTION_BANKS = {
    "fun": FUN_QUESTIONS,
    "nyc": NYC_QUESTIONS,
    "gaming": GAMING_QUESTIONS,
    "colgate": COLGATE_QUESTIONS,
    "cooleys": COOLEYS_QUESTIONS,
}

MODE_NAMES = {
    "fun": "Fun",
    "nyc": "NYC History",
    "gaming": "Gaming",
    "colgate": "Colgate University",
    "cooleys": "History of Cooleys",
}

TOTAL_ROUNDS = 10
POINTS_CORRECT = 10
POINTS_SPEED_BONUS = 5  # bonus for answering first AND correctly

COMPUTER_OPPONENTS = {
    "hal": {"name": "HAL 9000", "accuracy": 0.30, "min_time": 8.0, "max_time": 15.0},
    "terminator": {"name": "Terminator", "accuracy": 0.65, "min_time": 4.0, "max_time": 8.0},
    "claude": {"name": "Claude", "accuracy": 0.92, "min_time": 1.5, "max_time": 4.0},
}

# ── Game State ───────────────────────────────────────────────────────────────

games = {}       # game_id -> game dict
rooms = {}       # room_code -> game_id (for games waiting for player 2)
game_lock = threading.Lock()  # protects games/rooms from concurrent access
STALE_TIMEOUT = 30  # seconds before a waiting game is considered abandoned

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


def generate_room_code():
    """Generate a unique 4-letter room code."""
    while True:
        code = ''.join(random.choices('ABCDEFGHJKLMNPQRSTUVWXYZ', k=4))
        if code not in rooms:
            return code


def new_game(mode="fun"):
    game_id = str(uuid.uuid4())[:8]
    questions_pool = QUESTION_BANKS.get(mode, QUESTION_BANKS["fun"])
    # Exclude questions used in the last 2 games
    excluded = set()
    for s in recent_question_indices:
        excluded |= s
    eligible = [(i, q) for i, q in enumerate(questions_pool) if i not in excluded]
    if len(eligible) < TOTAL_ROUNDS:
        eligible = list(enumerate(questions_pool))  # fallback: use all
    chosen = random.sample(eligible, min(TOTAL_ROUNDS, len(eligible)))
    recent_question_indices.append({i for i, q in chosen})
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
        "room_code": None,      # set when game is created via room system
        "mode": mode,
    }
    games[game_id] = game
    return game


def is_waiting_game_stale(game):
    """Check if a waiting game's only player has stopped polling."""
    if game["state"] != "waiting":
        return False
    if not game["player_order"]:
        return True
    pid = game["player_order"][0]
    last = game["last_poll"].get(pid, game["created_at"])
    return time.time() - last > STALE_TIMEOUT


def cleanup_stale_rooms():
    """Remove room codes for stale waiting games."""
    stale_codes = [code for code, gid in rooms.items()
                   if gid not in games or is_waiting_game_stale(games[gid])]
    for code in stale_codes:
        gid = rooms.pop(code, None)
        if gid and gid in games:
            del games[gid]


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
        "mode_name": MODE_NAMES.get(g.get("mode", "fun"), "Fun"),
        "computer": bool(g.get("computer_player_id")),
    }

    if g["state"] == "waiting" and g.get("room_code"):
        state["room_code"] = g["room_code"]

    if g["state"] == "lobby":
        state["your_ready"] = player_id in g["ready"]
        state["opponent_ready"] = opponent_id in g["ready"] if opponent_id else False

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
            "choice": a["choice"],
            "correct": is_correct,
            "points": points,
            "was_first": pid == first_pid,
            "time_taken": time_taken,
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


def _update_leaderboard(game):
    """Add players from a finished game to the Supabase leaderboard."""
    mode = game.get("mode", "fun")
    cpu_id = game.get("computer_player_id")
    for pid in game["player_order"]:
        if pid == cpu_id:
            continue
        p = game["players"][pid]
        _insert_leaderboard_entry(p["name"], p["score"], round(p["total_time"], 1), mode)


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


def next_round_if_ready(game):
    """Move from round_result to active if enough time has passed."""
    if game["state"] == "round_result" and game["result_shown_at"]:
        if time.time() - game["result_shown_at"] > 2:
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
            if mode not in QUESTION_BANKS:
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
                "active_rooms": len(rooms),
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
                self._json_response(get_safe_state(game, player_id))
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

        if path == "/api/join":
            name = data.get("name", "Player")[:20].strip() or "Player"
            room_code = data.get("room_code", "").strip().upper()
            mode = data.get("mode", "fun").strip().lower()
            if mode not in QUESTION_BANKS:
                mode = "fun"
            player_id = str(uuid.uuid4())[:8]

            computer = data.get("computer", "").strip().lower()

            with game_lock:
                cleanup_stale_rooms()

                if computer and computer in COMPUTER_OPPONENTS:
                    # Create a game vs computer
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

                if room_code:
                    # Join an existing room
                    if room_code not in rooms:
                        self._json_response({"error": "Room not found. Check the code and try again."}, 404)
                        return
                    game_id = rooms[room_code]
                    if game_id not in games or games[game_id]["state"] != "waiting":
                        del rooms[room_code]
                        self._json_response({"error": "Room is no longer available."}, 404)
                        return
                    game = games[game_id]
                    game["players"][player_id] = {"name": name, "score": 0, "total_time": 0.0}
                    game["player_order"].append(player_id)
                    game["state"] = "lobby"
                    del rooms[room_code]
                    self._json_response({"game_id": game_id, "player_id": player_id, "mode": game.get("mode", "fun")})
                else:
                    # Create a new game with a room code
                    game = new_game(mode)
                    code = generate_room_code()
                    game["room_code"] = code
                    game["players"][player_id] = {"name": name, "score": 0, "total_time": 0.0}
                    game["player_order"].append(player_id)
                    rooms[code] = game["id"]
                    self._json_response({"game_id": game["id"], "player_id": player_id, "room_code": code, "mode": mode})
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

                game["answers"][rd][player_id] = {
                    "choice": choice,
                    "time": time.time(),
                    "correct": choice == game["questions"][rd]["answer"],
                }

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
