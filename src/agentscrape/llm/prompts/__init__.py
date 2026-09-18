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

Extract every resident and fellow visible on this page.
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
You are navigating an academic medical institution's website to find every current
resident and fellow. Decide how a careful human researcher should continue from the
current page.

Use the screenshot together with the page text, links, and interactive controls.
Return ONLY this JSON object:
{
  "page_type": "roster" | "program" | "directory" | "event" | "news" |
               "faculty" | "alumni" | "other" | "unknown",
  "control": {"role": "<supplied role>", "name": "<supplied name>"} | null,
  "visit_urls": ["<supplied URL>", ...],
  "reason": "<one concise sentence>"
}

Choose at most one control. Rank visit_urls in the order they should be visited.
Use only controls and URLs supplied in the prompt; never invent either. Explore
uncertain official program paths when they could lead to current trainee rosters.
Do not choose ordinary site chrome, event registration, news, donations, or general
patient-care navigation unless it is the only plausible route to a training program.
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
