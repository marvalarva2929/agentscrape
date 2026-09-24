# Validation and storage audit — September 23, 2026

Changes span this backend and the sibling `agentscrape-frontend` checkout.

- Data tables and Excel exports show separate PGY and class-year fields.
- Past crawls searches the server by label, school, domain, or run ID, across
  paginated history. Search input is treated literally, including `%` and `_`.
- Verification writes a single confirmed category into the record and creates
  a version in the same transaction. Repeating the same finding creates no
  extra version. Multiple confirmed roles remain visible in the data table,
  person detail, filters, counts, and frontend exports.
- Verification checks role labels; it does not rewrite people's names.

## Live directory checks

Used names already in the local database, with the model disabled. These were
read-only probes through the application's directory learner and lookup code;
no directory findings were saved into live records.

| School | Result |
| --- | --- |
| UChicago | Learned the public first/last-name GET search. Found an email for Aamir Aziz and Abson Madola; Adam Griffith was not listed. |
| Arizona | Redirected to `/login`; correctly reported that sign-in is required. |
| UNM | HTTP/browser loading failed; the browser timed out after 30 seconds. Public lookup was not verified. |

Directory integration tests separately verify that matched results fill blanks,
retain existing values, persist provenance, and work in directory-only and
combined crawl/directory runs. Live success at UChicago is not evidence that
all institutions expose public directories.

## Local storage

| Measurement | Before | After |
| --- | ---: | ---: |
| Main database | 204 MB | 132 MB |
| `site_runs` including its indexes and TOAST data | 75 MB | 3256 kB |

Reclaimed unused database space with `VACUUM (FULL, ANALYZE) site_runs` while no
runs were pending/running, with a two-second lock timeout. Preserved all rows,
record history and resumable checkpoints. This operation takes an exclusive
lock; it is maintenance, not part of automatic retention.

The retention sweep removed 106 generated orphan CSVs older than seven days.
It now resolves relative paths under `ARTIFACT_DIR`, retains jobs whose file
deletion failed so they can be retried, and clears old generated orphan files.
It runs at startup and hourly. Test artifacts now use temporary directories
instead of accumulating in the application's export folder.

The Mac had about 2.8 GB free at the start. This application is not the main
consumer: backend checkout about 348 MB, frontend about 117 MB. Larger measured
consumers include Hugging Face cache (8.2 GB), other projects (including 9.7 GB
and 6.3 GB checkouts), and Library caches (12 GB total). Package download caches
include pip (958 MB), uv (700 MB), and Homebrew (1 GB). These shared caches and
unrelated projects were not deleted. Browser binaries occupy 1.6 GB and are
needed by browser-dependent crawling. These measurements cover this Mac only;
no deployed server was identified or inspected.

## How crawls store data (applies to production)

Measured on the local database, which has the same schema and write path as
production. These are code changes, so they take effect wherever the migration
runs.

- **Checkpoints.** Each extract batch used to rewrite the whole growing resume
  state into `site_runs.checkpoint_state`, so every batch left behind a dead copy
  of the list (75 MB for 14 rows here). Checkpoints are now compressed 128-item
  chunks in `site_run_checkpoint_parts`. A batch writes only the chunks that
  changed. Finished crawls drop their frontier.
- **Scratch data.** When a site finishes, its checkpoint chunks and its
  `site_run_visits` rows are deleted after missing-person detection has used them.
- **Repeat sightings.** A person listed on several pages in one run no longer
  rewrites their row each time.
- **Indexes (migration `18b9c0d1e2f3`).** Dropped three indexes that cost a write
  on every update and served no query:
  - `ix_records_last_seen`: the people listing sorts by a `CASE` first, so it
    can't use this index (checked with EXPLAIN). Because every re-crawl sets
    `last_seen_at`, this index also stopped unchanged records from being
    updated in place (HOT), so all indexes were rewritten.
  - `ix_records_fts`: its expression never matched the search query's, so it
    was never used.
  - `ix_versions_record`: an exact duplicate of the `uq_version_record_no`
    constraint.

  In a simulation of five re-crawls on a copy of the 16.6k local records, with
  vacuum between them, `records` settled at 28 MB instead of 33 MB. The single
  largest win is the duplicate version index: about 8 MB here, roughly 16% of
  `record_versions`. Raising the fillfactor was also tested and gave no gain.

Remaining opportunity (not done): every `record_versions` row stores a full
snapshot of the person, not just the changed fields. Storing only the changed
fields would roughly halve that table but changes how history is read.

After deploying, run `alembic upgrade head`. Existing bloat is only returned to
the OS by a one-time `VACUUM FULL records, record_versions, site_runs` during
a quiet window; it takes an exclusive lock.

## Directory search errors (live check)

A directory-only run for Arizona through the API now ends `failed` with
`DIRECTORY_LOGIN_REQUIRED`: "The school directory requires sign-in. Automated
directory search cannot access it." Before this change it completed silently.
The run monitor shows this as an error banner labeled "directory search failed".
Timeouts, rate limits, blocking, missing links, and missing browsers each have
their own message. A search that runs but matches nobody is reported as a
warning, not a failure.

## Checks

- Backend: 518 tests passed, including verification persistence/idempotence,
  crawl search, directory integration, export retention and failure retry.
- Frontend: 34 tests passed; production build and ESLint passed.
- Existing dependency deprecation warnings and Vite's large-bundle warning remain.
- Changes are local; neither repository has been deployed.
