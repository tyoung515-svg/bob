from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.memory.exceptions import MemoryConfigError, SpliceFailed
from core.memory.models import ConfidenceStub, Fact, Section
from core.memory.splicer import MechanicalSplicer, _match_predicate


_VALID_TOML = """
[meta]
spec_version = "1.0"

[section.user_facts]
title = "User Facts"
predicate = { generation_method = "extract_facts_from_event", body_kind = "user_assertion" }
template = "section.j2"

[section.tool_outputs]
title = "Tool Outputs"
predicate = { generation_method = "extract_facts_from_event", body_kind = "tool_output" }
template = "section.j2"
"""


@pytest.fixture
def mapping_file(tmp_path: Path) -> Path:
    p = tmp_path / "mapping.toml"
    p.write_text(_VALID_TOML, encoding="utf-8")
    return p


def _make_fact(
    fact_id: str,
    generation_method: str = "extract_facts_from_event",
    body_kind: str | None = None,
) -> Fact:
    body = {"text": f"fact {fact_id}"}
    if body_kind is not None:
        body["kind"] = body_kind
    return Fact(
        fact_id=fact_id,
        generation_method=generation_method,
        body=body,
        source_event_id="evt_1",
        input_hash="blake3:" + "a" * 64,
        confidence=ConfidenceStub(),
        ts="2026-01-01T00:00:00+00:00",
    )


@pytest.fixture
def mock_fact_store():
    store = MagicMock()
    store.all_ids = AsyncMock(return_value=["f1", "f2"])

    async def _get(fid: str):
        facts = {
            "f1": _make_fact("f1", body_kind="user_assertion"),
            "f2": _make_fact("f2", body_kind="tool_output"),
        }
        return facts.get(fid)

    store.get = _get
    return store


class TestConstruction:
    def test_constructs_from_valid_toml(self, mapping_file, mock_fact_store):
        splicer = MechanicalSplicer(mock_fact_store, mapping_file)
        assert splicer is not None

    def test_rejects_missing_spec_version(self, tmp_path):
        p = tmp_path / "bad.toml"
        p.write_text("[section.x]\ntitle = 'X'\npredicate = { generation_method = 'test' }", encoding="utf-8")
        with pytest.raises(MemoryConfigError):
            MechanicalSplicer(MagicMock(), p)

    def test_rejects_empty_mapping(self, tmp_path):
        p = tmp_path / "empty.toml"
        p.write_text("[meta]\nspec_version = '1.0'", encoding="utf-8")
        with pytest.raises(MemoryConfigError):
            MechanicalSplicer(MagicMock(), p)


class TestRecompute:
    def test_recompute_returns_sections_with_matching_facts(
        self, mapping_file, mock_fact_store
    ):
        splicer = MechanicalSplicer(mock_fact_store, mapping_file)
        sections = splicer.recompute(["f1", "f2"])
        assert len(sections) == 2
        user = next(s for s in sections if s.section_id == "user_facts")
        tool = next(s for s in sections if s.section_id == "tool_outputs")
        assert user.fact_ids == ["f1"]
        assert tool.fact_ids == ["f2"]

    def test_section_input_hash_bit_identical(
        self, mapping_file, mock_fact_store
    ):
        splicer = MechanicalSplicer(mock_fact_store, mapping_file)
        s1 = splicer.recompute(["f1", "f2"])
        s2 = splicer.recompute(["f1", "f2"])
        assert s1[0].input_hash == s2[0].input_hash
        assert s1[1].input_hash == s2[1].input_hash


class TestGetSection:
    def test_get_section_returns_section(
        self, mapping_file, mock_fact_store
    ):
        splicer = MechanicalSplicer(mock_fact_store, mapping_file)
        section = splicer.get_section("user_facts")
        assert isinstance(section, Section)
        assert section.title == "User Facts"

    def test_get_section_unknown_raises(
        self, mapping_file, mock_fact_store
    ):
        splicer = MechanicalSplicer(mock_fact_store, mapping_file)
        with pytest.raises(SpliceFailed):
            splicer.get_section("nonexistent")


class TestAllSections:
    def test_all_sections_returns_one_per_declared(
        self, mapping_file, mock_fact_store
    ):
        splicer = MechanicalSplicer(mock_fact_store, mapping_file)
        sections = splicer.all_sections()
        assert len(sections) == 2
        assert {s.section_id for s in sections} == {"user_facts", "tool_outputs"}


class TestPredicateMatching:
    def test_facts_not_matching_predicate_excluded(
        self, mapping_file, mock_fact_store
    ):
        mock_fact_store.all_ids = AsyncMock(return_value=["f1", "f2"])

        async def _get(fid: str):
            return _make_fact(fid, body_kind="irrelevant")

        mock_fact_store.get = _get
        splicer = MechanicalSplicer(mock_fact_store, mapping_file)
        sections = splicer.recompute(["f1", "f2"])
        for sec in sections:
            assert sec.fact_ids == []

    def test_unknown_predicate_key_raises(self):
        pred = {"unknown_key": "value"}
        fact = _make_fact("f1")
        with pytest.raises(SpliceFailed):
            _match_predicate(fact, pred)
