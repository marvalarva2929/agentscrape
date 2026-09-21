# The school catalog

`schools-23.csv` is the client's fixed list of 23 institutions to crawl. The API
imports it on startup and makes exactly these schools active; every other school
already in the database stays there, inactive, with its people and history.

| Column | Meaning |
|---|---|
| `name` | The name shown in the UI (the client's own). |
| `entry_url` | Where a crawl starts: the graduate-medical-education hub. The school is keyed on this URL's host. |
| `homepage` | The institution's own site, for reference and fallback. |
| `directory_url` | Its people directory; blank if it has none ("DIRECTORY NOT AVAILABLE"). |
| `program_index_url` | A second page that lists programs (used by `scripts/preflight_schools.py`). |
| `affiliated_domains` | Other sites its rosters live on, separated by `;`. A crawl follows links within the entry's own site and these. |
| `tier` | The client's tier. |

Built from the client's *Institution Links* sheet (homepage, directory, tier) and
the *Shaun_23_Medical_Schools_Resident_Links* workbook (GME hubs).
`Shaun_23_Medical_Schools_Resident_Links.xlsx` is the workbook as delivered; it is
not read by the app. To change a school, edit the CSV and restart, or run
`agentscrape import-school-catalog` (`--dry-run` to check first).
