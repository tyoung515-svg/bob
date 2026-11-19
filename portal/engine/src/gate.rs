//! Gate wiring (T3a S2 §4): the evaluator's outputs feed the E2 ledger
//! events — `authority.decided` (always), `approval.requested` /
//! `approval.resolved` (only on `ask`) — through one `authorize_call` API
//! that takes a `tool.call` context and writes the correct ledger records
//! for the allow/ask/deny paths, plus the response-time (E2) and
//! pre-witness (E3) evaluation points.
//!
//! Gate order (S2 §3.3 / S4 §3): `tool.call` durable → rule evaluate,
//! `authority.decided` durable → on `ask`: `approval.requested` durable →
//! await response → E2 recompute → `approval.resolved` → E3 pre-witness
//! checks → (witness and execution are S4's, out of scope here).
//!
//! Every ledger consistency rule (outcome ↔ state, source ↔
//! matched_rule_ref, resolution ↔ decision, explanation-verbatim binding on
//! `denied` results) is enforced by the E2 ledger; the gate builds payloads
//! that satisfy them by construction.

use std::path::Path;

use serde_json::{json, Value};

use crate::authority::{
    ActorKind, ActorRef, ApprovalPreview, ApprovalResolved, AuthorityDecided, DecisionOutcome,
    DecisionRecord, DecisionSource, DecisionState, Explanation, Resolution, UserResponse,
};
use crate::digest::DigestError;
use crate::evaluator::{
    build_decision_pair, derive_preview, explanation_for, fresh_id, reason, resolve_targets,
    ActionDigests, Evaluator, RuleMatch, Verdict,
};
use crate::ledger::{AckedCall, DurableAck, Ledger, LedgerError};
use crate::payload::{ResultError, ResultStatus, StartState, ToolCall, ToolResult};

/// Typed, fail-closed gate errors.
#[derive(Debug, thiserror::Error)]
pub enum GateError {
    #[error(transparent)]
    Ledger(#[from] LedgerError),
    #[error(transparent)]
    Digest(#[from] DigestError),
    #[error(
        "approval_mismatch: echoed decision_id does not match the pending approval; wire error only, the request stays pending and nothing is appended (RULING-7)"
    )]
    ApprovalDecisionMismatch,
    #[error("gate internal: {0}")]
    Internal(String),
}

/// The retained context of a routed (allow or ask) decision: everything the
/// E2/E3 recomputes need, kept verbatim from E1. The digest set is also
/// embedded in the routed DecisionRecord's `reason.params` (durable).
#[derive(Debug, Clone)]
pub struct PendingDecision {
    pub call_id: String,
    pub decision_id: String,
    pub action_id: String,
    pub outcome: DecisionOutcome,
    pub matched: Option<RuleMatch>,
    pub digests: ActionDigests,
    pub tool_name: String,
    pub args: Value,
    pub targets: Value,
    pub evaluation_time: String,
}

/// The result of [`Gate::authorize_call`]. On `Allow`/`Ask` the [`AckedCall`]
/// token is returned for the downstream steps; on `Denied` it was consumed
/// by the terminal `tool.result{denied}` (S1 §4a row 2).
#[derive(Debug)]
pub enum Authorization {
    /// `authority.decided{allow}` durable (`proposed→approved`,
    /// `explicit_allow`). Proceed to the E3 pre-witness check.
    Allow {
        acked: AckedCall,
        decided: DurableAck,
        pending: PendingDecision,
    },
    /// `authority.decided{ask}` + `approval.requested` durable. Await a
    /// response, then [`Gate::resolve_approval`].
    Ask {
        acked: AckedCall,
        decided: DurableAck,
        requested: DurableAck,
        preview: ApprovalPreview,
        pending: PendingDecision,
    },
    /// `authority.decided{deny}` + terminal `tool.result{denied}` durable
    /// (`proposed→denied`). The call is closed.
    Denied { decided: DurableAck, result: DurableAck },
}

/// The approval response (RULING-7 affordances): `allow_once` or
/// `deny(reason?)`, echoing `decision_id` + `args_digest`.
#[derive(Debug, Clone)]
pub enum ApprovalResponse {
    AllowOnce { decision_id: String, args_digest: String },
    Deny {
        decision_id: String,
        args_digest: String,
        reason: Option<String>,
    },
}

impl ApprovalResponse {
    fn decision_id(&self) -> &str {
        match self {
            ApprovalResponse::AllowOnce { decision_id, .. } => decision_id,
            ApprovalResponse::Deny { decision_id, .. } => decision_id,
        }
    }
    fn args_digest(&self) -> &str {
        match self {
            ApprovalResponse::AllowOnce { args_digest, .. } => args_digest,
            ApprovalResponse::Deny { args_digest, .. } => args_digest,
        }
    }
    fn user_response(&self) -> UserResponse {
        match self {
            ApprovalResponse::AllowOnce { .. } => UserResponse::AllowOnce,
            ApprovalResponse::Deny { .. } => UserResponse::Deny,
        }
    }
}

/// The result of [`Gate::resolve_approval`].
#[derive(Debug)]
pub enum ApprovalOutcome {
    /// `approval.resolved{allow}` durable (`approval_required→approved`,
    /// `reviewer_approved`). Proceed to the E3 pre-witness check.
    Allowed {
        acked: AckedCall,
        resolved: DurableAck,
        pending: PendingDecision,
    },
    /// `approval.resolved{deny}` + terminal `tool.result{denied}` durable
    /// (`approval_required→denied`: `user_denied`, `capability_revoked` or a
    /// `*_mismatch` recompute rejection). The call is closed.
    Denied { resolved: DurableAck, result: DurableAck },
}

/// The result of [`Gate::pre_witness_check`] (E3).
#[derive(Debug)]
pub enum PreWitness {
    /// All E3 checks pass; the caller may append the durable start-witness
    /// (S4's next step).
    Cleared { acked: AckedCall },
    /// `approved→refused`: terminal `tool.result{denied}` with
    /// `terminal_decision state=refused` durable (S1 §4a row 4). The call is
    /// closed.
    Refused { result: DurableAck },
}

/// The gate: an evaluator plus the ledger wiring.
pub struct Gate {
    pub evaluator: Evaluator,
}

impl Gate {
    /// E1: evaluate one admitted `tool.call` and write the correct ledger
    /// records for the allow/ask/deny paths (T3a S2 §4 row 1). The clock is
    /// read once per evaluation by the caller and stored as `occurred_at`
    /// (S2 §2).
    pub fn authorize_call(
        &self,
        ledger: &mut Ledger,
        acked: AckedCall,
        call: &ToolCall,
        authority_snapshot: &Value,
        evaluation_time: &str,
        cwd: &Path,
    ) -> Result<Authorization, GateError> {
        let call_id = acked.call_id().to_owned();
        let decision_id = fresh_id();
        let action_id = fresh_id(); // MGR-9: distinct from call_id
        let argv = crate::evaluator::argv_of(&call.arguments).unwrap_or_default();
        let targets = resolve_targets(cwd, &argv);

        let verdict = self.evaluator.evaluate(&call.tool_name, &call.arguments);
        let (state, code, matched) = match &verdict {
            // policy_unknown at E1 → denied/missing_verdict, fail-closed
            // (S2 §4, AUD-5).
            Err(_) => (DecisionState::Denied, reason::MISSING_VERDICT, None),
            Ok(Verdict::Deny { code, rule }) => (DecisionState::Denied, *code, rule.clone()),
            Ok(Verdict::Allow { rule }) => {
                (DecisionState::Approved, reason::EXPLICIT_ALLOW, Some(rule.clone()))
            }
            Ok(Verdict::Ask { rule }) => (
                DecisionState::ApprovalRequired,
                reason::APPROVAL_ROUTE_SELECTED,
                rule.clone(),
            ),
        };
        // The digest set rides in the routed record's reason.params for the
        // allow/ask paths (durable verbatim, EXEC-27); E1 denials close the
        // call, so no recompute context is needed.
        let params = match state {
            DecisionState::Approved | DecisionState::ApprovalRequired => self
                .evaluator
                .compute_digests(&call.tool_name, &call.arguments, &targets, authority_snapshot)?
                .to_params(),
            _ => json!({}),
        };
        let text = explanation_for(code);

        let (proposed, decision) = build_decision_pair(
            &decision_id,
            &action_id,
            state,
            code,
            &text,
            params,
            matched.as_ref(),
            Some(&self.evaluator.config.revision),
            evaluation_time,
        );
        let (outcome, source) = match state {
            DecisionState::Approved => (DecisionOutcome::Allow, DecisionSource::Rule),
            DecisionState::ApprovalRequired => (
                DecisionOutcome::Ask,
                if matched.is_some() {
                    DecisionSource::Rule
                } else {
                    DecisionSource::ModeDefault
                },
            ),
            DecisionState::Denied => (
                DecisionOutcome::Deny,
                if matched.is_some() {
                    DecisionSource::Rule
                } else {
                    DecisionSource::ModeDefault
                },
            ),
            _ => return Err(GateError::Internal("E1 must route, not terminate".into())),
        };
        let decided_payload = AuthorityDecided {
            outcome,
            source,
            matched_rule_ref: matched.as_ref().map(|m| m.rule_ref.clone()),
            proposed,
            decision: decision.clone(),
            decision_ref: decision_id.clone(),
        };
        let decided = ledger.record_authority_decided(&acked, decided_payload)?;

        let pending = || -> Result<PendingDecision, GateError> {
            Ok(PendingDecision {
                call_id: call_id.clone(),
                decision_id: decision_id.clone(),
                action_id: action_id.clone(),
                outcome,
                matched: matched.clone(),
                digests: self.evaluator.compute_digests(
                    &call.tool_name,
                    &call.arguments,
                    &targets,
                    authority_snapshot,
                )?,
                tool_name: call.tool_name.clone(),
                args: call.arguments.clone(),
                targets: targets.clone(),
                evaluation_time: evaluation_time.to_owned(),
            })
        };

        match outcome {
            DecisionOutcome::Allow => Ok(Authorization::Allow {
                acked,
                decided,
                pending: pending()?,
            }),
            DecisionOutcome::Ask => {
                let effect_class = self
                    .evaluator
                    .manifest
                    .tools
                    .get(&call.tool_name)
                    .ok_or_else(|| GateError::Internal("ask for undeclared tool".into()))?
                    .effect_class;
                let preview = derive_preview(
                    &call_id,
                    &decision,
                    &call.tool_name,
                    &call.arguments,
                    effect_class,
                    targets.clone(),
                    matched.as_ref(),
                    evaluation_time,
                )?;
                let requested = ledger.record_approval_requested(&acked, preview.clone())?;
                Ok(Authorization::Ask {
                    acked,
                    decided,
                    requested,
                    preview,
                    pending: pending()?,
                })
            }
            DecisionOutcome::Deny => {
                let result = ledger.record_pre_witness_result(
                    acked,
                    denied_result(&call_id, &decision_id, code, &text),
                )?;
                Ok(Authorization::Denied { decided, result })
            }
        }
    }

    /// E2: resolve a pending approval (T3a S2 §3.7, §4 row 2).
    ///
    /// - Wrong `decision_id` → typed wire error, request stays pending,
    ///   NOTHING is appended (RULING-7).
    /// - Wrong `args_digest` → `denied/action_digest_mismatch`, engine actor.
    /// - Recompute both snapshot hashes, `action_digest`, scope and
    ///   revocation: any rejection denies with the matching reason (engine
    ///   actor; `user_response` still records what the user sent).
    /// - Otherwise the user's `allow_once` approves (`reviewer_approved`,
    ///   user actor) or `deny(reason?)` denies (`user_denied`, user actor);
    ///   a `denied` resolution also writes the terminal
    ///   `tool.result{denied}` with the explanation verbatim (S2 §4).
    pub fn resolve_approval(
        &self,
        ledger: &mut Ledger,
        acked: AckedCall,
        pending: PendingDecision,
        response: ApprovalResponse,
        user_id: &str,
        evaluation_time: &str,
        authority_snapshot: &Value,
    ) -> Result<ApprovalOutcome, GateError> {
        if response.decision_id() != pending.decision_id {
            return Err(GateError::ApprovalDecisionMismatch);
        }
        // R-7 echo binding first (EXEC-29), then the E2 recompute.
        let engine_rejection: Option<&'static str> =
            if response.args_digest() != pending.digests.args_digest {
                Some(reason::ACTION_DIGEST_MISMATCH)
            } else {
                self.evaluator.revalidate(
                    &pending.digests,
                    &pending.tool_name,
                    &pending.args,
                    &pending.targets,
                    authority_snapshot,
                )?
            };

        let (resolution, actor, reason_text, decision) = if let Some(code) = engine_rejection {
            let decision = followup_decision(
                &pending,
                DecisionState::Denied,
                code,
                &explanation_for(code),
                json!({}),
                ActorKind::PolicyEngine,
                "engine",
                evaluation_time,
            );
            (
                Resolution::Deny,
                ActorRef {
                    kind: ActorKind::PolicyEngine,
                    stable_id: "engine".into(),
                },
                None,
                decision,
            )
        } else {
            match &response {
                ApprovalResponse::AllowOnce { .. } => {
                    let decision = followup_decision(
                        &pending,
                        DecisionState::Approved,
                        reason::REVIEWER_APPROVED,
                        &explanation_for(reason::REVIEWER_APPROVED),
                        json!({"reviewer_id": user_id}),
                        ActorKind::User,
                        user_id,
                        evaluation_time,
                    );
                    (
                        Resolution::Allow,
                        ActorRef {
                            kind: ActorKind::User,
                            stable_id: user_id.to_owned(),
                        },
                        None,
                        decision,
                    )
                }
                ApprovalResponse::Deny { reason, .. } => {
                    // RULING-7: the free-text reason becomes the denial
                    // reason; the denied result's error.message is the
                    // explanation verbatim (S2 §4).
                    let text = reason
                        .clone()
                        .filter(|r| !r.is_empty())
                        .unwrap_or_else(|| explanation_for(reason::USER_DENIED));
                    let decision = followup_decision(
                        &pending,
                        DecisionState::Denied,
                        reason::USER_DENIED,
                        &text,
                        json!({"reviewer_id": user_id}),
                        ActorKind::User,
                        user_id,
                        evaluation_time,
                    );
                    (
                        Resolution::Deny,
                        ActorRef {
                            kind: ActorKind::User,
                            stable_id: user_id.to_owned(),
                        },
                        reason.clone(),
                        decision,
                    )
                }
            }
        };

        let resolved_payload = ApprovalResolved {
            resolution,
            actor,
            user_response: Some(response.user_response()),
            reason: reason_text,
            cancel_cause: None,
            decision: Some(decision.clone()),
        };
        let resolved = ledger.record_approval_resolved(&acked, resolved_payload)?;

        match resolution {
            Resolution::Allow => Ok(ApprovalOutcome::Allowed {
                acked,
                resolved,
                pending,
            }),
            Resolution::Deny => {
                let result = ledger.record_pre_witness_result(
                    acked,
                    denied_result(
                        &pending.call_id,
                        &decision.decision_id,
                        &decision.reason.code,
                        &decision.reason.explanation.text,
                    ),
                )?;
                Ok(ApprovalOutcome::Denied { resolved, result })
            }
            Resolution::Cancelled => Err(GateError::Internal(
                "resolve_approval never writes cancelled (recovery owns it)".into(),
            )),
        }
    }

    /// E3: after `approved`, before the witness (T3a S2 §3.12): the same
    /// recompute as E2 plus integration invariants; any failure is
    /// `approved→refused` with a terminal `tool.result{denied}` carrying
    /// `terminal_decision state=refused` (S1 §4a row 4). MGR-10 mappings:
    /// `policy_unknown` at E3 → `integration_invariant_failed
    /// (slice.policy_evaluable)`; sandbox loss at E3 →
    /// `integration_invariant_failed(slice.sandbox_available)`.
    pub fn pre_witness_check(
        &self,
        ledger: &mut Ledger,
        acked: AckedCall,
        pending: PendingDecision,
        evaluation_time: &str,
        authority_snapshot: &Value,
    ) -> Result<PreWitness, GateError> {
        let mut refusal: Option<(&'static str, Value)> = None;
        if self.evaluator.fail_verdict {
            refusal = Some((
                reason::INTEGRATION_INVARIANT_FAILED,
                json!({"invariant": "slice.policy_evaluable"}),
            ));
        } else if let Some(code) = self.evaluator.revalidate(
            &pending.digests,
            &pending.tool_name,
            &pending.args,
            &pending.targets,
            authority_snapshot,
        )? {
            refusal = Some((code, json!({})));
        } else if !self.evaluator.sandbox_available {
            refusal = Some((
                reason::INTEGRATION_INVARIANT_FAILED,
                json!({"invariant": "slice.sandbox_available"}),
            ));
        } else if let Some(invariant) = &self.evaluator.fail_integration {
            refusal = Some((
                reason::INTEGRATION_INVARIANT_FAILED,
                json!({"invariant": invariant}),
            ));
        }

        match refusal {
            None => Ok(PreWitness::Cleared { acked }),
            Some((code, params)) => {
                let text = explanation_for(code);
                let terminal = followup_decision(
                    &pending,
                    DecisionState::Refused,
                    code,
                    &text,
                    params,
                    ActorKind::PolicyEngine,
                    "engine",
                    evaluation_time,
                );
                let mut result =
                    denied_result(&pending.call_id, &terminal.decision_id, code, &text);
                result.terminal_decision = Some(terminal);
                let ack = ledger.record_pre_witness_result(acked, result)?;
                Ok(PreWitness::Refused { result: ack })
            }
        }
    }
}

/// A follow-up (resolved/terminal) DecisionRecord in a call's chain:
/// `decision_id` stable (IN-7), `previous_decision_id` = the chain id
/// (EXEC-28), fresh clock read (S2 §2), actor per MGR-7 (`reviewer_id` in
/// reason params MUST equal a user actor's `stable_id`). pub(crate) for the
/// S4 executor's terminal records (`merged`/`refused`, E4).
pub(crate) fn followup_decision(
    pending: &PendingDecision,
    state: DecisionState,
    code: &str,
    text: &str,
    mut params: Value,
    actor_kind: ActorKind,
    actor_id: &str,
    evaluation_time: &str,
) -> DecisionRecord {
    if let Value::Object(ref mut map) = params {
        map.insert(
            "digests".to_string(),
            serde_json::to_value(&pending.digests.to_params()).unwrap_or(Value::Null),
        );
    }
    DecisionRecord {
        decision_id: pending.decision_id.clone(),
        state,
        reason: DecisionReasonRef {
            code,
            params,
            text,
        }
        .into(),
        actor: ActorRef {
            kind: actor_kind,
            stable_id: actor_id.to_owned(),
        },
        matched_rule_ref: pending.matched.as_ref().map(|m| m.rule_ref.clone()),
        previous_decision_id: Some(pending.decision_id.clone()),
        occurred_at: evaluation_time.to_owned(),
        route: "user".into(),
        approval_mode: "default".into(),
        action_id: pending.action_id.clone(),
        policy_ref: None,
    }
}

/// Small builder shim to keep `followup_decision` readable.
struct DecisionReasonRef<'a> {
    code: &'a str,
    params: Value,
    text: &'a str,
}

impl From<DecisionReasonRef<'_>> for crate::authority::DecisionReason {
    fn from(r: DecisionReasonRef<'_>) -> Self {
        crate::authority::DecisionReason {
            code: r.code.to_owned(),
            params: r.params,
            explanation: Explanation {
                text: r.text.to_owned(),
            },
        }
    }
}

/// The terminal `tool.result{denied}` shape shared by all pre-witness
/// denial paths: `error.code` = the S2 reason code, `error.message` = the
/// governing explanation verbatim (S2 §4), `start_state=not_started`, model
/// delivery `deliver` (S1 §4a rows 2-4).
fn denied_result(call_id: &str, decision_id: &str, code: &str, text: &str) -> ToolResult {
    ToolResult {
        call_id: call_id.to_owned(),
        status: ResultStatus::Denied,
        output: None,
        output_digest: None,
        error: Some(ResultError {
            code: code.to_owned(),
            message: text.to_owned(),
            // A-3: retryable=true only for `cancelled`; a deny never is.
            retryable: crate::delivery::retryable_for(code),
        }),
        effect_ids: vec![],
        governing_decision_id: Some(decision_id.to_owned()),
        terminal_decision: None,
        start_state: StartState::NotStarted,
        model_delivery: crate::delivery::denied(),
        origin: None,
        recovered_by: None,
            executor_evidence: None,
    }
}
