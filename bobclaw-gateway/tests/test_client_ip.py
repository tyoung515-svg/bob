"""client_ip: socket peer by default; rightmost trusted X-Forwarded-For when enabled.

Regression guard for the reverse-proxy self-DoS: behind a proxy every request's peer
is the proxy, so the per-IP lockout / rate-limit must derive the real client from a
trusted X-Forwarded-For (rightmost hop), and must NOT trust XFF when the feature is off.
"""
from types import SimpleNamespace

import config as config_mod
from client_ip import client_ip


class _Headers:
    def __init__(self, d):
        self._d = d or {}

    def get(self, key, default=""):
        return self._d.get(key, default)


def _req(headers=None, remote="203.0.113.9"):
    # client_ip only touches request.remote and request.headers.get(...)
    return SimpleNamespace(remote=remote, headers=_Headers(headers))


def test_default_ignores_xff_uses_socket_peer(monkeypatch):
    monkeypatch.setattr(config_mod.config, "TRUST_X_FORWARDED_FOR", False)
    assert client_ip(_req({"X-Forwarded-For": "1.2.3.4"}, remote="10.0.0.1")) == "10.0.0.1"


def test_trusted_xff_uses_rightmost_hop(monkeypatch):
    monkeypatch.setattr(config_mod.config, "TRUST_X_FORWARDED_FOR", True)
    # A client-spoofed value sits on the left; the proxy appended the real client last.
    assert client_ip(_req({"X-Forwarded-For": "9.9.9.9, 203.0.113.5"}, remote="10.0.0.1")) == "203.0.113.5"


def test_trusted_but_no_xff_falls_back_to_peer(monkeypatch):
    monkeypatch.setattr(config_mod.config, "TRUST_X_FORWARDED_FOR", True)
    assert client_ip(_req({}, remote="10.0.0.1")) == "10.0.0.1"


def test_no_remote_returns_unknown(monkeypatch):
    monkeypatch.setattr(config_mod.config, "TRUST_X_FORWARDED_FOR", False)
    assert client_ip(_req({}, remote=None)) == "unknown"
