"""P1 — notebook-grounded surface (MS#4). SQLite = system-of-record; LKS = retrieval +
provenance (ONE collection partitioned by notebook_id). Slice 1: the NotebookBoundProvider
isolation spine (F-1 import boundary) — every retrieval is scoped to its notebook by construction.
"""
from core.surface.provider import NotebookBoundProvider  # noqa: F401
from core.surface.store import SurfaceStore, SurfaceStoreError  # noqa: F401
from core.surface.grounded import GroundedRetrieval  # noqa: F401
from core.surface.router import (  # noqa: F401
    RoutingDecision,
    cosine,
    decode_centroid,
    encode_centroid,
    route_query,
    routing_eval,
)
from core.surface.escalation import (  # noqa: F401
    EscalationRequest,
    EscalationTier,
    plan_escalation,
)
from core.surface.synthesis import (  # noqa: F401
    SynthesisApprovalRequest,
    SynthesisError,
    SynthesisGrant,
    SynthesisRetrieval,
    create_synthesis_grant,
    request_synthesis,
)
