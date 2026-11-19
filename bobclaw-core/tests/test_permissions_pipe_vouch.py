import hashlib
import time
import uuid

import pytest
from core.permissions import compute_pipe_vouch, verify_pipe_vouch, canonical_pipe_json


def claims():
    now = int(time.time())
    return dict(iss="core", aud="gateway", sub="alice", face="assistant",
                conversation_id="conversation", op="runs.get",
                args_sha256=hashlib.sha256(canonical_pipe_json({"run_id": "run"}).encode()).hexdigest(),
                iat=now, exp=now + 60, nonce=uuid.uuid4().hex)


def test_round_trip_and_tampering():
    c = claims()
    v = compute_pipe_vouch(c, "test-secret")
    assert verify_pipe_vouch(c, v, "test-secret")
    assert not verify_pipe_vouch(c, v, "wrong-secret")
    assert not verify_pipe_vouch(dict(c, sub="other"), v, "test-secret")


@pytest.mark.parametrize("change", [
    {"iss": "gateway"}, {"aud": "core"}, {"exp": 1}, {"iat": True},
    {"exp": int(time.time()) - 1, "iat": int(time.time()) - 30},
    {"extra": "authority"}, {"nonce": []},
])
def test_invalid_claims(change):
    c = dict(claims(), **change)
    assert not verify_pipe_vouch(c, compute_pipe_vouch(c, "s"), "s")


@pytest.mark.parametrize("secret", ["", " ", None, 123])
def test_bad_secret(secret):
    c = claims()
    assert not compute_pipe_vouch(c, secret)
    assert not verify_pipe_vouch(c, "vouch", secret)
