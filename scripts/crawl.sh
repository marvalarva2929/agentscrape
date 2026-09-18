#!/usr/bin/env bash
# Re-crawl one or more benchmarked institutions from scratch.
#
# Each site row is dropped first: known-good paths score +100, so leaving the
# previous run's paths in place pins the ranking to whatever that run already
# found and new pages never get visited.
set -uo pipefail
cd "$(dirname "$0")/.."

DB="${DATABASE_URL_PSQL:-postgresql://agentscrape:agentscrape@localhost:5432/agentscrape}"
LOGS="${CRAWL_LOG_DIR:-/tmp/agentscrape-crawls}"
mkdir -p "$LOGS"

pids=()
for slug in "$@"; do
  host=$(.venv/bin/python -c "import json;print(json.load(open('benchmarks/institutions.json'))['$slug']['host'])")
  entry=$(.venv/bin/python -c "import json;print(json.load(open('benchmarks/institutions.json'))['$slug']['entry'])")
  # An institution that publishes on more than one registrable domain needs
  # each of them in scope, or its rosters are unreachable from the entry point.
  also=$(.venv/bin/python -c "
import json
cfg = json.load(open('benchmarks/institutions.json'))['$slug']
print(' '.join('--also ' + d for d in cfg.get('affiliated_domains', [])))")
  # Department sites built as single-page apps serve a 9KB shell over HTTP, so
  # HTML-only crawling reads nothing at all there. Chromium costs time, so it is
  # switched on per institution rather than globally.
  budget=$(.venv/bin/python -c "
import json
cfg = json.load(open('benchmarks/institutions.json'))['$slug']
print('--budget ' + str(cfg['step_budget']) if cfg.get('step_budget') else '')")
  browser=$(.venv/bin/python -c "
import json
cfg = json.load(open('benchmarks/institutions.json'))['$slug']
print('' if cfg.get('needs_browser') else '--no-browser')")
  psql -q "$DB" -c "delete from sites where root_domain='$host';" >/dev/null
  echo "[$slug] $entry $also ${browser:-(browser)} -> $LOGS/$slug.log"
  # shellcheck disable=SC2086
  .venv/bin/agentscrape site "$entry" $also $browser $budget --force > "$LOGS/$slug.log" 2>&1 &
  pids+=($!)
done

status=0
for pid in "${pids[@]}"; do wait "$pid" || status=1; done
echo "all crawls finished (exit $status)"
exit $status
