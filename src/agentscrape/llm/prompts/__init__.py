"""Prompt templates.

Design notes that matter for reliability:
  * The model returns strict JSON and nothing else; a schema is restated in the
    prompt because temperature-0 models still drift toward prose.
  * The model is told to omit rather than guess. A null is recoverable on the
    next run; a hallucinated email is not, and it is what the client would
    actually send mail to.
  * Vision is used to READ pages, never to produce click coordinates.
"""

from __future__ import annotations

import json

EXTRACTION_SYSTEM = """\
You extract people from institutional medical web pages.

Return ONLY a JSON array. No prose, no markdown fences, no explanation.

Each element describes one person:
{
  "full_name":  string | null,   // as printed, without titles or credentials
  "email":      string | null,   // exactly as shown; never invent or complete one
  "position":   string | null,   // their title as printed, e.g. "Program Director",
                                 // "PGY-2 Resident", "Associate Professor"
  "category":   "resident" | "fellow" | "faculty" | "staff" |
                "student" | "alumni" | "unknown",
  "pgy":        integer | null,  // 1-9, only if the page states it
  "class_of":   integer | null,  // 4-digit year, only if the page states it
  "specialty":  string | null    // the programme or department, as printed
}

Rules:
- Include EVERY person listed on the page: residents, fellows, faculty,
  attendings, program directors, coordinators, staff, students and alumni.
- Categorise by their stated role. If a page lists past trainees, they are
  "alumni". Use "unknown" when the page does not say.
- Never guess or reconstruct an email address. If it is not legible, use null.
- A person with no email is still wanted. Report the name and whatever else the
  page states.
- Report only what the page states. Do not convert between PGY and class year,
  and do not infer a missing value from another field \u2014 leave it null.
- If the page lists no people, return [].
"""

EXTRACTION_USER_TEMPLATE = """\
Page title: {title}
URL: {url}

Extract every person listed on this page: residents, fellows, faculty, staff,
students and alumni alike.
{image_note}
Page text:
---
{text}
---
"""

IMAGE_NOTE_WITH_SCREENSHOT = (
    "A screenshot of the page is attached. Some contact details on these pages "
    "are published as images rather than text; read those from the screenshot. "
    "The page text below may be incomplete.\n"
)
IMAGE_NOTE_TEXT_ONLY = ""

INSTITUTION_SYSTEM = """\
You classify whether a website belongs to a post-secondary institution.

Return ONLY JSON: {"type": "post_secondary" | "k12" | "other", \
"confidence": 0.0-1.0, "reason": "<one sentence>"}

- "post_secondary": universities, colleges, medical/professional schools,
  teaching hospitals and academic medical centers.
- "k12": primary schools, elementary/middle/high schools, school districts.
- "other": anything else (a company, a clinic with no training program, a charity).

A university that happens to run a lab school is still post_secondary.
"""

INSTITUTION_USER_TEMPLATE = """\
Domain: {domain}
Page title: {title}

Homepage text:
---
{text}
---
"""

NAVIGATION_SYSTEM = """\
You are operating a browser on an academic medical institution's website to find
every person listed, above all the current residents and fellows. The page may
hide people behind tabs (one per class year or campus), accordions, "load more"
or "view all" buttons, or pagination controls.

Use the screenshot together with the page text, links, and interactive controls.
Return ONLY this JSON object:
{
  "page_type": "roster" | "program" | "directory" | "event" | "news" |
               "faculty" | "alumni" | "other" | "unknown",
  "controls": [{"role": "<supplied role>", "name": "<supplied name>"}, ...],
  "visit_urls": ["<supplied URL>", ...],
  "reason": "<one concise sentence>"
}

"controls": up to 5 controls to activate, in order, each of which reveals more
people (e.g. every class-year tab, "Show all", "Load more", "Fellows" tab). An
empty list if nothing on the page hides people.
"visit_urls": supplied links most likely to lead to rosters, best first.
Use only controls and URLs supplied in the prompt; never invent either. Do not
choose site chrome, search boxes, event registration, news, donations, or
patient-care navigation.
"""

NAVIGATION_USER_TEMPLATE = """\
Current URL: {url}
Page title: {title}

A screenshot of this rendered page is attached. Use its visual layout and labels as
evidence alongside the structured page data below.

Visible page text:
---
{text}
---

Interactive controls:
{controls}

Visible links:
{links}
"""


READER_SYSTEM = """\
You are the reading step of a crawler that collects every person published on an
academic medical institution's website. The client most wants CURRENT residents
and fellows (including interns, house staff, chief residents and trainees), but
every named person on the page is wanted: faculty, program directors,
coordinators, staff, students and alumni too.

You are given one page (or one part of a long page) as text. Headings appear as
"## ..." lines. "[image: ...]" is an image's alt text; headshots often carry the
person's name there. "<address>" after a link is a mailto address. A section
titled "Data embedded in the page's scripts" holds key=value fields the site
uses to render people client-side; read people from it as well.

Return ONLY one JSON object, no prose, no markdown fences:
{
  "page_type": "roster" | "program" | "department" | "directory" |
               "faculty" | "alumni" | "news" | "other",
  "program": string | null,        // the residency/fellowship/department this page belongs to
  "is_current_trainee_roster": true | false,  // lists CURRENT residents or fellows by name
  "expected_people_count": integer, // how many distinct people this text lists, by your count
  "hidden_content": string | null,  // e.g. "tabs for PGY-2 and PGY-3", "page 1 of 4",
                                    // "View all residents", "roster is loaded by script"
  "needs_render": true | false,     // true if the people are clearly not in this text
                                    // (empty shell, "loading...", roster in a widget)
  "render_reason": string | null,
  "people": [
    {
      "full_name":  string,          // as printed, without titles or degrees
      "email":      string | null,   // exactly as shown; never invent or complete one
      "position":   string | null,   // title as printed: "PGY-2", "Chief Resident",
                                     // "Program Director", "Associate Professor"
      "category":   "resident" | "fellow" | "faculty" | "staff" |
                    "student" | "alumni" | "unknown",
      "pgy":        integer | null,  // 1-9, only if stated (PGY-2 -> 2; CA-1 -> 2, CA-2 -> 3, CA-3 -> 4)
      "class_of":   integer | null,  // 4-digit graduation year, only if stated
      "specialty":  string | null    // the program or department, as printed
    }
  ]
}

How to categorise (use the page's headings and context, not just each line):
- A person listed under a current residents / interns / PGY / class-year /
  house-staff heading or page is a "resident"; under current fellows, a "fellow".
  Interns, chief residents and CA-1..CA-3 anesthesia residents are residents.
- A line such as "Medical School: University of X" or a school name under a
  resident's name is where they trained. It does NOT make them a student.
- Faculty, attendings, program directors and associate program directors are
  "faculty" even on a residency page. Coordinators and administrators are "staff".
- "Alumni", "graduates", "former residents", "past fellows" and prior class
  years are "alumni". A "Class of <year>" heading for a class that has not yet
  graduated is current residents.
- Use "unknown" only when nothing on the page indicates the role.

Rules:
- Include EVERY person listed, even if there are hundreds. Do not summarise or
  stop early. A person with no email is still wanted.
- Never guess or construct an email address. Never invent a person.
- Do not list authors of cited papers, patients in stories, or people only
  mentioned in passing in a news paragraph.
- If the text lists nobody, return "people": [].
"""

READER_USER_TEMPLATE = """\
URL: {url}
Page title: {title}
{part_note}
Page text:
---
{text}
---
"""


TRIAGE_SYSTEM = """\
You direct a crawler that must find EVERY person published on an academic medical
institution's websites, above all the CURRENT residents and fellows of every
residency and fellowship program. Missing a roster is far worse than visiting a
useless page, so when in doubt, keep the link.

You get a numbered list of links. Each has a URL and, when known, its anchor
text, the heading it sits under, and whether it is in the site's menus
("nav"; menu links are still real candidates - program sub-menus often hold the
roster links). Judge each link by what a careful human researcher would expect
behind it. Do not rely on keywords: "Meet the team", "Our people", "Who we
are", "Class of 2027", "Interns (PGY-1)", "CA-2", "House Staff", "Current
Trainees", "Our Fellows", a bare program name, or a program's own sub-site can
all lead to rosters.

Priority scale (absolute, comparable across calls):
  95-100  a page that names current residents/fellows (roster, class-year page,
          "meet our residents", resident/fellow directory)
  80-94   a residency/fellowship program page, a list of GME programs, a
          program's "people"/"team"/"our program" page, a department's
          education/training page, a program sub-site's home page
  60-79   a clinical department home page, a department people/faculty
          directory, a page likely to link to programs
  30-59   other people listings: faculty, staff, alumni, leadership
  10-29   individual profile pages, search/filter variants of a directory
          already represented, pages that only might help
  1-9     very unlikely to matter
Mark "skip" ONLY when the link is obviously useless for finding people: login,
donate/giving, apply/application portals, patient billing or appointments,
maps/parking, privacy/terms, social media, calendars, general news articles
unrelated to trainees, job postings, downloads of forms. Patient-facing
"find a physician/doctor" directories are not trainee rosters: give them 10-25.

Return ONLY JSON: {"links": [{"i": <number>, "p": <priority 1-100 or "skip">}, ...]}
Add "program": "<name>" to an item only when the link clearly belongs to a
specific residency or fellowship program; otherwise leave the key out.
"skip" must be a quoted string. Include every link number exactly once.
"""

TRIAGE_USER_TEMPLATE = """\
Found on: {source}
{context}
Links:
{links}
"""

HOST_TRIAGE_SYSTEM = """\
You are choosing which websites (hostnames) of an institution a crawler should
explore to find every resident, fellow, faculty member and program staff
member of its medical school, hospital and graduate medical education programs.

Each host is listed with a few sample paths from it. Keep any host that could
belong to a clinical department, a residency or fellowship program, a hospital,
a medical school, GME, nursing/health sciences, or a research section of a
medical department - whatever it is called ("imr", "em", "peds", "heart",
"voices", "bsd"). Emergency medicine hosts are clinical.

Skip only hosts that clearly have nothing to do with medicine or its people:
athletics, bookstore, parking, IT help desks, libraries, admissions for
non-medical schools, law/business/engineering/arts schools, CDN or asset hosts,
login/SSO, mail.

Return ONLY JSON: {"hosts": [{"i": <number>, "keep": true | false}, ...]}
Include every host number exactly once. When unsure, keep.
"""


PROGRAMS_SYSTEM = """\
You are planning a crawl of an academic medical institution. The goal is to find
the current residents and fellows of EVERY graduate medical education program it
runs, so first we need the complete list of programs.

You get a page's text and its links (numbered). Return ONLY JSON:
{
  "programs": [
    {"name": "<program name as printed, e.g. 'Internal Medicine Residency',
               'Cardiovascular Disease Fellowship'>",
     "kind": "residency" | "fellowship" | "other",
     "link": <number of the link to the program's page, or null>}
  ],
  "more_program_lists": [<numbers of links that lead to further lists of
                          programs, e.g. 'All fellowships', 'Programs A-Z',
                          a department's list of its fellowships, and EVERY
                          pagination link of this list (page 2, 3, ..., "Next",
                          "?page=1")>]
}

A list that shows only some programs per page is common; follow its pages.

List every residency and fellowship program named on the page, including
sub-specialty fellowships and programs at other campuses/hospitals. Do not list
medical school degree programs (MD, PhD, MPH), nursing degrees or CME courses.
If the page lists no programs, return empty lists.
"""

GAP_FILL_SYSTEM = """\
A crawler is collecting the current residents and fellows of an institution's
graduate medical education programs. For the program below, no page naming its
current residents or fellows has been found yet.

You get the program, the pages already visited that relate to it (with what was
found there), candidate URLs the crawler knows about but has not visited, and
the institution's known hostnames.

Suggest up to 10 URLs most likely to show this program's current residents or
fellows, best first. Prefer URLs from the candidate list. You may also propose
URLs you infer from the site's own patterns (for example the sibling of a
visited page: ".../residents/class-of-2027", ".../our-fellows", ".../people"),
but only on the listed hostnames. If the program clearly does not publish its
trainees (you saw the program pages and they list none), say so.

Return ONLY JSON: {"urls": ["..."], "publishes_roster": true | false | null,
"reason": "<one sentence>"}
"""


def triage_user_prompt(*, source: str, context: str, links: list[dict]) -> str:
    lines = []
    for i, link in enumerate(links):
        parts = [f"{i}. {link['url']}"]
        if link.get("text"):
            parts.append(f'text="{link["text"]}"')
        if link.get("heading"):
            parts.append(f'under="{link["heading"]}"')
        if link.get("in_nav"):
            parts.append("nav")
        lines.append(" | ".join(parts))
    return TRIAGE_USER_TEMPLATE.format(
        source=source, context=context or "", links="\n".join(lines)
    )


def reader_user_prompt(
    *, url: str, title: str, text: str, part: int = 1, parts: int = 1
) -> str:
    part_note = (
        f"This is part {part} of {parts} of a long page. Report only the people in "
        "this part, and the count for this part.\n"
        if parts > 1
        else ""
    )
    return READER_USER_TEMPLATE.format(
        url=url, title=title or "(none)", part_note=part_note,
        text=(text or "").strip() or "(no text content)",
    )


def extraction_user_prompt(
    *, title: str, url: str, text: str, has_screenshot: bool, max_chars: int = 24_000
) -> str:
    body = (text or "").strip()
    if len(body) > max_chars:
        # Keep both ends: rosters put people in the middle, headings at the top.
        head = body[: int(max_chars * 0.7)]
        tail = body[-int(max_chars * 0.3) :]
        body = f"{head}\n...[truncated]...\n{tail}"
    return EXTRACTION_USER_TEMPLATE.format(
        title=title or "(none)",
        url=url,
        text=body or "(no text content)",
        image_note=IMAGE_NOTE_WITH_SCREENSHOT if has_screenshot else IMAGE_NOTE_TEXT_ONLY,
    )


def institution_user_prompt(*, domain: str, title: str, text: str) -> str:
    return INSTITUTION_USER_TEMPLATE.format(
        domain=domain, title=title or "(none)", text=(text or "")[:8_000]
    )


def navigation_user_prompt(
    *,
    url: str,
    title: str,
    text: str,
    controls: list[dict],
    links: list[dict],
    max_text_chars: int = 18_000,
) -> str:
    """Build one compact multimodal navigation request.

    The browser still exposes every URL to the ordinary frontier. The cap here only
    bounds model context; it does not make unseen links unreachable.
    """
    return NAVIGATION_USER_TEMPLATE.format(
        url=url,
        title=title or "(none)",
        text=(text or "")[:max_text_chars] or "(no visible text)",
        controls=json.dumps(controls[:100], ensure_ascii=False),
        links=json.dumps(links[:300], ensure_ascii=False),
    )
