"""web_search: find public sources, then let this turn's web_fetch read them.

The pairing is the point. A search tool that cannot be followed by a fetch leaves
the model quoting search snippets as if they were sources, which is exactly the
failure this face is meant to avoid.
"""
from __future__ import annotations

import json

import pytest

from core.tools import web as webmod
from core.tools.web import begin_turn, end_turn, granted_hosts, parse_results, web_fetch, web_search

SEARCH_HTML = """
<tr><td>1.&nbsp;</td><td>
<a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.eesel.ai%2Fblog%2Fre%2Dflint&amp;rut=abc"
   class='result-link'>Recursive <b>Flint</b> review</a>
</td></tr>
<tr><td>&nbsp;</td><td class='result-snippet'>131 tokens/sec on an M4 Pro.</td></tr>
<tr><td>2.&nbsp;</td><td>
<a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fmindstudio.ai%2Fblog%2Fflint"
   class='result-link'>Flint tested</a>
</td></tr>
<tr><td>&nbsp;</td><td class='result-snippet'>Hands-on with llama.cpp.</td></tr>
"""


def test_parse_results_unwraps_and_pairs_snippets():
    results = parse_results(SEARCH_HTML)
    assert [r["host"] for r in results] == ["www.eesel.ai", "mindstudio.ai"]
    assert results[0]["url"] == "https://www.eesel.ai/blog/re-flint"
    assert "131 tokens/sec" in results[0]["snippet"]
    assert "Hands-on" in results[1]["snippet"]
    # HTML tags inside a title are flattened, not returned raw.
    assert "<b>" not in results[0]["title"]


def test_parse_refuses_non_https_and_empty(monkeypatch):
    html = '<a rel="nofollow" href="http://insecure.example/x" class=\'result-link\'>No</a>'
    assert parse_results(html) == []


def test_search_returns_results_and_grants_fetch(monkeypatch):
    monkeypatch.setattr(webmod, "_open", lambda url: (SEARCH_HTML.encode(), "text/html", 200))
    monkeypatch.setattr(webmod, "_addresses", lambda host: ["93.184.216.34"])
    token = begin_turn("test-turn")
    try:
        payload = json.loads(web_search.invoke({"query": "recursive flint review"}))
        assert payload["ok"] is True
        assert payload["count"] == 2
        assert granted_hosts() == {"www.eesel.ai", "mindstudio.ai"}
    finally:
        pass

    # A host the search surfaced is fetchable for the rest of the turn...
    monkeypatch.setattr(
        webmod, "_open", lambda url: (b"<html><body>Flint review</body></html>", "text/html", 200)
    )
    page = json.loads(web_fetch.invoke({"url": "https://www.eesel.ai/blog/re-flint"}))
    assert page["ok"] is True and "Flint review" in page["text"]
    end_turn("test-turn", token)

    # ...and the grant dies with the turn.
    monkeypatch.setattr(webmod, "_addresses", lambda host: ["93.184.216.34"])
    after = json.loads(web_fetch.invoke({"url": "https://www.eesel.ai/blog/re-flint"}))
    assert after["ok"] is False and "allow-list" in after["error"]


def test_search_does_not_grant_arbitrary_hosts(monkeypatch):
    monkeypatch.setattr(webmod, "_open", lambda url: (SEARCH_HTML.encode(), "text/html", 200))
    monkeypatch.setattr(webmod, "_addresses", lambda host: ["93.184.216.34"])
    begin_turn("test-turn-2")
    web_search.invoke({"query": "recursive flint review"})

    refused = json.loads(web_fetch.invoke({"url": "https://evil.example/page"}))
    assert refused["ok"] is False
    assert "allow-list" in refused["error"]


def test_searched_host_still_rejects_private_addresses(monkeypatch):
    monkeypatch.setattr(webmod, "_open", lambda url: (SEARCH_HTML.encode(), "text/html", 200))
    monkeypatch.setattr(webmod, "_addresses", lambda host: ["93.184.216.34"])
    begin_turn("test-turn-3")
    web_search.invoke({"query": "recursive flint review"})

    monkeypatch.setattr(webmod, "_addresses", lambda host: ["127.0.0.1"])
    refused = json.loads(web_fetch.invoke({"url": "https://www.eesel.ai/blog/re-flint"}))
    assert refused["ok"] is False
    assert "non-public" in refused["error"]


def test_search_requires_a_query():
    payload = json.loads(web_search.invoke({"query": "   "}))
    assert payload["ok"] is False
