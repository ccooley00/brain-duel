# BrainDuel

2-player real-time trivia & logic game with computer opponents and team mode. 8 question banks: fun, NYC history, gaming, Colgate, Cooleys, famous buildings, logic, AWH.

## Live

- **Canonical:** https://brainduelgame.com
- Private fallback: https://brainduel.coolshome.duckdns.org
- Legacy (decommission pending): https://brain-duel.onrender.com

## Stack

- **Server:** Python 3.13 stdlib only — `http.server.ThreadingHTTPServer` with threading/locks for game state. No `requirements.txt`.
- **Client:** vanilla HTML/CSS/JS in `public/`, served by the same handler.
- **Leaderboard:** Supabase (publishable anon key, RLS-protected).

Game state is in-memory — a server restart drops active games.

## Run locally

```bash
SUPABASE_URL=https://your-project.supabase.co \
SUPABASE_KEY=sb_publishable_... \
python server.py
```

Then open http://localhost:8080 in two browser tabs.

If `SUPABASE_*` is unset, the leaderboard silently no-ops and the game still works.

## Deploy: home server (canonical, as of 2026-05-10)

Docker container behind a Caddy reverse proxy. See the full architecture pattern in the Home Server project notes (`Z:\Private\ccooley\AI Projects\Non AWH\Home Server\PROJECT_PLAN.md` §11.6).

```bash
# On the server (one-time setup):
git clone https://github.com/ccooley00/brain-duel.git /srv/server/brainduel

# Add to /srv/server/.env (mode 600, never in git):
#   SUPABASE_URL=...
#   SUPABASE_KEY=...

# Add to /srv/server/docker-compose.yml:
#   brainduel:
#     build: ./brainduel
#     container_name: brainduel
#     restart: unless-stopped
#     env_file: .env

# Add to /srv/server/caddy/Caddyfile:
#   brainduelgame.com, www.brainduelgame.com {
#       @www host www.brainduelgame.com
#       redir @www https://brainduelgame.com 301
#       reverse_proxy brainduel:8080
#   }

docker compose up -d --build brainduel
docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
```

## Update flow (self-hosted)

```bash
cd /srv/server/brainduel
git pull
cd ..
docker compose up -d --build brainduel
```

## Deploy: Render (legacy)

`server.py` reads `PORT` (Render sets this automatically) and binds to `0.0.0.0:$PORT`. Connect the GitHub repo as a Web Service. No further config.

## Environment variables

| Name | Required | Purpose |
|---|---|---|
| `PORT` | No (default `8080`) | Listen port |
| `SUPABASE_URL` | No (graceful no-op) | Supabase project URL for leaderboard |
| `SUPABASE_KEY` | No (graceful no-op) | Supabase publishable (anon) key |

Secrets must never be embedded in code — load via env at runtime only.

## Files

- `server.py` — single-file Python server: HTTP, game state, matchmaking, leaderboard integration
- `questions_*.py` — eight question banks
- `public/` — static frontend (HTML, CSS, JS, images)
- `Dockerfile` — Python 3.13-slim base; copies source and runs `python -u server.py`
- `package.json` — leftover Node.js metadata; **dead code, do not run** (the actual app is Python)
- `seed_leaderboard.py` — gitignored; one-shot leaderboard seeder, contains hardcoded creds (already run; do not commit)
