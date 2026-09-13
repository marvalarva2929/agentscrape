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

EXTRACTION_SYSTEM = """\
You extract contact information for medical residents and fellows from \
institutional web pages.

Return ONLY a JSON array. No prose, no markdown fences, no explanation.

Each element describes one person:
{
  "full_name":   string | null,   // as printed, without titles or credentials
  "email":       string | null,   // exactly as shown; never invent or complete one
  "role":        "resident" | "fellow" | "unknown",
  "pgy":         integer | null,  // 1-9, from "PGY-2", "R2", "Post-Graduate Year 2"
  "class_of":    integer | null,  // 4-digit graduation year, from "Class of 2027"
  "specialty":   string | null    // the program this person is in, as printed
}

Rules:
- Include ONLY residents, fellows, and people whose role you cannot determine.
- EXCLUDE attending physicians, faculty, professors, program directors,
  coordinators, administrators, nurses, and medical students.
- Never guess or reconstruct an email address. If it is not legible, use null.
- Do not convert between PGY and class year; report only what the page states.
- If the page lists no residents or fellows, return [].
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
