import pytest

from core.scout import sources as sc


def test_load_scout_sources_default():
    r = sc.load_scout_sources()
    assert r.version == 0
    assert len(r.sources) >= 18  # the fuller fleet view (M1)
    # ids are unique (enforced by the shared registry validator)
    assert len(r.ids()) == len(set(r.ids()))


def test_all_seven_fleet_providers_present():
    r = sc.load_scout_sources()
    providers = {s.provider for s in r.sources}
    # every provider BoB actually routes must appear (MODULES.md M1) — including the
    # three the generic watch registry omits: moonshot, zai, minimax.
    for p in ("anthropic", "openai", "google", "moonshot", "zai", "deepseek", "minimax"):
        assert p in providers, f"missing fleet provider source: {p}"


def test_substrate_classes_present():
    r = sc.load_scout_sources()
    kinds = {s.kind for s in r.sources}
    # pricing + changelog + registry + release + community + docs all represented
    assert {"pricing", "changelog", "registry", "release", "community"} <= kinds
    tags = {t for s in r.sources for t in s.tags}
    assert "mcp" in tags          # MCP directories
    assert "benchmark" in tags    # benchmark drops
    assert "local" in tags        # local runtime / registries


def test_no_credentials_in_v0():
    r = sc.load_scout_sources()
    # NO credentials in v0: every source is public.
    assert all(s.auth_required is False for s in r.sources)


def test_pricing_and_changelog_for_each_provider():
    r = sc.load_scout_sources()
    by_provider_kind = {(s.provider, s.kind) for s in r.sources}
    for p in ("anthropic", "openai", "google", "moonshot", "zai", "deepseek", "minimax"):
        assert (p, "pricing") in by_provider_kind, f"{p} missing a pricing source"


def test_schedule_binds_weekly():
    r = sc.load_scout_sources()
    assert r.schedule is not None
    assert r.schedule.cron == "0 8 * * 1"
    profile = r.schedule_profile("scout-weekly")
    assert profile["name"] == "scout-weekly"
    assert profile["schedule"]["task"] == "run_watch"


def test_scout_sources_path_points_at_yaml():
    p = sc.scout_sources_path()
    assert p.name == "sources_v0.yaml"
    assert p.exists()
