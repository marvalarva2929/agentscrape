# agentscrape

Backend for a contact-extraction platform. It takes a CSV of teaching-hospital
and medical-school URLs, finds the **residents and fellows** published on each
institution's web presence, stores them with full provenance, and tracks how the
rosters change across repeated runs.

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

1. **`area` is the normalized specialty; `year` is the class-of year.** The
   input CSV is only links, so neither is supplied by the upload. Specialty is
   inferred per record from URL path, subdomain and page title, then collapsed
   onto a fixed ACGME vocabulary. `/meta/areas` and `/meta/years` return exactly
   the values these filters accept.

2. **Extra `/records` filters, all optional and additive:** `role`
   (`resident|fellow|unknown` — your R/F column), `pgy`, `hospital`,
   `has_email`, `include_role_accounts`. `/records/stats` accepts the identical
   set and returns every aggregate you need, so nothing has to be counted
   client-side.

3. **SSE auth.** `EventSource` cannot set an `Authorization` header, so
   `GET /runs/{id}/events` also accepts `?token=`. It is the only endpoint that
   does. Everything else requires `Authorization: Bearer <token>`.

4. **`pgy` is computed, `pgy_at_capture` is stored.** A PGY-2 in 2026 is a PGY-3
   in 2027, so the API returns `pgy` rolled forward to today (July 1 rollover)
   alongside the raw captured value and its date. Filtering on `pgy` means
   "PGY-n today". Change detection uses the captured value, so the annual
   rollover never shows up as a data change.

`Has Been Emailed?` is deliberately **not** stored. It is your team's state, not
scraped data. `POST /records/export` takes `include_emailed_column: true` and
emits the column empty for you to fill in.

---

## How a site is processed

```
validate → skip check → link discovery → rank → extract (loop) → reconcile → finalize
```

**Validate.** K-12 institutions are rejected explicitly, with a stated reason,
never silently filtered. Hostname patterns decide the clear cases; homepage
content decides the rest; the model is consulted only when both are
inconclusive. A rejection surfaces in `POST /runs/validate` before compute is
spent, and as `K12_INSTITUTION_REJECTED` on the SiteRun.

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

Screenshots expire on a configurable window; **provenance survives expiry**. The
sweeper deletes the image and flips `screenshot_available` to false, leaving URL,
title, timestamp, method and field locations intact. It runs at API startup and
via `agentscrape sweep`, not cron, because a box that stops when idle would
never fire a schedule.

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
