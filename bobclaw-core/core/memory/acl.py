from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from core.memory.exceptions import ACLViolation, MemoryConfigError

logger = logging.getLogger("bobclaw.memory.security")

_LOCALITY_VALUES: frozenset[str] = frozenset({"local", "remote"})


@dataclass(frozen=True)
class StoreACL:
    store_id: str
    allowed_locality: frozenset[Literal["local", "remote"]]
    allowed_provider_ids: frozenset[str]
    allowed_capability_classes: frozenset[str]


class ACLRegistry:
    def __init__(self, stores_file: Path) -> None:
        self._stores: dict[str, StoreACL] = _parse_stores_file(stores_file)

    def get(self, store_id: str) -> StoreACL:
        acl = self._stores.get(store_id)
        if acl is None:
            raise ACLViolation(store_id, "unknown store")
        return acl

    def enforce(
        self,
        store_id: str,
        provider_id: str,
        locality: str,
        capability_class: str,
    ) -> None:
        try:
            acl = self.get(store_id)
        except ACLViolation:
            logger.warning(
                "ACL violation for store_id=%s provider_id=%s reason=unknown_store",
                store_id,
                provider_id,
            )
            raise

        reasons: list[str] = []
        if provider_id not in acl.allowed_provider_ids:
            reasons.append(f"provider_id {provider_id!r} not in allowlist")
        if locality not in acl.allowed_locality:
            reasons.append(f"locality {locality!r} not in allowlist")
        if capability_class not in acl.allowed_capability_classes:
            reasons.append(
                f"capability_class {capability_class!r} not in allowlist"
            )

        if reasons:
            reason_str = "; ".join(reasons)
            logger.warning(
                "ACL violation for store_id=%s provider_id=%s locality=%s "
                "capability_class=%s reason=%s",
                store_id,
                provider_id,
                locality,
                capability_class,
                reason_str,
            )
            raise ACLViolation(store_id, reason_str)


def _parse_stores_file(path: Path) -> dict[str, StoreACL]:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    store_section = raw.get("store", {})
    stores: dict[str, StoreACL] = {}
    for store_id, value in store_section.items():
        if not isinstance(value, dict):
            raise MemoryConfigError(f"store {store_id!r} must be a table")
        missing = [
            k
            for k in (
                "allowed_locality",
                "allowed_provider_ids",
                "allowed_capability_classes",
            )
            if k not in value
        ]
        if missing:
            raise MemoryConfigError(
                f"store {store_id!r} missing required keys: {missing}"
            )
        locality_raw = value["allowed_locality"]
        if not isinstance(locality_raw, list):
            raise MemoryConfigError(
                f"store {store_id!r} allowed_locality must be a list"
            )
        for loc in locality_raw:
            if loc not in _LOCALITY_VALUES:
                raise MemoryConfigError(
                    f"store {store_id!r} invalid locality value: {loc!r}"
                )
        stores[store_id] = StoreACL(
            store_id=store_id,
            allowed_locality=frozenset(locality_raw),
            allowed_provider_ids=frozenset(value["allowed_provider_ids"]),
            allowed_capability_classes=frozenset(
                value["allowed_capability_classes"]
            ),
        )
    return stores
