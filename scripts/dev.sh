#!/usr/bin/env bash
#
# One command to run the whole stack locally: Postgres, the API, and the UI.
#
#   ./scripts/dev.sh              set everything up and run it
#   ./scripts/dev.sh --no-seed    skip the demo data
#   ./scripts/dev.sh --reset      wipe the database and reseed
#
# Safe to re-run. Everything it does is idempotent.

set -euo pipefail

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
FRONTEND_REPO="${FRONTEND_REPO:-https://github.com/marvalarva2929/agentscrape-frontend.git}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIR="${FRONTEND_DIR:-$ROOT/../agentscrape-frontend}"

SEED=1
RESET=""
for arg in "$@"; do
  case "$arg" in
    --no-seed) SEED=0 ;;
    --reset)   RESET="--reset" ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;36m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m!\033[0m  %s\n' "$1"; }
die()  { printf '\033[1;31mx\033[0m  %s\n' "$1" >&2; exit 1; }

cd "$ROOT"

port_free() {
  # Prefer lsof; fall back to a bind attempt via Python.
  if command -v lsof >/dev/null; then
    ! lsof -iTCP:"$1" -sTCP:LISTEN -n -P >/dev/null 2>&1
  else
    python3 - "$1" <<'PY' >/dev/null 2>&1
import socket, sys
s = socket.socket()
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
finally:
    s.close()
PY
  fi
}

resolve_port() {
  # Use the requested port, or the next free one, rather than failing to bind.
  local requested="$1" label="$2" candidate
  if port_free "$requested"; then echo "$requested"; return; fi
  for candidate in $(seq $((requested + 1)) $((requested + 20))); do
    if port_free "$candidate"; then
      warn "Port $requested is busy; using $candidate for the $label instead."
      echo "$candidate"; return
    fi
  done
  die "Ports $requested-$((requested + 20)) are all in use. Free one and re-run."
}

# --- prerequisites ---------------------------------------------------------
command -v node >/dev/null || die "Node.js is required: https://nodejs.org (or: brew install node)"

if ! command -v uv >/dev/null; then
  say "Installing uv (Python toolchain manager)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
command -v uv >/dev/null || die "uv installed but not on PATH. Add ~/.local/bin to PATH and re-run."

# Resolve ports first: .env, CORS and the UI config all embed them.
BACKEND_PORT="$(resolve_port "$BACKEND_PORT" backend)"
FRONTEND_PORT="$(resolve_port "$FRONTEND_PORT" frontend)"

# --- database --------------------------------------------------------------
# Docker is the documented path, but plenty of machines do not have it running,
# so fall back to a local Postgres before giving up.
DB_URL=""
if docker info >/dev/null 2>&1; then
  say "Starting Postgres in Docker"
  docker compose up -d >/dev/null
  for _ in $(seq 1 60); do
    docker compose exec -T postgres pg_isready -U agentscrape -d agentscrape >/dev/null 2>&1 && break
    sleep 1
  done
  docker compose exec -T postgres pg_isready -U agentscrape -d agentscrape >/dev/null 2>&1 \
    || die "Postgres container did not become ready. Try: docker compose logs postgres"
  DB_URL="postgresql+asyncpg://agentscrape:agentscrape@localhost:5433/agentscrape"
elif command -v pg_isready >/dev/null && pg_isready -q -h localhost -p 5432 2>/dev/null; then
  warn "Docker is not running; using the Postgres already on localhost:5432"
  psql -h localhost -p 5432 -d postgres -tAc \
    "SELECT 1 FROM pg_roles WHERE rolname='agentscrape'" 2>/dev/null | grep -q 1 \
    || psql -h localhost -p 5432 -d postgres -q \
         -c "CREATE ROLE agentscrape LOGIN PASSWORD 'agentscrape' SUPERUSER" >/dev/null 2>&1 || true
  psql -h localhost -p 5432 -d postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname='agentscrape'" 2>/dev/null | grep -q 1 \
    || psql -h localhost -p 5432 -d postgres -q \
         -c "CREATE DATABASE agentscrape OWNER agentscrape" >/dev/null 2>&1 || true
  DB_URL="postgresql+asyncpg://agentscrape:agentscrape@localhost:5432/agentscrape"
else
  die "No database available. Start Docker Desktop and re-run, or install Postgres (brew install postgresql@16 && brew services start postgresql@16)."
fi

# --- backend config --------------------------------------------------------
if [ ! -f .env ]; then
  say "Creating .env from .env.example"
  cp .env.example .env
fi
# Point .env at whichever database we actually found.
if grep -q '^DATABASE_URL=' .env; then
  tmp="$(mktemp)"; sed "s|^DATABASE_URL=.*|DATABASE_URL=$DB_URL|" .env > "$tmp"; mv "$tmp" .env
else
  echo "DATABASE_URL=$DB_URL" >> .env
fi

# The browser enforces CORS on every call, so the API has to name the exact
# origin the UI is served from — including a non-default FRONTEND_PORT.
UI_ORIGINS="http://localhost:$FRONTEND_PORT,http://127.0.0.1:$FRONTEND_PORT"
if grep -q '^CORS_ORIGINS=' .env; then
  current="$(grep '^CORS_ORIGINS=' .env | cut -d= -f2-)"
  case ",$current," in
    *",http://localhost:$FRONTEND_PORT,"*) ;;
    *) tmp="$(mktemp)"
       sed "s|^CORS_ORIGINS=.*|CORS_ORIGINS=$current,$UI_ORIGINS|" .env > "$tmp"
       mv "$tmp" .env ;;
  esac
else
  echo "CORS_ORIGINS=$UI_ORIGINS" >> .env
fi

say "Installing Python dependencies"
uv sync --quiet

if [ ! -d "$HOME/Library/Caches/ms-playwright" ] && [ ! -d "$HOME/.cache/ms-playwright" ]; then
  say "Installing headless Chromium (only needed for crawling)"
  uv run playwright install chromium >/dev/null 2>&1 || warn "Chromium install failed; crawling will not work, everything else will."
fi

say "Applying database migrations"
uv run alembic upgrade head >/dev/null

if [ "$SEED" = "1" ]; then
  say "Seeding demo data"
  uv run agentscrape seed-demo $RESET >/dev/null
fi

# --- frontend --------------------------------------------------------------
if [ ! -d "$FRONTEND_DIR" ]; then
  say "Cloning the frontend to $FRONTEND_DIR"
  git clone --quiet "$FRONTEND_REPO" "$FRONTEND_DIR"
fi

say "Installing frontend dependencies"
(cd "$FRONTEND_DIR" && npm install --silent)

cat > "$FRONTEND_DIR/.env.local" <<EOF
VITE_API_BASE_URL=http://localhost:$BACKEND_PORT/api/v1
VITE_USE_MOCK_API=false
EOF

# --- run both --------------------------------------------------------------
cleanup() {
  trap - INT TERM EXIT
  [ -n "${API_PID:-}" ] && kill "$API_PID" 2>/dev/null || true
  [ -n "${UI_PID:-}"  ] && kill "$UI_PID"  2>/dev/null || true
}
trap cleanup INT TERM EXIT

say "Starting the API on :$BACKEND_PORT"
uv run uvicorn agentscrape.api.main:app --port "$BACKEND_PORT" --log-level warning &
API_PID=$!

for _ in $(seq 1 40); do
  curl -sf "http://localhost:$BACKEND_PORT/api/v1/health" >/dev/null 2>&1 && break
  sleep 0.5
done
curl -sf "http://localhost:$BACKEND_PORT/api/v1/health" >/dev/null 2>&1 \
  || die "The API did not start. Run it directly to see why: uv run uvicorn agentscrape.api.main:app --port $BACKEND_PORT"

say "Starting the UI on :$FRONTEND_PORT"
(cd "$FRONTEND_DIR" && npm run dev -- --port "$FRONTEND_PORT" --strictPort) &
UI_PID=$!

CLIENT_PW="$(grep -E '^APP_PASSWORD=' .env | cut -d= -f2-)"
ADMIN_PW="$(grep -E '^ADMIN_PASSWORD=' .env | cut -d= -f2-)"

cat <<EOF

  ──────────────────────────────────────────────────────────────
   Open:      http://localhost:$FRONTEND_PORT
   API:       http://localhost:$BACKEND_PORT/api/v1
   API docs:  http://localhost:$BACKEND_PORT/api/v1/docs

   Passwords (from .env)
     client:  ${CLIENT_PW:-change-me}          browse schools, people, sources
     admin:   ${ADMIN_PW:-change-me-admin}     the above, plus billable crawls

   Demo data is already loaded. To crawl a real institution instead:
     uv run agentscrape site medicine.uchicago.edu

   Ctrl-C stops both.
  ──────────────────────────────────────────────────────────────

EOF

wait
