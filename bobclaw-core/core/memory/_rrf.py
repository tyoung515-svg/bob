from __future__ import annotations

from core.memory.models import Hit


def rrf_fuse(rankings: list[list[Hit]], k: int = 60) -> list[Hit]:
    fused_scores: dict[str, float] = {}
    hit_map: dict[str, Hit] = {}

    for provider_ranking in rankings:
        for rank, hit in enumerate(provider_ranking):
            item_id = hit.id
            fused_scores[item_id] = fused_scores.get(item_id, 0.0) + 1.0 / (k + rank + 1)
            if item_id not in hit_map:
                hit_map[item_id] = hit

    sorted_items = sorted(
        hit_map.items(),
        key=lambda item: (-fused_scores[item[0]], item[0]),
    )

    return [
        Hit(id=item_id, score=fused_scores[item_id], payload=h.payload)
        for item_id, h in sorted_items
    ]
