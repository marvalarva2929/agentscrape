"""Record identity within a site.

Identity is the normalized email, scoped to a site. Two guards make that safe:

  * Generic mailboxes (info@, residency@, gme@ ...) identify an office, not a
    person, so a record carrying one is flagged `role_account` and keyed by name
    instead. Without this every department's shared inbox collapses its whole
    roster into a single record.
  * A same-email/different-name collision resolves to one record; the name change
    is written as an ordinary field diff on a new version.

Changing this key later means re-keying all history, so it is deliberately narrow.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Local parts that name an office rather than a person.
ROLE_LOCAL_PARTS: frozenset[str] = frozenset({
    "info", "information", "contact", "contactus", "admin", "administration",
    "administrator", "webmaster", "web", "office", "support", "help", "helpdesk",
    "inquiries", "inquiry", "enquiries", "general", "mail", "email", "noreply",
    "no-reply", "donotreply", "residency", "residencies", "residencyprogram",
    "fellowship", "fellowships", "gme", "gmeoffice", "hr", "humanresources",
    "recruiting", "recruitment", "feedback", "media", "press", "news", "alumni",
    "giving", "development", "dept", "department", "program", "programs",
    "coordinator", "education", "students", "admissions", "apply", "application",
    "registrar", "secretary", "reception", "frontdesk", "team", "staff", "faculty",
    "communications", "marketing", "events", "scheduling", "appointments",
})
# Suffix/prefix shapes: "surgery-residency@", "im.program@", "peds_gme@"
_ROLE_AFFIX_RE = re.compile(
    r"(^|[._\-])(residency|fellowship|program|gme|office|info|admin|coordinator|"
    r"recruit(ing|ment)?|education|admissions)([._\-]|$)",
    re.IGNORECASE,
)

_TITLE_PREFIXES = re.compile(
    r"^(dr|doctor|prof|professor|mr|mrs|ms|mx)\.?\s+", re.IGNORECASE
)
_CREDENTIAL_SUFFIX = re.compile(
    r"[,\s]+(m\.?d\.?(/|,|\s)*(ph\.?d\.?)?|d\.?o\.?|ph\.?d\.?|m\.?b\.?b\.?s\.?|"
    r"m\.?p\.?h\.?|m\.?s\.?c?\.?|m\.?b\.?a\.?|r\.?n\.?|p\.?a\.?[-\s]?c\.?|"
    r"f\.?a\.?c\.?[sp]\.?|b\.?s\.?|b\.?a\.?|esq\.?)\s*$",
    re.IGNORECASE,
)
_NON_NAME = re.compile(r"[^a-z0-9' \-]+")
_WS = re.compile(r"\s+")


class IdentityKind(StrEnum):
    EMAIL = "email"
    NAME = "name"


def normalize_email(raw: str | None) -> str | None:
    """Lowercase and trim. Deliberately does not strip dots or +tags: on .edu
    domains those are meaningful and two addresses that differ are two people."""
    if not raw:
        return None
    value = raw.strip().strip("<>").strip().lower()
    value = value.removeprefix("mailto:")
    value = value.split("?", 1)[0]  # mailto:a@b?subject=...
    if not EMAIL_RE.fullmatch(value):
        return None
    return value


def is_role_account(email: str | None) -> bool:
    """True when the address names an office rather than a person."""
    if not email or "@" not in email:
        return False
    local = email.split("@", 1)[0].lower()
    if local in ROLE_LOCAL_PARTS:
        return True
    if local.replace(".", "").replace("-", "").replace("_", "") in ROLE_LOCAL_PARTS:
        return True
    return bool(_ROLE_AFFIX_RE.search(local))


def normalize_name(raw: str | None) -> str | None:
    """Fold a display name to a comparable form.

    Handles "Last, First", academic credentials, titles, accents and middle
    initials. Middle initials are dropped because the same person is listed with
    and without one across pages of the same site.
    """
    if not raw:
        return None
    value = unicodedata.normalize("NFKD", raw)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.strip()
    # Strip credentials repeatedly: "Jane Doe, MD, MPH"
    previous = None
    while previous != value:
        previous = value
        value = _CREDENTIAL_SUFFIX.sub("", value).strip().rstrip(",")
    value = _TITLE_PREFIXES.sub("", value).strip()
    if "," in value:  # "Doe, Jane" -> "Jane Doe"
        last, _, first = value.partition(",")
        if first.strip():
            value = f"{first.strip()} {last.strip()}"
    value = _NON_NAME.sub(" ", value.lower())
    value = _WS.sub(" ", value).strip()
    tokens = [t for t in value.split() if len(t) > 1 or "'" in t]
    if len(tokens) < 2:
        return " ".join(value.split()) or None
    return " ".join(tokens)


@dataclass(frozen=True)
class Identity:
    key: str
    kind: IdentityKind
    role_account: bool


def build_identity(
    *, email: str | None, full_name: str | None
) -> Identity | None:
    """Identity key for a record within one site.

    Email wins unless it names an office; then we fall back to the name, which is
    what keeps two people behind one shared inbox apart. Returns None when there
    is neither a usable email nor a usable name — such a record is not storable.

    The role is deliberately *not* part of the key. It used to be, on the theory
    that it separated two same-name people, but a role is read from whatever page
    a person turned up on and is the least stable thing we record: the same
    trainee appears as "resident" on a departmental roster and "unknown" on an
    institution-wide directory that prints one combined "Resident/Fellow" term.
    Keying on it split 105 Arizona people into 210 records, each holding half
    their evidence, which is a worse error than merging a rare name collision
    whose sightings both survive in the record's version history anyway.
    """
    normalized_email = normalize_email(email)
    role_account = is_role_account(normalized_email)

    if normalized_email and not role_account:
        return Identity(f"email:{normalized_email}", IdentityKind.EMAIL, False)

    normalized_name = normalize_name(full_name)
    if normalized_name:
        return Identity(f"name:{normalized_name}", IdentityKind.NAME, role_account)
    return None


# Fields that participate in change detection. `pgy_at_capture` is included but
# `pgy_current` deliberately is not: the July rollover is not a data change.
VERSIONED_FIELDS: tuple[str, ...] = (
    "full_name", "email", "category", "position", "specialty_normalized",
    "specialty_raw", "pgy_at_capture", "class_of",
)


def diff_fields(
    previous: dict[str, object], current: dict[str, object]
) -> dict[str, dict[str, object]]:
    """Backend-computed diff. The frontend never derives what changed.

    A newly-null incoming value is not treated as a change: a page that stopped
    listing a PGY is missing information, not evidence the person's PGY was
    cleared. Overwriting good data with a null would lose it permanently.
    """
    changes: dict[str, dict[str, object]] = {}
    for field in VERSIONED_FIELDS:
        before = previous.get(field)
        after = current.get(field)
        if after is None and before is not None:
            continue
        if before != after:
            changes[field] = {"from": before, "to": after}
    return changes
