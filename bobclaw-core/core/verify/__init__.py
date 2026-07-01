"""BoBClaw Core — verification spine (§2.6).

Tier-1 (this package, MS-2): the cross-family **post-condition critic** — an actor declares an
expected post-condition; a decorrelated (different model family) critic verifies that the
post-condition HOLDS given the resulting state, not that the output changed. Tier-2 (claim
entailment + Default-FAIL + ERG) lands in MS-3 on top of this seam.
"""
from __future__ import annotations

from core.verify.postcondition import (  # noqa: F401
    DEFAULT_CRITIC_PREFERENCE,
    FAMILY_BY_BACKEND,
    PCVerdict,
    PostConditionError,
    PostConditionResult,
    build_pc_prompt,
    decorrelated_critic_backend,
    family_of,
    is_decorrelated,
    make_postcondition_verifier,
    parse_pc_verdict,
    verify_post_condition,
)

# MS-3 (§2.6 tier-2): claim-entailment verifier + Default-FAIL termination + the ERG-wired gate.
from core.verify.entailment import (  # noqa: F401
    ENTAILMENT_PROMPT_TEMPLATE,
    Claim,
    EntailmentError,
    EntailmentResult,
    EntailmentVerdict,
    GateOutcome,
    RetrieveRequest,
    Source,
    SourceKind,
    VerificationTag,
    build_entailment_prompt,
    make_entailment_verifier,
    new_gate_entry,
    parse_entailment_verdict,
    run_entailment_gate,
    surface_could_not_verify,
    tag_for,
    verify_claim,
    verify_claim_against_source,
)
from core.verify.termination import (  # noqa: F401
    Criterion,
    could_not_verify,
    criterion_from_outcome,
    default_fail_criteria,
    is_complete,
    termination_decision,
)
