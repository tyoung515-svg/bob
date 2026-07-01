from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

from core.memory.models import ConfidenceStub

HALF_LIFE_DAYS: dict[str, float | None] = {
    "stable_biographical": None,
    "current_role": 182.5,
    "recent_status": 7.0,
    "preference": 30.0,
    "version_dependent": 14.0,
    "event_factual": None,
}


def _parse_ts(ts: str | None) -> datetime | None:
    if ts is None:
        return None
    cleaned = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(cleaned)


def decay_alpha(
    alpha: float,
    last_corroboration_ts: str | None,
    decay_class: str,
    now: datetime,
) -> float:
    half_life = HALF_LIFE_DAYS.get(decay_class)
    if half_life is None:
        return alpha

    ts = _parse_ts(last_corroboration_ts)
    if ts is None:
        return alpha

    elapsed = (now - ts).total_seconds() / 86400.0
    if elapsed <= 0:
        return alpha

    factor = math.exp(-elapsed * math.log(2) / half_life)
    return alpha * factor


def credibility_mean(confidence: ConfidenceStub, now: Optional[datetime] = None) -> float:
    if now is None:
        now = datetime.now(timezone.utc)

    alpha_d = decay_alpha(
        confidence.alpha,
        confidence.last_corroboration_ts,
        confidence.decay_class,
        now,
    )
    beta = confidence.beta

    denom = alpha_d + beta
    if denom == 0:
        return 0.0

    mean = alpha_d / denom
    return max(0.0, min(1.0, mean))
