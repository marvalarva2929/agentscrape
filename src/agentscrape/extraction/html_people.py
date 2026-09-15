"""Extract residents and fellows from static HTML — the cheap default path.

Residency rosters come in three shapes and this handles all of them:
  * tables with a header row (Name | PGY | Email)
  * repeated card blocks (photo, heading, mailto link)
  * plain prose lists

Only residents, fellows and unclassifiable people are kept. Anyone confidently
identified as faculty/attending/staff/admin is dropped at this layer, per scope.
"""

from __future__ import annotations

import hashlib
import logging
import re

from selectolax.parser import HTMLParser, Node

from ..db.enums import PersonCategory
from ..domain.matching import EMAIL_RE, normalize_email, normalize_name
from ..domain.pgy import parse_class_of, parse_pgy
from ..domain.specialty import normalize_specialty
from ..validation.email import deobfuscate, extract_emails, is_plausible
from .person import ExtractedPerson

log = logging.getLogger("agentscrape.extract.html")

# Name characters. Accented Latin letters must be included: without them the
# regex breaks at the accent and "André Thomas Jr." is read as "Thomas Jr".
_UPPER = (
    "A-Z\u00c0-\u00d6\u00d8-\u00de"
    "\u0100\u0102\u0104\u0106\u0108\u010a\u010c\u010e\u0110\u0112\u0116\u0118"
    "\u011a\u011e\u0122\u012a\u012e\u0130\u0141\u0143\u0145\u0147\u014c\u0150"
    "\u0152\u0154\u0158\u015a\u015e\u0160\u0162\u0164\u016a\u016e\u0170\u0172"
    "\u0174\u0176\u0178\u0179\u017b\u017d"
)
_LOWER = (
    "a-z\u00df-\u00f6\u00f8-\u00ff"
    "\u0101\u0103\u0105\u0107\u0109\u010b\u010d\u010f\u0111\u0113\u0117\u0119"
    "\u011b\u011f\u0123\u012b\u012f\u0131\u0142\u0144\u0146\u0148\u014d\u0151"
    "\u0153\u0155\u0159\u015b\u015f\u0161\u0163\u0165\u016b\u016f\u0171\u0173"
    "\u0175\u0177\u017a\u017c\u017e"
)
# One name word, in two parts so apostrophes and internal capitals both work:
# the core handles Tom / Doe / McDonald, the tail handles O’Brien and
# McDonald-Smith. The apostrophe must NOT be in the core class or it gets
# consumed there and the capital after it is lost ("Tom O'").
_NAME_CORE = rf"[{_UPPER}][{_LOWER}]*(?:[{_UPPER}][{_LOWER}]+)*"
_NAME_WORD = rf"{_NAME_CORE}(?:[\u2019'\-][{_UPPER}{_LOWER}][{_LOWER}]*(?:[{_UPPER}][{_LOWER}]+)*)*"
# Generational suffixes are part of the name, not a job title.
_SUFFIX = r"(?:\s+(?:Jr|Sr|II|III|IV)\.?)?"
# 2-4 name words, optionally with a middle initial or a nobiliary particle.
_NAME_RE = re.compile(
    rf"\b({_NAME_WORD}"
    rf"(?:\s+(?:van|von|de|del|della|di|da|dos|la|le|el|bin|al))?"
    rf"(?:\s+[{_UPPER}]\.?)?"
    rf"(?:\s+{_NAME_WORD}){{1,2}}"
    rf"{_SUFFIX})\b"
)
_CREDENTIALS = re.compile(
    r"\b(m\.?d\.?|d\.?o\.?|ph\.?d\.?|m\.?b\.?b\.?s\.?|m\.?p\.?h\.?|do|md)\b", re.IGNORECASE
)
# One post-nominal token. Only ever matched AFTER a comma: bare patterns for
# "MA"/"BS"/"MS" would otherwise eat real surnames like "Ma" or "Ba".
_CREDENTIAL_TOKEN = re.compile(
    r"(?:M\.?D|D\.?O|Ph\.?D|M\.?B\.?B\.?S|M\.?P\.?H|M\.?Sc?|M\.?A|M\.?B\.?A|"
    r"M\.?H\.?A|M\.?Ed|B\.?S|B\.?A|R\.?N|D\.?D\.?S|D\.?M\.?D|P\.?A-?C|"
    r"Pharm\.?D|D\.?V\.?M|D\.?P\.?T|O\.?D|D\.?Sc|J\.?D|M\.?Div|F\.?R\.?C\.?[A-Z]|"
    r"F\.?A\.?C\.?[SPRG]|Sc\.?D|Dr\.?P\.?H|C\.?N\.?P|N\.?P)\.?",
    re.IGNORECASE,
)


def _strip_credentials(text: str) -> str:
    """Remove post-nominal letters from a display name.

    Handles runs of them ("Ryan Zitter, MD MSc"). Anything after the first comma
    is dropped only when every token there is a credential, so "Doe, Jane" and
    "Kim-Wang, Sophia" survive intact.
    """
    head, separator, tail = text.partition(",")
    if separator and tail.strip():
        tokens = [t for t in re.split(r"[,\s]+", tail) if t]
        if tokens and all(_CREDENTIAL_TOKEN.fullmatch(t) for t in tokens):
            return head.strip()
    return _CREDENTIALS.sub("", text).strip(" ,.")
# Stripped from display names. Identity already folds these via normalize_name,
# but the stored name should read "Jane Doe", not "Dr. Jane Doe".
_TITLE_PREFIX = re.compile(r"^(dr|doctor|prof|professor|mr|mrs|ms|mx)\.?\s+", re.IGNORECASE)
# --- classification -------------------------------------------------------
#
# Everyone published on the site is collected. These patterns decide what to
# *label* a person, never whether to keep them. Order matters: the first group
# to match wins, so "Fellowship Program Director" is faculty rather than fellow,
# while "Chief Resident" stays a resident.

_FACULTY_HINT = re.compile(
    r"\b(program director|associate director|assistant director|fellowship director|"
    r"residency director|clerkship director|medical director|director of|"
    r"professor|attending|physician[- ]scientist|chair(man|person|)\b|vice chair|"
    r"chief of|division chief|section chief|dean|principal investigator|"
    r"staff physician|consultant physician|program leadership|leadership team|course director|site director)\b",
    re.IGNORECASE,
)
_STAFF_HINT = re.compile(
    r"\b(coordinator|administrator|administrative|program manager|manager|secretary|"
    r"receptionist|nurse practitioner|registered nurse|\bnurse\b|physician assistant|"
    r"\bpa-?c\b|technician|technologist|analyst|specialist ii?i?\b|assistant to|"
    r"office of|support staff|scheduler|registrar)\b",
    re.IGNORECASE,
)
_STUDENT_HINT = re.compile(
    r"\b(medical student|ms[1-4]\b|m[1-4] student|undergraduate|graduate student|"
    r"phd (student|candidate)|doctoral (student|candidate)|postdoc(toral)?|"
    r"research (assistant|associate|scientist)|summer (student|intern))\b",
    re.IGNORECASE,
)
_ALUMNI_HINT = re.compile(
    r"\b(alumn(us|a|i|ae)|graduated|former (resident|fellow|trainee)|"
    r"past (resident|fellow|trainee)|class of \d{4} graduate)\b",
    re.IGNORECASE,
)
_FELLOW_HINT = re.compile(r"\bfellows?\b", re.IGNORECASE)
_RESIDENT_HINT = re.compile(
    r"\bresidents?\b|\bhouse\s?staff\b|\bpgy\b|\bintern\b|\bchief resident\b",
    re.IGNORECASE,
)

# Titles worth lifting verbatim into `position` when they appear. Longest first
# so "Associate Program Director" beats "Program Director".
_TITLE_PATTERNS = sorted(
    [
        "Associate Program Director", "Assistant Program Director",
        "Fellowship Program Director", "Residency Program Director",
        "Program Director", "Program Coordinator", "Program Manager",
        "Clerkship Director", "Medical Director", "Division Chief",
        "Section Chief", "Vice Chair", "Chair", "Dean",
        "Clinical Professor", "Associate Professor", "Assistant Professor",
        "Adjunct Professor", "Professor",
        "Attending Physician", "Attending", "Staff Physician",
        "Chief Resident", "Senior Resident", "Resident Physician", "Resident",
        "Clinical Fellow", "Research Fellow", "Fellow",
        "Nurse Practitioner", "Physician Assistant", "Registered Nurse",
        "Medical Student", "Research Assistant", "Research Associate",
        "Postdoctoral Fellow", "Coordinator", "Administrator", "Manager",
        "Intern", "Alumnus", "Alumna",
    ],
    key=len,
    reverse=True,
)
_TITLE_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in _TITLE_PATTERNS) + r")\b", re.IGNORECASE
)
# Noise to strip before falling back to "whatever text sits next to the name".
_POSITION_NOISE = re.compile(
    r"(\bclass of\s*'?\d{2,4}\b|\bpgy[-\s]?\d\b|[\w.+-]+@[\w.-]+|"
    r"\b\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b|https?://\S+)",
    re.IGNORECASE,
)
_MAX_POSITION_CHARS = 90
# Other labelled fields that sit beside a name on profile cards. Without this
# the free-text fallback returns things like "MD Medical School: Chicago Med.
# College" as though it were a job title.
_NOT_A_POSITION = re.compile(
    r"\b(medical school|med school|undergrad(uate)?|hometown|home town|interests|"
    r"hobbies|fun fact|college|university|degree|born|raised|from|research|"
    r"publications|education|b\.?s\.?|b\.?a\.?)\b",
    re.IGNORECASE,
)
# Part of a name, not an acronym.
_NAME_SUFFIX_TOKENS = frozenset({"II", "III", "IV", "JR", "SR"})

_NON_PERSON_TEXT = re.compile(
    r"^(home|about|contact|search|menu|skip to|read more|learn more|apply now|"
    r"privacy|terms|copyright|all rights|back to top|view all|next|previous)\b",
    re.IGNORECASE,
)
_WS = re.compile(r"\s+")

# Pages listing people who have ALREADY finished the program. Out of scope: the
# client wants current trainees. Ranking pushes these down, but a departmental
# index can still link one, so extraction refuses them outright.
_OUT_OF_SCOPE_PAGE = re.compile(
    r"\b(alumni|alumnae|graduates?|former\s+(residents?|fellows?|trainees?)|"
    r"past\s+(residents?|fellows?|trainees?)|job\s+placement|where\s+are\s+they\s+now|"
    r"residents?\s+who\s+have\s+graduated|graduating\s+class\s+of\s+\d{4}\s+alumni)\b",
    re.IGNORECASE,
)


def _clean(text: str | None) -> str:
    return _WS.sub(" ", (text or "").replace("\xa0", " ")).strip()


# Words that never appear inside a personal name. Their presence means the text
# is a name plus a job title, so it must go through the regex rather than be
# accepted whole ("Maria Gonzalez MD Vascular Surgery Fellow").
_NOT_IN_NAME = frozenset({
    "fellow", "fellows", "resident", "residents", "intern", "interns", "chief",
    "attending", "faculty", "professor", "director", "coordinator", "program",
    "surgery", "medicine", "pediatrics", "psychiatry", "radiology", "neurology",
    "pathology", "anesthesiology", "dermatology", "oncology", "cardiology",
    "department", "division", "school", "hospital", "clinic", "center", "centre",
    "class", "year", "pgy", "email", "phone", "office", "profile", "biography",
    "physician", "doctor", "nurse", "staff", "team", "member", "student",
    # Generic UI/prose words. Needed because the span search below examines
    # sub-spans, which bypass the whole-string nav-phrase check.
    "read", "more", "about", "us", "view", "all", "here", "click", "learn",
    "our", "the", "and", "home", "next", "previous", "page", "back", "info",
    "details", "contact", "search", "menu", "apply", "now", "see", "meet",
    "welcome", "overview", "current", "former", "past", "new", "index",
    # Organisational units. Headings like "BSD Academic Affairs" are title-cased
    # and word-shaped, so without these they parse as a person.
    "affairs", "services", "service", "resources", "council", "committee",
    "board", "association", "society", "foundation", "institute", "laboratory",
    "lab", "group", "network", "initiative", "consortium", "academy",
    "academic", "administration", "operations", "unit", "branch",
    "section", "campus", "library", "bureau", "agency", "authority",
    "graduate", "medical", "education", "training", "programs",
    # Section headings on programme pages. The heading fallback reads every
    # <h3>, so these arrive looking exactly like a two-word name.
    "experience", "activity", "activities", "scholarly", "curriculum",
    "rotations", "rotation", "conferences", "conference", "schedule",
    "didactics", "wellness", "benefits", "salary", "housing", "mentorship",
    "attendings", "trainees", "alumni", "leadership",
    "highlights", "testimonials", "life", "community", "outreach", "global",
    "simulation", "quality", "safety", "innovation", "mission", "vision",
    # Site furniture that reads as a title-cased phrase in a card block.
    "jobs", "job", "careers", "career", "giving", "donate", "news",
    "events", "calendar", "links", "policies", "policy", "forms",
    "application", "applications", "requirements", "diversity", "inclusion",
    "research", "publications", "gallery", "photos", "videos", "sitemap",
})


def _looks_like_person_name(text: str) -> bool:
    """Filter out headings and nav labels that happen to be title-cased."""
    text = _clean(text)
    if not text or len(text) > 80 or _NON_PERSON_TEXT.match(text):
        return False
    stripped = _strip_credentials(text)
    words = [w for w in re.split(r"[\s,]+", stripped) if w]
    if not 2 <= len(words) <= 5:
        return False
    if any(ch.isdigit() for ch in stripped):
        return False
    if any(w.lower().strip(".,") in _NOT_IN_NAME for w in words):
        return False
    # "A Sanchez" / "B Yu" come from citation lists and call rotas, not
    # directories. A real listing has at least two substantial tokens; a middle
    # initial is fine because the first and last names still count.
    substantial = [w for w in words if len(w.strip(".,")) > 1]
    if len(substantial) < 2:
        return False
    # A comma with more than three words is a list of people, not one person
    # ("A Sanchez, K Little"). "Doe, Jane" and "Smith, John Paul" still pass.
    if "," in stripped and len(words) > 3:
        return False
    # Anything that resolves to a specialty is a department or section heading,
    # not a person ("Cardiac Anesthesia", "Vascular Surgery"). More durable than
    # listing every clinical word, and no real surname collides with one.
    if normalize_specialty(stripped).canonical:
        return False
    # An all-caps token is an acronym, not part of a name ("BSD", "GME", "UCSF").
    # Credentials have already been stripped by this point. Generational
    # suffixes are genuinely part of the name and are exempt.
    if any(
        w.isupper()
        and 2 <= len(w.strip(".,")) <= 5
        and w.strip(".,") not in _NAME_SUFFIX_TOKENS
        for w in words
    ):
        return False
    capitalized = sum(1 for w in words if w[:1].isupper())
    return capitalized >= 2


def _extract_name(text: str) -> str | None:
    # Addresses sit right next to names on directory pages and would otherwise
    # be absorbed into the name itself.
    text = EMAIL_RE.sub(" ", _clean(text))
    text = _TITLE_PREFIX.sub("", _clean(text))
    if _looks_like_person_name(text):
        return _strip_credentials(text).strip(" ,.-–")
    for match in _NAME_RE.finditer(text):
        candidate = _strip_credentials(match.group(1)).strip(" ,.-–")
        # A greedy match absorbs job words on either side ("Tom O’Brien Fellow",
        # "Resident Jane Smith"), which the stopword guard then rejects wholesale.
        # Take the longest contiguous span that is a plausible name rather than
        # discarding the person entirely.
        words = candidate.split()
        for length in range(len(words), 1, -1):
            for offset in range(len(words) - length + 1):
                trial = " ".join(words[offset : offset + length])
                if _looks_like_person_name(trial):
                    return trial
    return None


def _extract_position(text: str, name: str | None) -> str | None:
    """The person's title as printed, e.g. "Program Director", "PGY-2 Resident".

    Prefers a recognised title phrase; otherwise falls back to whatever short
    descriptive text sits beside the name, with contact details and class years
    stripped out.
    """
    blob = _clean(text)
    if not blob:
        return None

    match = _TITLE_RE.search(blob)
    if match:
        return match.group(1).strip()

    if name:
        blob = blob.replace(name, " ")
    blob = _POSITION_NOISE.sub(" ", blob)
    blob = _WS.sub(" ", blob).strip(" ,-\u2013|\u00b7\u2022")
    if not blob or len(blob) > _MAX_POSITION_CHARS:
        return None
    # "MD" on its own is a credential, not a job title.
    tokens = [t for t in re.split(r"[,\s]+", blob) if t]
    if tokens and all(_CREDENTIAL_TOKEN.fullmatch(t) for t in tokens):
        return None
    # The fallback only guesses when no recognised title matched, so be strict:
    # a colon or another field's label means this is somebody else's data.
    if ":" in blob or _NOT_A_POSITION.search(blob):
        return None
    # A leftover that is just a person's own name is not a position.
    return blob if not _looks_like_person_name(blob) else None


def classify_person(
    text: str, page_context: str, *, page_is_alumni: bool = False
) -> PersonCategory:
    """Label a person. Never decides whether to keep them — everyone is kept.

    Person-level text wins over the page's heading, except for alumni listings,
    where the page itself is the authoritative signal: someone described as a
    resident on an alumni page has already finished.
    """
    blob = text or ""

    if page_is_alumni or _ALUMNI_HINT.search(blob):
        return PersonCategory.ALUMNI
    # Faculty first: "Fellowship Program Director" is faculty, not a fellow.
    if _FACULTY_HINT.search(blob):
        return PersonCategory.FACULTY
    if _STUDENT_HINT.search(blob):
        return PersonCategory.STUDENT
    if _STAFF_HINT.search(blob):
        return PersonCategory.STAFF
    if parse_pgy(blob) is not None or _RESIDENT_HINT.search(blob):
        return PersonCategory.RESIDENT
    if _FELLOW_HINT.search(blob):
        return PersonCategory.FELLOW

    # Nothing person-level; fall back to what the page is about.
    if _FACULTY_HINT.search(page_context):
        return PersonCategory.FACULTY
    if _FELLOW_HINT.search(page_context) and not _RESIDENT_HINT.search(page_context):
        return PersonCategory.FELLOW
    if _RESIDENT_HINT.search(page_context):
        return PersonCategory.RESIDENT
    return PersonCategory.UNKNOWN


def page_is_alumni_listing(page_title: str, url: str) -> bool:
    """True when a page lists people who have already finished the programme.

    These pages are collected like any other; the people on them are simply
    labelled alumni.
    """
    haystack = f"{page_title or ''} {(url or '').replace('-', ' ').replace('/', ' ')}"
    return bool(_ALUMNI_HINT.search(haystack))


def _mailto_emails(node: Node) -> list[str]:
    out = []
    for anchor in node.css("a[href^='mailto:'], a[href^='MAILTO:']"):
        email = normalize_email(anchor.attributes.get("href", ""))
        if email and is_plausible(email):
            out.append(email)
    return out


def _block_text(node: Node) -> str:
    return _clean(node.text(separator=" "))


def _block_key(node: Node) -> str:
    """Stable identity for a DOM block, safe to keep in a set."""
    markup = node.html or ""
    return hashlib.sha1(markup[:4000].encode("utf-8", "replace")).hexdigest()


def _from_tables(
    tree: HTMLParser, page_context: str, *, page_is_alumni: bool = False
) -> list[ExtractedPerson]:
    """Header-driven table parsing: the most reliable shape when it is present."""
    people: list[ExtractedPerson] = []
    for table in tree.css("table"):
        rows = table.css("tr")
        if len(rows) < 2:
            continue
        header_cells = [_clean(c.text()).lower() for c in rows[0].css("th, td")]
        if not header_cells:
            continue

        # Loop variables bound as defaults: these closures are only used in
        # the iteration that defines them, but late binding is a trap for
        # anyone who later moves the call.
        def column(*names: str, headers: list[str] = header_cells) -> int | None:
            for index, header in enumerate(headers):
                if any(n in header for n in names):
                    return index
            return None

        name_col = column("name", "resident", "fellow", "trainee")
        email_col = column("email", "e-mail", "contact")
        pgy_col = column("pgy", "year", "level", "class")
        specialty_col = column("specialty", "program", "department", "division")
        position_col = column("position", "title", "role", "rank", "appointment")
        # Without a name or email column this is a data table, not a roster.
        if name_col is None and email_col is None:
            continue

        for row in rows[1:]:
            cells = row.css("td")
            if not cells:
                continue
            texts = [_clean(c.text()) for c in cells]
            row_text = " | ".join(texts)

            def cell(index: int | None, values: list[str] = texts) -> str:
                return values[index] if index is not None and index < len(values) else ""

            emails = _mailto_emails(row) or extract_emails(cell(email_col) or row_text)
            name = _extract_name(cell(name_col)) if name_col is not None else None
            if not name:
                name = _extract_name(row_text)
            if not name and not emails:
                continue

            year_text = cell(pgy_col) or row_text
            person = ExtractedPerson(
                full_name=name,
                email=emails[0] if emails else None,
                category=classify_person(
                    row_text, page_context, page_is_alumni=page_is_alumni
                ),
                position=_extract_position(cell(position_col) or row_text, name),
                pgy=parse_pgy(year_text),
                class_of=parse_class_of(year_text),
                specialty_raw=cell(specialty_col) or None,
                locate_hints=[t for t in (name, emails[0] if emails else None) if t],
                confidence=0.85,  # a labelled table is strong structure
                source_note="html:table",
            )
            if person.is_usable:
                people.append(person)
    return people


# Blocks that name a person explicitly. Safe to scan on every page.
_PERSON_SELECTORS = (
    "[class*='resident' i]", "[class*='fellow' i]", "[class*='person' i]",
    "[class*='profile' i]", "[class*='member' i]", "[class*='card' i]",
    "[class*='staff' i]", "[class*='people' i]", "[class*='bio' i]",
    "[class*='faculty' i]", "[class*='directory' i]",
)
# Generic containers. Only scanned when nothing else matched, since almost any
# page has list items and articles.
_GENERIC_SELECTORS = ("li", "article")
# Last resort: a bare list of headings. Faculty directories often publish one
# heading per person with no wrapping card, no CSS hook and no mailto link.
_HEADING_SELECTORS = ("h2", "h3", "h4")
_MAX_HEADING_CONTEXT = 600


def _from_cards(
    tree: HTMLParser, page_context: str, *, page_is_alumni: bool = False
) -> list[ExtractedPerson]:
    """Repeated card blocks. Anchored on mailto links where they exist, because
    an address is the one unambiguous marker of a person block."""
    people: list[ExtractedPerson] = []
    # Keyed on block content, never on id(node): selectolax builds a fresh Python
    # wrapper for every .parent access, so once one is garbage-collected CPython
    # reuses its id and unrelated cards collide as "already seen". That silently
    # dropped most of the roster, and did so nondeterministically.
    seen_blocks: set[str] = set()

    for anchor in tree.css("a[href^='mailto:'], a[href^='MAILTO:']"):
        email = normalize_email(anchor.attributes.get("href", ""))
        if not email or not is_plausible(email):
            continue
        # Walk up to the smallest ancestor that also carries a name.
        block: Node | None = anchor
        chosen: Node | None = None
        for _ in range(5):
            block = block.parent if block else None
            if block is None:
                break
            text = _block_text(block)
            if len(text) > 600:
                break
            if _extract_name(text):
                chosen = block
                break
        target = chosen or anchor
        block_key = _block_key(target)
        if block_key in seen_blocks:
            continue
        seen_blocks.add(block_key)

        text = _block_text(target)
        name = _extract_name(text) or _extract_name(_clean(anchor.text()))
        person = ExtractedPerson(
            full_name=name,
            email=email,
            category=classify_person(text, page_context, page_is_alumni=page_is_alumni),
            position=_extract_position(text, name),
            pgy=parse_pgy(text),
            class_of=parse_class_of(text),
            locate_hints=[t for t in (name, email) if t],
            confidence=0.8 if name else 0.55,
            source_note="html:card",
        )
        if person.is_usable:
            people.append(person)

    # Also scan explicit person blocks, even when mailto links were found. A
    # roster table with addresses and a faculty card grid without them commonly
    # sit on the same page, and returning early here loses the second group.
    people.extend(_from_person_blocks(tree, page_context, page_is_alumni, _PERSON_SELECTORS))

    if not people:
        # Nothing person-shaped matched, so fall back to generic containers.
        people.extend(
            _from_person_blocks(tree, page_context, page_is_alumni, _GENERIC_SELECTORS)
        )
    if not people:
        people.extend(_from_headings(tree, page_context, page_is_alumni))
    return people


def _from_headings(
    tree: HTMLParser, page_context: str, page_is_alumni: bool
) -> list[ExtractedPerson]:
    """People published as a plain run of headings.

    Faculty directories frequently list one <h3> per person with no card, no
    class name to hook onto and no mailto link. Section headings are filtered
    out by the same name validation used everywhere else.
    """
    found: list[ExtractedPerson] = []
    seen: set[str] = set()

    for selector in _HEADING_SELECTORS:
        for node in tree.css(selector):
            heading = _clean(node.text())
            name = _extract_name(heading)
            if not name:
                continue
            key = normalize_name(name) or name
            if key in seen:
                continue
            seen.add(key)

            # Look just around the heading for a title and address, not the
            # whole page, or every person inherits the same context.
            parent = node.parent
            context = _block_text(parent) if parent is not None else heading
            if len(context) > _MAX_HEADING_CONTEXT:
                context = heading
            emails = extract_emails(context)

            found.append(
                ExtractedPerson(
                    full_name=name,
                    email=emails[0] if emails else None,
                    category=classify_person(
                        context, page_context, page_is_alumni=page_is_alumni
                    ),
                    position=_extract_position(context, name),
                    pgy=parse_pgy(context),
                    class_of=parse_class_of(context),
                    locate_hints=[t for t in (name, emails[0] if emails else None) if t],
                    confidence=0.5 if emails else 0.35,
                    source_note="html:heading",
                )
            )
        if found:
            break
    return found


def _from_person_blocks(
    tree: HTMLParser,
    page_context: str,
    page_is_alumni: bool,
    selectors: tuple[str, ...],
) -> list[ExtractedPerson]:
    """Name-bearing blocks, for people published without a mailto link."""
    found: list[ExtractedPerson] = []
    seen: set[str] = set()
    for selector in selectors:
        for node in tree.css(selector):
            text = _block_text(node)
            if not text or len(text) > 400:
                continue
            heading = node.css_first(
                "h1, h2, h3, h4, h5, strong, b, .name, [class*='name' i]"
            )
            name = _extract_name(_block_text(heading)) if heading else None
            if not name:
                continue
            key = _block_key(node)
            if key in seen:
                continue
            seen.add(key)

            emails = extract_emails(text)
            person = ExtractedPerson(
                full_name=name,
                email=emails[0] if emails else None,
                category=classify_person(
                    text, page_context, page_is_alumni=page_is_alumni
                ),
                position=_extract_position(text, name),
                pgy=parse_pgy(text),
                class_of=parse_class_of(text),
                locate_hints=[t for t in (name, emails[0] if emails else None) if t],
                confidence=0.6 if emails else 0.4,
                source_note="html:block",
            )
            if person.is_usable:
                found.append(person)
        if found:
            break
    return found


def _dedupe(people: list[ExtractedPerson]) -> list[ExtractedPerson]:
    """Collapse the same person found by more than one strategy, keeping the
    most complete copy."""
    best: dict[str, ExtractedPerson] = {}
    for person in people:
        key = person.email or f"name:{normalize_name(person.full_name)}"
        if not key or key == "name:None":
            continue
        existing = best.get(key)
        if existing is None:
            best[key] = person
            continue
        # `position` counts: the same person can be found by two strategies and
        # only one of them sees their title.
        def completeness(candidate: ExtractedPerson) -> int:
            return sum(
                1
                for value in (
                    candidate.full_name, candidate.email, candidate.position,
                    candidate.pgy, candidate.class_of,
                )
                if value is not None
            )

        if (completeness(person), person.confidence) > (
            completeness(existing), existing.confidence
        ):
            best[key] = person
    return list(best.values())


def extract_people(
    html: str, *, page_title: str = "", url: str = ""
) -> list[ExtractedPerson]:
    """Parse every person published on a page, labelled with their position.

    Nobody is filtered out here. Faculty, program directors, coordinators,
    students and alumni are all collected and categorised; an alumni listing is
    a valid source whose people are simply labelled alumni.
    """
    if not html or not html.strip():
        return []

    page_is_alumni = page_is_alumni_listing(page_title, url)
    tree = HTMLParser(deobfuscate(html))
    for tag in ("script", "style", "noscript", "svg", "nav", "footer", "header"):
        for node in tree.css(tag):
            node.decompose()

    page_context = f"{page_title} {url}"
    people = _dedupe(
        _from_tables(tree, page_context, page_is_alumni=page_is_alumni)
        + _from_cards(tree, page_context, page_is_alumni=page_is_alumni)
    )
    log.debug("html extraction: %d people from %s", len(people), url or "<html>")
    return people


def page_looks_thin(html: str, text: str, people_found: int) -> tuple[bool, str]:
    """Decide whether to escalate to a rendered page + vision.

    Cheap first, escalate only on evidence: a JS shell, almost no text, or a page
    that clearly advertises a roster but yielded nobody.
    """
    stripped = (text or "").strip()
    if people_found > 0:
        return False, ""
    if len(stripped) < 400:
        return True, "page body has almost no text (likely client-rendered)"
    lowered = (html or "").lower()
    if any(marker in lowered for marker in ('id="root"', 'id="app"', "ng-app", "data-reactroot")):
        return True, "single-page-app shell detected"
    if "@" not in stripped and re.search(r"resident|fellow|house ?staff", stripped, re.IGNORECASE):
        return True, "roster page with no addresses in the DOM (may be images)"
    if re.search(r"resident|fellow|house ?staff", stripped, re.IGNORECASE):
        return True, "roster keywords present but no people parsed from HTML"
    return False, ""
