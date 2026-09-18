"""One bad link, or one host that says no, must not end a crawl.

Both failures here cost whole runs during benchmarking: a malformed href aborted
an Arizona crawl 848 steps in, and Baylor Scott & White's bot protection ended
that crawl after 120 of its 1,500 steps with most of its rosters unvisited.
"""

from __future__ import annotations

import pytest

from agentscrape.browser.ratelimit import DomainRateLimiter
from agentscrape.discovery.scoring import rank_candidates, score_url
from agentscrape.urls import canonicalize, host_of, path_depth

MALFORMED = [
    "https://[bad ipv6/x",          # ValueError: Invalid IPv6 URL
    "https://example.com:99999/x",  # ValueError: Port out of range
    "https://[::1/unclosed",
]


class TestMalformedUrlsAreNotFatal:
    @pytest.mark.parametrize("url", MALFORMED)
    def test_canonicalize_returns_none_instead_of_raising(self, url):
        assert canonicalize(url) is None

    @pytest.mark.parametrize("url", MALFORMED)
    def test_the_url_helpers_do_not_raise(self, url):
        host_of(url)
        path_depth(url)

    @pytest.mark.parametrize("url", MALFORMED)
    def test_scoring_rejects_rather_than_raising(self, url):
        assert score_url(url).score < 0

    def test_one_bad_link_does_not_lose_the_good_ones(self):
        roster = "https://ok.edu/residency/current-residents"
        ranked = rank_candidates([*MALFORMED, roster], min_score=1.0)
        assert [r.url for r in ranked] == [roster]


    def test_a_bad_relative_href_is_survived_too(self):
        """`urljoin` parses as well, so guarding only `urlsplit` fixed nothing.

        The Arizona crawl failed a second time at the same step, on the same
        link, because the crash had simply moved one line earlier.
        """
        assert canonicalize("//[oops", base="https://ok.edu/page") is None
        assert canonicalize("/fine", base="https://ok.edu/page") == "https://ok.edu/fine"

    def test_link_extraction_keeps_the_good_links_on_a_page_with_a_bad_one(self):
        from agentscrape.discovery.sitemap import extract_links

        html = (
            '<a href="//[oops">x</a>'
            '<a href="/good/current-residents">y</a>'
            '<a href="https://[bad">z</a>'
        )
        assert extract_links(html, "https://ok.edu/page") == [
            "https://ok.edu/good/current-residents"
        ]


class TestBackingOffWhenAHostRefuses:
    def _refuse(self, limiter, host, times):
        for _ in range(times):
            limiter.note_throttled(host)

    def test_sustained_refusals_halve_the_rate_for_that_host_only(self):
        limiter = DomainRateLimiter(2.0)
        self._refuse(limiter, "salud.bswhealth.com", 3)
        assert limiter.rate_for("salud.bswhealth.com") == 1.0
        assert limiter.rate_for("medicine.arizona.edu") == 2.0

    def test_scattered_refusals_do_not_slow_the_crawl(self):
        """BCM returned three 403s among 980 successes — a few pages it will not
        serve, not a rate limit. Backing off on each made the crawl four times
        slower for nothing."""
        limiter = DomainRateLimiter(2.0)
        for _ in range(3):
            limiter.note_throttled("www.bcm.edu")
            limiter.note_success("www.bcm.edu")
        assert limiter.rate_for("www.bcm.edu") == 2.0

    def test_a_host_that_stops_refusing_returns_to_full_speed(self):
        limiter = DomainRateLimiter(2.0)
        self._refuse(limiter, "salud.bswhealth.com", 3)
        assert limiter.rate_for("salud.bswhealth.com") == 1.0
        for _ in range(25):
            limiter.note_success("salud.bswhealth.com")
        assert limiter.rate_for("salud.bswhealth.com") == 2.0

    def test_backoff_applies_across_a_hosts_subdomains(self):
        """One institution's twenty departmental subdomains are one server."""
        limiter = DomainRateLimiter(2.0)
        self._refuse(limiter, "salud.bswhealth.com", 3)
        assert limiter.rate_for("www.bswhealth.com") == 1.0

    def test_repeated_refusals_settle_at_a_floor_rather_than_stopping(self):
        limiter = DomainRateLimiter(2.0)
        self._refuse(limiter, "salud.bswhealth.com", 60)
        assert limiter.rate_for("salud.bswhealth.com") > 0
