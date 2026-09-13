# agentscrape

Backend for a contact-extraction platform. It takes a CSV of teaching-hospital
and medical-school URLs and collects **everyone published on each institution's
web presence** — residents, fellows, faculty, program directors, coordinators,
staff, students and alumni — each labelled with their printed title, stored with
full provenance, and tracked across repeated runs.

Clients do not start crawls: they submit a CSV of the schools they want, and
staff review and launch it from the admin area (billing is per school).

Everything runs on one box: API, orchestrator, agents, Postgres, file storage.
The server is started on demand and stopped when idle, so nothing assumes
continuous uptime.

---

## Quick start

```bash
# Toolchain (system Python 3.9 is too old for LangGraph)
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv --python 3.12 && uv sync
uv run playwright install chromium

cp .env.example .env          # set APP_PASSWORD and LLM_BASE_URL
docker compose up -d          # Postgres on :5433
uv run alembic upgrade head

# One site, end to end, no concurrency — the fastest correctness check
uv run agentscrape site medicine.uchicago.edu --dry-run   # ranked candidates only
uv run agentscrape site radonc.uchicago.edu               # full pipeline

# API
uv run uvicorn agentscrape.api.main:app --port 8000
```

Tests need the Postgres container up; they use a separate `agentscrape_test`
database:

```bash
docker compose exec -T postgres psql -U agentscrape -d postgres \
  -c "CREATE DATABASE agentscrape_test;"
uv run pytest -q
```

---

## Notes for the frontend developer

The `/api/v1` contract is implemented as specified. Four things you need:

1. **Navigation is School → Program → Person.** A programme is one training
   programme at a school, keyed on the normalized specialty and created
   automatically as people are extracted. `/schools`, `/schools/:id/programs`,
   `/programs/:id/people`.

2. **`category` and `position`.** Everyone on a site is collected, so a person
   has a `category` (`resident|fellow|faculty|staff|student|alumni|unknown`) for
   filtering and a `position` holding their title exactly as printed
   ("Program Director", "Associate Professor").

3. **Nothing is inferred.** If a page does not state a value, the field is
   blank. `pgy` is exactly what was printed, never rolled forward, and
   `pgy_capture_date` says when it was read. There is no derived class-of.

4. **Two passwords, Bearer tokens.** The client password grants browse, export
   and CSV submission; a separate admin password grants the submissions queue,
   launching runs and spend. Tokens rather than cookies because the frontend is
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

---

## How a site is processed

```
validate → skip check → link discovery → rank → extract (loop) → reconcile → finalize
```

**Validate.** K-12 institutions are rejected explicitly, with a stated reason,
never silently filtered. Hostname patterns decide the clear cases; homepage
content decides the rest; the model is consulted only when both are
inconclusive. A rejection surfaces in `POST /runs/validate` before compute is
spent, and as `K12_INSTITUTION_REJECTED` on the SiteRun. (Schools are a separate
project; this backend is for post-secondary institutions only.)

**Skip check.** Two cheap tiers. First, re-fetch the known-good paths and
compare content hashes — all unchanged means skip without parsing anything.
Otherwise parse addresses out of those pages and take the Jaccard similarity
against the site's stored identity keys; at or above the run's threshold
(default 0.90) the roster is assumed unchanged. Both the score and the reason
are always recorded, and `force_rescan` overrides at run and site level.

**Discovery.** No browser. sitemap.xml (following indexes), robots.txt
`Sitemap:` hints, and certificate-transparency logs via crt.sh, which is what
surfaces departmental subdomains like `radonc.<univ>.edu` that are never linked
from the homepage. A search provider is wired but stubbed: with no API key it is
skipped silently, so there is no hard dependency on a paid service. Every source
is individually timed out and the whole stage has a budget, because discovery is
best-effort and must degrade rather than hang.

**Ranking.** Heuristic scoring over path and title. Known-good paths jump the
queue. Faculty-only listings rank below trainee rosters, and alumni pages are
pushed far down — they are former trainees, out of scope.

**Extraction.** Cheapest first, escalating only on evidence:

| Step | Cost | When |
|---|---|---|
| Plain HTML fetch | cheap | always tried first |
| Render + screenshot | expensive | JS-rendered page, no addresses in the DOM, or contacts published as images |
| One interaction | expensive | only when the accessibility tree offers pagination or "load more" |

A site stops early once several consecutive candidate pages yield nobody new
(`STOP_AFTER_BARREN_PAGES`). Candidates are ranked, so a barren stretch means
the productive pages are already behind us — the crawl ends when the site stops
giving rather than at a fixed count.

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

Hard stops (max records, max spend) are live counters checked before claiming
and after each step. When one trips, the run stops claiming, in-flight sites
finish their current step, the queue drains, and the run is marked
`stopped_at_limit` — not completed, not failed. Partial results are valid
results. Spend is metered from each model response as it happens, never computed
at the end.

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
