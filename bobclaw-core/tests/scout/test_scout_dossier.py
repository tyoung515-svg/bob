from pathlib import Path

import pytest

from core.scout import dossier as dm
from core.scout.dossier import (
    CostDirection,
    CostImpact,
    Dossier,
    Maturity,
    VerificationStatus,
)

FIXTURES = Path(__file__).parent / "fixtures"
VALID_FIXTURES = [
    "dossier_deepseek_pricecut.yaml",
    "dossier_new_model.yaml",
    "dossier_mcp_tool.yaml",
]


# ── valid fixtures: load + round-trip ────────────────────────────────────────
@pytest.mark.parametrize("fixture", VALID_FIXTURES)
def test_fixture_loads_and_validates(fixture):
    d = dm.load_dossier(FIXTURES / fixture)
    assert d.schema_version == 0
    assert d.id and d.title
    assert d.claims, "a dossier must carry at least one claim"
    for claim in d.claims:
        assert claim.evidence, "every claim carries evidence"
        assert claim.cost_impact.framing == "EST"


@pytest.mark.parametrize("fixture", VALID_FIXTURES)
def test_dict_round_trip(fixture):
    d = dm.load_dossier(FIXTURES / fixture)
    assert Dossier.from_dict(d.to_dict()) == d


@pytest.mark.parametrize("fixture", VALID_FIXTURES)
def test_yaml_round_trip(fixture):
    d = dm.load_dossier(FIXTURES / fixture)
    assert Dossier.from_yaml_str(d.to_yaml()) == d


@pytest.mark.parametrize("fixture", VALID_FIXTURES)
def test_est_framing_preserved_through_round_trip(fixture):
    d = dm.load_dossier(FIXTURES / fixture)
    dumped = d.to_dict()
    for claim in dumped["claims"]:
        # the EST badge survives serialize->load and is never dropped
        assert claim["cost_impact"]["framing"] == "EST"
    reloaded = Dossier.from_dict(dumped)
    for claim in reloaded.claims:
        assert claim.cost_impact.framing == "EST"
        assert claim.cost_impact.badge.endswith("(EST)")


@pytest.mark.parametrize("fixture", VALID_FIXTURES)
def test_file_round_trip(tmp_path, fixture):
    d = dm.load_dossier(FIXTURES / fixture)
    out = tmp_path / "rt.yaml"
    dm.dump_dossier(d, out)
    assert dm.load_dossier(out) == d


def test_deepseek_fixture_specifics():
    d = dm.load_dossier(FIXTURES / "dossier_deepseek_pricecut.yaml")
    assert d.category.value == "pricing"
    c = d.claims[0]
    assert c.cost_impact.direction == CostDirection.CHEAPER
    assert c.cost_impact.magnitude_usd_month == 18.0
    assert c.maturity == Maturity.GA
    assert c.verification.status == VerificationStatus.CORROBORATED
    assert c.verification.vclass == "claim-ratchet"
    # badge is always EST-badged and carries the magnitude
    assert c.cost_impact.badge == "~$18/mo cheaper (EST)"


def test_badge_variants():
    assert CostImpact(direction=CostDirection.NO_CHANGE).badge == "no cost change (EST)"
    assert CostImpact(direction=CostDirection.UNKNOWN).badge == "cost impact unknown (EST)"
    b = CostImpact(direction=CostDirection.PRICIER, note="EST higher").badge
    assert b == "pricier (EST)"
    b2 = CostImpact(direction=CostDirection.CHEAPER, magnitude_usd_month=5.0).badge
    assert b2.endswith("(EST)") and "5" in b2


# ── negative cases: fail-loud validation ─────────────────────────────────────
def _dossier(**over):
    base = {
        "schema_version": 0,
        "id": "d1",
        "title": "t",
        "category": "pricing",
        "claims": [
            {
                "id": "c1",
                "statement": "s",
                "evidence": [
                    {"source_id": "deepseek-pricing", "url": "https://x.example/p",
                     "excerpt": "e"}
                ],
                "cost_impact": {"direction": "no_change"},
                "maturity": "ga",
            }
        ],
    }
    base.update(over)
    return base


def test_valid_minimal_dossier():
    # the helper baseline itself must validate (guards the negative tests below)
    Dossier.model_validate(_dossier())


def test_bad_schema_version():
    with pytest.raises(ValueError, match="schema_version must be 0"):
        Dossier.model_validate(_dossier(schema_version=1))


def test_empty_claims():
    with pytest.raises(ValueError, match="at least one claim"):
        Dossier.model_validate(_dossier(claims=[]))


def test_duplicate_claim_ids():
    claim = _dossier()["claims"][0]
    dup = dict(claim)
    with pytest.raises(ValueError, match="duplicate claim ids"):
        Dossier.model_validate(_dossier(claims=[claim, dup]))


def test_claim_without_evidence_rejected():
    d = _dossier()
    d["claims"][0]["evidence"] = []
    with pytest.raises(ValueError, match="no evidence"):
        Dossier.model_validate(d)


@pytest.mark.parametrize("bad_url", ["ftp://nope", "httpss://typo.example", "http", ""])
def test_bad_evidence_url_rejected(bad_url):
    d = _dossier()
    d["claims"][0]["evidence"][0]["url"] = bad_url
    with pytest.raises(ValueError, match="must be non-empty and start with http"):
        Dossier.model_validate(d)


def test_uppercase_scheme_accepted():
    # http(s) scheme is case-insensitive — a valid URL must not be wrongly rejected.
    d = _dossier()
    d["claims"][0]["evidence"][0]["url"] = "HTTPS://Example.COM/path"
    Dossier.model_validate(d)


def test_empty_evidence_excerpt_rejected():
    d = _dossier()
    d["claims"][0]["evidence"][0]["excerpt"] = "   "
    with pytest.raises(ValueError, match="excerpt must be non-empty"):
        Dossier.model_validate(d)


def test_bad_maturity_rejected():
    d = _dossier()
    d["claims"][0]["maturity"] = "vaporware"
    with pytest.raises(ValueError):
        Dossier.model_validate(d)


def test_directional_cost_needs_magnitude_or_note():
    d = _dossier()
    d["claims"][0]["cost_impact"] = {"direction": "cheaper"}
    with pytest.raises(ValueError, match="needs a magnitude or a note"):
        Dossier.model_validate(d)


def test_negative_magnitude_rejected():
    d = _dossier()
    d["claims"][0]["cost_impact"] = {"direction": "cheaper", "magnitude_usd_month": -3}
    with pytest.raises(ValueError, match="magnitude_usd_month must be >= 0"):
        Dossier.model_validate(d)


def test_unbadged_framing_rejected():
    d = _dossier()
    d["claims"][0]["cost_impact"] = {"direction": "no_change", "framing": "GUESS"}
    with pytest.raises(ValueError, match="EST"):
        Dossier.model_validate(d)


def test_extra_field_forbidden():
    with pytest.raises(ValueError, match="[Ee]xtra"):
        Dossier.model_validate(_dossier(surprise="x"))
