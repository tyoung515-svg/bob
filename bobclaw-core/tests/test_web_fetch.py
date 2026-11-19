"""
BoBClaw Core — web_fetch (read-only retrieval) unit tests.

Nothing here touches the network: ``_open`` and ``_addresses`` are the only two
seams that reach outside the process, and every test replaces both. The tests
pin the boundary, not just the happy path — scheme, allow-list, credentials,
private addresses, redirect hops and size/shape of what comes back.
"""
from __future__ import annotations

import json

import pytest

import core.tools.web as web


def _call(url: str) -> dict:
    return json.loads(web.web_fetch.invoke({"url": url}))


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    monkeypatch.setattr(web, "_addresses", lambda host: ["93.184.216.34"])


def test_https_only(monkeypatch):
    result = _call("http://huggingface.co/models")
    assert result["ok"] is False
    assert "https" in result["error"]


def test_host_must_be_on_the_allow_list(monkeypatch):
    result = _call("https://example.com/anything")
    assert result["ok"] is False
    assert "allow-list" in result["error"]


def test_credentials_are_refused():
    result = _call("https://user:pass@huggingface.co/models")
    assert result["ok"] is False
    assert "credentials" in result["error"]


def test_private_address_is_refused(monkeypatch):
    monkeypatch.setattr(web, "_addresses", lambda host: ["10.0.0.7"])
    result = _call("https://huggingface.co/api/models")
    assert result["ok"] is False
    assert "non-public" in result["error"]


def test_html_is_reduced_to_readable_text(monkeypatch):
    html = b"<html><head><style>x{}</style><script>bad()</script></head><body><h1>Releases</h1><p>New model &amp; notes</p></body></html>"
    monkeypatch.setattr(web, "_open", lambda url: (html, "text/html", 200))
    result = _call("https://huggingface.co/models")
    assert result["ok"] is True
    assert "Releases" in result["text"]
    assert "New model & notes" in result["text"]
    assert "bad()" not in result["text"]
    assert "<" not in result["text"]


def test_json_is_returned_as_text(monkeypatch):
    payload = json.dumps([{"id": "meta-llama/Llama-4.1-8B", "createdAt": "2026-10-20"}]).encode()
    monkeypatch.setattr(web, "_open", lambda url: (payload, "application/json", 200))
    result = _call("https://huggingface.co/api/models?sort=createdAt")
    assert result["ok"] is True
    assert "meta-llama/Llama-4.1-8B" in result["text"]
    assert result["content_type"] == "application/json"


def test_a_redirect_to_a_refused_host_is_not_followed(monkeypatch):
    def redirecting(url):
        if url.endswith("/start"):
            raise web._Redirect("https://example.com/elsewhere")
        raise AssertionError("should not fetch the redirect target")

    monkeypatch.setattr(web, "_open", redirecting)
    result = _call("https://huggingface.co/start")
    assert result["ok"] is False
    assert "allow-list" in result["error"]


def test_a_redirect_inside_the_allow_list_is_followed(monkeypatch):
    def redirecting(url):
        if url.endswith("/start"):
            raise web._Redirect("https://huggingface.co/api/models")
        return (b'{"ok":true}', "application/json", 200)

    monkeypatch.setattr(web, "_open", redirecting)
    result = _call("https://huggingface.co/start")
    assert result["ok"] is True
    assert result["url"] == "https://huggingface.co/api/models"


def test_environment_can_extend_the_allow_list(monkeypatch):
    monkeypatch.setenv("BOB_WEB_FETCH_HOSTS", "example.com, news.example.org")
    monkeypatch.setattr(web, "_open", lambda url: (b"hello", "text/plain", 200))
    result = _call("https://example.com/page")
    assert result["ok"] is True
    assert result["text"] == "hello"


def test_large_text_is_truncated(monkeypatch):
    body = ("A" * (web.MAX_CHARS + 500)).encode()
    monkeypatch.setattr(web, "_open", lambda url: (body, "text/plain", 200))
    result = _call("https://en.wikipedia.org/wiki/Test")
    assert result["ok"] is True
    assert "truncated at" in result["text"]
    assert result["truncated"] is True
