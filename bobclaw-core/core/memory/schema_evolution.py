from __future__ import annotations

import inspect
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from core.memory.exceptions import SchemaEvolutionError

_UPCASTERS: dict[tuple[str, str, str], Callable[[dict], dict]] = {}

CURRENT_SCHEMA_VERSION = "2.0"


def register_upcaster(
    *,
    generation_method: str,
    from_version: str,
    to_version: str,
) -> Callable[[Callable], Callable]:
    def decorator(func: Callable) -> Callable:
        sig = inspect.signature(func)
        if len(sig.parameters) != 1:
            raise SchemaEvolutionError(
                f"upcaster {func.__name__} must accept exactly 1 argument, "
                f"got {len(sig.parameters)}"
            )
        key = (generation_method, from_version, to_version)
        if key in _UPCASTERS:
            raise SchemaEvolutionError(
                f"duplicate upcaster registration for "
                f"{generation_method} v{from_version}->v{to_version}"
            )
        _UPCASTERS[key] = func
        return func

    return decorator


def _find_next_upcaster(
    generation_method: str, from_version: str,
) -> tuple[str, Callable[[dict], dict]] | None:
    candidates: list[tuple[str, Callable[[dict], dict]]] = []
    for (method, fv, tv), func in _UPCASTERS.items():
        if method == generation_method and fv == from_version:
            candidates.append((tv, func))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0]


def get_upcaster_chain(
    generation_method: str,
    from_version: str,
    to_version: str,
) -> list[Callable[[dict], dict]]:
    if from_version == to_version:
        return []
    chain: list[Callable[[dict], dict]] = []
    current = from_version
    visited: set[str] = {current}
    while current != to_version:
        step = _find_next_upcaster(generation_method, current)
        if step is None:
            raise SchemaEvolutionError(
                f"no upcaster path from {generation_method} "
                f"v{from_version} to v{to_version}: "
                f"stuck at v{current}"
            )
        target_v, func = step
        if target_v in visited:
            raise SchemaEvolutionError(
                f"cyclic upcaster chain detected for {generation_method} "
                f"v{from_version}->v{to_version}: v{current}->v{target_v} repeats"
            )
        chain.append(func)
        visited.add(target_v)
        current = target_v
    return chain


def apply_upcaster_chain(
    chain: list[Callable[[dict], dict]],
    data: dict,
) -> dict:
    current = data
    for func in chain:
        result = func(current)
        if not isinstance(result, dict):
            raise SchemaEvolutionError(
                f"upcaster {func.__name__} returned non-dict: "
                f"{type(result).__name__}"
            )
        current = result
    return current


def upgrade_body_to_latest(
    generation_method: str,
    body: dict,
) -> dict:
    version = body.get("_schema_version", "1.0")
    if version == CURRENT_SCHEMA_VERSION:
        return body
    chain = get_upcaster_chain(generation_method, version, CURRENT_SCHEMA_VERSION)
    return apply_upcaster_chain(chain, body)


# Stub registrations for section-split / section-merge / type-change
# These register successfully per Hard Rule 13 but raise on apply.

@register_upcaster(
    generation_method="*section_split*",
    from_version="1.0",
    to_version="2.0",
)
def _section_split_upcaster(old: dict) -> dict:
    raise SchemaEvolutionError("section-split is Phase 2 deferred")


@register_upcaster(
    generation_method="*section_merge*",
    from_version="1.0",
    to_version="2.0",
)
def _section_merge_upcaster(old: dict) -> dict:
    raise SchemaEvolutionError("section-merge is Phase 2 deferred")


@register_upcaster(
    generation_method="*type_change*",
    from_version="1.0",
    to_version="2.0",
)
def _type_change_upcaster(old: dict) -> dict:
    raise SchemaEvolutionError("type-change is Phase 2 deferred")
