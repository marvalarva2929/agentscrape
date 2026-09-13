"""Email extraction and validation, including the obfuscation .edu sites favour."""

from __future__ import annotations

import re

from ..domain.matching import EMAIL_RE, normalize_email

# "jane [at] uchicago [dot] edu", "jane (at) uchicago (dot) edu", "jane AT x DOT edu"
_OBFUSCATED = re.compile(
    r"([A-Za-z0-9._%+\-]+)\s*(?:\[|\(|\{|&#64;|\s)\s*(?:at|@)\s*(?:\]|\)|\}|\s)\s*"
    r"([A-Za-z0-9.\-]+?)\s*(?:\[|\(|\{|\s)\s*(?:dot|\.)\s*(?:\]|\)|\}|\s)\s*([A-Za-z]{2,})",
    re.IGNORECASE,
)
_ENTITY_AT = re.compile(r"&#0*64;|&commat;|&#x0*40;", re.IGNORECASE)
_ENTITY_DOT = re.compile(r"&#0*46;|&period;|&#x0*2e;", re.IGNORECASE)

# Addresses that are never a person at the institution.
_JUNK_DOMAINS = frozenset({
    "example.com", "example.org", "domain.com", "email.com", "yourdomain.com",
    "sentry.io", "wordpress.org", "w3.org", "schema.org", "adobe.com",
})
_JUNK_LOCAL_PREFIXES = ("u00", "x00", "font", "icon", "sprite", "image", "logo")
_IMAGE_EXT = re.compile(r"\.(png|jpe?g|gif|svg|webp|ico|css|js)$", re.IGNORECASE)


def deobfuscate(text: str) -> str:
    """Rewrite common obfuscations into plain addresses before extraction."""
    text = _ENTITY_AT.sub("@", text)
    text = _ENTITY_DOT.sub(".", text)
    return _OBFUSCATED.sub(r"\1@\2.\3", text)


def extract_emails(text: str) -> list[str]:
    """All plausible, normalized, deduped addresses in a blob of text."""
    seen: dict[str, None] = {}
    for candidate in EMAIL_RE.findall(deobfuscate(text or "")):
        normalized = normalize_email(candidate)
        if normalized and is_plausible(normalized):
            seen.setdefault(normalized, None)
    return list(seen)


def is_plausible(email: str) -> bool:
    """Reject the addresses that regexes scrape out of markup rather than content."""
    if not email or email.count("@") != 1:
        return False
    local, _, domain = email.partition("@")
    if not local or not domain or ".." in email:
        return False
    if len(email) > 254 or len(local) > 64:
        return False
    if domain in _JUNK_DOMAINS or domain.startswith("."):
        return False
    if _IMAGE_EXT.search(email):  # "logo@2x.png" style false positives
        return False
    if any(local.lower().startswith(p) for p in _JUNK_LOCAL_PREFIXES):
        return False
    tld = domain.rsplit(".", 1)[-1]
    return tld.isalpha() and len(tld) >= 2


def institutional_match(email: str, site_host: str) -> bool:
    """True when the address belongs to the institution being scraped.

    Used as a confidence signal, not a filter: residents legitimately publish
    personal addresses, and those are still wanted.
    """
    from ..urls import registrable_domain

    if "@" not in email:
        return False
    return registrable_domain(email.split("@", 1)[1]) == registrable_domain(site_host)
