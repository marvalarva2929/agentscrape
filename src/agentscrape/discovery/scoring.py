"""Rank discovered URLs by how likely they are to carry resident/fellow contacts.

Browsing is the expensive part of the system, so arriving with a good ordered
list instead of exploring blind is the single biggest compute lever. Scoring is
pure heuristics over the URL and (when known) the page title — it must stay cheap
enough to run over thousands of candidates without touching the network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..domain.specialty import normalize_specialty
from ..urls import canonicalize, path_depth

# Path/title tokens that indicate a roster of people. Weight ~ directness.
_STRONG_SIGNALS: dict[str, float] = {
    "current-residents": 10.0, "currentresidents": 10.0, "our-residents": 10.0,
    "meet-our-residents": 10.0, "meet-the-residents": 10.0, "resident-directory": 10.0,
    "housestaff": 9.5, "house-staff": 9.5,
    "residents": 9.0, "fellows": 9.0, "residents-fellows": 9.5,
    "current-fellows": 9.5, "our-fellows": 9.5, "meet-our-fellows": 9.5,
    "resident-roster": 9.0, "class-of": 8.0, "residency-class": 8.0,
    "directory": 7.0, "roster": 7.0, "people": 6.5, "our-team": 6.0,
    "profiles": 6.0, "trainees": 8.0, "residency": 5.5, "fellowship": 5.5,
    "gme": 5.0, "graduate-medical-education": 5.5, "who-we-are": 4.5,
    "team": 4.0, "staff": 4.0, "contact": 3.0,
    # Everyone on the site is now collected, so faculty and alumni listings are
    # wanted sources rather than pages to avoid. Ranked below current-trainee
    # rosters, which are still the densest and most frequently updated.
    "faculty": 6.5, "faculty-directory": 7.5, "our-faculty": 7.0,
    "alumni": 5.0, "graduates": 5.0, "former-residents": 5.5,
}
# Tokens that make a page less likely to be a roster.
_NEGATIVE_SIGNALS: dict[str, float] = {
    "news": -6.0, "blog": -6.0, "events": -5.0, "calendar": -5.0, "giving": -7.0,
    "donate": -7.0, "login": -8.0, "search": -6.0, "privacy": -8.0, "terms": -8.0,
    "sitemap": -6.0, "rss": -8.0, "feed": -8.0, "archive": -4.0, "tag": -5.0,
    "category": -4.0, "press": -5.0, "media": -4.0, "career": -3.0, "jobs": -3.0,
    "placement": -2.0, "placements": -2.0, "outcomes": -4.0,
    "history": -5.0, "apply": -2.5, "admissions": -2.0, "research": -1.5,
    "publication": -4.0, "patient": -3.0, "appointment": -4.0, "billing": -7.0,
    "insurance": -6.0, "cart": -8.0, "shop": -8.0, "policy": -6.0,
}
_BAD_EXTENSIONS = re.compile(
    r"\.(pdf|docx?|xlsx?|pptx?|zip|jpg|jpeg|png|gif|svg|mp4|mp3|css|js|xml|json)$", re.IGNORECASE
)
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")
_YEAR_IN_PATH = re.compile(r"/(19|20)\d{2}(/|$)")


@dataclass(frozen=True)
class ScoredUrl:
    url: str
    score: float
    reasons: tuple[str, ...]
    is_known_path: bool = False

    def __lt__(self, other: ScoredUrl) -> bool:
        return self.score < other.score


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_SPLIT.split(text.lower()) if t]


def score_url(
    url: str,
    *,
    title: str | None = None,
    known_path_score: float | None = None,
    site_specialties: set[str] | None = None,
) -> ScoredUrl:
    """Score one candidate. Higher is more likely to hold contact information."""
    canonical = canonicalize(url) or url
    reasons: list[str] = []
    score = 0.0

    if _BAD_EXTENSIONS.search(canonical.split("?")[0]):
        return ScoredUrl(canonical, -100.0, ("non-html asset",))

    from urllib.parse import urlsplit

    parts = urlsplit(canonical)
    path = parts.path.lower()
    hyphenated = path.strip("/").replace("/", "-")

    # Multi-word phrases first so "current-residents" beats a bare "residents".
    for phrase, weight in sorted(
        _STRONG_SIGNALS.items(), key=lambda kv: -len(kv[0])
    ):
        if phrase in hyphenated:
            score += weight
            reasons.append(f"+{weight:g} path:{phrase}")
            break

    path_tokens = set(_tokens(path))
    for token, weight in _NEGATIVE_SIGNALS.items():
        if token in path_tokens:
            score += weight
            reasons.append(f"{weight:g} path:{token}")

    if title:
        title_tokens = set(_tokens(title))
        for phrase, weight in sorted(_STRONG_SIGNALS.items(), key=lambda kv: -len(kv[0])):
            if phrase.replace("-", " ") in title.lower():
                bonus = weight * 0.8  # a title match is strong but weaker than the path
                score += bonus
                reasons.append(f"+{bonus:g} title:{phrase}")
                break
        for token, weight in _NEGATIVE_SIGNALS.items():
            if token in title_tokens:
                score += weight * 0.5
                reasons.append(f"{weight * 0.5:g} title:{token}")

    # A recognizable specialty anywhere in the URL means we are inside a program.
    specialty = normalize_specialty(path.replace("/", " "))
    if specialty.canonical:
        score += 2.5
        reasons.append(f"+2.5 specialty:{specialty.canonical}")
        if site_specialties and specialty.canonical in site_specialties:
            score += 1.0
            reasons.append("+1 specialty seen on this site before")

    # A subdomain that is itself a department is a good sign.
    host_labels = (parts.hostname or "").split(".")
    if len(host_labels) > 2 and normalize_specialty(host_labels[0]).canonical:
        score += 2.0
        reasons.append(f"+2 subdomain:{host_labels[0]}")

    # Class-year pages ("/residents/2027") are rosters.
    if _YEAR_IN_PATH.search(path) and score > 0:
        score += 1.5
        reasons.append("+1.5 year in path")

    depth = path_depth(canonical)
    if depth > 4:
        penalty = -0.5 * (depth - 4)
        score += penalty
        reasons.append(f"{penalty:g} depth:{depth}")

    if parts.query:
        score -= 1.0
        reasons.append("-1 query string")

    if known_path_score is not None:
        # Known-good paths jump the queue: proven yield beats any heuristic.
        score += 100.0 + known_path_score
        reasons.append(f"+{100.0 + known_path_score:g} known good path")
        return ScoredUrl(canonical, score, tuple(reasons), is_known_path=True)

    return ScoredUrl(canonical, score, tuple(reasons))


def rank_candidates(
    urls: list[str],
    *,
    titles: dict[str, str] | None = None,
    known_paths: dict[str, float] | None = None,
    site_specialties: set[str] | None = None,
    limit: int | None = None,
    min_score: float = 0.0,
) -> list[ScoredUrl]:
    """Score, filter and order a candidate list. Known paths always come first."""
    titles = titles or {}
    known_paths = known_paths or {}
    seen: set[str] = set()
    scored: list[ScoredUrl] = []

    for url in urls:
        canonical = canonicalize(url)
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        scored.append(
            score_url(
                canonical,
                title=titles.get(canonical),
                known_path_score=known_paths.get(canonical),
                site_specialties=site_specialties,
            )
        )

    # Any known path is kept regardless of heuristic score — it has proven yield.
    kept = [s for s in scored if s.is_known_path or s.score >= min_score]
    kept.sort(key=lambda s: (-s.score, path_depth(s.url), s.url))
    return kept[:limit] if limit else kept
