# Graph Report - .  (2026-09-17)

## Corpus Check
- 113 files · ~66,642 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1449 nodes · 3942 edges · 90 communities (84 shown, 6 thin omitted)
- Extraction: 80% EXTRACTED · 20% INFERRED · 0% AMBIGUOUS · INFERRED: 805 edges (avg confidence: 0.65)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Test Domain Components|Test Domain Components]]
- [[_COMMUNITY_Checkpoint Components|Checkpoint Components]]
- [[_COMMUNITY_Test Extraction Components|Test Extraction Components]]
- [[_COMMUNITY_Search Components|Search Components]]
- [[_COMMUNITY_Html People Components|Html People Components]]
- [[_COMMUNITY_Test Reconcile Components|Test Reconcile Components]]
- [[_COMMUNITY_Test Export Sse Components|Test Export Sse Components]]
- [[_COMMUNITY_Sites Components|Sites Components]]
- [[_COMMUNITY_Test End To End Components|Test End To End Components]]
- [[_COMMUNITY_Test Api Components|Test Api Components]]
- [[_COMMUNITY_Sitemap Components|Sitemap Components]]
- [[_COMMUNITY_Ids Components|Ids Components]]
- [[_COMMUNITY_Test Reconcile Components|Test Reconcile Components]]
- [[_COMMUNITY_Queue Components|Queue Components]]
- [[_COMMUNITY_Renderer Components|Renderer Components]]
- [[_COMMUNITY_Records Components|Records Components]]
- [[_COMMUNITY_Test Extraction Components|Test Extraction Components]]
- [[_COMMUNITY_Test Schools Api Components|Test Schools Api Components]]
- [[_COMMUNITY_Query Components|Query Components]]
- [[_COMMUNITY_Sites Components|Sites Components]]
- [[_COMMUNITY_Cli Components|Cli Components]]
- [[_COMMUNITY_Test Extraction Components|Test Extraction Components]]
- [[_COMMUNITY_Test Skip Components|Test Skip Components]]
- [[_COMMUNITY_Test Host Discovery Components|Test Host Discovery Components]]
- [[_COMMUNITY_Service Components|Service Components]]
- [[_COMMUNITY_Readme Components|Readme Components]]
- [[_COMMUNITY_Schools Components|Schools Components]]
- [[_COMMUNITY_Test Domain Components|Test Domain Components]]
- [[_COMMUNITY_Vision Components|Vision Components]]
- [[_COMMUNITY_Test Conftest Components|Test Conftest Components]]
- [[_COMMUNITY_Submissions Components|Submissions Components]]
- [[_COMMUNITY_Ratelimit Components|Ratelimit Components]]
- [[_COMMUNITY_Events Components|Events Components]]
- [[_COMMUNITY_Artifacts Components|Artifacts Components]]
- [[_COMMUNITY_Test Frontier Components|Test Frontier Components]]
- [[_COMMUNITY_Fetcher Components|Fetcher Components]]
- [[_COMMUNITY_Benchmark Components|Benchmark Components]]
- [[_COMMUNITY_Limits Components|Limits Components]]
- [[_COMMUNITY_Admin Components|Admin Components]]
- [[_COMMUNITY_Specialty Components|Specialty Components]]
- [[_COMMUNITY_Test Extraction Components|Test Extraction Components]]
- [[_COMMUNITY_Errors Components|Errors Components]]
- [[_COMMUNITY_Scoring Components|Scoring Components]]
- [[_COMMUNITY_Institution Components|Institution Components]]
- [[_COMMUNITY_Pool Components|Pool Components]]
- [[_COMMUNITY_Test Discovery Scoring Components|Test Discovery Scoring Components]]
- [[_COMMUNITY_Auth Components|Auth Components]]
- [[_COMMUNITY_Deps Components|Deps Components]]
- [[_COMMUNITY_Security Components|Security Components]]
- [[_COMMUNITY_Session Components|Session Components]]
- [[_COMMUNITY_Service Components|Service Components]]
- [[_COMMUNITY_Records Components|Records Components]]
- [[_COMMUNITY_Dev Components|Dev Components]]
- [[_COMMUNITY_Limits Components|Limits Components]]
- [[_COMMUNITY_Config Components|Config Components]]
- [[_COMMUNITY_Test Resilience Components|Test Resilience Components]]
- [[_COMMUNITY_Coverage Check Components|Coverage Check Components]]
- [[_COMMUNITY_Dev Components|Dev Components]]
- [[_COMMUNITY_Fixture Server Components|Fixture Server Components]]
- [[_COMMUNITY_Test Api Components|Test Api Components]]
- [[_COMMUNITY_Test Extraction Components|Test Extraction Components]]
- [[_COMMUNITY_Env Components|Env Components]]
- [[_COMMUNITY_Test Extraction Components|Test Extraction Components]]
- [[_COMMUNITY_Project Components|Project Components]]
- [[_COMMUNITY_Fingerprint Components|Fingerprint Components]]
- [[_COMMUNITY_Records Components|Records Components]]
- [[_COMMUNITY_Readme Components|Readme Components]]
- [[_COMMUNITY_Crawl Components|Crawl Components]]
- [[_COMMUNITY_Person Components|Person Components]]
- [[_COMMUNITY_Pyproject Components|Pyproject Components]]

## God Nodes (most connected - your core abstractions)
1. `Record` - 80 edges
2. `Site` - 77 edges
3. `SiteRun` - 72 edges
4. `Run` - 63 edges
5. `RecordVersion` - 60 edges
6. `PersonCategory` - 56 edges
7. `AppError` - 39 edges
8. `RunLimits` - 35 edges
9. `ExtractedPerson` - 34 edges
10. `reconcile_people()` - 32 edges

## Surprising Connections (you probably didn't know these)
- `client()` --calls--> `create_app()`  [INFERRED]
  tests/test_schools_api.py → src/agentscrape/api/main.py
- `test_non_roster_pages_stay_below_the_roster_band()` --calls--> `score_url()`  [INFERRED]
  tests/test_discovery_scoring.py → src/agentscrape/discovery/scoring.py
- `test_roster_pages_clear_the_roster_band()` --calls--> `score_url()`  [INFERRED]
  tests/test_discovery_scoring.py → src/agentscrape/discovery/scoring.py
- `Score` --uses--> `Record`  [INFERRED]
  scripts/benchmark.py → src/agentscrape/db/models.py
- `Score` --uses--> `RecordVersion`  [INFERRED]
  scripts/benchmark.py → src/agentscrape/db/models.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **End-to-End Site Processing** — readme_institution_validation, readme_two_tier_skip_check, readme_cross_host_discovery, readme_candidate_ranking, readme_dynamic_frontier, readme_cost_escalating_extraction, readme_record_reconciliation [EXTRACTED 1.00]
- **Resilient PostgreSQL-Backed Execution** — readme_site_runs_work_queue, readme_checkpointed_resume, readme_record_reconciliation, readme_postgresql, docker_compose_postgres, docker_compose_agentscrape_pgdata [INFERRED 0.85]
- **Cost-Aware Crawling** — readme_two_tier_skip_check, readme_candidate_ranking, readme_cost_escalating_extraction, readme_barren_page_early_stop, readme_resource_and_spend_limits [INFERRED 0.85]

## Communities (90 total, 6 thin omitted)

### Community 0 - "Test Domain Components"
Cohesion: 0.07
Nodes (45): Exception, JSONResponse, AppError, Any, Base for every error that should reach the client as a clean envelope., ResourceLimitError, Cursor, cancel_run() (+37 more)

### Community 1 - "Checkpoint Components"
Cohesion: 0.08
Nodes (41): AuthedUser, Lets the frontend restore a session on reload without re-prompting., session(), active_known_paths(), get_site(), URLs already handled in this SiteRun — the resume anchor., Known-good paths, best first. These jump the queue on the next run., visited_hashes() (+33 more)

### Community 2 - "Test Extraction Components"
Cohesion: 0.05
Nodes (23): extract_people(), page_is_alumni_listing(), Parse every person published on a page, labelled with their position.      Nobod, True when a page lists people who have already finished the programme.      Thes, deobfuscate(), Rewrite common obfuscations into plain addresses before extraction., large_card_roster(), Roster HTML in the three shapes residency sites actually publish. (+15 more)

### Community 3 - "Search Components"
Cohesion: 0.07
Nodes (27): ABC, BraveSearchProvider, discover_via_search(), get_search_provider(), NullSearchProvider, Search-engine discovery behind a provider interface.  Deliberately a stub: with, Resolve the configured provider. Falls back to the no-op when unusable., Return result URLs for `query` restricted to `site`. (+19 more)

### Community 4 - "Html People Components"
Cohesion: 0.12
Nodes (42): HTMLParser, Node, parse_class_of(), parse_pgy(), PGY and Class-of handling.  PGY is *time-relative*: a PGY-2 in 2026 is a PGY-3 i, Extract a PGY level from free text. Returns None when absent or implausible., Extract a graduation year. Two-digit years resolve to 2000-2099., _block_key() (+34 more)

### Community 5 - "Test Reconcile Components"
Cohesion: 0.12
Nodes (38): ExportStatus, ExtractionMethod, FetchMode, IdentityKind, PersonCategory, Enumerations shared by the ORM, the API schemas and the pipeline., Lifecycle of a legacy school request., Coarse bucket for filtering. The person's printed title is kept verbatim     alo (+30 more)

### Community 6 - "Test Export Sse Components"
Cohesion: 0.09
Nodes (22): Queue, event_stream(), format_event(), Server-sent events for the live run monitor.  Two guarantees the frontend relies, Yield SSE frames for one run until it reaches a terminal state., Export, Async filtered CSV export job., Generate the CSV. Failures are recorded on the job, never raised. (+14 more)

### Community 7 - "Sites Components"
Cohesion: 0.10
Nodes (37): DeclarativeBase, site_detail(), _site_out(), ValidationStatus, Base, KnownPath, A URL that previously yielded records for a site, with success statistics., One institution's web presence. Persists across runs. (+29 more)

### Community 8 - "Test End To End Components"
Cohesion: 0.16
Nodes (24): RunStatus, SiteRunStatus, One execution over a list of sites., Every URL touched during one SiteRun.      This is the in-site dedupe set (multi, Run, SiteRunVisit, RunConfigIn, RunCreate (+16 more)

### Community 9 - "Test Api Components"
Cohesion: 0.07
Nodes (12): Immutable snapshot, written only when something changed. Carries provenance., RecordVersion, API contract tests: auth, error envelope, pagination, filters, provenance.  The, URL, title, timestamp and method are kept after the image is swept., One site, one run, three records, one with two versions., seeded(), TestAuth, TestErrorEnvelope (+4 more)

### Community 10 - "Sitemap Components"
Cohesion: 0.12
Nodes (30): discover_subdomains(), Subdomain enumeration via certificate transparency logs (crt.sh).  This is what, Distinct hostnames under the registrable domain seen in CT logs., Turn hostnames into root URLs worth probing for a sitemap., subdomain_seed_urls(), discover_from_sitemaps(), extract_links(), fetch_robots() (+22 more)

### Community 11 - "Ids Components"
Cohesion: 0.13
Nodes (28): export_id(), new_id(), path_id(), program_id(), Opaque string IDs, per the API contract.  Prefixed so a stray ID in a log or a b, record_id(), run_id(), site_id() (+20 more)

### Community 12 - "Test Reconcile Components"
Cohesion: 0.23
Nodes (14): A person found at a site. Stable identity across runs. Never deleted., Record, lock_site(), mark_missing_records(), Match a page's people against stored records and write the differences.      The, Mark records absent from this run as missing — but only when the page they     c, Serialize reconciliation for one site.      Transaction-scoped, so it is release, reconcile_people() (+6 more)

### Community 13 - "Queue Components"
Cohesion: 0.15
Nodes (21): StopReason, One site's participation in one run. Also the orchestrator's work queue row., SiteRun, Worker pool: reusable agents, each owning one isolated browser context.  Require, cancel_pending(), claim_next_site(), heartbeat(), pending_count() (+13 more)

### Community 14 - "Renderer Components"
Cohesion: 0.12
Nodes (24): BrowserContext, Cheap HTTP fetching — the default path. The browser is the escalation., get_rate_limiter(), Per-domain token bucket.  robots.txt Disallow is not enforced (see RESPECT_ROBOT, click_by_accessible_name(), Playwright rendering — the expensive path, used only on escalation.  Two deliber, Scroll the page, then poll until extractable content stops growing.      Two fai, Get this agent's context, creating it once and reusing it thereafter. (+16 more)

### Community 15 - "Records Components"
Cohesion: 0.16
Nodes (27): alias, NotFoundError, _changes(), download_export(), export_status(), _filters_from_query(), get_person(), list_records() (+19 more)

### Community 16 - "Test Extraction Components"
Cohesion: 0.09
Nodes (17): classify_person(), Drop "Undergraduate:"-style field labels, keeping their values., Label a person. Never decides whether to keep them — everyone is kept.      Evid, Resident or fellow according to the roster, or None when it is silent., True when a heading marks people who have finished the programme., section_is_alumni(), strip_history_labels(), _trainee_from_context() (+9 more)

### Community 17 - "Test Schools Api Components"
Cohesion: 0.12
Nodes (9): client(), _headers(), School -> Person navigation, removed CSV upload and admin scope.  These are the, Captured as PGY-2 today, so it reads PGY-2 with its capture date., seeded(), TestAdminScope, TestNavigation, TestRemovedCsvUpload (+1 more)

### Community 18 - "Query Components"
Cohesion: 0.15
Nodes (21): Select, apply_filters(), _coerce_sort_value(), distinct_areas(), distinct_years(), get_record(), get_version(), Any (+13 more)

### Community 19 - "Sites Components"
Cohesion: 0.15
Nodes (23): areas(), categories(), _counts_subqueries(), list_sites(), AuthedUser, DbSession, description, Query (+15 more)

### Community 20 - "Cli Components"
Cohesion: 0.11
Nodes (20): BrowserPool, One isolated context per concurrent agent, reused across sites.      Contexts ar, Drop an agent's context, e.g. after a crash. Normal reuse keeps it., config(), _dry_run(), _print_outcome(), _print_records(), Command line entry points.  `agentscrape site <url>` runs the whole per-site pip (+12 more)

### Community 21 - "Test Extraction Components"
Cohesion: 0.11
Nodes (10): _extract_name(), Directory pages mix people with unit headings.      Collecting everyone means th, Defects found by running against live university sites.      Collecting everyone, The heading fallback reads every <h3>, so page furniture has to be     rejected, Real rosters print the role next to the name, in the same element.      Each of, TestNameExtraction, TestNamesPrintedWithTheirRole, TestOrganisationalNames (+2 more)

### Community 22 - "Test Skip Components"
Cohesion: 0.12
Nodes (8): compare_fingerprints(), jaccard(), Overlap of two identity sets. Two empty sets are 0.0, not 1.0 — nothing     know, True when every probed path returned byte-identical content.      The comparison, Skip-check semantics (Section 3.1).  Skipping is the main compute saving in the, TestDecideSkip, TestFingerprint, TestJaccard

### Community 23 - "Test Host Discovery Components"
Cohesion: 0.15
Nodes (23): _is_infrastructure(), _is_out_of_scope(), _leading_label(), _observed_hosts(), _rank_subdomains(), The label that names the host, ignoring a bare `www.` in front of it., True for a sibling host that belongs to the university, not the hospital.      O, Hosts inside the institution that the site itself pointed at.      Ranked by how (+15 more)

### Community 24 - "Service Components"
Cohesion: 0.13
Nodes (22): CsvRowPreview, One parsed school row retained for historical/admin request previews., ValidatePreview, get_active(), register(), unregister(), cancel_run(), create_run() (+14 more)

### Community 25 - "Readme Components"
Cohesion: 0.10
Nodes (22): agentscrape-pgdata Volume, PostgreSQL Service, PostgreSQL Health Check, Accessibility-Tree Browser Interaction, agentscrape, Barren-Page Early Stop, Candidate Page Ranking, Checkpointed Mid-Site Resume (+14 more)

### Community 26 - "Schools Components"
Cohesion: 0.22
Nodes (21): list_program_people(), list_school_people(), list_school_programs(), list_schools(), program_detail(), _program_out(), AuthedUser, DbSession (+13 more)

### Community 27 - "Test Domain Components"
Cohesion: 0.12
Nodes (12): build_identity(), Identity, is_role_account(), normalize_name(), Record identity within a site.  Identity is the normalized email, scoped to a si, Identity key for a record within one site.      Email wins unless it names an of, True when the address names an office rather than a person., Fold a display name to a comparable form.      Handles "Last, First", academic c (+4 more)

### Community 28 - "Vision Components"
Cohesion: 0.12
Nodes (14): normalize_email(), Lowercase and trim. Deliberately does not strip dots or +tags: on .edu     domai, _coerce_person(), extract_with_vision(), Any, Vision-language extraction — the escalation path.  Used when HTML parsing comes, Validate one model-emitted object. Returns None when it is not usable., Ask the model to read a page. Returns [] on any failure — never raises     into (+6 more)

### Community 29 - "Test Conftest Components"
Cohesion: 0.12
Nodes (15): async_sessionmaker, clean_tables(), Integration fixtures backed by a real Postgres database.  Reconciliation depends, Create the schema once for the whole test session., Truncate before each test, synchronously, so no async loop is involved., Give every test a fresh application engine.      `agentscrape.db.session` memoiz, reset_global_engine(), schema() (+7 more)

### Community 30 - "Submissions Components"
Cohesion: 0.19
Nodes (18): clamp_limit(), Opaque keyset cursors. Never OFFSET — pages must stay stable while rows are bein, list_submissions(), _out(), AdminUser, DbSession, Query, Staff queue for historical school requests.  Clients now request schools by emai (+10 more)

### Community 31 - "Ratelimit Components"
Cohesion: 0.15
Nodes (8): DomainRateLimiter, Honour a site-declared Crawl-delay when it is stricter than our rate., One bucket per registrable domain, shared across all agents in a process.      B, Slow down for a host that is refusing us — but only if it keeps at it., Clear the refusal streak, and climb back toward the base rate.          Without, BCM returned three 403s among 980 successes — a few pages it will not         se, One institution's twenty departmental subdomains are one server., TestBackingOffWhenAHostRefuses

### Community 32 - "Events Components"
Cohesion: 0.18
Nodes (15): EventEmitter, EventType, NullEmitter, Progress events.  Two consumers: the SSE stream and the database counters. Indiv, Convenience wrapper bound to one run (and optionally one site)., Used by the single-site CLI, where there is no stream to feed., Coarse phase, for the monitor's stage indicator.      Deliberately not one chip, RunStage (+7 more)

### Community 33 - "Artifacts Components"
Cohesion: 0.16
Nodes (18): Transactional scope. Commits on success, rolls back on any exception., session_scope(), absolute_path(), orphaned_screenshot_dirs(), datetime, Path, Screenshots on local disk with a configurable retention window.  Provenance must, Store paths relative to ARTIFACT_DIR so the directory can be relocated. (+10 more)

### Community 34 - "Test Frontier Components"
Cohesion: 0.19
Nodes (17): _merge_frontier(), Fold newly seen links into the part of the work list not yet visited.      Only, candidate(), The work list has to grow as pages are read.  Sitemap discovery runs once, befor, The caller's cursor indexes this list, so the head must not move., Every page carries the department's own nav; admitting it would refill     the l, A medical centre spans the university and the health system it staffs.      Chic, `&amp;` in an attribute is one separator, not a parameter called "amp".      Can (+9 more)

### Community 35 - "Fetcher Components"
Cohesion: 0.15
Nodes (8): Response, RuntimeError, Fetcher, FetchResult, Honour Retry-After when the server sends one; otherwise 0.5s, 1s, 2s., Concurrent cheap fetches. Parallelism here is safe; browser work is not., Shared async HTTP client with per-domain rate limiting and bounded retries., GET with rate limiting and exponential backoff on transient failures.

### Community 36 - "Benchmark Components"
Cohesion: 0.20
Nodes (11): first_last(), fold(), main(), print_failures(), print_scoreboard(), Lowercase, strip accents, credentials and anything after a comma., The two parts a sheet and a web page reliably agree on., Our trainee count over the institution's published one. (+3 more)

### Community 37 - "Limits Components"
Cohesion: 0.19
Nodes (3): Live counters for one run, shared by every worker., RunLimits, TestHardStops

### Community 38 - "Admin Components"
Cohesion: 0.20
Nodes (14): admin_sites(), admin_stats(), clear_known_path(), AuthedUser, DbSession, Query, Admin: site table with success rates, platform aggregates, path management.  `/a, Clear a stale known path manually. (+6 more)

### Community 39 - "Specialty Components"
Cohesion: 0.20
Nodes (11): infer_specialty(), normalize_specialty(), Controlled ACGME specialty vocabulary and normalizer.  Specialty is a *page* pro, Lowercase, collapse everything non-alphanumeric to single spaces., Collapse a free-text specialty string onto the controlled vocabulary.      Retur, Infer specialty from URL path segments, most specific segment first., Best specialty for a page, most specific evidence first.      Order: an explicit, specialty_from_url() (+3 more)

### Community 40 - "Test Extraction Components"
Cohesion: 0.20
Nodes (5): `.../current-and-past-residents` is the commonest roster URL on a .edu     medic, The heading directly above a graduate reads "Class of 2025"; the one         tha, An institution-wide directory prints the title inside the name cell.      Joinin, TestAlumniBlocksOnCurrentRosters, TestTableCellsWithNestedTitles

### Community 41 - "Errors Components"
Cohesion: 0.25
Nodes (11): FastAPI, ConflictError, ErrorCode, Error envelope. Every non-2xx returns {"error": {code, message, details}}.  The, register_exception_handlers(), create_app(), lifespan(), Serve screenshots from local disk.  Authenticated like everything else, and path (+3 more)

### Community 42 - "Scoring Components"
Cohesion: 0.23
Nodes (11): _path_signal(), rank_candidates(), Rank discovered URLs by how likely they are to carry resident/fellow contacts., Positive score for a path, driven by its last segment.      The leaf names the p, Score one candidate. Higher is more likely to hold contact information., Score, filter and order a candidate list. Known paths always come first., score_url(), ScoredUrl (+3 more)

### Community 43 - "Institution Components"
Cohesion: 0.22
Nodes (11): institution_user_prompt(), hostname(), _model_review(), Stage 0: reject K-12 institutions before any work is done., Only reached when hostname and content heuristics were both inconclusive., classify_content(), classify_domain(), InstitutionVerdict (+3 more)

### Community 44 - "Pool Components"
Cohesion: 0.15
Nodes (5): Stop taking new work now; in-flight sites finish their current step., Claim, run, repeat. The agent and its context are reused throughout., Fold a finished site's numbers into the run's live counters., Authoritative aggregate progress every couple of seconds.          Individual ev, Run until the queue drains, a limit trips, or cancellation.

### Community 45 - "Test Discovery Scoring Components"
Cohesion: 0.14
Nodes (13): Ranking has to separate a roster from the pages that merely mention one.  Every, The bands must not overlap: the step budget is spent in rank order., current-and-past-residents" is the commonest roster spelling on .edu sites., `medicine.<univ>.edu` is the whole school, not the medicine department.      Awa, A department's news feed is full of leaves ending in "residents"., A roster page is named in a few words; past that the leaf is a headline., test_and_past_infix_does_not_break_the_current_roster_signal(), test_article_slug_is_not_a_page_name() (+5 more)

### Community 46 - "Auth Components"
Cohesion: 0.23
Nodes (12): BaseModel, login(), LoginRequest, LoginResponse, logout(), Single-credential auth. No users, no roles, no registration., Shaped for the frontend's `AuthSession`, plus the token it must store., Tokens are stateless, so the client simply discards it. Present because     the (+4 more)

### Community 47 - "Deps Components"
Cohesion: 0.23
Nodes (12): Header, include_in_schema, Query, Shared FastAPI dependencies., Any valid token. `?token=` is accepted too, because EventSource cannot     set h, Staff-only areas: launching runs, spend, site stats., require_admin(), require_auth() (+4 more)

### Community 48 - "Security Components"
Cohesion: 0.24
Nodes (12): InvalidTokenError, decode_token(), _hkdf_secret(), issue_token(), datetime, Two passwords, two scopes, bearer tokens.  A client password grants `client` sco, HKDF-Extract/Expand over the configured password (RFC 5869, one block)., Signing key. Derived from both passwords so rotating either invalidates     ever (+4 more)

### Community 49 - "Session Components"
Cohesion: 0.21
Nodes (9): AsyncEngine, get_db(), AsyncSession, health(), Liveness. Unauthenticated on purpose: the frontend polls it to tell 'server stop, get_engine(), get_sessionmaker(), AsyncSession (+1 more)

### Community 50 - "Service Components"
Cohesion: 0.23
Nodes (11): Start an async filtered CSV export. Returns a job id immediately., start_export(), ExportCreate, create_export_job(), _iso(), AsyncSession, Asynchronous filtered CSV export.  Async because a filtered export can span the, Persist the job and kick off generation in the background.      Exports are norm (+3 more)

### Community 51 - "Records Components"
Cohesion: 0.20
Nodes (12): _attach_program(), _create_record(), _make_version(), AsyncSession, The subset of fields that a version snapshot records., Give the record a programme, creating it on first sight., Returns True when a new version was written., Identity keys of a site's live records — the skip check's comparison set. (+4 more)

### Community 52 - "Dev Components"
Cohesion: 0.27
Nodes (7): Find-PostgresBin(), Get-PortHolder(), Have(), Die(), Resolve-Port(), Warn(), Test-PortFree()

### Community 53 - "Limits Components"
Cohesion: 0.33
Nodes (6): check_memory_ceiling(), MemoryCeilingExceeded, Hard stops and the memory ceiling.  The client pays compute directly, so a run m, Fail loudly rather than letting concurrent Chromium instances thrash the box., Raised before a run starts when the requested concurrency will not fit., TestMemoryCeiling

### Community 54 - "Config Components"
Cohesion: 0.25
Nodes (5): BaseSettings, get_settings(), Path, An unset retention var in .env arrives as "" — that means "keep forever"., Settings

### Community 55 - "Test Resilience Components"
Cohesion: 0.22
Nodes (3): One bad link, or one host that says no, must not end a crawl.  Both failures her, `urljoin` parses as well, so guarding only `urlsplit` fixed nothing.          Th, TestMalformedUrlsAreNotFatal

### Community 56 - "Coverage Check Components"
Cohesion: 0.39
Nodes (7): first_last(), fold(), main(), Lowercase, strip accents, credentials and anything after a comma., The two parts a sheet and a web page reliably agree on., report(), scraped()

### Community 57 - "Dev Components"
Cohesion: 0.46
Nodes (6): port_free(), dev.sh script, die(), resolve_port(), say(), warn()

### Community 58 - "Fixture Server Components"
Cohesion: 0.29
Nodes (5): Starlette, build_app(), FixtureSite, A tiny institution website, served locally.  Lets the orchestrator be exercised, Mutable state so tests can change the roster between runs.

### Community 61 - "Env Components"
Cohesion: 0.52
Nodes (6): _database_url(), do_run_migrations(), include_object(), The alembic config wins when it names a URL, otherwise app settings.      Lets a, run_async_migrations(), run_migrations_offline()

### Community 62 - "Test Extraction Components"
Cohesion: 0.47
Nodes (3): page_looks_thin(), Decide whether to escalate to a rendered page + vision.      Cheap first, escala, TestEscalation

### Community 63 - "Project Components"
Cohesion: 0.40
Nodes (5): get_artifact(), FileResponse, Header, include_in_schema, Query

### Community 64 - "Fingerprint Components"
Cohesion: 0.40
Nodes (3): SkipReason, Skip check: decide whether a site changed without scraping it properly.  Two tie, SkipDecision

### Community 65 - "Records Components"
Cohesion: 0.50
Nodes (4): _adopt_name_record(), _name_key(), The name-based identity this person would have had without an address., Reuse the record built before this person's address was known.      Rosters and

## Knowledge Gaps
- **5 isolated node(s):** `agentscrape`, `crawl.sh script`, `Contact Extraction Platform`, `Client Spreadsheet Ground Truth`, `PostgreSQL Health Check`
  These have ≤1 connection - possible missing edges or undocumented components.
- **6 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `PersonCategory` connect `Test Reconcile Components` to `Test Extraction Components`, `Html People Components`, `Test Export Sse Components`, `Sites Components`, `Test Extraction Components`, `Test Api Components`, `Ids Components`, `Test Reconcile Components`, `Test Extraction Components`, `Test Extraction Components`, `Test Schools Api Components`, `Test Extraction Components`, `Test Api Components`, `Vision Components`, `Test Extraction Components`?**
  _High betweenness centrality (0.113) - this node is a cross-community bridge._
- **Why does `Site` connect `Sites Components` to `Test Domain Components`, `Checkpoint Components`, `Test Reconcile Components`, `Test Export Sse Components`, `Test End To End Components`, `Test Api Components`, `Ids Components`, `Test Reconcile Components`, `Queue Components`, `Test Schools Api Components`, `Query Components`, `Sites Components`, `Service Components`, `Schools Components`, `Benchmark Components`, `Limits Components`, `Admin Components`, `Limits Components`, `Test Api Components`?**
  _High betweenness centrality (0.077) - this node is a cross-community bridge._
- **Why does `Record` connect `Test Reconcile Components` to `Checkpoint Components`, `Test Reconcile Components`, `Test Export Sse Components`, `Sites Components`, `Test End To End Components`, `Test Api Components`, `Ids Components`, `Test Schools Api Components`, `Query Components`, `Sites Components`, `Cli Components`, `Service Components`, `Schools Components`, `Artifacts Components`, `Benchmark Components`, `Admin Components`, `Records Components`, `Test Api Components`, `Records Components`?**
  _High betweenness centrality (0.057) - this node is a cross-community bridge._
- **Are the 59 inferred relationships involving `Record` (e.g. with `Score` and `admin_stats()`) actually correct?**
  _`Record` has 59 INFERRED edges - model-reasoned connections that need verification._
- **Are the 55 inferred relationships involving `Site` (e.g. with `Score` and `admin_sites()`) actually correct?**
  _`Site` has 55 INFERRED edges - model-reasoned connections that need verification._
- **Are the 53 inferred relationships involving `SiteRun` (e.g. with `admin_stats()` and `run_sites()`) actually correct?**
  _`SiteRun` has 53 INFERRED edges - model-reasoned connections that need verification._
- **Are the 46 inferred relationships involving `Run` (e.g. with `admin_stats()` and `list_runs()`) actually correct?**
  _`Run` has 46 INFERRED edges - model-reasoned connections that need verification._