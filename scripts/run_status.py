#!/usr/bin/env python
"""A live run monitor that works without the frontend and without restarting the API.

Shows what the frontend's run monitor shows — status, operation, stage,
elapsed time, pages read, people found, residents and fellows, sites complete,
skipped and failed, programs covered, model spend, the stage bar and the agent
activity feed — for every run in the last day, on http://127.0.0.1:8765.

    .venv/bin/python scripts/run_status.py [--api http://localhost:8000/api/v1]

Where the numbers come from:
  * The activity feed, stage and live spend come from the API's own event
    stream (`/runs/{id}/events`), the same one the frontend uses. This process
    subscribes to every active run in the background and keeps the feed, so
    reloading the page does not lose it. The stream does not replay history:
    the feed starts when this script started (or when the run started, if
    later). Earlier pipeline log lines are shown under the feed.
  * People and residents/fellows are unique records in the database seen
    during the run, not a sum of per-page sightings (the frontend's number
    counts a person once per page they appear on).
  * Programs covered come from the site's checkpoint; pages read from the
    site run's step count.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
import urllib.request
from collections import deque
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
FEED_LIMIT = 300
_PIPELINE = re.compile(r"\[agentscrape\.(pipeline|directory|runs|llm\.(?:reader|triage|planner)|schools)[^\]]*\]")
_STAGES = {"discovering", "directory", "finalizing", "complete"}
_TERMINAL = {"run_completed", "run_stopped_at_limit", "run_cancelled", "run_failed"}


def env_value(key: str, default: str) -> str:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    return default


def database_url() -> str:
    return env_value(
        "DATABASE_URL", "postgresql://agentscrape:agentscrape@localhost:5432/agentscrape"
    ).replace("postgresql+asyncpg://", "postgresql://")


# --- live event subscriptions ---------------------------------------------

class Live:
    """What the event stream has said about one run, folded like the
    frontend's applyRunEvent."""

    def __init__(self) -> None:
        self.feed: deque[dict] = deque(maxlen=FEED_LIMIT)
        self.stage: str | None = None
        self.spend: float | None = None
        self.tokens: tuple[int, int] | None = None
        self.connected_at = datetime.now(UTC).isoformat()
        self.error: str | None = None

    def apply(self, kind: str, p: dict) -> None:
        at = p.get("at") or datetime.now(UTC).isoformat()
        if kind in ("heartbeat", "run_progress"):
            if isinstance(p.get("spend_usd"), (int, float)):
                self.spend = float(p["spend_usd"])
            if isinstance(p.get("tokens_in"), int):
                self.tokens = (p["tokens_in"], p.get("tokens_out") or 0)
        elif kind == "site_started":
            self.stage = "discovering"
            self.feed.appendleft({"at": at, "kind": "note",
                                  "message": f"Started crawling {p.get('domain') or 'the school'}"})
        elif kind == "site_step":
            if p.get("stage") in _STAGES:
                self.stage = p["stage"]
                return
            message = p.get("message")
            if p.get("action") == "note":
                if message:
                    self.feed.appendleft({"at": at, "kind": "note", "message": message})
                return
            if self.stage in (None, "discovering"):
                self.stage = "directory"
            self.feed.appendleft({
                "at": at, "kind": "page", "message": message or f"Read {p.get('url')}",
                "url": p.get("url"), "records": p.get("records") or 0,
                "trainees": p.get("trainees") or 0,
            })
        elif kind == "site_skipped":
            self.feed.appendleft({"at": at, "kind": "note",
                                  "message": f"Skipped: {p.get('reason') or 'site unchanged since the last crawl'}"})
        elif kind == "site_failed":
            self.feed.appendleft({"at": at, "kind": "error",
                                  "message": f"Stopped: {p.get('reason') or p.get('error_code') or 'site failed'}"})
        elif kind == "site_completed":
            self.feed.appendleft({"at": at, "kind": "note", "message": "Finished the site; reconciling records"})
        elif kind in _TERMINAL:
            self.stage = "complete"


LIVE: dict[str, Live] = {}
LOCK = threading.Lock()


def login(api: str) -> str:
    body = json.dumps({"password": env_value("APP_PASSWORD", "change-me")}).encode()
    request = urllib.request.Request(
        f"{api}/auth/login", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)["token"]


def subscribe(api: str, run_id: str) -> None:
    """Follow one run's event stream until it ends, reconnecting on errors."""
    live = LIVE[run_id]
    while True:
        try:
            token = login(api)
            url = f"{api}/runs/{run_id}/events?token={token}"
            with urllib.request.urlopen(url, timeout=120) as stream:
                kind, data = "message", []
                for raw in stream:
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    if line.startswith("event:"):
                        kind = line[6:].strip()
                    elif line.startswith("data:"):
                        data.append(line[5:].strip())
                    elif not line and data:
                        try:
                            payload = json.loads("\n".join(data))
                        except json.JSONDecodeError:
                            payload = {}
                        with LOCK:
                            live.apply(kind, payload)
                        if kind in _TERMINAL:
                            return
                        kind, data = "message", []
            return  # the server ended the stream: the run is over
        except Exception as exc:  # keep following through restarts and timeouts
            live.error = f"{type(exc).__name__}: {exc}"
            time.sleep(5)


def follow_active_runs(api: str) -> None:
    while True:
        try:
            with psycopg.connect(database_url()) as conn:
                active = [r[0] for r in conn.execute(
                    "select id from runs where status in ('running', 'pending')"
                )]
            for run_id in active:
                with LOCK:
                    if run_id in LIVE:
                        continue
                    LIVE[run_id] = Live()
                threading.Thread(target=subscribe, args=(api, run_id), daemon=True).start()
        except Exception:
            pass
        time.sleep(5)


# --- snapshot ---------------------------------------------------------------

def snapshot(log_path: Path) -> dict:
    with psycopg.connect(database_url()) as conn:
        runs = conn.execute(
            """
            select r.id, r.status, r.config->'modes', r.label, r.started_at, r.finished_at,
                   r.sites_total, r.sites_completed, r.sites_skipped, r.sites_failed,
                   r.records_new, r.records_changed, r.spend_usd, r.error_message
            from runs r
            where r.status in ('running', 'pending') or r.created_at > now() - interval '24 hours'
            order by r.created_at desc
            """
        ).fetchall()
        sites = conn.execute(
            """
            select sr.run_id, s.name, s.root_domain, sr.status, sr.steps_taken, sr.started_at,
                   sr.heartbeat_at, sr.error_message, sr.checkpoint_state->'programs',
                   (select count(*) from records x where x.site_id = s.id
                      and sr.started_at is not null and x.last_seen_at >= sr.started_at),
                   (select count(*) from records x where x.site_id = s.id
                      and sr.started_at is not null and x.last_seen_at >= sr.started_at
                      and x.category in ('resident', 'fellow')),
                   (select count(*) from records x where x.site_id = s.id
                      and sr.started_at is not null and x.last_seen_at >= sr.started_at
                      and x.email is not null)
            from site_runs sr join sites s on s.id = sr.site_id
            where sr.run_id = any(%s)
            """,
            ([r[0] for r in runs],),
        ).fetchall()

    by_run: dict[str, list] = {}
    for row in sites:
        by_run.setdefault(row[0], []).append(row)

    recent: list[str] = []
    if log_path.exists():
        with log_path.open(errors="replace") as handle:
            for line in deque(handle, maxlen=50_000):
                if _PIPELINE.search(line):
                    recent.append(line.rstrip()[:260])

    now = datetime.now(UTC)
    out = []
    for (run_id, status, modes, label, started, finished, total, done, skipped, failed,
         new, changed, spend, error) in runs:
        rows = by_run.get(run_id, [])
        programs = [p for row in rows for p in (row[8] or [])]
        with LOCK:
            live = LIVE.get(run_id)
            feed = list(live.feed) if live else []
            stage = live.stage if live else None
            live_spend = live.spend if live else None
            tokens = live.tokens if live else None
            connected = live.connected_at if live else None
            stream_error = live.error if live else None
        if stage is None:
            if status in ("completed", "cancelled", "failed", "stopped_at_limit"):
                stage = "complete"
            elif any((row[4] or 0) > 0 for row in rows):
                stage = "directory"
            else:
                stage = "discovering"
        modes = modes or ["crawl"]
        end = finished or now
        out.append({
            "id": run_id,
            "school": ", ".join(row[1] or row[2] for row in rows) or label or run_id,
            "status": "stopped at limit" if status == "stopped_at_limit" else status,
            "operation": " + ".join("Crawl" if m == "crawl" else "Directory search" for m in modes),
            "stage": stage,
            "elapsed_s": round((end - started).total_seconds()) if started else 0,
            "pages_read": sum(row[4] or 0 for row in rows),
            "people": sum(row[9] for row in rows),
            "trainees": sum(row[10] for row in rows),
            "emails": sum(row[11] for row in rows),
            "sites_total": total, "sites_completed": done,
            "sites_skipped": skipped, "sites_failed": failed,
            "programs_total": len(programs),
            "programs_covered": sum(1 for p in programs if p.get("status") != "pending"),
            "spend": live_spend if live_spend is not None else float(spend or 0),
            "tokens": tokens,
            "new": new, "changed": changed,
            "error": error or next((row[7] for row in rows if row[7]), None),
            "heartbeat_s": min(
                (round((now - row[6]).total_seconds()) for row in rows if row[6]), default=None
            ),
            "feed": feed, "feed_since": connected, "stream_error": stream_error,
            "finished": status not in ("running", "pending"),
        })
    return {"at": now.isoformat(), "runs": out, "log": recent[-60:]}


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Run Monitor</title>
<style>
:root{--bg:#f5f7f6;--card:#fff;--ink:#13201c;--muted:#5b6b66;--rule:#d8e0dd;--ok:#0d6a5b;--ok-soft:#d9efe9;--warn:#9a5b07;--bad:#9e3434}
@media (prefers-color-scheme:dark){:root{--bg:#0e1513;--card:#151f1c;--ink:#e3ebe8;--muted:#93a5a0;--rule:#2a3733;--ok:#4fbfa4;--ok-soft:#173a32;--warn:#e0a24a;--bad:#e07a7a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif;padding:24px 16px}
main{max-width:1100px;margin:0 auto;display:grid;gap:18px}
h1,h2,h3{margin:0}h1{font-size:1.4rem}h2{font-size:1.25rem}h3{font-size:1rem}
.muted{color:var(--muted)}.small{font-size:.85rem}
.tabs{display:flex;flex-wrap:wrap;gap:8px}
.tab{border:1px solid var(--rule);background:var(--card);color:var(--ink);border-radius:999px;padding:6px 14px;cursor:pointer;font:inherit;font-size:.9rem}
.tab.active{border-color:var(--ok);background:var(--ok-soft);color:var(--ok);font-weight:600}
.tab .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--ok);margin-right:6px}
.card{background:var(--card);border:1px solid var(--rule);border-radius:10px;padding:18px;display:grid;gap:16px}
.strip{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:1px;background:var(--rule);border:1px solid var(--rule);border-radius:8px;overflow:hidden}
.strip div{background:var(--card);padding:10px 12px;display:grid;gap:2px}
.strip span{font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.strip strong{font-variant-numeric:tabular-nums}
.stages{display:flex;flex-wrap:wrap;gap:8px}
.chip{padding:5px 12px;border-radius:999px;border:1px solid var(--rule);font-size:.85rem;color:var(--muted)}
.chip.active{border-color:var(--ok);color:var(--ok);background:var(--ok-soft);font-weight:600}
.done{border:1px solid var(--ok);background:var(--ok-soft);border-radius:8px;padding:12px 14px;display:grid;gap:8px}
.bad{color:var(--bad)}
ol{list-style:none;margin:0;padding:0;display:grid}
li{display:grid;grid-template-columns:78px 1fr auto;gap:10px;padding:7px 0;border-bottom:1px solid var(--rule);align-items:start}
li time{color:var(--muted);font:12px/1.6 ui-monospace,Menlo,monospace}
li.note span{color:var(--muted)}li.error span{color:var(--bad)}
li a{display:block;color:var(--muted);font-size:.8rem;word-break:break-all}
li strong{color:var(--ok);font-variant-numeric:tabular-nums}
.live{color:var(--ok);font-size:.85rem;font-weight:600}
pre{margin:0;font:12px/1.5 ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-word;max-height:360px;overflow:auto}
details summary{cursor:pointer;color:var(--muted)}
</style></head><body><main>
<div><h1>Run Monitor</h1><div class="muted small" id="at">loading…</div></div>
<div class="tabs" id="tabs"></div>
<div id="run"></div>
<div class="card"><details><summary>Pipeline log (all runs)</summary><pre id="log"></pre></details></div>
</main><script>
const STAGES = {discovering:"Mapping the site", directory:"Reading pages", finalizing:"Finalizing records", complete:"Completed"};
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const n = v => (v ?? 0).toLocaleString();
const dur = s => { s = Math.max(0, s|0); const h = s/3600|0, m = (s%3600)/60|0, x = s%60; return (h ? h+"h " : "") + (h||m ? m+"m " : "") + x+"s"; };
const usd = v => v == null ? "—" : "$" + Number(v).toFixed(2);
let selected = null;
try { selected = localStorage.getItem("run-monitor.selected"); } catch (e) {}
function pick(id){ selected = id; try { localStorage.setItem("run-monitor.selected", id); } catch (e) {} tick(); }
function render(r){
  const stageBar = Object.entries(STAGES).map(([k,l]) => `<div class="chip ${r.stage===k?"active":""}">${l}</div>`).join("");
  const feed = r.feed.length ? `<ol>${r.feed.map(i => `<li class="${esc(i.kind)}">
      <time>${new Date(i.at).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit"})}</time>
      <div><span>${esc(i.message)}</span>${i.url ? `<a href="${esc(i.url)}" target="_blank" rel="noreferrer">${esc(i.url)}</a>` : ""}</div>
      ${i.records ? `<strong>+${i.records}</strong>` : "<span></span>"}</li>`).join("")}</ol>`
    : `<p class="muted">${r.finished ? "No activity was recorded while this monitor was running." : "Waiting for the next event…"}</p>`;
  return `<div class="card">
    <div><div class="muted small">Schools / ${esc(r.school)}</div><h2>${r.finished ? "Crawl finished" : "Crawling"} ${esc(r.school)}</h2></div>
    <div class="strip">
      <div><span>Status</span><strong>${esc(r.status)}</strong></div>
      <div><span>Operation</span><strong>${esc(r.operation)}</strong></div>
      <div><span>Stage</span><strong>${esc(STAGES[r.stage] || r.stage)}</strong></div>
      <div><span>Elapsed</span><strong>${dur(r.elapsed_s)}</strong></div>
      <div><span>Pages read</span><strong>${n(r.pages_read)}</strong></div>
      <div><span>People found</span><strong>${n(r.people)}</strong></div>
      <div><span>Sites</span><strong>${n(r.sites_completed)}/${n(r.sites_total)} complete</strong></div>
      <div><span>Skipped / failed</span><strong>${n(r.sites_skipped)} / ${n(r.sites_failed)}</strong></div>
      <div><span>Residents &amp; fellows</span><strong>${n(r.trainees)}</strong></div>
      <div><span>Programs covered</span><strong>${r.programs_total ? n(r.programs_covered)+" / "+n(r.programs_total) : "—"}</strong></div>
      <div><span>Model spend</span><strong>${usd(r.spend)}</strong></div>
      <div><span>With an email</span><strong>${n(r.emails)}</strong></div>
      <div><span>Heartbeat</span><strong>${r.heartbeat_s == null ? "—" : r.heartbeat_s + " s ago"}</strong></div>
    </div>
    <div class="stages">${stageBar}</div>
    ${r.error ? `<div class="bad">${esc(r.error)}</div>` : ""}
    ${r.finished ? `<div class="done"><strong>${r.error ? "Crawl stopped early — results so far are saved" : "Crawl complete"}</strong>
      <div class="strip">
        <div><span>People found</span><strong>${n(r.people)}</strong></div>
        <div><span>Residents &amp; fellows</span><strong>${n(r.trainees)}</strong></div>
        <div><span>New</span><strong>${n(r.new)}</strong></div>
        <div><span>Changed</span><strong>${n(r.changed)}</strong></div>
        <div><span>Pages read</span><strong>${n(r.pages_read)}</strong></div>
        <div><span>Time</span><strong>${dur(r.elapsed_s)}</strong></div>
        <div><span>Model spend</span><strong>${usd(r.spend)}</strong></div>
      </div></div>` : ""}
    <div><div style="display:flex;justify-content:space-between;align-items:baseline"><h3>Agent activity</h3>${r.finished ? "" : '<span class="live">● Live</span>'}</div>
      <div class="muted small">${r.feed_since ? "Events since " + new Date(r.feed_since).toLocaleTimeString() + " (the stream does not replay earlier ones; see the pipeline log below)." : "This monitor was not following this run while it was active."}${r.stream_error ? " · stream: " + esc(r.stream_error) : ""}</div>
      ${feed}</div>
  </div>`;
}
async function tick(){
  try{
    const d = await (await fetch("/data")).json();
    document.getElementById("at").textContent = "Updated " + new Date(d.at).toLocaleTimeString() + " · refreshes every 3 s · people counts are unique records";
    if (!d.runs.length){ document.getElementById("run").innerHTML = '<div class="card muted">No runs in the last 24 hours.</div>'; }
    else {
      if (!d.runs.some(r => r.id === selected)) selected = (d.runs.find(r => !r.finished) || d.runs[0]).id;
      document.getElementById("tabs").innerHTML = d.runs.map(r => `<button class="tab ${r.id===selected?"active":""}" onclick="pick('${r.id}')">${r.finished ? "" : '<span class="dot"></span>'}${esc(r.school)} · ${esc(r.status)}</button>`).join("");
      document.getElementById("run").innerHTML = render(d.runs.find(r => r.id === selected));
    }
    document.getElementById("log").textContent = d.log.join("\\n") || "No pipeline messages yet.";
  }catch(e){ document.getElementById("at").textContent = "Could not load status: " + e; }
}
tick(); setInterval(tick, 3000);
</script></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default="http://localhost:8000/api/v1")
    parser.add_argument("--log", default="/tmp/agentscrape-crawls/api-hybrid.log")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    log_path = Path(args.log)
    threading.Thread(target=follow_active_runs, args=(args.api,), daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.startswith("/data"):
                body = json.dumps(snapshot(log_path), default=str).encode()
                kind = "application/json"
            else:
                body, kind = PAGE.encode(), "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    print(f"run monitor on http://127.0.0.1:{args.port}")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
