"""R3 (v0.98) — the canonical L1 extraction dedup identity.

Regression guard for the P1 defect where the extractor hashed the *whole* event
body (including a random per-turn ``turn_id`` plus ``cost_usd`` / ``duration_ms``)
for ``input_hash``. Because those fields change every turn, the whole-event dedup
gate never fired and repeating the same fact multiplied L1 facts (the review saw
2 -> 6). The fix hashes only the canonical extraction input (user/assistant text)
plus the extractor/prompt versions.

These are pure unit tests over ``_extraction_identity_input`` and the event-level
dedup gate in ``_dedup_and_build_facts`` — no live Qdrant/LMStudio.
"""
from __future__ import annotations

import pytest

from core.memory._hashing import compute_input_hash
from core.memory.extractor import (
    FactExtractor,
    _EXTRACTOR_VERSION,
    _GENERATION_METHOD,
    _PROMPT_VERSION,
    _extraction_identity_input,
)
from core.memory.models import ConfidenceStub, Event, Fact


class _StubSlotResolver:
    class _Resolution:
        backend = "lmstudio"
        endpoint = "http://localhost:1234"
        model = "gemma-4-e4b-it"

    def get(self, name: str) -> "_StubSlotResolver._Resolution":
        return self._Resolution()


class _StubFactStore:
    def __init__(self, existing: list[Fact] | None = None) -> None:
        self.existing = existing or []

    async def query(self, filters: dict) -> list[Fact]:
        return self.existing


def _turn_event(
    *,
    turn_id: str,
    user: str = "I work as a marine biologist at UCSB.",
    assistant: str = "Noted — marine biologist at UCSB.",
    event_id: str = "evt",
    cost_usd: float | None = None,
    duration_ms: int | None = None,
) -> Event:
    """An agent-turn event shaped exactly like the real _l0_events body: the
    semantic text plus the volatile per-turn provenance the bug hashed."""
    return Event(
        event_id=event_id,
        kind="agent_turn",
        body={
            "user_message": user,
            "assistant_response": assistant,
            "face_id": "assistant",
            "turn_id": turn_id,               # volatile — random every turn
            "cost_usd": cost_usd,             # volatile
            "duration_ms": duration_ms,       # volatile
            "model_capability_class": "synth_mid",
            "error": None,
        },
        ts="2026-07-10T00:00:00+00:00",
        hash="h",
        prev_hash=None,
    )


def _hash_for(event: Event) -> str:
    return compute_input_hash(
        _GENERATION_METHOD,
        {
            "event.extraction_input": _extraction_identity_input(event),
            "event.kind": event.kind,
            "extractor.version": _EXTRACTOR_VERSION,
            "prompt.version": _PROMPT_VERSION,
        },
    )


class TestCanonicalIdentity:
    def test_excludes_volatile_turn_metadata(self):
        """Same user/assistant text, different turn_id/cost/duration -> SAME key."""
        e1 = _turn_event(turn_id="aaaa", event_id="evt1", cost_usd=0.01, duration_ms=100)
        e2 = _turn_event(turn_id="bbbb", event_id="evt2", cost_usd=0.99, duration_ms=9999)
        assert e1.body["turn_id"] != e2.body["turn_id"]
        assert _hash_for(e1) == _hash_for(e2)

    def test_material_fact_change_changes_key(self):
        base = _turn_event(turn_id="aaaa")
        other = _turn_event(turn_id="aaaa", user="I work as an astronomer at Caltech.")
        assert _hash_for(base) != _hash_for(other)

    def test_prompt_version_changes_key(self, monkeypatch: pytest.MonkeyPatch):
        e = _turn_event(turn_id="aaaa")
        before = _hash_for(e)
        monkeypatch.setattr("core.memory.extractor._PROMPT_VERSION", "v-next")
        # recompute using the patched module constant
        import core.memory.extractor as ex

        after = compute_input_hash(
            _GENERATION_METHOD,
            {
                "event.extraction_input": _extraction_identity_input(e),
                "event.kind": e.kind,
                "extractor.version": ex._EXTRACTOR_VERSION,
                "prompt.version": ex._PROMPT_VERSION,
            },
        )
        assert before != after

    def test_identity_input_has_no_volatile_fields(self):
        ident = _extraction_identity_input(_turn_event(turn_id="aaaa"))
        assert set(ident) == {"user_message", "assistant_response"}


class TestEventLevelDedupGate:
    @pytest.mark.asyncio
    async def test_repeat_turn_with_new_turn_id_dedups(self):
        """The whole-event dedup gate fires across two turns whose only
        difference is volatile provenance — the fix's core behaviour."""
        turn1 = _turn_event(turn_id="first", event_id="evt1")
        seeded = Fact(
            fact_id="f_seed",
            generation_method=_GENERATION_METHOD,
            body={"text": "marine biologist at UCSB"},
            source_event_id="evt1",
            input_hash=_hash_for(turn1),
            confidence=ConfidenceStub(),
            ts="2026-07-10T00:00:00+00:00",
        )
        extractor = FactExtractor(_StubSlotResolver(), _StubFactStore([seeded]))

        turn2 = _turn_event(turn_id="second", event_id="evt2")
        new_facts = await extractor._dedup_and_build_facts(
            [{"text": "marine biologist at UCSB"}], turn2
        )
        assert new_facts == []  # deduped despite a fresh turn_id
