"""Stable, user-facing explanations for directory failures."""


def directory_error(reason: str) -> tuple[str, str]:
    detail = reason.lower()
    if any(word in detail for word in ("signing in", "sign-in", "login", "401")):
        return "DIRECTORY_LOGIN_REQUIRED", "The school directory requires sign-in. Automated directory search cannot access it."
    if "429" in detail or "rate limit" in detail:
        return "DIRECTORY_RATE_LIMITED", "The school directory is rate-limiting requests. Try again later."
    if "403" in detail or "blocked" in detail:
        return "DIRECTORY_BLOCKED", "The school directory blocked automated access. Try again later or check the directory directly."
    if "timeout" in detail or "timed out" in detail:
        return "DIRECTORY_TIMEOUT", "The school directory did not respond in time. Try again later."
    if "browser" in detail:
        return "DIRECTORY_BROWSER_REQUIRED", "This directory requires a browser, but the server could not provide one."
    if "search box" in detail or "search form" in detail:
        return "DIRECTORY_SEARCH_UNSUPPORTED", "No working public people-search form was found. Check the school's directory link."
    if "listed but not read" in detail:
        return "DIRECTORY_RESULTS_UNREADABLE", "The directory returned results, but no reliable person details could be read."
    return "DIRECTORY_UNAVAILABLE", "The school directory could not be searched. Try again later or check the school's directory link."
