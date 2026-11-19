"""Real gateway/vouch/3.1 owner sessions and both shipping broker contracts."""
import json
from pathlib import Path
import time

import pytest

from auth import create_access_token, create_agent_token, create_portal_token
from tests.test_owner_sessions import owner_h, stepup
from tests.test_execution_broker_v2 import broker
from core.execution.models import Authority
from core.execution.owner import require_owner
from core.permissions import execution_vouch, verify_execution_vouch


async def test_settings_and_flags_capabilities_keep_independent_availability(owner_h, monkeypatch):
    from aiohttp import web
    from routers import execution, settings
    h = owner_h
    monkeypatch.setattr(settings.settings_client, 'unavailable_reason', lambda: None)
    async def unavailable(*args):
        return web.json_response({'storage_available': False, 'execution_enabled': False})
    monkeypatch.setattr(execution, 'proxy', unavailable)
    response = await h.client.get('/capabilities', headers=h.headers,
                                  params={'operations': '1', 'contract_version': '2'})
    assert response.status == 200
    operations = (await response.json())['operations']
    assert operations['owner.begin']['available'] and operations['secrets.set']['available']
    for op in ('flags.resolve', 'policy.get', 'policy.set'):
        assert operations[op]['owner_session_required'] and not operations[op]['available']
    for op in ('flags.list', 'flags.get'):
        assert operations[op]['contract_versions'] == [2]
        assert not operations[op].get('owner_session_required', False)


async def test_flag_reads_use_portal_and_resolution_requires_31_owner_session(owner_h):
    h = owner_h
    for path, params, op, expected in (
        ("/agent-services/flags", {"contract_version": 2, "run_id": "run-ref", "status": "open"}, "flags.list", {"run_id": "run-ref", "status": "open"}),
        ("/agent-services/flags/flag-ref", {"contract_version": 2, "after_event_seq": 9}, "flags.get", {"flag_id": "flag-ref", "after_event_seq": 9}),
    ):
        response = await h.client.get(path, headers=h.headers, params=params)
        assert response.status == 200
        assert h.hits[-1]["envelope"]["operation"] == op and h.hits[-1]["envelope"]["args"] == expected
        assert h.hits[-1]["envelope"]["authority"]["token_type"] == "portal"
    args = {"contract_version": 2, "expected_revision": 4, "request_key": "same-after-reconnect",
            "decision": "inspected", "guidance": "Preserve uncertain effects and inspect worker receipts."}
    before = len(h.hits)
    for token in (create_access_token("owner"), create_portal_token("owner", {}), create_agent_token("owner", faces=["researcher"])):
        response = await h.client.post("/agent-services/flags/flag-ref/resolve", headers={"Authorization": "Bearer " + token}, json=args)
        assert response.status in {403, 404}
    assert len(h.hits) == before
    owner, result = await stepup(h.client, h.headers, operation="flags.resolve", resource={"flag_id": "flag-ref"})
    assert h.hits[-1]["envelope"]["operation"] == "flags.get"  # server-authored review summary
    response = await h.client.post("/agent-services/flags/flag-ref/resolve", headers=owner, json=args)
    assert response.status == 200
    envelope = h.hits[-1]["envelope"]
    assert envelope["operation"] == "flags.resolve"
    assert envelope["args"] == {**{k: v for k, v in args.items() if k != "contract_version"}, "flag_id": "flag-ref"}
    await require_owner(Authority(**envelope["authority"]), "flags.resolve")
    assert "flags.resolve" in result["owner_session"]["allowed_operations"]


@pytest.mark.parametrize("suffix", ["", "/flag-ref"])
async def test_flag_reads_require_explicit_version_and_reject_extra_fields(owner_h, suffix):
    h = owner_h
    before = len(h.hits)
    for params in ({}, {"contract_version": 1}, {"contract_version": 2, "owner": "foreign"}):
        response = await h.client.get("/agent-services/flags" + suffix, headers=h.headers, params=params)
        assert response.status in {403, 422}  # v2 allowlist rejects old clients before proxy
    assert len(h.hits) == before


def test_flag_vouch_binds_identity_decision_and_revision():
    now = int(time.time())
    envelope = {"operation": "flags.resolve", "args": {"flag_id": "flag", "expected_revision": 1,
                "request_key": "key", "decision": "inspected", "guidance": "inspect"}, "authority": {}, "iat": now, "exp": now+30}
    vouch = execution_vouch(envelope, "fixture-secret")
    assert verify_execution_vouch(envelope, vouch, "fixture-secret")
    for key in envelope["args"]:
        changed = json.loads(json.dumps(envelope))
        changed["args"][key] = "forged"
        assert not verify_execution_vouch(changed, vouch, "fixture-secret")


def test_flag_brokers_forward_exact_contract_and_keep_owner_transport_separate(broker):
    module, instance, hits = broker
    ordinary, owner = [], []
    def rest(method, path, **kw):
        ordinary.append((method, path, kw))
        if path == "/capabilities":
            return {"operations": {op: {"contract_versions": [2], "available": True} for op in instance._handlers}}
        return {"source": "server", "items": []}
    instance._rest = rest
    instance.op_flags_list({"contract_version": 2, "run_id": "run-ref", "cursor": "flag-1", "status": "all", "limit": 5})
    assert ordinary[-1][1] == "/agent-services/flags" and ordinary[-1][2]["params"]["run_id"] == "run-ref"
    instance.op_flags_get({"contract_version": 2, "flag_id": "flag-ref", "after_event_seq": 17, "job_cursor": "execution-1"})
    assert ordinary[-1][1].endswith("flag-ref") and ordinary[-1][2]["params"] == {"contract_version": 2, "after_event_seq": 17, "job_cursor": "execution-1"}
    args = {"contract_version": 2, "flag_id": "flag-ref", "expected_revision": 4,
            "request_key": "same-key", "decision": "inspected", "guidance": "inspected"}
    with pytest.raises(module.BrokerError, match="owner_confirmation_required"):
        instance.op_flags_resolve(args)
    instance._owner_rest = lambda method, path, **kw: owner.append((method, path, kw))
    instance.op_flags_resolve(args)
    assert owner[-1][1] == "/agent-services/flags/flag-ref/resolve"
    assert owner[-1][2]["body"] == {k: v for k, v in args.items() if k != "flag_id"}
    assert not any(path.endswith("/resolve") for _, path, _ in ordinary)
    instance._rest = lambda *a, **kw: {"operations": {"runs.start": {"contract_versions": [2]}}}
    with pytest.raises(module.BrokerError, match="client_upgrade_required"):
        instance.op_flags_list({"contract_version": 2})


@pytest.mark.parametrize("operation,args", [
    ("flags.list", {"contract_version": 2}),
    ("flags.get", {"contract_version": 2, "flag_id": "flag-ref"}),
    ("flags.resolve", {"contract_version": 2, "flag_id": "flag-ref", "expected_revision": 1,
                       "request_key": "key", "decision": "inspected", "guidance": "Inspected"}),
])
def test_flag_negotiation_is_independent_per_operation(broker, operation, args):
    module, instance, hits = broker
    instance._rest = lambda *a, **kw: {"operations": {operation: {"contract_versions": [2], "available": True}}}
    instance._owner_rest = lambda *a, **kw: {"resolved": True}
    instance._handlers[operation](args)
    instance._rest = lambda *a, **kw: {"operations": {operation: {"contract_versions": [2], "available": False}}}
    with pytest.raises(module.BrokerError, match="operation_unavailable"):
        instance._handlers[operation](args)
