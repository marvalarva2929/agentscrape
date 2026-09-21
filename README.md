# agentscrape

Backend for a contact-extraction platform. Staff load teaching-hospital and
medical-school URLs and it collects **everyone published on each institution's
web presence** — residents, fellows, faculty, program directors, coordinators,
staff, students and alumni — each labelled with their printed title, stored with
full provenance, and tracked across repeated runs.

Clients do not upload CSVs. They send a spreadsheet of schools; staff drop it
into [`schools/`](schools/README.md), where it is loaded on startup (or with
`agentscrape load-schools`) and the schools appear in the UI. There is no upload
flow or admin page. Clients start crawls and directory searches on those schools
themselves.

Everything runs on one box: API, orchestrator, agents, Postgres, file storage.
The server is started on demand and stopped when idle, so nothing assumes
continuous uptime.

---

## Quick start

**macOS / Linux**

```bash
git clone https://github.com/marvalarva2929/agentscrape.git
cd agentscrape
./scripts/dev.sh
```

**Windows** (PowerShell)

```powershell
git clone https://github.com/marvalarva2929/agentscrape.git
cd agentscrape
powershell -ExecutionPolicy Bypass -File .\scripts\dev.ps1
```

That installs everything, starts Postgres, applies migrations, loads demo data,
clones the UI next to this repo, and runs the API and the UI together. Then open
**<http://localhost:5173>** and sign in with `change-me`.

Requires Node.js, Git, and either Docker Desktop or a local Postgres; it
installs the Python toolchain itself. `Ctrl-C` stops both. Re-running it is safe.

On Windows, if either is missing:

```powershell
winget install OpenJS.NodeJS.LTS
winget install Git.Git
winget install Docker.DockerDesktop   # or PostgreSQL.PostgreSQL.16
```

Open a fresh PowerShell window afterwards so the new tools are on `PATH`.

| | |
|---|---|
| UI | <http://localhost:5173> |
| API | <http://localhost:8000/api/v1> |
| API docs | <http://localhost:8000/api/v1/docs> |
| Client password | `change-me` — browse schools, people, sources and exports, and start crawls and directory searches |
| Admin password | `change-me-admin` — the same access, with the `admin` scope |

`--reset` wipes the database and reseeds it; `--no-seed` starts empty
(`./scripts/dev.sh --reset`, or `.\scripts\dev.ps1 -Reset` on Windows).

If a port is already taken the script moves to the next free one and tells
you, so the URLs it prints at the end are the ones to use.

### If it cannot find your database

The Windows Postgres installer does not put `psql` on `PATH`. The script
looks in `C:\Program Files\PostgreSQL\*\bin` anyway, but if your install
is elsewhere, point at it directly:

```powershell
$env:PGBIN = "D:\Postgres\18\bin"
.\scripts\dev.ps1
# or skip detection entirely
.\scripts\dev.ps1 -DatabaseUrl "postgresql+asyncpg://postgres:PASSWORD@localhost:5432/agentscrape"
```

### Demo: open on Arizona, run a live crawl

`seed-demo` (run by `dev.sh`) loads the real crawl snapshots in `demo/`
instead of invented schools; `demo/arizona.json` holds the University of
Arizona results. `FEATURED_SCHOOL=medicine.arizona.edu` lists it first, so the
UI opens on it. On a fresh deployment:

```bash
uv run alembic upgrade head
uv run agentscrape seed-demo            # or: agentscrape import-school demo/arizona.json
uv run agentscrape llm-check            # the model must answer before a live run
```

Starting an update from the UI opens the run monitor, which
streams what the agent is doing — sites mapped, programs identified, each page
read and who was on it, each program covered — and shows totals, time and model
spend at the end. For a demo-length run (about 5–10 minutes) set:

```
DEFAULT_STEP_BUDGET=60
DISCOVERY_TIMEOUT_SECONDS=120
ENABLE_CRT_SH=false
```

A real crawl uses `DEFAULT_STEP_BUDGET=5000`. The model runs on the Hugging Face
router (`LLM_API_KEY=hf_...`); the account needs prepaid credits or PRO, or
every site stops with `LLM_UNAVAILABLE`. `agentscrape export-school <domain>
<file>` snapshots any crawled school for the same purpose.

### Crawling a real institution

Two strategies, set by `CRAWL_STRATEGY` or per run (`config.crawl_strategy`,
`--strategy` on the CLI):

- **`agent`** (default): the model ranks every discovered link and reads
  every page it visits. On UChicago over 30 minutes it found 73.4% of the
  client sheet for $0.98.
- **`hybrid`**: a plain-HTML pass maps up to `HTML_MAP_MAX_PAGES`
  pages with no model calls — following links, ranking them by the keyword
  heuristic, and noting which pages show people (three or more names, two or
  more addresses, a JavaScript shell, or a roster-like URL). The model then
  reads only those pages, and link triage runs on the cheaper
  `LLM_CHEAP_MODEL`. A page with one or two names (a footer, a byline, a
  contact block) is not read. It is cheaper per minute ($0.73 in the same
  test) but far lower in recall (18.2%): the keyword ranking misses rosters
  the model's ranking finds. Use it only where cost matters more than recall.

Both need a working OpenAI-compatible endpoint (`LLM_BASE_URL`, `LLM_API_KEY` in
`.env`); check it first with `agentscrape llm-check`.

```bash
uv run agentscrape site medicine.uchicago.edu --dry-run   # discovery and ranking only
uv run agentscrape site radonc.uchicago.edu               # full pipeline, hybrid
uv run agentscrape site radonc.uchicago.edu --strategy agent
uv run agentscrape site radonc.uchicago.edu --mode directory   # directory search only
```

`--no-browser` skips Chromium (faster, but misses rosters rendered by
JavaScript).

### Directory search

Each school sheet row carries the school's people directory. A run with
`config.modes` containing `"directory"` looks up the people already found —
residents and fellows first — and fills in only what their roster left blank:
address, PGY, class year, title. The first run learns how to search the
directory (its own GET form, or the URL a JavaScript search produces) and
stores it on the site; a directory behind a sign-in is reported and skipped. A
search that names two possible matches fills nothing. Directory-only runs need
a school that has already been crawled.

### Model timeouts (HTTP 504)

The Hugging Face router answers 504 when its upstream provider does not finish
in time — in practice a long roster read. A 504 or client timeout is retried
once; after that the page chunk (or triage batch) is split in half and read in
parts, instead of resending the same oversized request. Every failed attempt
logs its status, prompt size, `max_tokens`, model and elapsed time. Keep
`LLM_CONCURRENCY` modest (default 8): queued calls at the provider are what time
out.

### Running schools one at a time

`LLM_CONCURRENCY` is one budget for the whole process, so runs started side by
side split the model throughput between them — and each one still pays its own
discovery and link-ranking startup in full before it reads a roster. Measured
on this code: `gme.uchicago.edu` alone for 30 minutes found 883 residents and
fellows for $0.89, while three schools started together for the same 30 minutes
spent $1.30 between them and surfaced 22. The money went into three startups
instead of one harvest.

Queue the runs instead. A queued run waits for the process to go idle and
starts on its own the moment the run before it finishes:

```bash
curl -X POST "$API/runs" -H "Authorization: Bearer $TOKEN" \
  -d '{"sites": ["gme.uchicago.edu"], "config": {"queued": true}}'
```

`GET /runs/{id}` then reports `queued` and `queue_position` (1 goes next) until
it starts. Set `QUEUE_RUNS=true` to make it the default for every run, which is
the right setting on one machine with one model endpoint; `config.queued`
overrides it per run. The queue orders the runs sharing this process's model
gate, so it is held in the process, like the worker pool — a restart resumes
the queue and starts one run, not all of them.

Queueing does not slow the work down: three schools through the queue get the
whole model budget each in turn, where three at once get a third of it each and
pay three startups out of it.

### Checking a school against the client's sheet

The client's working spreadsheet is the ground truth for a school. After a run,
compare the two — coverage grouped by specialty says *where* the crawl went
wrong, not just that it did:

```bash
uv run python scripts/coverage_check.py medicine.arizona.edu ~/Arizona.csv
```

A whole specialty missing means a roster page was never visited, which is a
ranking or discovery problem. A few people missing across many specialties means
extraction dropped them off pages it did reach.

### Manual setup

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv --python 3.12 && uv sync
uv run playwright install chromium
cp .env.example .env
docker compose up -d                 # Postgres on :5433
uv run alembic upgrade head
uv run agentscrape seed-demo
uv run uvicorn agentscrape.api.main:app --port 8000
```

Tests need the Postgres container up; they use a separate `agentscrape_test`
database:

```bash
docker compose exec -T postgres psql -U agentscrape -d postgres \
  -c "CREATE DATABASE agentscrape_test;"
uv run pytest -q
```

### Deployment shape

**Frontend on GitHub Pages:** build the sibling `agentscrape-frontend` repo with
`BASE_PATH=/agentscrape-frontend/ npm run build`, copy `dist/index.html` to
`dist/404.html`, and publish `dist/`. Set `VITE_API_BASE_URL` to the deployed
API URL at build time, or use the login screen/API query override at runtime.

**Backend on AWS:** use a container service such as App Runner or ECS/Fargate
for this FastAPI app, plus RDS Postgres. Run `uv run alembic upgrade head`
against the RDS database during deploy. Persist `ARTIFACT_DIR` on EFS or move
artifacts to object storage before relying on screenshots/exports in production.

Deploys are additive: every `drop_table` in `alembic/versions/` belongs to a
`downgrade()`, and the startup retention sweep only touches screenshots and
exports. Crawled people are erased by exactly two things, neither of which
belongs anywhere near a deploy script: `agentscrape seed-demo --reset`, which
truncates every table, and `scripts/crawl.sh`, which deletes the sites it is
about to re-benchmark (and cascades to their records). Point `DATABASE_URL` at
RDS rather than a database inside the task, or each deployment starts empty.
`SEED_DEMO_ON_STARTUP` is off unless set, so an empty production database stays
empty instead of filling with demo schools.
Set `CORS_ORIGINS` to the exact GitHub Pages origin and any local origins you
still use.

**Model endpoint:** the crawler expects an OpenAI-compatible
`/v1/chat/completions` vision endpoint. A Hugging Face key works only when paired
with a Hugging Face/provider endpoint that exposes that OpenAI-compatible API
for the configured vision model; a plain model-page token by itself is not
enough.

---

## Notes for the frontend developer

The `/api/v1` contract is implemented as specified. Four things you need:

1. **Navigation is School → Person.** Programmes remain internal metadata keyed
   on normalized specialty, but clients browse all people for a school from
   `/schools` and `/schools/:id/people`.

2. **`category` and `position`.** Everyone on a site is collected, so a person
   has a `category` (`resident|fellow|faculty|staff|student|alumni|unknown`) for
   filtering and a `position` holding their title exactly as printed
   ("Program Director", "Associate Professor").

3. **Nothing is inferred.** If a page does not state a value, the field is
   blank. `pgy` is exactly what was printed, never rolled forward, and
   `pgy_capture_date` says when it was read. There is no derived class-of.

4. **Two passwords, Bearer tokens.** Both grant browse, export and starting,
   cancelling or retrying runs (crawls and directory searches); the admin
   password signs in with the `admin` scope. `max_spend_usd` on a run caps its
   spend.
   Tokens rather than cookies because the frontend is
   served from GitHub Pages — a session cookie would be third-party to this API
   and blocked by Safari and increasingly Chrome. `GET /runs/{id}/events` also
   accepts `?token=` since `EventSource` cannot set headers.

5. **Screenshots use signed links.** `screenshot_url` comes back with `exp` and
   `sig` query parameters and is valid for an hour, so it can go straight into
   an `<img src>` without an Authorization header. Each version also carries
   `screenshot_width`/`screenshot_height` so the stored per-field boxes (in
   screenshot pixels) can be positioned as percentages.

6. **Missing people sort to the bottom** of any list rather than being filtered
   out, marked with `status: "missing"`.

7. **Crawl, directory search, or both.** `POST /runs` takes
   `config.modes`: `["crawl"]` (the default), `["directory"]` or both. Each
   school carries `has_crawl_data`, `directory_url` and
   `directory_search_available`; offer directory search only when the last is
   true. A directory-only run on a school without it is refused with `409
   DIRECTORY_SEARCH_UNAVAILABLE`, whose `details.sites` says why per school.
   Values filled from the directory carry `extraction_method: "directory"`.

8. **Run limits.** `config.max_records`, `max_trainees`, `max_emails` and
   `max_spend_usd` (all optional). Heartbeats carry `records_collected`,
   `trainees_collected`, `emails_collected` and `spend_usd`; a run that hit one
   ends `stopped_at_limit` with `stop_reason` `max_records`, `max_trainees`,
   `max_emails` or `max_spend`.

---

## How a site is processed

```
validate → skip check → link discovery → [HTML map] → rank → extract (loop)
         → reconcile → [directory search] → finalize
```

The HTML map runs on the hybrid strategy; directory search runs when the run
asks for it, and a directory-only run goes straight from entry to directory
search.

**Validate.** K-12 institutions are rejected explicitly, with a stated reason,
never silently filtered. Hostname patterns decide the clear cases; homepage
content decides the rest; the model is consulted only when both are
inconclusive. A rejection surfaces as `K12_INSTITUTION_REJECTED` on the
SiteRun before extraction compute is spent. (Schools are a separate project;
this backend is for post-secondary institutions only.)

**Skip check.** Two cheap tiers. First, re-fetch the known-good paths and
compare content hashes — all unchanged means skip without parsing anything.
Otherwise parse addresses out of those pages and take the Jaccard similarity
against the site's stored identity keys; at or above the run's threshold
(default 0.90) the roster is assumed unchanged. Both the score and the reason
are always recorded, and `force_rescan` overrides at run and site level.

**Discovery.** No browser. sitemap.xml (following indexes), robots.txt
`Sitemap:` hints, the entry page's own links, and then **a sitemap walk of every
in-scope host the site itself pointed at**. That last source is the one that
matters on a medical school: departments live on *siblings* of the entry point —
`surgery.<univ>.edu`, `obgyn.<univ>.edu`, `pathology.<univ>.edu` sit beside
`medicine.<univ>.edu`, each with its own sitemap and its own rosters. Walking
only the entry host reached a handful of their pages through cross-links and
none of their rosters. Because a university shares its registrable domain with
its bookstore and its CDN, sibling hosts are filtered by label before being
walked.

Certificate-transparency logs (crt.sh) run last and only on hosts the site did
not already reveal: a university apex resolves to thousands of CT hostnames,
almost all infrastructure, and none of the four departmental hosts above appear
there at all. A search provider is wired but stubbed: with no API key it is
skipped silently, so there is no hard dependency on a paid service. Every source
is individually timed out and the whole stage has a budget, because discovery is
best-effort and must degrade rather than hang.

**Ranking.** Heuristic scoring over path and title, driven by the URL's **last
path segment** — that is what names the page, while its ancestors only say which
section it sits in. Token sets rather than phrase matching, so the commonest
roster spelling on `.edu` sites (`current-and-past-residents`) is not broken by
the `and-past` infix. A news or photo section vetoes a roster-shaped leaf
outright, and a leaf longer than about six tokens is an article headline rather
than a page name. Known-good paths jump the queue. Faculty listings and alumni
pages rank below current-trainee rosters but are still collected — everyone
published on the site is in scope.

**Frontier.** Discovery runs once, before anything is fetched, so it cannot see
a page reachable only by following a link. The links off every page fetched are
scored and folded back into the unvisited tail of the work list, which is then
re-sorted: a roster found on page 3 is visited before the eighty faculty pages
already queued behind it. Only the tail is touched, so the loop's cursor stays
valid and nothing is re-scored or re-fetched.

**Extraction.** Cheapest first, escalating only on evidence:

| Step | Cost | When |
|---|---|---|
| Plain HTML fetch | cheap | always tried first |
| Render + screenshot | expensive | JS-rendered page, no addresses in the DOM, or contacts published as images |
| One interaction | expensive | only when the accessibility tree offers pagination or "load more" |

A site stops early once `STOP_AFTER_BARREN_PAGES` consecutive candidate pages
yield **nobody at all**. Candidates are ranked, so a barren stretch means the
productive pages are already behind us — the crawl ends when the site stops
giving rather than at a fixed count. The counter tracks people *found*, not
people new or changed: a re-scrape of a site whose rosters have not moved yields
nothing new on every page, and counting that as barren stopped the second run of
a site before it reached anything it had not already seen.

The method used is recorded on every extraction, as both a debugging and a cost
signal. When the agent drives the browser it identifies elements from the
**accessibility tree by role and name** — never by predicting pixel coordinates
from a screenshot. Vision is used to read pages, read contacts rendered as
images, and disambiguate similar links; it never produces click targets.

**Reconciliation.** Serialized per site with a Postgres advisory lock, so sites
run in parallel but one site's writes never interleave. Identical data touches
`last_seen_at` and writes no version; a difference writes a new immutable
`RecordVersion` carrying the diff; an unmatched person creates a record. Records
are **never deleted**. A record is marked missing only when the page it came
from was successfully revisited in that run — otherwise a discovery miss or a
500 would be indistinguishable from someone leaving the programme.

---

## Provenance

Every `RecordVersion` stores the exact URL, the page title at capture, a
screenshot, the capture timestamp, the extraction method, a confidence score,
and per-field bounding boxes so the frontend can highlight exactly where on the
page a value was found. Boxes are measured from the real DOM with
`getBoundingClientRect`, not guessed by the model.

A school keeps only its **most recent run's** screenshots: when a school is
crawled again, the previous images are deleted and replaced. **Provenance
survives** — URL, title, timestamp, method and field locations stay, and
`screenshot_available` flips to false for the superseded versions. Skipped runs
capture nothing and therefore replace nothing.

---

## Concurrency

`site_runs` is the work queue. Workers claim rows with `FOR UPDATE SKIP LOCKED`,
which gives atomic hand-out with no broker and survives a restart because the
claim is row state rather than memory.

- Concurrency is per run, 1–8, and the pool size is respected.
- Each agent owns one isolated browser context and is reused across sites.
- Within a site, cheap HTML fetches run concurrently (default 4) while browser
  steps stay serial, so parallelism goes where it is safe and memory does not
  scale with pages × agents.
- Every URL is claimed in `site_run_visits` before work starts, so multiple
  entry points into one site never re-fetch the same page and a resumed run
  skips what it already did.
- **Resume is checkpointed mid-site.** The ranked candidate list and the loop's
  position are written to `site_runs.checkpoint_state` after every batch, so a
  restarted run goes straight back to extracting instead of repeating discovery.
  Reconciliation runs per batch, so an interrupted site keeps everything it had
  already collected.
- Bounded everywhere: per-site step budget, per-page timeout, per-site timeout,
  discovery budget, overall run timeout.
- **Memory is the real constraint.** The ceiling is explicit: run creation is
  refused with `RESOURCE_LIMIT_EXCEEDED` when the requested concurrency will not
  fit in available RAM, rather than letting the box thrash.

Limits are live counters, all optional, set per run in `config`:
`max_records` (people), `max_trainees` (residents and fellows), `max_emails`
(people with an address) and `max_spend_usd`. Counts are unique people seen in
the run, reported after every batch of pages, so a limit trips mid-school. A
count limit ends the crawl — no new school is claimed and the current one stops
reading pages — but a requested directory search still looks up the people
already found. The spend limit stops everything. Either way the run is marked
`stopped_at_limit` (with `stop_reason` saying which) — not completed, not
failed. Partial results are valid results. Spend is metered from each model
response as it happens, never computed at the end.

**Program list first.** The planner's residency and fellowship programs set the
order of work: pages of programs still missing a roster come before every other
page, whatever else the page lists. When none are queued, the model is asked
where those rosters are before the crawl moves on to staff directories,
departments and the rest of the site.

---

## Decisions that would be expensive to reverse

1. **`area` = specialty, `year` = class-of year.** Bound into the fixed contract.
   Changing the meaning invalidates saved frontend filters.
2. **Record identity is the normalized email, scoped to site**, with a name+role
   fallback and a guard that treats generic mailboxes (`info@`, `residency@`) as
   offices rather than people. Changing the key later means re-keying all
   history.
3. **`SPECIALTY_FINAL_PGY` stores the final PGY level, not accredited programme
   length.** Radiation Oncology is a four-year programme whose residents are
   PGY-2 to PGY-5; storing `4` produced no class year at all for the graduating
   cohort. Class-of derivation depends on this semantic.
4. **Postgres advisory locks serialize per-site reconciliation.** Correct and
   cheap on one box; would need rework if this ever split across machines.
5. **`site_runs` doubles as the work queue.** Simple and resumable; throughput is
   capped by one Postgres, which is far above eight agents.
6. **robots.txt `Disallow` is not enforced** (explicit client decision). Mitigated
   by a per-domain rate limit, an identifying User-Agent with a contact address,
   honouring `Crawl-delay`, and only fetching publicly published pages. The
   parser is retained behind `RESPECT_ROBOTS`, so enforcing it is a one-line
   config change.
7. **Screenshots live on local disk**, consistent with the one-box constraint.
   Moving to object storage later is a path migration.

## Known limitations

- The skip check probes with plain HTTP, so on JavaScript-rendered rosters it
  cannot see the people and relies on tier one (content hashes) alone. If such a
  site's HTML shell changes, it will be rescanned unnecessarily. This fails
  toward doing more work, never toward returning stale data.
- Class-of back-fill from PGY assumes a standard programme length; off-cycle
  trainees (research years, dual programmes) will be derived incorrectly.
  Derived values are tagged `year_source: "derived"` so they can be told apart
  from what a page actually stated.
- `Emergency Medicine` is entered as a three-year programme; four-year EM
  programmes will derive a class year one year early.
