"""Visits' rules (src/server/visits.py, ADR-025): where a visitor came from is a site's name and nothing else, a
crawler or a script is a bot, and a visitor is a hash that changes with the day's salt. End to end:
tests/test_visits.py."""

from __future__ import annotations

import pytest

from src.server.visits import came_from, is_bot, visitor

pytestmark = pytest.mark.fast


def test_where_a_visitor_came_from_is_a_sites_name_or_nothing():
    for given, kept in (("www.Facebook.com", "facebook.com"), ("news.example.org.", "news.example.org"),
                        ("t.co", "t.co"), ("lumiverb.io", "lumiverb.io")):
        assert came_from(given) == kept, given
    for junk in (None, "", "localhost", "javascript:alert(1)", "https://facebook.com/x", "a b.com", "-a.com",
                 "x." * 130 + "com", "face book.com", "100%.com", "<b>.com"):
        assert came_from(junk) == "", junk


def test_a_crawler_a_preview_or_a_script_is_a_bot():
    for agent in ("Mozilla/5.0 (compatible; Googlebot/2.1)", "facebookexternalhit/1.1", "Slackbot-LinkExpanding",
                  "curl/8.7.1", "python-requests/2.32", "Mozilla/5.0 HeadlessChrome/120", ""):
        assert is_bot(agent), agent
    for agent in ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15",
                  "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"):
        assert not is_bot(agent), agent


def test_a_visitor_is_the_same_all_day_and_unknowable_with_another_days_salt():
    who = "203.0.113.7\nMozilla/5.0"
    today, tomorrow = b"a" * 32, b"b" * 32
    assert visitor(today, who) == visitor(today, who)
    assert visitor(today, who) != visitor(tomorrow, who)
    assert len(visitor(today, who)) == 32 and "203.0.113.7" not in visitor(today, who)
