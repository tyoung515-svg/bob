import pytest
import core.watch.registry as reg

def test_load_sources_default():
    r = reg.load_sources()
    assert r.version == 0
    assert len(r.sources) == 10
    expected_ids = [
        "anthropic-news",
        "anthropic-pricing",
        "openai-pricing",
        "google-ai-updates",
        "deepseek-news",
        "llamacpp-releases",
        "hf-trending",
        "mcp-servers",
        "localllama",
        "hn-frontpage",
    ]
    assert r.ids() == expected_ids
    assert r.get("anthropic-news").kind == "changelog"
    with pytest.raises(KeyError, match="unknown source id: unknown"):
        r.get("unknown")
    assert r.schedule is not None
    assert r.schedule.cron == "0 8 * * 1"
    assert r.schedule.task == "run_watch"

def test_duplicate_source_ids():
    data = {
        "version": 0,
        "sources": [
            {"id": "dup", "name": "a", "kind": "changelog", "url": "http://example.com"},
            {"id": "dup", "name": "b", "kind": "changelog", "url": "http://example2.com"},
        ],
        "schedule": None,
    }
    with pytest.raises(ValueError, match="duplicate source id"):
        reg.SourcesRegistry.model_validate(data)

def test_unknown_kind():
    data = {
        "version": 0,
        "sources": [{"id": "x", "name": "x", "kind": "invalid", "url": "http://example.com"}],
        "schedule": None,
    }
    with pytest.raises(ValueError, match="not a valid source kind"):
        reg.SourcesRegistry.model_validate(data)

def test_auth_required_true():
    data = {
        "version": 0,
        "sources": [
            {
                "id": "x",
                "name": "x",
                "kind": "changelog",
                "url": "http://example.com",
                "auth_required": True,
            }
        ],
        "schedule": None,
    }
    with pytest.raises(ValueError, match="authed sources require S3 credential custody"):
        reg.SourcesRegistry.model_validate(data)

def test_empty_url():
    data = {
        "version": 0,
        "sources": [{"id": "x", "name": "x", "kind": "changelog", "url": ""}],
        "schedule": None,
    }
    with pytest.raises(ValueError, match="must start with http"):
        reg.SourcesRegistry.model_validate(data)

def test_invalid_url_scheme():
    data = {
        "version": 0,
        "sources": [{"id": "x", "name": "x", "kind": "changelog", "url": "ftp://example.com"}],
        "schedule": None,
    }
    with pytest.raises(ValueError, match="must start with http"):
        reg.SourcesRegistry.model_validate(data)

def test_bad_cron():
    data = {
        "version": 0,
        "sources": [{"id": "x", "name": "x", "kind": "changelog", "url": "http://example.com"}],
        "schedule": {"cron": "invalid", "task": "run_watch"},
    }
    with pytest.raises(ValueError, match="invalid cron"):
        reg.SourcesRegistry.model_validate(data)

def test_version_not_zero():
    data = {
        "version": 1,
        "sources": [{"id": "x", "name": "x", "kind": "changelog", "url": "http://example.com"}],
        "schedule": None,
    }
    with pytest.raises(ValueError, match="version must be 0"):
        reg.SourcesRegistry.model_validate(data)

def test_empty_sources():
    data = {"version": 0, "sources": [], "schedule": None}
    with pytest.raises(ValueError, match="sources cannot be empty"):
        reg.SourcesRegistry.model_validate(data)

def test_schedule_profile_default():
    r = reg.load_sources()
    profile = r.schedule_profile("scout-weekly")
    assert profile == {
        "name": "scout-weekly",
        "schedule": {
            "cron": "0 8 * * 1",
            "task": "run_watch",
            "face_hint": "assistant",
            "owner": "admin",
        },
    }

def test_recurrence_binding():
    from core.scheduler import build_schedule_seed

    r = reg.load_sources()
    prof = r.schedule_profile("scout-weekly")
    seed = build_schedule_seed(
        {"name": prof["name"]},
        prof["schedule"],
        "2026-07-07T08:00:00+00:00",
    )
    assert seed["task"] == "run_watch"
    assert seed["conversation_id"] == "sched:scout-weekly:2026-07-07T08:00:00+00:00"
    assert seed["approval_required"] is False
