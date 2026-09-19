# School sheets

Drop the spreadsheet of schools here — CSV (exported from Google Sheets) or
`.xlsx`, one row per institution. The API loads every sheet in this folder on
startup, and `uv run agentscrape load-schools` loads them on demand. New
schools then appear in the UI's school list.

Headers vary between sheets and are matched loosely. Three columns are required:

| Column | Recognized by a header containing | Value |
|---|---|---|
| Institution name | `institution`, `school` or `name` | text |
| Website | `website`, `url`, `site` or `homepage` | a URL |
| People / student directory | `directory` | a URL (so `Directory Type` is not mistaken for it) |

Optional: a column whose header contains `hub`, `residency`, `fellowship` or
`gme` with a URL becomes the crawl's starting page, and the school is keyed on
that page's host. When the hub is on a different domain from the website
(gme.uchicago.edu vs uchicagomedicine.org), both domains are crawled.

A PDF export cannot be loaded; transcribe it to CSV beside it (see
`institution-links.csv`, from the PDF of the same name). Every other column is ignored. Rows missing a required value are logged and
skipped. Loading never deletes a school or anything collected for it, and
re-running it is safe.

## Corrections to `institution-links.csv`

Transcribed from `institution-links.pdf` (verified by its authors 2026-09-17),
with one fix: Arizona's hub `https://medicine.arizona.edu/education/residency-fellowship`
returns 404; the site's own link is `/education/residencies-fellowships`, used here.
UVA's hub `https://med.virginia.edu/gme/programs/` returns 404; its GME home
`https://med.virginia.edu/gme/` is used. Johns Hopkins' hub is a 404 page in a
browser and every plain HTTP request to hopkinsmedicine.org is refused (403), so
it cannot be crawled until the crawler can fetch through a browser.

A dead entry link no longer stops a crawl: validation and discovery fall back to
the site's home page.
