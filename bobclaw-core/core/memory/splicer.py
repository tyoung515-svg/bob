from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.memory._hashing import canonical_json, blake3_hex
from core.memory.exceptions import MemoryConfigError, SpliceFailed
from core.memory.models import Section

if TYPE_CHECKING:
    from core.memory.interfaces import FactStore


class MechanicalSplicer:
    def __init__(
        self, fact_store: FactStore, section_mapping_path: Path
    ) -> None:
        self._fact_store = fact_store
        self._mapping = self._load_mapping(section_mapping_path)

    def _load_mapping(self, path: Path) -> dict:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        meta = raw.get("meta", {})
        if "spec_version" not in meta:
            raise MemoryConfigError(
                "section_mapping.toml must have [meta] spec_version"
            )
        section_block = raw.get("section", {})
        if not section_block:
            raise MemoryConfigError(
                "section_mapping.toml must define at least one section"
            )
        sections = {}
        for section_id, value in section_block.items():
            if "title" not in value or "predicate" not in value:
                raise MemoryConfigError(
                    f"section {section_id!r} requires title and predicate"
                )
            sections[section_id] = {
                "title": value["title"],
                "predicate": value["predicate"],
                "template": value.get("template", "section.j2"),
                "spec_version": meta["spec_version"],
            }
        return sections

    def recompute(self, affected_fact_ids: list[str]) -> list[Section]:
        sections = []
        for section_id, spec in self._mapping.items():
            facts = asyncio.run(
                self._gather_facts_for_section(section_id, spec)
            )
            matching = [f for f in facts if _match_predicate(f, spec["predicate"])]
            input_hash = _compute_section_hash(
                section_id, matching, spec["spec_version"]
            )
            sections.append(
                Section(
                    section_id=section_id,
                    title=spec["title"],
                    fact_ids=sorted(f.fact_id for f in matching),
                    spec_version=spec["spec_version"],
                    input_hash=input_hash,
                )
            )
        return sections

    def get_section(self, section_id: str) -> Section:
        spec = self._mapping.get(section_id)
        if spec is None:
            raise SpliceFailed(
                section_id, f"no such section in mapping"
            )
        facts = asyncio.run(
            self._gather_facts_for_section(section_id, spec)
        )
        matching = [f for f in facts if _match_predicate(f, spec["predicate"])]
        input_hash = _compute_section_hash(
            section_id, matching, spec["spec_version"]
        )
        return Section(
            section_id=section_id,
            title=spec["title"],
            fact_ids=sorted(f.fact_id for f in matching),
            spec_version=spec["spec_version"],
            input_hash=input_hash,
        )

    def all_sections(self) -> list[Section]:
        return self.recompute(list(self._all_fact_ids()))

    def _all_fact_ids(self) -> list[str]:
        return asyncio.run(self._fact_store.all_ids())

    async def _gather_facts_for_section(
        self, section_id: str, spec: dict
    ) -> list:
        try:
            all_ids = await self._fact_store.all_ids()
            facts = []
            for fid in all_ids:
                try:
                    facts.append(await self._fact_store.get(fid))
                except Exception as e:
                    raise SpliceFailed(
                        section_id, f"failed to fetch fact {fid}: {e}"
                    )
            return facts
        except SpliceFailed:
            raise
        except Exception as e:
            raise SpliceFailed(
                section_id, f"failed to gather facts: {e}"
            )


def _match_predicate(fact, predicate: dict) -> bool:
    for key, expected in predicate.items():
        if key == "generation_method":
            if fact.generation_method != expected:
                return False
        elif key.startswith("body_"):
            field = key[5:]
            if fact.body.get(field) != expected:
                return False
        else:
            raise SpliceFailed(
                "splice",
                f"unsupported predicate key: {key!r}",
            )
    return True


def _compute_section_hash(
    section_id: str,
    matching_facts: list,
    spec_version: str,
) -> str:
    body_hashes = [
        blake3_hex(canonical_json(f.body)) for f in matching_facts
    ]
    payload = {
        "section_id": section_id,
        "fact_ids": sorted(f.fact_id for f in matching_facts),
        "fact_body_hashes": body_hashes,
        "spec_version": spec_version,
    }
    return blake3_hex(canonical_json(payload))
