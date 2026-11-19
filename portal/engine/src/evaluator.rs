//! S2 authority evaluator (RULING-1, T3a S2 §2/§3): one pure `evaluate`,
//! four points (E1 at `tool.call`, E2 at the approval response, E3 after
//! approval before the witness, E4 at incorporation — E4 is S4's, out of
//! scope here).
//!
//! - **PermissionMode (RULING-1):** six reserved values, one implemented
//!   (`default`). The other five are REJECTED at config load with typed
//!   `mode_unsupported`, never silently degraded.
//! - **Rule stack (RULING-1):** `Rule{effect: allow|ask|deny, matcher,
//!   source}`; precedence deny → ask → allow, first match wins, no match →
//!   the mode default (`ask`). All matches are retained as evidence.
//! - **DecisionRecord generation:** the proposed + routed records with
//!   `matched_rule_ref` (RULING-1), MGR-9 constants (`route=user`,
//!   `approval_mode=default`, `action_id` uuidv4 per call distinct from
//!   `call_id`), engine actor (MGR-7), one clock read per evaluation stored
//!   as `occurred_at`, and the MGR-1 digest set carried in `reason.params`.
//! - **ApprovalPreview derivation (RULING-7, MGR-4):** the nine fields
//!   exactly; deterministic engine-derived `action_summary`; `args_preview`
//!   capped at 2048 bytes of JCS on a UTF-8 char boundary with
//!   `args_truncated` + `args_digest`; resolved `targets`; `effect_class`
//!   from the tool registry (declared, never inferred).
//!
//! The reason-code strings and explanation texts are the slice stand-in for
//! the upstream generated S2 reason catalog (which does not exist yet);
//! `configuration_disabled` (D-13) and `authority_expired` (DEFERRED-EXT-1)
//! are deliberately NOT in the slice catalog (EXEC-23).

use std::collections::BTreeMap;
use std::path::Path;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use uuid::Uuid;

use crate::authority::{
    ActorKind, ActorRef, DecisionReason, DecisionRecord, DecisionState, EffectClass, Explanation,
    MatchedRulePreview,
};
use crate::digest::{jcs_bytes, jcs_sha256, scope_digest, sha256_digest, DigestError};

/// `args_preview` cap: first 2048 bytes of the JCS canonical args, cut on a
/// UTF-8 char boundary (MGR-4).
pub const ARGS_PREVIEW_CAP_BYTES: usize = 2048;

/// `action_summary` cap: 200 chars total including the trailing `…` when cut
/// (S4 §1b; the exact cut convention is EXEC-26).
pub const ACTION_SUMMARY_CAP_CHARS: usize = 200;

/// The slice S2 reason catalog (S2 §2 subset). `configuration_disabled` is
/// unreachable without granular settings (D-13) and `authority_expired` is
/// removed (DEFERRED-EXT-1) — neither appears here.
pub mod reason {
    pub const PROPOSED: &str = "proposed";
    pub const APPROVAL_ROUTE_SELECTED: &str = "approval_route_selected";
    pub const EXPLICIT_ALLOW: &str = "explicit_allow";
    pub const RULE_FORBIDDEN: &str = "rule_forbidden";
    pub const MISSING_VERDICT: &str = "missing_verdict";
    pub const CAPABILITY_UNDECLARED: &str = "capability_undeclared";
    pub const CAPABILITY_REVOKED: &str = "capability_revoked";
    pub const SANDBOX_UNAVAILABLE: &str = "sandbox_unavailable";
    pub const REVIEWER_APPROVED: &str = "reviewer_approved";
    pub const USER_DENIED: &str = "user_denied";
    pub const ACTION_DIGEST_MISMATCH: &str = "action_digest_mismatch";
    pub const AUTHORITY_SNAPSHOT_MISMATCH: &str = "authority_snapshot_mismatch";
    pub const POLICY_SNAPSHOT_MISMATCH: &str = "policy_snapshot_mismatch";
    pub const SCOPE_MISMATCH: &str = "scope_mismatch";
    pub const INTEGRATION_INVARIANT_FAILED: &str = "integration_invariant_failed";

    /// Every reason code the slice evaluator may emit (S2.9#3 catalog scan).
    pub const SLICE_CATALOG: [&str; 15] = [
        PROPOSED,
        APPROVAL_ROUTE_SELECTED,
        EXPLICIT_ALLOW,
        RULE_FORBIDDEN,
        MISSING_VERDICT,
        CAPABILITY_UNDECLARED,
        CAPABILITY_REVOKED,
        SANDBOX_UNAVAILABLE,
        REVIEWER_APPROVED,
        USER_DENIED,
        ACTION_DIGEST_MISMATCH,
        AUTHORITY_SNAPSHOT_MISMATCH,
        POLICY_SNAPSHOT_MISMATCH,
        SCOPE_MISMATCH,
        INTEGRATION_INVARIANT_FAILED,
    ];
}

/// Compiled explanation text per reason code (slice stand-in for the
/// generated S2 catalog; the sole explanation source on the authority side).
pub fn explanation_for(code: &str) -> String {
    match code {
        reason::PROPOSED => "call proposed".into(),
        reason::APPROVAL_ROUTE_SELECTED => "routed to user approval".into(),
        reason::EXPLICIT_ALLOW => "an allow rule matched the call".into(),
        reason::RULE_FORBIDDEN => "a deny rule matched the call".into(),
        reason::MISSING_VERDICT => {
            "the policy evaluator failed to produce a verdict; failing closed".into()
        }
        reason::CAPABILITY_UNDECLARED => {
            "the tool is not declared in the capability manifest".into()
        }
        reason::CAPABILITY_REVOKED => "the capability was revoked".into(),
        reason::SANDBOX_UNAVAILABLE => "no sandbox backend is available".into(),
        reason::REVIEWER_APPROVED => "approved by the reviewer".into(),
        reason::USER_DENIED => "denied by the reviewer".into(),
        reason::ACTION_DIGEST_MISMATCH => {
            "the echoed args_digest does not match the pending approval".into()
        }
        reason::AUTHORITY_SNAPSHOT_MISMATCH => {
            "the authority snapshot changed after the decision".into()
        }
        reason::POLICY_SNAPSHOT_MISMATCH => "the policy snapshot changed after the decision".into(),
        reason::SCOPE_MISMATCH => "the capability scope changed after the decision".into(),
        reason::INTEGRATION_INVARIANT_FAILED => "an integration invariant failed".into(),
        other => other.to_string(),
    }
}

/// `PermissionMode` (RULING-1): six reserved values, one implemented
/// (`default`). Parsing any other value is a typed `mode_unsupported`
/// config-load error; the engine never degrades silently to another mode.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub enum PermissionMode {
    Default,
    AcceptEdits,
    Plan,
    Auto,
    DontAsk,
    BypassPermissions,
}

impl PermissionMode {
    pub fn as_str(&self) -> &'static str {
        match self {
            PermissionMode::Default => "default",
            PermissionMode::AcceptEdits => "acceptEdits",
            PermissionMode::Plan => "plan",
            PermissionMode::Auto => "auto",
            PermissionMode::DontAsk => "dontAsk",
            PermissionMode::BypassPermissions => "bypassPermissions",
        }
    }

    /// Config-load parse (RULING-1): only `default` is implemented; every
    /// other reserved value is rejected with typed `mode_unsupported`.
    /// Unknown strings are malformed config, not a mode.
    pub fn load(s: &str) -> Result<PermissionMode, ConfigError> {
        match s {
            "default" => Ok(PermissionMode::Default),
            "acceptEdits" | "plan" | "auto" | "dontAsk" | "bypassPermissions" => {
                Err(ConfigError::ModeUnsupported { mode: s.to_owned() })
            }
            other => Err(ConfigError::Malformed(format!(
                "unknown permission mode '{other}'"
            ))),
        }
    }
}

/// Typed, fail-closed config-load errors.
#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error(
        "mode_unsupported: permission mode '{mode}' is reserved but not implemented in the slice; no silent downgrade (RULING-1)"
    )]
    ModeUnsupported { mode: String },
    #[error("policy config malformed: {0}")]
    Malformed(String),
}

impl ConfigError {
    /// Stable typed error code.
    pub fn code(&self) -> &'static str {
        match self {
            ConfigError::ModeUnsupported { .. } => "mode_unsupported",
            ConfigError::Malformed(_) => "policy_malformed",
        }
    }
}

/// Rule effects (RULING-1).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum RuleEffect {
    Allow,
    Ask,
    Deny,
}

/// Slice matcher (EXEC-24): exact tool name (or `*`), plus an optional argv
/// prefix matched against `args.argv` (the slice fixture tool is argv-form,
/// S4 §1b). Compound matchers are out of slice (S4 D-23).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Matcher {
    pub tool: String,
    #[serde(default)]
    pub argv_prefix: Option<Vec<String>>,
}

impl Matcher {
    pub fn matches(&self, tool_name: &str, args: &Value) -> bool {
        if self.tool != "*" && self.tool != tool_name {
            return false;
        }
        if let Some(prefix) = &self.argv_prefix {
            match argv_of(args) {
                Some(argv) => {
                    if argv.len() < prefix.len() {
                        return false;
                    }
                    if argv[..prefix.len()] != prefix[..] {
                        return false;
                    }
                }
                None => return false,
            }
        }
        true
    }
}

/// `Rule{effect, matcher, source}` (RULING-1). `source` is the provenance
/// file/origin string, retained verbatim.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Rule {
    pub effect: RuleEffect,
    pub matcher: Matcher,
    pub source: String,
}

/// A rule match retained as evidence (S4 §6.2: all matches retained).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RuleMatch {
    /// Stable ref `{source}#rule-<index>` (index in config order; EXEC-25).
    pub rule_ref: String,
    pub effect: RuleEffect,
    pub source: String,
}

/// Slice policy configuration: the permission mode plus the rule stack.
/// Validated at load; there is no runtime selection (S4 §1, D-44).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PolicyConfig {
    pub mode: PermissionMode,
    #[serde(default)]
    pub rules: Vec<Rule>,
    /// Policy revision string carried as `policy_ref` (EXEC-25).
    #[serde(default = "default_policy_revision")]
    pub revision: String,
}

fn default_policy_revision() -> String {
    "slice-policy#1".to_string()
}

impl PolicyConfig {
    /// Load and validate a policy config from JSON
    /// (`{"mode": "...", "rules": [...], "revision": "..."}`). The mode check
    /// is first and fail-closed (RULING-1); a rejected load produces no
    /// evaluator and no lineage (FX-13).
    pub fn from_json(v: &Value) -> Result<PolicyConfig, ConfigError> {
        let mode_s = v
            .get("mode")
            .and_then(Value::as_str)
            .ok_or_else(|| ConfigError::Malformed("missing string field 'mode'".into()))?;
        let mode = PermissionMode::load(mode_s)?;
        let rules: Vec<Rule> = match v.get("rules") {
            None => vec![],
            Some(r) => serde_json::from_value(r.clone())
                .map_err(|e| ConfigError::Malformed(format!("rules: {e}")))?,
        };
        for (i, rule) in rules.iter().enumerate() {
            if rule.matcher.tool.is_empty() {
                return Err(ConfigError::Malformed(format!("rule {i}: empty matcher.tool")));
            }
            if rule.source.is_empty() {
                return Err(ConfigError::Malformed(format!("rule {i}: empty source")));
            }
        }
        let revision = v
            .get("revision")
            .and_then(Value::as_str)
            .unwrap_or("slice-policy#1")
            .to_owned();
        Ok(PolicyConfig { mode, rules, revision })
    }

    /// The value hashed into `policy_snapshot_digest` (MGR-1): mode, rules
    /// and revision, JCS-canonicalized.
    pub fn snapshot_value(&self) -> Value {
        json!({
            "mode": self.mode.as_str(),
            "rules": serde_json::to_value(&self.rules).unwrap_or(Value::Null),
            "revision": self.revision,
        })
    }
}

/// One tool's manifest entry (S4: `effect_class` is declared in the tool
/// registry, never inferred from argv).
#[derive(Debug, Clone)]
pub struct ToolEntry {
    pub effect_class: EffectClass,
    /// Capability scope: read roots (ro-bind list, S4 §1b).
    pub read_roots: Vec<String>,
    /// Revocation flag; bumping `Evaluator::revocation_epoch` accompanies a
    /// flip so pending decisions detect it (FX-07, FX-08).
    pub revoked: bool,
}

/// The capability manifest (A-27: built-ins have entries).
#[derive(Debug, Clone, Default)]
pub struct ToolManifest {
    pub tools: BTreeMap<String, ToolEntry>,
}

/// The evaluator: one pure `evaluate` (S2 §3.12) over a fixed, load-time
/// validated config. Sandbox feasibility and the fault hooks below are
/// injected facts (S4 §6 fault-injection style), not probes.
#[derive(Debug, Clone)]
pub struct Evaluator {
    pub config: PolicyConfig,
    pub manifest: ToolManifest,
    /// Sandbox feasibility fact: is a backend available (S2 §3.3 step 5).
    pub sandbox_available: bool,
    /// Revocation epoch; stored in each decision's digest set and recomputed
    /// at E2/E3 (FX-10).
    pub revocation_epoch: u64,
    /// Fixture hook: the evaluator fails to produce a verdict (FX-05
    /// `missing_verdict`; FX-11 `slice.policy_evaluable`). Fail-closed.
    #[doc(hidden)]
    pub fail_verdict: bool,
    /// Fixture hook: integration invariant failure at E3 (FX-11), value is
    /// the invariant name recorded in reason params.
    #[doc(hidden)]
    pub fail_integration: Option<String>,
}

/// The evaluator failed to produce a verdict (injected in fixtures;
/// `policy_unknown` at evaluation time, mapped to `missing_verdict` at E1).
#[derive(Debug, thiserror::Error)]
#[error("policy evaluator failed to produce a verdict")]
pub struct VerdictFailure;

/// The routed verdict of one evaluation.
#[derive(Debug, Clone)]
pub enum Verdict {
    /// An `allow` rule matched → `explicit_allow` (`proposed→approved`).
    Allow { rule: RuleMatch },
    /// An `ask` rule matched, or no rule matched (mode default `ask`) →
    /// `approval_route_selected` (`proposed→approval_required`).
    Ask { rule: Option<RuleMatch> },
    /// Denied at E1 with the S2 reason code (`proposed→denied`).
    Deny {
        code: &'static str,
        rule: Option<RuleMatch>,
    },
}

/// The MGR-1 digest set computed at E1 and recomputed at E2/E3. Stored in
/// the routed DecisionRecord's `reason.params` so it is durable verbatim
/// (EXEC-27).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ActionDigests {
    pub args_digest: String,
    pub action_digest: String,
    pub scope_digest: String,
    pub authority_snapshot_digest: String,
    pub policy_snapshot_digest: String,
    pub revocation_epoch: u64,
}

impl ActionDigests {
    /// The `reason.params` embedding (durable verbatim in the DecisionRecord).
    pub fn to_params(&self) -> Value {
        json!({
            "args_digest": self.args_digest,
            "action_digest": self.action_digest,
            "scope_digest": self.scope_digest,
            "authority_snapshot_digest": self.authority_snapshot_digest,
            "policy_snapshot_digest": self.policy_snapshot_digest,
            "revocation_epoch": self.revocation_epoch,
        })
    }
}

/// Extract the argv array from argv-form args (`{"argv": [...]}`), if the
/// shape is argv-form at all.
pub fn argv_of(args: &Value) -> Option<Vec<String>> {
    args.get("argv")?
        .as_array()?
        .iter()
        .map(|v| v.as_str().map(str::to_owned))
        .collect()
}

impl Evaluator {
    /// Every rule that matches this call, in config order, retained as
    /// evidence (S4 §6.2; the routed effect still follows deny→ask→allow,
    /// first match wins).
    pub fn rule_matches(&self, tool_name: &str, args: &Value) -> Vec<RuleMatch> {
        self.config
            .rules
            .iter()
            .enumerate()
            .filter(|(_, r)| r.matcher.matches(tool_name, args))
            .map(|(i, r)| RuleMatch {
                rule_ref: format!("{}#rule-{i}", r.source),
                effect: r.effect,
                source: r.source.clone(),
            })
            .collect()
    }

    /// E1 evaluation (S2 §3.12), gate order per S2 §3.3: manifest →
    /// revocation → hook (empty, D-3) → rule stack → sandbox feasibility →
    /// incorporation checks (E4's, not run here).
    pub fn evaluate(&self, tool_name: &str, args: &Value) -> Result<Verdict, VerdictFailure> {
        if self.fail_verdict {
            return Err(VerdictFailure);
        }
        // 1. Manifest.
        let entry = match self.manifest.tools.get(tool_name) {
            Some(e) => e,
            None => {
                return Ok(Verdict::Deny {
                    code: reason::CAPABILITY_UNDECLARED,
                    rule: None,
                })
            }
        };
        // 2. Revocation.
        if entry.revoked {
            return Ok(Verdict::Deny {
                code: reason::CAPABILITY_REVOKED,
                rule: None,
            });
        }
        // 3. Hook: empty (D-3).
        // 4. Rule stack: precedence deny → ask → allow, first match wins
        //    (RULING-1). All matches retained as evidence.
        let matches = self.rule_matches(tool_name, args);
        let first = |effect: RuleEffect| matches.iter().find(|m| m.effect == effect).cloned();
        if let Some(rule) = first(RuleEffect::Deny) {
            return Ok(Verdict::Deny {
                code: reason::RULE_FORBIDDEN,
                rule: Some(rule),
            });
        }
        // The routed outcome (ask match > allow match > mode default). Only
        // `default` (= ask) is implemented (RULING-1); any other mode was
        // rejected at load — a hand-built config with a reserved mode fails
        // closed, never silently downgrades.
        let routed = if let Some(rule) = first(RuleEffect::Ask) {
            Verdict::Ask { rule: Some(rule) }
        } else if let Some(rule) = first(RuleEffect::Allow) {
            Verdict::Allow { rule }
        } else {
            match self.config.mode {
                PermissionMode::Default => Verdict::Ask { rule: None },
                _ => {
                    return Ok(Verdict::Deny {
                        code: reason::MISSING_VERDICT,
                        rule: None,
                    })
                }
            }
        };
        // 5. Sandbox feasibility (after the rule stack, S2 §3.3): applies to
        //    every forward-routed outcome (FX-09).
        if !self.sandbox_available {
            return Ok(Verdict::Deny {
                code: reason::SANDBOX_UNAVAILABLE,
                rule: None,
            });
        }
        Ok(routed)
    }

    /// The MGR-1 digest set for a declared tool. `args` are hashed as
    /// received; resolved `targets` ride in the `action_digest` input
    /// (`{tool_name, args, targets, effect_class}`); the scope (read roots)
    /// is sorted+deduped first; the authority and policy snapshots are the
    /// two S2 snapshot hashes.
    pub fn compute_digests(
        &self,
        tool_name: &str,
        args: &Value,
        targets: &Value,
        authority_snapshot: &Value,
    ) -> Result<ActionDigests, DigestError> {
        let entry = self.manifest.tools.get(tool_name).ok_or_else(|| {
            DigestError::Jcs(format!("tool '{tool_name}' not in manifest"))
        })?;
        let args_digest = jcs_sha256(args)?;
        let action_digest = jcs_sha256(&json!({
            "tool_name": tool_name,
            "args": args,
            "targets": targets,
            "effect_class": entry.effect_class,
        }))?;
        let scope_digest = scope_digest(&entry.read_roots)?;
        let authority_snapshot_digest = jcs_sha256(authority_snapshot)?;
        let policy_snapshot_digest = jcs_sha256(&self.config.snapshot_value())?;
        Ok(ActionDigests {
            args_digest,
            action_digest,
            scope_digest,
            authority_snapshot_digest,
            policy_snapshot_digest,
            revocation_epoch: self.revocation_epoch,
        })
    }

    /// Recompute-and-compare at E2 (response) and E3 (pre-witness) (S2
    /// §3.12): revocation epoch and live revocation flag first, then both
    /// snapshot hashes, then the scope digest. The `action_digest` echo
    /// binding (`args_digest` mismatch) is checked separately at the
    /// response (RULING-7). Returns the reason code on rejection.
    pub fn revalidate(
        &self,
        stored: &ActionDigests,
        tool_name: &str,
        args: &Value,
        targets: &Value,
        authority_snapshot: &Value,
    ) -> Result<Option<&'static str>, DigestError> {
        let revoked = self
            .manifest
            .tools
            .get(tool_name)
            .map(|e| e.revoked)
            .unwrap_or(true);
        let now = self.compute_digests(tool_name, args, targets, authority_snapshot)?;
        if revoked || now.revocation_epoch != stored.revocation_epoch {
            return Ok(Some(reason::CAPABILITY_REVOKED));
        }
        if now.authority_snapshot_digest != stored.authority_snapshot_digest {
            return Ok(Some(reason::AUTHORITY_SNAPSHOT_MISMATCH));
        }
        if now.policy_snapshot_digest != stored.policy_snapshot_digest {
            return Ok(Some(reason::POLICY_SNAPSHOT_MISMATCH));
        }
        if now.action_digest != stored.action_digest {
            return Ok(Some(reason::ACTION_DIGEST_MISMATCH));
        }
        if now.scope_digest != stored.scope_digest {
            return Ok(Some(reason::SCOPE_MISMATCH));
        }
        Ok(None)
    }
}

/// Build the proposed + routed DecisionRecords of one evaluation (T3a S2
/// §2/§4). MGR-9 constants: `route=user`, `approval_mode=default`,
/// `action_id` uuidv4 per call distinct from `call_id`. MGR-7: engine
/// actor. The clock is read once per evaluation and stored as
/// `occurred_at`. Chain linkage: `decision_id` is stable across the chain
/// (IN-7); `previous_decision_id` is null on the proposed record and the
/// shared chain id on every later record, with `state` marking the chain
/// position (EXEC-28).
pub fn build_decision_pair(
    decision_id: &str,
    action_id: &str,
    state: DecisionState,
    code: &str,
    text: &str,
    params: Value,
    matched: Option<&RuleMatch>,
    policy_ref: Option<&str>,
    evaluation_time: &str,
) -> (DecisionRecord, DecisionRecord) {
    let base = |state: DecisionState, code: &str, text: &str, params: Value, prev| {
        DecisionRecord {
            decision_id: decision_id.to_owned(),
            state,
            reason: DecisionReason {
                code: code.to_owned(),
                params,
                explanation: Explanation { text: text.to_owned() },
            },
            actor: ActorRef {
                kind: ActorKind::PolicyEngine,
                stable_id: "engine".into(),
            },
            matched_rule_ref: None,
            previous_decision_id: prev,
            occurred_at: evaluation_time.to_owned(),
            route: "user".into(),
            approval_mode: "default".into(),
            action_id: action_id.to_owned(),
            policy_ref: policy_ref.map(str::to_owned),
        }
    };
    let proposed = base(
        DecisionState::Proposed,
        reason::PROPOSED,
        &explanation_for(reason::PROPOSED),
        json!({}),
        None,
    );
    let mut decision = base(
        state,
        code,
        text,
        params,
        Some(decision_id.to_owned()),
    );
    decision.matched_rule_ref = matched.map(|m| m.rule_ref.clone());
    (proposed, decision)
}

/// A fresh uuidv4 (decision ids and MGR-9 action ids).
pub fn fresh_id() -> String {
    Uuid::new_v4().to_string()
}

/// POSIX single-quote an argv element iff it has a char outside
/// `[A-Za-z0-9_@%+=:,./-]` (S4 §1b).
fn quote_if_needed(s: &str) -> String {
    if !s.is_empty()
        && s.chars()
            .all(|c| c.is_ascii_alphanumeric() || "_@%+=:,./-".contains(c))
    {
        s.to_owned()
    } else {
        format!("'{}'", s.replace('\'', "'\\''"))
    }
}

/// Cut at [`ACTION_SUMMARY_CAP_CHARS`] chars total, ending in `…` when cut
/// (S4 §1b; convention: the ellipsis is the 200th char, EXEC-26).
fn cut_summary(s: String) -> String {
    if s.chars().count() <= ACTION_SUMMARY_CAP_CHARS {
        s
    } else {
        let mut cut: String = s.chars().take(ACTION_SUMMARY_CAP_CHARS - 1).collect();
        cut.push('…');
        cut
    }
}

/// Engine-derived deterministic `action_summary`, never model-generated
/// (RULING-7 #4): for the argv-form slice tool, `exec_command: ` + argv
/// joined by one space with the S4 §1b quoting rule. Non-argv-form tools
/// (unreachable for declared slice tools; EXEC-24) get
/// `{tool_name}: {JCS(args)}`.
pub fn action_summary(tool_name: &str, args: &Value) -> String {
    let body = match argv_of(args) {
        Some(argv) => format!(
            "{tool_name}: {}",
            argv.iter().map(|a| quote_if_needed(a)).collect::<Vec<_>>().join(" ")
        ),
        None => {
            let jcs = jcs_bytes(args)
                .map(|b| String::from_utf8_lossy(&b).into_owned())
                .unwrap_or_else(|_| "<unencodable args>".to_string());
            format!("{tool_name}: {jcs}")
        }
    };
    cut_summary(body)
}

/// `args_preview` + `args_truncated` + `args_digest` (MGR-4): the first 2048
/// bytes of the JCS canonical args cut on a UTF-8 char boundary; the digest
/// is over the full canonical args.
pub fn args_preview(args: &Value) -> Result<(String, bool, String), DigestError> {
    let bytes = jcs_bytes(args)?;
    let digest = sha256_digest(&bytes);
    if bytes.len() <= ARGS_PREVIEW_CAP_BYTES {
        let s = String::from_utf8(bytes).map_err(|e| DigestError::Jcs(e.to_string()))?;
        Ok((s, false, digest))
    } else {
        let s = std::str::from_utf8(&bytes).map_err(|e| DigestError::Jcs(e.to_string()))?;
        let mut end = ARGS_PREVIEW_CAP_BYTES;
        while !s.is_char_boundary(end) {
            end -= 1;
        }
        Ok((s[..end].to_owned(), true, digest))
    }
}

/// Resolved `targets` (RULING-7 #7, S4 §1b): `{cwd: realpath, paths:
/// [{given, resolved|null}]}` for argv[1..] entries beginning `/`, `./` or
/// `../`. `resolved` is null when the path cannot be canonicalized.
pub fn resolve_targets(cwd: &Path, argv: &[String]) -> Value {
    let cwd_real = std::fs::canonicalize(cwd)
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_else(|_| cwd.to_string_lossy().into_owned());
    let mut paths = Vec::new();
    for a in argv.iter().skip(1) {
        if a.starts_with('/') || a.starts_with("./") || a.starts_with("../") {
            let given = Path::new(a);
            let joined = if given.is_absolute() {
                given.to_path_buf()
            } else {
                cwd.join(given)
            };
            let resolved = std::fs::canonicalize(&joined)
                .ok()
                .map(|p| p.to_string_lossy().into_owned());
            paths.push(json!({"given": a, "resolved": resolved}));
        }
    }
    json!({"cwd": cwd_real, "paths": paths})
}

/// Derive the RULING-7 nine-field `ApprovalPreview` from the routed
/// DecisionRecord plus the `tool.call` context (T3a S2 §3.13). Stored
/// verbatim as `approval.requested`; the UI never re-derives it.
pub fn derive_preview(
    call_id: &str,
    decision: &DecisionRecord,
    tool_name: &str,
    args: &Value,
    effect_class: EffectClass,
    targets: Value,
    matched: Option<&RuleMatch>,
    requested_at: &str,
) -> Result<crate::authority::ApprovalPreview, DigestError> {
    let (preview, truncated, digest) = args_preview(args)?;
    Ok(crate::authority::ApprovalPreview {
        call_id: call_id.to_owned(),
        decision_id: decision.decision_id.clone(),
        tool_name: tool_name.to_owned(),
        action_summary: action_summary(tool_name, args),
        args_preview: preview,
        args_truncated: truncated,
        args_digest: digest,
        effect_class,
        targets,
        matched_rule: matched.map(|m| MatchedRulePreview {
            rule_ref: m.rule_ref.clone(),
            source: m.source.clone(),
        }),
        requested_at: requested_at.to_owned(),
    })
}
