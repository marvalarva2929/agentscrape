"""Rank discovered URLs by how likely they are to carry resident/fellow contacts.

Browsing is the expensive part of the system, so arriving with a good ordered
list instead of exploring blind is the single biggest compute lever. Scoring is
pure heuristics over the URL and (when known) the page title — it must stay cheap
enough to run over thousands of candidates without touching the network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..domain.specialty import GENERIC_HOST_LABELS, normalize_specialty
from ..urls import canonicalize, path_depth

# --- roster vocabulary ----------------------------------------------------
#
# Scoring is driven by the *last* path segment, because that is what names the
# page; ancestors only say which section of the site it sits in. An earlier
# version matched phrases against the whole path and stopped at the first hit,
# which compressed every real roster into the same 11-14 band as faculty
# listings and programme brochures: "current-and-past-residents" scored the same
# as a bare "/residents" because the "and-past" infix broke the phrase match.
# Token sets do not care about infixes or word order.

# The people the client is actually buying. A leaf naming one of these is a
# roster until proven otherwise.
_TRAINEE_NOUNS = frozenset({
    "resident", "residents", "fellow", "fellows", "trainee", "trainees",
    "housestaff", "houseofficer", "houseofficers", "intern", "interns",
    "residentfellow", "residentsfellows",
})
# Words that make a trainee leaf unambiguously a roster of named people rather
# than prose about the programme ("current-residents", "meet-our-fellows").
_ROSTER_QUALIFIERS = frozenset({
    "current", "our", "meet", "all", "class", "classes", "incoming", "new",
    "past", "the", "directory", "roster", "profiles", "list", "bios",
})
# Leaves that list people without naming a trainee: weight by how reliably the
# word means "a page of named people". The programme words at the bottom are
# weaker but they matter: a small fellowship rarely gets its own roster page and
# publishes its three or four fellows inline on the programme page instead,
# which is where the Arizona GI, ID and nephrology fellows turned out to live.
_ROSTER_NOUNS: dict[str, float] = {
    "directory": 7.5, "roster": 7.5, "profiles": 7.0, "faculty": 6.5,
    "people": 6.5, "physicians": 6.0, "providers": 6.0, "team": 5.5,
    "members": 5.5, "attendings": 6.5, "graduates": 5.0, "alumni": 5.0,
    "alumnae": 5.0, "fellowship": 5.0, "fellowships": 5.0, "residency": 5.0,
    "residencies": 5.0, "staff": 4.0, "leadership": 4.0, "contact": 2.5,
}
# Ancestor segments that place the page inside a training programme. Weak on
# their own — every brochure page under /residency-program/ has them — but they
# separate a department's roster from the hospital's general staff directory.
_TRAINING_CONTEXT = frozenset({
    "residency", "residencies", "fellowship", "fellowships", "gme",
    "housestaff", "traineeship", "residencyprogram", "trainees",
})
_EDUCATION_CONTEXT = frozenset({"education", "educational", "training", "academics"})
# Ancestor segments that mean the leaf is prose *about* people, not a listing of
# them. These veto the leaf bonus outright rather than subtracting from it: a
# department's news feed is full of "residents-attended-ao-course" leaves, and a
# fixed penalty still left them ranking above real programme pages.
_NON_ROSTER_SECTIONS = frozenset({
    "news", "blog", "story", "stories", "article", "articles", "press",
    "events", "event", "calendar", "media", "newsroom", "announcements",
    "announce", "uannounce", "publications", "research", "search", "tag",
    "tags", "category", "categories", "archive", "archives", "photos",
    "photo", "gallery", "galleries", "videos", "video", "podcast", "podcasts",
    "awards", "honors", "honours", "spotlight", "spotlights",
})
# A roster page is named, not slugged: "current-residents", "meet-our-fellows",
# "anesthesiology-current-and-past-residents". Past about six tokens the leaf is
# an article headline that happens to contain "residents" or "fellows" — which
# is how a university news item about a fellowship award came to outrank half
# the departmental rosters on the site.
_MAX_ROSTER_LEAF_TOKENS = 6

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
    # Pages *about* recruitment rather than pages listing the people here now.
    # Without these, "prospective-residents-fellows" outranked half the real
    # rosters on the site.
    "prospective": -7.0, "applicant": -6.0, "applicants": -6.0,
    "visiting": -5.0, "recruitment": -5.0, "salaries": -5.0, "benefits": -4.0,
    "testimonials": -3.0, "curriculum": -3.0, "rotations": -3.0,
    "moonlighting": -4.0, "faq": -5.0, "handbook": -4.0, "eligibility": -5.0,
}
# Phrases still matched against the page *title*, where there is no path
# structure to lean on.
_TITLE_SIGNALS: dict[str, float] = {
    "current residents": 10.0, "our residents": 10.0, "meet our residents": 10.0,
    "meet the residents": 10.0, "resident directory": 10.0, "house staff": 9.5,
    "housestaff": 9.5, "residents and fellows": 9.5, "current fellows": 9.5,
    "our fellows": 9.5, "meet our fellows": 9.5, "resident roster": 9.0,
    "residents": 9.0, "fellows": 9.0, "trainees": 8.0, "class of": 8.0,
    "faculty directory": 7.5, "our faculty": 7.0, "directory": 7.0,
    "roster": 7.0, "people": 6.5, "our team": 6.0, "faculty": 6.5,
    "profiles": 6.0, "residency": 5.5, "fellowship": 5.5, "alumni": 5.0,
    "graduates": 5.0, "former residents": 5.5, "who we are": 4.5,
    "team": 4.0, "staff": 4.0,
}
_BAD_EXTENSIONS = re.compile(
    r"\.(pdf|docx?|xlsx?|pptx?|zip|jpg|jpeg|png|gif|svg|mp4|mp3|css|js|xml|json)$", re.IGNORECASE
)
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")
_YEAR_IN_PATH = re.compile(r"/(19|20)\d{2}(/|$)")
_YEAR = re.compile(r"^(19|20)\d{2}$")


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


def _path_signal(path: str) -> tuple[float, list[str]]:
    """Positive score for a path, driven by its last segment.

    The leaf names the page ("current-and-past-residents"); the ancestors only
    say where it sits ("/education/residency-program/"). Weighting them equally
    is what made a programme brochure indistinguishable from its own roster, so
    the leaf carries the signal and ancestors add a small context bonus.
    """
    segments = [s for s in path.strip("/").split("/") if s]
    if not segments:
        return 0.0, []

    score = 0.0
    reasons: list[str] = []
    leaf_tokens = _tokens(segments[-1])
    leaf = set(leaf_tokens)
    ancestors: set[str] = set()
    for segment in segments[:-1]:
        ancestors.update(_tokens(segment))

    blocked = ancestors & _NON_ROSTER_SECTIONS
    if blocked:
        return 0.0, [f"leaf signal vetoed by section:{min(blocked)}"]

    if len(leaf_tokens) > _MAX_ROSTER_LEAF_TOKENS:
        return 0.0, ["leaf is an article slug, not a page name"]

    trainees = leaf & _TRAINEE_NOUNS
    if trainees:
        score += 10.0
        reasons.append(f"+10 leaf trainee:{min(trainees)}")
        qualifiers = leaf & _ROSTER_QUALIFIERS
        if qualifiers:
            # "current-residents" is a roster; a bare "/residents" is as often
            # the programme's landing page.
            score += 2.5
            reasons.append(f"+2.5 leaf qualifier:{min(qualifiers)}")
    else:
        best = max(
            ((weight, noun) for noun, weight in _ROSTER_NOUNS.items() if noun in leaf),
            default=None,
        )
        if best:
            score += best[0]
            reasons.append(f"+{best[0]:g} leaf roster:{best[1]}")

    # "class-2028" and "class-of-2027" are per-year roster pages.
    if leaf & {"class", "classes"} and any(_YEAR.match(t) for t in leaf):
        score += 8.0
        reasons.append("+8 leaf class year")

    context = ancestors & _TRAINING_CONTEXT
    if context:
        score += 2.0
        reasons.append(f"+2 path context:{min(context)}")
    elif ancestors & _EDUCATION_CONTEXT:
        score += 1.0
        reasons.append("+1 path context:education")

    return score, reasons


def score_url(
    url: str,
    *,
    title: str | None = None,
    known_path_score: float | None = None,
    site_specialties: set[str] | None = None,
) -> ScoredUrl:
    """Score one candidate. Higher is more likely to hold contact information."""
    canonical = canonicalize(url)
    if canonical is None:
        # Unparseable or non-HTTP. Falling back to the raw string here meant the
        # `urlsplit` below raised on exactly the URLs canonicalization had just
        # rejected, taking the whole site run with it.
        return ScoredUrl(url, -100.0, ("not a fetchable url",))

    reasons: list[str] = []
    score = 0.0

    if _BAD_EXTENSIONS.search(canonical.split("?")[0]):
        return ScoredUrl(canonical, -100.0, ("non-html asset",))

    from urllib.parse import urlsplit

    parts = urlsplit(canonical)
    path = parts.path.lower()

    path_score, path_reasons = _path_signal(path)
    score += path_score
    reasons.extend(path_reasons)

    path_tokens = set(_tokens(path))
    for token, weight in _NEGATIVE_SIGNALS.items():
        if token in path_tokens:
            score += weight
            reasons.append(f"{weight:g} path:{token}")

    if title:
        title_tokens = set(_tokens(title))
        for phrase, weight in sorted(_TITLE_SIGNALS.items(), key=lambda kv: -len(kv[0])):
            if phrase in title.lower():
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

    # A subdomain that is itself a department is a good sign — but only when the
    # label names one program. `medicine.<univ>.edu` is the whole medical school,
    # so awarding it here handed the same +2 to every one of the site's 6,000
    # pages, lifting all of them over the candidate floor and crowding the real
    # rosters out of the ranked list.
    host_labels = (parts.hostname or "").split(".")
    if (
        len(host_labels) > 2
        and host_labels[0] not in GENERIC_HOST_LABELS
        and normalize_specialty(host_labels[0]).canonical
    ):
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
