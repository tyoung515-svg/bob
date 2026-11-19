"""Bounded checkpoint context. No transcript replay or uncharged summary call."""
from .models import canonical


def context_for(row, outputs, recent_receipts):
    return canonical({
        "original_evidence": row["request"]["task"],
        "evidence_ids": row["request"].get("evidence_ids", []),
        "acceptance": row["request"]["acceptance"],
        "accepted_outputs": [{k: o[k] for k in ("id", "digest", "criteria_ids", "content")} for o in outputs],
        "recent_receipts": recent_receipts[-8:],
    })
