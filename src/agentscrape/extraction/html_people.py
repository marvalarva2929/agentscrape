"""Extract every person published in static HTML — the cheap default path.

Rosters come in four shapes and this handles all of them:
  * tables with a header row (Name | PGY | Email)
  * repeated card blocks (photo, heading, mailto link)
  * headerless layout tables, one person and their bio per cell
  * plain runs of headings, with no card and no address

Everyone published on the page is collected and labelled with their printed
title: residents, fellows, faculty, coordinators, students and alumni alike.
Nothing is filtered out here — `classify_person` decides what to call a person,
never whether to keep them.
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
from ..llm.role_evidence import has_direct_current_trainee_evidence, is_non_roster_context
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
    r"M\.?B\.?B\.?Ch|M\.?B\.?Ch\.?B|M\.?S\.?N|D\.?N\.?P|Ed\.?D|"
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
    r"staff physician|consultant physician|program leadership|leadership team|course director|site director|"
    r"faculty (member|appointment)|instructor of|"
    r"(core|clinical|teaching|associated|affiliated|volunteer|adjunct|recurrent|"
    r"joint|emeritus|research|academic) faculty)\b",
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
# The role noun only. A PGY number is deliberately absent: fellows are numbered
# too ("PGY-6 Fellow"), so reading a PGY as a claim of being a resident made
# every fellowship roster ambiguous with itself.
_RESIDENT_NOUN = re.compile(
    r"\bresidents?\b|\bhouse\s?staff\b|\bintern\b", re.IGNORECASE
)
# Used for a page or section heading, where a training year *is* evidence of a
# residency. CA-1..CA-3 is the anaesthesiology equivalent of PGY-n — it is what
# the client's own sheet records in its PGY column — and a section headed "CA-1"
# is a list of residents however the page above it is titled.
_RESIDENT_HINT = re.compile(
    r"\bresidents?\b|\bhouse\s?staff\b|\bpgy\b|\bintern\b|\bchief resident\b|"
    r"\bca[\s-]?[123]\b|\bpg[\s-]?[1-9]\b",
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
    r"(\bclass of\s*'?\d{2,4}\b|\bpgy[-\s]?\d\b|(?<![\w.+-])[\w.+-]{1,64}@[\w.-]{1,253}|"
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
# A nickname printed inside quotes, straight or curly: `Nicholas "Nick" D'Amico`.
# Left in place it splits the name into two runs, and the regex then returns
# whichever run is longer — so the surname was being dropped, and an adjacent
# image caption picked up instead ("Image Nicholas").
_NICKNAME = re.compile(
    "[\u201c\u2018\"']"          # an opening quote, straight or curly
    "[^\u201d\u2019\"']{1,20}"   # the nickname itself
    "[\u201d\u2019\"']"          # the matching closing quote
)

_NON_PERSON_TEXT = re.compile(
    r"^(home|about|contact|search|menu|skip to|read more|learn more|apply now|"
    r"privacy|terms|copyright|all rights|back to top|view all|next|previous)\b",
    re.IGNORECASE,
)
_WS = re.compile(r"\s+")


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
    # Image captions and alt text sit immediately before the name in a card.
    "image", "images", "photo", "photos", "picture", "headshot", "portrait",
    "avatar", "placeholder", "thumbnail", "logo", "icon",
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
    "award", "awards", "appreciation", "recognition", "responsibilities",
    "responsibility", "duties", "call", "expectations", "requirements",
    "policies", "guidelines", "handbook", "faq", "eligibility", "stipend",
    # Site furniture that reads as a title-cased phrase in a card block.
    "jobs", "job", "careers", "career", "giving", "donate", "news",
    "events", "calendar", "links", "policy", "forms",
    "application", "applications", "diversity", "inclusion",
    "research", "publications", "gallery", "videos", "sitemap",
    "specialties", "specialty", "clinical", "conditions", "treatments",
    "procedures", "disease", "diseases", "disorders", "locations",
    # Job titles that run straight into the name in a university staff
    # directory, with no separator: "Scott Berman Assistant Clinical Professor",
    # "Jennifer Chiaia-Price Health Care Partner".
    "assistant", "associate", "partner", "developer", "engineer",
    "care", "health", "practitioner", "technologist", "librarian", "advisor",
    "web", "systems", "communications",
    # Institution names. A trainee's card lists where they trained, and those
    # lines parse as title-cased two-word names: "Baylor College", "Texas Tech
    # University", "M University System". No surname collides with these.
    "university", "college", "colleges", "universities", "system", "sciences",
    "med", "healthcare", "memorial", "regional", "affiliate",
})


def _looks_like_person_name(text: str) -> bool:
    """Filter out headings and nav labels that happen to be title-cased."""
    text = _clean(text)
    if not text or len(text) > 80 or _NON_PERSON_TEXT.match(text):
        return False
    stripped = _strip_credentials(text)
    # Split on the separators a site uses to join a name to its printed role,
    # not just whitespace: "Abbey Bayless, Resident/Fellow" was accepted whole
    # because "resident/fellow" is not a stopword while "resident" is.
    words = [w for w in re.split(r"[\s,/|\u00b7\u2022]+", stripped) if w]
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
    # A quoted nickname breaks the name into two runs; removing it first keeps
    # the surname attached to the given name.
    text = _clean(_NICKNAME.sub(" ", text))
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


# Labels a profile card uses to introduce someone's history rather than their
# current role. The label word itself is what misfires: "Undergraduate: University
# of Florida" on a PGY-1's card made 35 Chicago pathology residents come back as
# students, and "Fellowship: Mayo Clinic" on a resident's card is the same trap.
# Only the label is removed — the value after it is harmless, and cutting to the
# next label would take the rest of the card with it on a run-together render.
_HISTORY_LABEL = re.compile(
    r"\b(medical school|med school|undergraduate|undergrad|graduate school|"
    r"college|university|residency|internship|fellowship|training|education|"
    r"hometown|home town|interests|hobbies|research interests|degrees?|"
    r"prior education|next stop|alma mater)\s*:",
    re.IGNORECASE,
)


def strip_history_labels(text: str) -> str:
    """Drop "Undergraduate:"-style field labels, keeping their values."""
    return _HISTORY_LABEL.sub(" ", text or "")


def classify_person(
    text: str,
    page_context: str,
    *,
    page_is_alumni: bool = False,
    section: str | list[str] = "",
) -> PersonCategory:
    """Label a person. Never decides whether to keep them — everyone is kept.

    Evidence is read most-specific first: the heading this person sits under,
    then their own text, then the page. `section` outranks everything because a
    roster that carries both groups says which is which only in its headings —
    a card under "Alumni" is an alumnus however the page is titled, and a card
    under "PGY-2" is a current resident on that same page.
    """
    blob = strip_history_labels(text or "")

    headings = [section] if isinstance(section, str) else list(section)
    # Residency/fellowship sites often put committee, advisory, faculty,
    # alumni, recruitment, or research groups beside real rosters.  A heading
    # such as "Resident Advisory Council" describes the group, not the GME
    # status of everyone it names.  Preserve an explicit person-level PGY or
    # current clinical trainee title, but never infer from that heading/page.
    governance_context = any(is_non_roster_context(heading) for heading in headings)
    if any(section_is_alumni(heading) for heading in headings):
        return PersonCategory.ALUMNI
    if _ALUMNI_HINT.search(blob):
        return PersonCategory.ALUMNI
    # The page as a whole is an alumni listing — unless this person sits under a
    # heading that says they are here now, which outranks the page title.
    if page_is_alumni and not any(
        _CURRENT_SECTION.search(heading) for heading in headings
    ):
        return PersonCategory.ALUMNI
    # Faculty first: "Fellowship Program Director" is faculty, not a fellow.
    if _FACULTY_HINT.search(blob):
        return PersonCategory.FACULTY
    if _STUDENT_HINT.search(blob):
        return PersonCategory.STUDENT
    if _STAFF_HINT.search(blob):
        return PersonCategory.STAFF
    # Resident or fellow. Unambiguous person-level evidence settles it; a card
    # naming both settles nothing, so it defers to the roster it sits on.
    #
    # Deferring matters because "Resident/Fellow" is one combined taxonomy term
    # on every Drupal-built medical school site, printed on the card of every
    # trainee whatever they actually are. Reading it as a claim about fellows
    # relabelled a whole anaesthesiology residency; reading it as a claim about
    # residents relabelled a whole cardiology fellowship. Neither is a claim.
    says_resident = bool(_RESIDENT_NOUN.search(blob))
    says_fellow = bool(_FELLOW_HINT.search(blob))
    if says_resident and not says_fellow and (
        not governance_context or has_direct_current_trainee_evidence(blob)
    ):
        return PersonCategory.RESIDENT
    if says_fellow and not says_resident and (
        not governance_context or has_direct_current_trainee_evidence(blob)
    ):
        return PersonCategory.FELLOW

    # The section is asked on its own first. "Residents & Fellows" is the
    # commonest title for a page that carries both, so folding it in with the
    # headings makes every section ambiguous — and every person on such a page
    # came back `unknown` even though their own heading said "CA-1".
    resolved = _trainee_from_context(" ".join(headings))
    if resolved is None:
        resolved = _trainee_from_context(page_context)

    if says_resident and says_fellow:
        # Both named and the page does not disambiguate: say so rather than
        # guess, because the R/F split is a field the client sorts on.
        return (
            resolved
            if resolved is not None and not governance_context
            else PersonCategory.UNKNOWN
        )

    # No role noun at all. A bare PGY is a training year and nothing else
    # carries one, so it means resident unless the roster says fellowship.
    if parse_pgy(blob) is not None:
        return resolved or PersonCategory.RESIDENT

    # Nothing person-level; fall back to what the page is about.
    if _FACULTY_HINT.search(page_context):
        return PersonCategory.FACULTY
    return (
        resolved
        if resolved is not None and not governance_context
        else PersonCategory.UNKNOWN
    )


def _trainee_from_context(context: str) -> PersonCategory | None:
    """Resident or fellow according to the roster, or None when it is silent."""
    fellow = bool(_FELLOW_HINT.search(context))
    resident = bool(_RESIDENT_HINT.search(context))
    if fellow and not resident:
        return PersonCategory.FELLOW
    if resident and not fellow:
        return PersonCategory.RESIDENT
    return None


# Headings that split one roster page into current trainees and people who have
# already finished. `.../current-and-past-residents` is the commonest roster URL
# on a .edu medical site and almost always carries both groups, with an "Alumni"
# heading between them. Judging such a page only by its title labelled every
# alumnus a current resident: on Arizona's internal medicine, paediatrics and
# anaesthesiology rosters that was 407 of 658 people.
# A bare "Alumni"/"Graduates"/"Past Residents" heading is the usual spelling.
# Deliberately not applied to the page title, where "Current and Past Residents"
# describes the whole page and says nothing about one person.
_ALUMNI_SECTION = re.compile(
    r"\b(alumni|alumnae|graduates|graduated|past|former|previous)\b", re.IGNORECASE
)
# Headings that name people who are here now. They override a page-level
# alumni flag, because a page titled "Current Residents and Alumni" carries both
# groups and only its headings say which is which — without this, every PGY-3 on
# such a page inherited the title's "Alumni" and the whole roster was written off
# as graduated.
_CURRENT_SECTION = re.compile(
    r"\bpgy[\s-]?[0-9ivx]|\bca[\s-]?[123]\b|\bcurrent\b|\bchief\s+resident|"
    r"\bincoming\b|\binterns?\b",
    re.IGNORECASE,
)
_SECTION_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5"})
_MAX_SECTION_WALK = 40
# Each previous sibling costs a subtree scan, so an unbounded walk is quadratic
# in the number of cards: on a directory listing 5,000 people it never finished.
# A heading more than this many siblings back is not this card's section anyway;
# the walk moves up a level instead, which is where such a heading really sits.
_MAX_SIBLING_SCAN = 50


def _heading_level(tag: str | None) -> int:
    return int(tag[1]) if tag in _SECTION_TAGS else 9


def _preceding_heading(node: Node, above_level: int) -> Node | None:
    """The nearest heading before `node` whose level is more senior than given.

    Walks back through previous siblings and then up through parents, which is
    how a reader decides which heading something sits under.
    """
    selector = ", ".join(
        tag for tag in ("h1", "h2", "h3", "h4", "h5") if _heading_level(tag) < above_level
    )
    if not selector:
        return None

    current: Node | None = node
    for _ in range(_MAX_SECTION_WALK):
        if current is None:
            return None
        sibling = current.prev
        for _ in range(_MAX_SIBLING_SCAN):
            if sibling is None:
                break
            if _heading_level(sibling.tag) < above_level:
                return sibling
            # A heading nested inside an earlier sibling still precedes us; the
            # last one in that subtree is the closest.
            inner = sibling.css(selector)
            if inner:
                return inner[-1]
            sibling = sibling.prev
        current = current.parent
    return None


def section_trail(node: Node, *, above_level: int = 9) -> list[str]:
    """Headings enclosing `node`, nearest first, one per level.

    The nearest heading alone is not enough. An alumni block is routinely split
    into per-year sub-headings, so the heading directly above a graduate reads
    "Class of 2025" while the one that says these people have left is the `<h1>`
    above all of them. Reading the whole trail is what separates a page's
    current trainees from its graduates.
    """
    trail: list[str] = []
    heading = _preceding_heading(node, above_level)
    for _ in range(len(_SECTION_TAGS) + 1):
        if heading is None:
            break
        text = _clean(heading.text())
        if text:
            trail.append(text)
        heading = _preceding_heading(heading, _heading_level(heading.tag))
    return trail


def enclosing_section(node: Node, *, above_level: int = 9) -> str:
    """The nearest enclosing heading's text, or "" when there is none."""
    trail = section_trail(node, above_level=above_level)
    return trail[0] if trail else ""


def section_is_alumni(section: str) -> bool:
    """True when a heading marks people who have finished the programme."""
    if not section:
        return False
    # A bare "Alumni"/"Graduates" heading is the usual spelling, and the page
    # title's own "Current and Past Residents" must not match here — only the
    # heading above this particular person counts.
    return bool(_ALUMNI_HINT.search(section) or _ALUMNI_SECTION.search(section))


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
    """Table parsing. Header-driven where there is a header, cell-by-cell where
    the table is only being used to lay bios out in a grid."""
    people: list[ExtractedPerson] = []
    for table in tree.css("table"):
        rows = table.css("tr")
        header_cells = (
            [_block_text(c).lower() for c in rows[0].css("th, td")] if rows else []
        )
        if len(rows) < 2 or not header_cells:
            # Too few rows to have a header at all. A single row of cells is
            # still a common way to lay out a handful of trainee bios.
            people.extend(
                _from_layout_table(table, page_context, page_is_alumni=page_is_alumni)
            )
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
        # No name or email column: either a data table, or a table used purely
        # for layout with one person per cell. The cell scan tells them apart.
        if name_col is None and email_col is None:
            people.extend(
                _from_layout_table(table, page_context, page_is_alumni=page_is_alumni)
            )
            continue

        for row in rows[1:]:
            cells = row.css("td")
            if not cells:
                continue
            # Separator matters: a cell routinely wraps the printed title in its
            # own element, and joining without one produced "Obaidah AdiResident
            # Instructor - 3rd Yr". The role noun then has no word boundary in
            # front of it, so 135 Texas Tech residents classified as `unknown`.
            texts = [_block_text(c) for c in cells]
            row_text = " | ".join(texts)

            def cell(index: int | None, values: list[str] = texts) -> str:
                return values[index] if index is not None and index < len(values) else ""

            emails = _mailto_emails(row) or extract_emails(cell(email_col) or row_text)
            name = None
            if name_col is not None and name_col < len(cells):
                # A name cell routinely wraps the printed title in its own
                # element: `<td>Obaidah Adi<div>Resident Instructor</div></td>`.
                # The cell's own text is the name; the nested element is not, and
                # reading the two together turned the title's first word into
                # part of the surname.
                own = _clean(cells[name_col].text(deep=False))
                name = _extract_name(own) or _extract_name(cell(name_col))
            if not name:
                name = _extract_name(row_text)
            if not name and not emails:
                continue

            year_text = cell(pgy_col) or row_text
            person = ExtractedPerson(
                full_name=name,
                email=emails[0] if emails else None,
                category=classify_person(
                    row_text, page_context, page_is_alumni=page_is_alumni,
                    section=section_trail(row),
                ),
                position=_extract_position(cell(position_col) or row_text, name),
                pgy=parse_pgy(year_text),
                class_of=parse_class_of(year_text),
                specialty_raw=cell(specialty_col) or None,
                confidence=0.85,  # a labelled table is strong structure
                source_note="html:table",
            )
            if person.is_usable:
                people.append(person)
    return people


# A person's own cell in a layout table can hold their whole biography, which is
# far longer than the card blocks scanned elsewhere. The name still comes from
# the bolded heading, and `_extract_position` refuses anything this long, so the
# extra room buys the name without letting prose become a job title.
_MAX_CELL_CHARS = 4_000


def _from_layout_table(
    table: Node, page_context: str, *, page_is_alumni: bool = False
) -> list[ExtractedPerson]:
    """People in a table that has no header row.

    Departments publish trainee bios as a borderless table, one person per cell,
    with the name in a `<strong>` and a paragraph of prose under it. There is no
    header to drive column parsing and no card class to hook onto, so each cell
    is treated the way a card block is — which is what it is, rendered with
    `<td>` instead of `<div>`.
    """
    found: list[ExtractedPerson] = []
    seen: set[str] = set()
    for cell in table.css("td, th"):
        text = _block_text(cell)
        if not text or len(text) > _MAX_CELL_CHARS:
            continue
        heading = cell.css_first("strong, b, h2, h3, h4, h5, .name, [class*='name' i]")
        name = _extract_name(_block_text(heading)) if heading else None
        if not name:
            continue
        key = normalize_name(name) or name
        if key in seen:
            continue
        seen.add(key)

        emails = _mailto_emails(cell) or extract_emails(text)
        person = ExtractedPerson(
            full_name=name,
            email=emails[0] if emails else None,
            category=classify_person(
                text, page_context, page_is_alumni=page_is_alumni,
                section=section_trail(cell),
            ),
            position=_extract_position(text, name),
            pgy=parse_pgy(text),
            class_of=parse_class_of(text),
            confidence=0.6 if emails else 0.4,
            source_note="html:layout-table",
        )
        if person.is_usable:
            found.append(person)
    return found


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
            category=classify_person(
                text, page_context, page_is_alumni=page_is_alumni,
                section=section_trail(target),
            ),
            position=_extract_position(text, name),
            pgy=parse_pgy(text),
            class_of=parse_class_of(text),
            confidence=0.8 if name else 0.55,
            source_note="html:card",
        )
        if person.is_usable:
            people.append(person)

    # Also scan explicit person blocks, even when mailto links were found. A
    # roster table with addresses and a faculty card grid without them commonly
    # sit on the same page, and returning early here loses the second group.
    people.extend(_from_person_blocks(tree, page_context, page_is_alumni, _PERSON_SELECTORS))

    # Headings are scanned on every page, not only when nothing else matched.
    # A roster whose person blocks carry no recognisable class and no address —
    # BCM publishes its residents as bare <h3>s inside `definition-terms` divs —
    # was yielding only the programme director, whose mailto happened to be in
    # the sidebar. One incidental address anywhere on the page was enough to
    # suppress the scan that finds everyone else. Name validation rejects section
    # headings, and `_dedupe` keeps the richer copy of anyone found twice.
    people.extend(_from_headings(tree, page_context, page_is_alumni))

    # Shape is scanned unless the page has already given up a full roster.
    #
    # This is a cost guard, not the "nothing else matched" gate that cost this
    # crawler three separate rosters — one stray match used to silence the
    # detector that would have found everybody. The threshold sits far above a
    # stray match: a page that yielded a handful still gets scanned, while an
    # institution-wide directory that yielded thousands does not, because the
    # scan is quadratic in cards and would add nothing there anyway.
    if len(people) < _GRID_SCAN_CEILING:
        people.extend(_from_repeated_blocks(tree, page_context, page_is_alumni))

    if not people:
        # Genuinely last resort: almost every page has list items and articles.
        people.extend(
            _from_person_blocks(tree, page_context, page_is_alumni, _GENERIC_SELECTORS)
        )
    return people


def _from_headings(
    tree: HTMLParser, page_context: str, page_is_alumni: bool
) -> list[ExtractedPerson]:
    """People published as a plain run of headings.

    Faculty directories frequently list one <h3> per person with no card, no
    class name to hook onto and no mailto link. Section headings are filtered
    out by the same name validation used everywhere else.

    Every heading level is scanned, not just the first that yields anybody. BCM
    lists each resident as an <h3> under <h2>PGY-5 Residents</h2> headings, and
    stopping at the first productive level meant one stray <h2> elsewhere on the
    page hid all twenty residents below it.
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
                        context, page_context, page_is_alumni=page_is_alumni,
                        section=section_trail(
                            node, above_level=_heading_level(node.tag)
                        ),
                    ),
                    position=_extract_position(context, name),
                    pgy=parse_pgy(context),
                    class_of=parse_class_of(context),
                    confidence=0.5 if emails else 0.35,
                    source_note="html:heading",
                )
            )
    return found


# A roster grid repeats one card shape. Three is the smallest run that is clearly
# a list rather than a coincidence of layout.
_MIN_REPEATED_SIBLINGS = 3
_MAX_REPEATED_BLOCK_CHARS = 400
# A page of 5,000 people has one signature repeated 5,000 times; a page of
# utility-class wrappers has thousands of signatures repeated a handful of times
# each. Both are bounded here so a very large page cannot dominate the crawl.
_MAX_GRID_NODES = 40_000
# Above this many people already found, the page plainly has structure the other
# detectors can read, and the grid scan only re-finds them at quadratic cost.
_GRID_SCAN_CEILING = 20
_MAX_GRID_CANDIDATES = 6_000


def _from_repeated_blocks(
    tree: HTMLParser, page_context: str, page_is_alumni: bool
) -> list[ExtractedPerson]:
    """Roster grids built from utility classes, with no semantic hook at all.

    A Tailwind-styled card carries no `person`, `card` or `profile` class to
    match on — BCM's ophthalmology residents sit in
    `<div class="p-20 border border-gray-silver ...">` — and no address. What
    identifies them is repetition: one class signature used many times over
    short blocks is a list of like things, and when those blocks parse as people
    it is a roster.

    Grouping is by class signature across the whole document in a single pass.
    Walking parent by parent instead was quadratic and stalled outright on an
    8.5MB directory page.
    """
    by_signature: dict[str, list[Node]] = {}
    for index, node in enumerate(tree.css("[class]")):
        if index >= _MAX_GRID_NODES:
            break
        signature = _clean(node.attributes.get("class") or "")
        if signature:
            by_signature.setdefault(signature, []).append(node)

    # One member decides the whole group. Reading every candidate's subtree text
    # is what made this stall on an 8.5MB page: the outer wrappers repeat too,
    # and each of those costs a full-document walk to reject. Members of a group
    # share a shape, so sampling the first tells us whether the group is
    # card-sized long before we touch the rest.
    candidates: list[Node] = []
    for group in by_signature.values():
        if len(group) < _MIN_REPEATED_SIBLINGS:
            continue
        sample = _block_text(group[0])
        if not sample or len(sample) > _MAX_REPEATED_BLOCK_CHARS:
            continue
        candidates.extend(group)
        if len(candidates) >= _MAX_GRID_CANDIDATES:
            break

    found: list[ExtractedPerson] = []
    seen: set[str] = set()
    for node in candidates[:_MAX_GRID_CANDIDATES]:
        text = _block_text(node)
        if not text or len(text) > _MAX_REPEATED_BLOCK_CHARS:
            continue
        name = _name_in_block(node)
        if not name:
            continue
        key = normalize_name(name) or name
        if key in seen:
            continue
        seen.add(key)

        emails = _mailto_emails(node) or extract_emails(text)
        person = ExtractedPerson(
            full_name=name,
            email=emails[0] if emails else None,
            category=classify_person(
                text, page_context, page_is_alumni=page_is_alumni,
                section=section_trail(node),
            ),
            position=_extract_position(text, name),
            pgy=parse_pgy(text),
            class_of=parse_class_of(text),
            confidence=0.55 if emails else 0.4,
            source_note="html:grid",
        )
        if person.is_usable:
            found.append(person)
    return found


# The name sits near the top of a card, so there is no reason to read all of it.
_MAX_NAME_LINES = 12


def _name_in_block(node: Node) -> str | None:
    """The person's name inside one card, from the most name-like element.

    A heading or an explicit name class first, then the card's own short text
    lines — a utility-class card puts the name in a bare `<div>`, so there is
    nothing to select on and the text has to be read line by line.
    """
    heading = node.css_first("h1, h2, h3, h4, h5, strong, b, .name, [class*='name' i]")
    if heading is not None and (name := _extract_name(_block_text(heading))):
        return name
    for child in node.css("div, p, span, a")[:_MAX_NAME_LINES]:
        line = _clean(child.text(deep=False))
        if 0 < len(line) <= 80 and (name := _extract_name(line)):
            return name
    return None


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
                    text, page_context, page_is_alumni=page_is_alumni,
                    section=section_trail(node),
                ),
                position=_extract_position(text, name),
                pgy=parse_pgy(text),
                class_of=parse_class_of(text),
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
