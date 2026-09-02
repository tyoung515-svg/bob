"""E1 earning layer (MS#4) — the deterministic proposed-contract lifecycle.

`propose → critique → triage → ratify → execute → audit → close`. NO AI model sits in any
gate — every decision is deterministic or an operator write; the git-DAG ledger carries state.
Slice 0: the canonical schema (`contract.py`, vendored in-tree) + triage predicates +
`to_grant()` + the `grant_to_scope` seam to the Gate Router (`core/permissions.py`).
"""
from core.earn.contract import (  # noqa: F401
    AIPolicy,
    BugBountyProfile,
    ContractStatus,
    CriticStance,
    CriticVerdict,
    DatasetProfile,
    Lane,
    PayoffType,
    ProposedContract,
    Provenance,
    RiskDisclosure,
    RiskItem,
    RiskLevel,
    ScopeClause,
    Terms,
    TriageBucket,
)
from core.earn.grant import grant_to_scope  # noqa: F401
from core.earn.critic_config import (  # noqa: F401
    CriticConfigError,
    assert_family_divergence,
    critic_backend_for,
    resolve_critic_backend,
    resolve_lane_seats,
)
from core.earn.lifecycle import (  # noqa: F401
    OperatorSurface,
    deadline_of,
    is_decayed,
    sweep_decayed,
)
from core.earn.poc_gate import evaluate_poc  # noqa: F401
from core.earn.audit import AuditEvent, AuditVerdict, audit_action  # noqa: F401
from core.earn.issuance import Fetcher, IssuanceError, issue_grant  # noqa: F401
