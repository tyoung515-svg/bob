//! S4 slice executor: the FX-READ-01 read-only local file-read command and
//! the MGR-13 executor-evidence types.
//!
//! - Spawn is argv-form (`std::process::Command`, argv array, no shell),
//!   capturing stdout/stderr/exit into a typed [`ChildOutcome`] (MGR-13).
//! - The full call ordering — authority decision → approval (if ask) → E3
//!   pre-witness check → preflight → durable `tool.started` witness → spawn →
//!   `tool.result` — is driven exclusively through the E2/E3 token APIs
//!   ([`crate::ledger::AckedCall`] → [`crate::ledger::AckedStart`]); the
//!   ledger structurally refuses out-of-order writes (RULING-4/5, STALE-4,
//!   S4 §3).
//! - Linux uses real bubblewrap: explicit read-only roots, isolated namespaces,
//!   dropped capabilities and a denylist seccomp layer. Preflight is read-only
//!   (no helper/version probe); verified helper/payload snapshots are sealed
//!   after the durable witness. No fallback exists. D-18 platforms retain
//!   their explicit `platform_deferred` posture, not a confinement claim.

use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::digest::sha256_digest;
use crate::evaluator::RuleMatch;
use crate::policy;

/// Executor-class error/abort codes and the terminal reason code for a
/// merged read (T3b S4 §2b exact spellings; EXEC-34: where these diverge
/// from the T3a S1 §4a table spellings — e.g. S1's `cancelled_before_start`
/// — the S4 §2b spelling governs the wire `error.code`).
pub mod code {
    // A-4 (SPEC-ERRATA-1): the spellings are owned by the single normative
    // table ([`crate::errcode`]); these are the S4 §2b names as constants.
    /// §2b row 4: cancel before the witness (incl. pending approval).
    pub const CANCELLED: &str = crate::errcode::CANCELLED;
    /// §2b row 5: witness append/fsync failure.
    pub const WITNESS_WRITE_FAILED: &str = crate::errcode::WITNESS_WRITE_FAILED;
    /// §2b row 6: preflight failure — isolation could not be provided.
    pub const ISOLATION_UNAVAILABLE: &str = crate::errcode::ISOLATION_UNAVAILABLE;
    /// §2b rows 6/7: sandbox launch failure (preflight or post-witness spawn).
    pub const SANDBOX_LAUNCH_FAILED: &str = crate::errcode::SANDBOX_LAUNCH_FAILED;
    /// §2b row 6: sandbox helper verification failure.
    pub const HELPER_VERIFICATION_FAILED: &str = crate::errcode::HELPER_VERIFICATION_FAILED;
    /// §2b row 8: the child ran and returned an error (exit ≠ 0 or signalled).
    pub const EXECUTION_FAILED: &str = crate::errcode::EXECUTION_FAILED;
    /// §2b row 9: cancel after the witness; the child was killed and reaped.
    pub const CANCELLED_AFTER_START: &str = crate::errcode::CANCELLED_AFTER_START;
    /// §2b row 10: after the witness, no typed outcome (reap unconfirmed,
    /// panic, dropped future, channel closed).
    pub const EXECUTION_SUPERVISION_LOST: &str = crate::errcode::EXECUTION_SUPERVISION_LOST;
    /// T3a S1 §4a row 10 / FX-23: executor `completed_ok` but the E4
    /// incorporation check refused.
    pub const INCORPORATION_REFUSED: &str = crate::errcode::INCORPORATION_REFUSED;
    /// The `merged` terminal reason code (AUD-4, IN-6(b)).
    pub const EFFECT_INCORPORATED: &str = crate::errcode::EFFECT_INCORPORATED;
}

/// Compiled explanation texts for the executor-class codes (the engine-side
/// stand-in for S4's generated executor-error catalog; MGR-5: explanation
/// text comes only from the catalog, executor display text is never shown).
pub fn explanation_for(code: &str) -> String {
    match code {
        code::CANCELLED => {
            "the call was cancelled before the durable start-witness; it did not run".into()
        }
        code::WITNESS_WRITE_FAILED => {
            "the durable start-witness could not be written; the tool did not run".into()
        }
        code::ISOLATION_UNAVAILABLE => {
            "the required isolation is unavailable for this call".into()
        }
        code::SANDBOX_LAUNCH_FAILED => "the sandboxed child could not be launched".into(),
        code::HELPER_VERIFICATION_FAILED => "the sandbox helper failed verification".into(),
        code::EXECUTION_FAILED => "the tool ran and returned an error".into(),
        code::CANCELLED_AFTER_START => {
            "the call was cancelled after the durable start-witness; the child was killed and reaped"
                .into()
        }
        code::EXECUTION_SUPERVISION_LOST => {
            "supervision of the child was lost after the durable start-witness; the outcome is unknown and is never retried"
                .into()
        }
        code::INCORPORATION_REFUSED => {
            "the post-execution incorporation check refused the result".into()
        }
        code::EFFECT_INCORPORATED => "the call's output was incorporated".into(),
        other => other.to_string(),
    }
}

/// `RejectionCode` (S4 §2): the closed set of eight, exact spellings.
/// Call-level reachable: `exec_forbidden`, `approval_rejected` (source
/// `user` only), `isolation_unavailable`, `sandbox_launch_failed`,
/// `helper_verification_failed`. `policy_unknown` is a config-load typed
/// error only (D-44); `preflight_unavailable` belongs to the S10 launcher
/// (D-20); `platform_deferred` is the D-18 platform posture label, not a
/// call-level rejection.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RejectionCode {
    ExecForbidden,
    ApprovalRejected,
    IsolationUnavailable,
    SandboxLaunchFailed,
    HelperVerificationFailed,
    PolicyUnknown,
    PreflightUnavailable,
    PlatformDeferred,
}

impl RejectionCode {
    /// Every code in the closed set (S4 §2).
    pub const CLOSED_SET: [RejectionCode; 8] = [
        RejectionCode::ExecForbidden,
        RejectionCode::ApprovalRejected,
        RejectionCode::IsolationUnavailable,
        RejectionCode::SandboxLaunchFailed,
        RejectionCode::HelperVerificationFailed,
        RejectionCode::PolicyUnknown,
        RejectionCode::PreflightUnavailable,
        RejectionCode::PlatformDeferred,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            RejectionCode::ExecForbidden => "exec_forbidden",
            RejectionCode::ApprovalRejected => "approval_rejected",
            RejectionCode::IsolationUnavailable => code::ISOLATION_UNAVAILABLE,
            RejectionCode::SandboxLaunchFailed => code::SANDBOX_LAUNCH_FAILED,
            RejectionCode::HelperVerificationFailed => code::HELPER_VERIFICATION_FAILED,
            RejectionCode::PolicyUnknown => "policy_unknown",
            RejectionCode::PreflightUnavailable => "preflight_unavailable",
            RejectionCode::PlatformDeferred => policy::BACKEND_PLATFORM_DEFERRED,
        }
    }

    /// S4 §2: only these five are call-level reachable.
    pub fn is_call_level_reachable(self) -> bool {
        matches!(
            self,
            RejectionCode::ExecForbidden
                | RejectionCode::ApprovalRejected
                | RejectionCode::IsolationUnavailable
                | RejectionCode::SandboxLaunchFailed
                | RejectionCode::HelperVerificationFailed
        )
    }

    /// The S2 authority reason an authority-class rejection maps to, if any
    /// (EXEC-35): the ledger's `tool.result.error.code` for `denied` rows
    /// carries the S2 reason code (ledger-enforced, T3a S1 §3); the
    /// RejectionCode is the executor-side API vocabulary.
    pub fn s2_reason(self) -> Option<&'static str> {
        match self {
            RejectionCode::ExecForbidden => Some(crate::evaluator::reason::RULE_FORBIDDEN),
            RejectionCode::ApprovalRejected => Some(crate::evaluator::reason::USER_DENIED),
            RejectionCode::IsolationUnavailable => {
                Some(crate::evaluator::reason::SANDBOX_UNAVAILABLE)
            }
            _ => None,
        }
    }
}

/// Inline cap for [`ContentValue`] (EXEC-39): above it, content is carried
/// as a digest + byte length (a `Stored` ref; the blob store itself is out
/// of slice, D-24 covers output-cap policy).
pub const INLINE_CAP_BYTES: usize = 64 * 1024;

/// `ContentValue` (MGR-13): inline text, or a digest ref for oversized
/// content. Redaction (S4 §4/S4.11): evidence carries content or its digest,
/// never raw command lines or paths.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ContentValue {
    Inline { text: String },
    Stored { digest: String, byte_len: u64 },
}

impl ContentValue {
    /// Build from raw child output bytes: inline (lossy UTF-8) up to
    /// [`INLINE_CAP_BYTES`], a `Stored` digest ref above it.
    pub fn from_bytes(bytes: &[u8]) -> ContentValue {
        if bytes.len() <= INLINE_CAP_BYTES {
            ContentValue::Inline {
                text: String::from_utf8_lossy(bytes).into_owned(),
            }
        } else {
            ContentValue::Stored {
                digest: sha256_digest(bytes),
                byte_len: bytes.len() as u64,
            }
        }
    }
}

/// `child_spawned: true|false|unknown` (MGR-13): JSON booleans plus the
/// string `"unknown"`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChildSpawned {
    True,
    False,
    Unknown,
}

impl Serialize for ChildSpawned {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        match self {
            ChildSpawned::True => s.serialize_bool(true),
            ChildSpawned::False => s.serialize_bool(false),
            ChildSpawned::Unknown => s.serialize_str("unknown"),
        }
    }
}

impl<'de> Deserialize<'de> for ChildSpawned {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        match Value::deserialize(d)? {
            Value::Bool(true) => Ok(ChildSpawned::True),
            Value::Bool(false) => Ok(ChildSpawned::False),
            Value::String(s) if s == "unknown" => Ok(ChildSpawned::Unknown),
            other => Err(serde::de::Error::custom(format!(
                "child_spawned must be true|false|\"unknown\", got {other}"
            ))),
        }
    }
}

/// `ChildOutcome` (MGR-13, closes A-39). Callers branch on the code, never
/// on display text.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChildOutcome {
    pub exit_code: Option<i32>,
    pub signal: Option<i32>,
    pub stdout: ContentValue,
    pub stderr: ContentValue,
}

/// `ExecutorEvidence.preflight` (MGR-13): populated only on a preflight
/// failure; both fields absent on success.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PreflightEvidence {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub construction_stage: Option<ConstructionStage>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub failure_class: Option<String>,
}

/// `construction_stage ∈ {glob_expansion, argv_build, spawn}` (MGR-13;
/// further vocabulary A-40).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ConstructionStage {
    GlobExpansion,
    ArgvBuild,
    Spawn,
}

/// `ExecutorEvidence.helper` (MGR-13). Absent under `platform_deferred`:
/// the sandbox helper does not exist on the build host (encoded absent =
/// `not_applicable_to_decision_kind`, MGR-13).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HelperEvidence {
    pub identity: String,
    pub verified: bool,
}

/// `ExecutorEvidence.policy` (MGR-13): the configured policy constants as
/// they applied to this call. `seccomp` is a label, not a claim of
/// enforcement: `"platform_deferred"` where the backend is deferred.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EvidencePolicy {
    pub read_roots: Vec<String>,
    pub network: bool,
    pub seccomp: String,
}

/// `ExecutorEvidence` (MGR-13) — the executor half of `GoverningDecision`,
/// riding in `tool.result.executor_evidence` (S4 §4). Inapplicable fields
/// are encoded absent (`not_applicable_to_decision_kind`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ExecutorEvidence {
    /// `linux_bubblewrap` when the sandbox backend was actually applied;
    /// `platform_deferred` when the host lacks it (D-18). Never a weaker
    /// backend label (S4 §1 fail-closed).
    pub backend: String,
    /// The declared capability set consulted for this call.
    pub capability_set: Value,
    pub policy: EvidencePolicy,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub helper: Option<HelperEvidence>,
    pub protected_path_violations: Vec<String>,
    /// Every rule match retained as evidence (S4 §6.2), not only the winner.
    pub matches: Vec<RuleMatch>,
    pub preflight: PreflightEvidence,
    pub child_spawned: ChildSpawned,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub child: Option<ChildOutcome>,
}

/// The host platform for the sandbox posture decision (D-18).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Platform {
    Linux,
    Windows,
    Other,
}

impl Platform {
    pub fn current() -> Platform {
        if cfg!(target_os = "linux") {
            Platform::Linux
        } else if cfg!(target_os = "windows") {
            Platform::Windows
        } else {
            Platform::Other
        }
    }

    /// The backend label recorded in evidence (EXEC-31).
    pub fn backend_label(self) -> &'static str {
        match self {
            Platform::Linux => policy::BACKEND_ID,
            Platform::Windows | Platform::Other => policy::BACKEND_PLATFORM_DEFERRED,
        }
    }
}

/// The engine-side cancellation token (T3b S4 §2b: cancellation in the
/// slice is an engine-side token fired by the harness; the wire
/// `turn.interrupt` stays D-28). The slice supervises cancel only (D-24).
#[derive(Debug, Clone, Default)]
pub struct CancelToken {
    flag: Arc<AtomicBool>,
}

impl CancelToken {
    pub fn new() -> CancelToken {
        CancelToken::default()
    }
    pub fn cancel(&self) {
        self.flag.store(true, Ordering::SeqCst);
    }
    pub fn is_cancelled(&self) -> bool {
        self.flag.load(Ordering::SeqCst)
    }
}

/// A preflight failure (S4 §2b row 6): typed by construction stage and
/// failure class, mapped to one of the row-6 error codes
/// (`isolation_unavailable` / `helper_verification_failed` /
/// `sandbox_launch_failed`). Fail-closed: a preflight failure is never a
/// downgrade to a weaker backend.
#[derive(Debug, Clone, thiserror::Error)]
#[error("preflight failed at {stage:?}: {failure_class} ({code})")]
pub struct PreflightFailure {
    pub stage: ConstructionStage,
    pub failure_class: String,
    pub code: &'static str,
}

/// The successful preflight record (S4 §3 step 6: probe, apply, helper
/// verify, argv build — read-only, no child). Under `platform_deferred` the
/// probe/apply/helper steps are labelled deferred, never claimed.
#[derive(Clone)]
pub struct PreflightRecord {
    pub backend_label: &'static str,
    /// The logical argv as authorized (argv[0] is the fixture command token).
    pub argv: Vec<String>,
    /// The resolved host program for argv[0] (EXEC-32).
    pub program: PathBuf,
    pub cwd: PathBuf,
    #[cfg(target_os = "linux")]
    linux_bytes: Option<(Vec<u8>, Vec<u8>)>,
    #[cfg(target_os = "linux")]
    linux_root: Option<std::sync::Arc<std::fs::File>>,
}

impl std::fmt::Debug for PreflightRecord {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PreflightRecord")
            .field("backend_label", &self.backend_label)
            .field(
                "provisioning",
                &"digest-bound immutable bytes and pinned root",
            )
            .finish_non_exhaustive()
    }
}

/// A spawn/supervision I/O failure after the durable witness (S4 §2b row 7
/// → `sandbox_launch_failed`).
#[derive(Debug, thiserror::Error)]
pub enum SpawnFailure {
    #[error("no child spawned: {0}")]
    NotSpawned(#[source] io::Error),
    #[error("child supervision lost: {0}")]
    SupervisionLost(#[source] io::Error),
}

/// The executor's report for a spawned child: the typed outcome plus the
/// raw stdout bytes (for the output digest) and whether the engine's cancel
/// token ended the run.
#[derive(Debug)]
pub struct Supervised {
    pub outcome: ChildOutcome,
    pub stdout_bytes: Vec<u8>,
    /// sha256 of the whole proof-stripped stdout stream, retained so a capped
    /// buffer of oversized output still digests the real bytes (F-H1).
    pub stdout_digest: String,
    pub cancelled: bool,
    /// The child outlived [`Executor::execution_timeout`] and was killed and
    /// reaped; the outcome of the call is unknown (F-H1).
    pub timed_out: bool,
    /// The provisioned payload emitted its startup proof, distinct from bwrap.
    pub payload_started: bool,
}

/// Default wall-clock bound for one supervised child (F-H1). A payload that
/// outlives it is killed and reaped, and the call reaches the existing typed
/// `indeterminate` terminal instead of wedging the turn.
pub const DEFAULT_EXECUTION_TIMEOUT: Duration = Duration::from_secs(30);

/// Retained bytes per stream (F-H1). The reader threads hash and count the
/// whole stream but keep at most this much in memory, so a hostile payload
/// cannot make the engine buffer unbounded output. Above
/// [`INLINE_CAP_BYTES`] the evidence value is a digest ref anyway (EXEC-39).
pub const RETAINED_OUTPUT_CAP_BYTES: usize = 1024 * 1024;

/// The FX-READ-01 read-only executor: one provisioned command (the fixture
/// binary), argv-form spawn, cancel supervision. Spawn attempts are counted
/// for the acceptance check "witness/preflight failure ⇒ spawn count 0"
/// (S4 §6.1).
///
/// The host platform is not a runtime selection or fallback surface:
/// ```compile_fail
/// use bob_engine::exec::{Executor, Platform};
/// let mut executor = Executor::new("fixture", "fx-read".into());
/// executor.platform = Platform::Windows;
/// ```
#[derive(Debug)]
pub struct Executor {
    /// `tool.started.executor_id`.
    pub id: String,
    /// The provisioned host program for the FX-READ-01 command (EXEC-32).
    pub fx_read_path: PathBuf,
    platform: Platform,
    /// Trusted system/bundled helper provisioning, never PATH lookup.
    pub bwrap_path: PathBuf,
    pub bwrap_digest: Option<String>,
    fx_read_digest: Option<String>,
    /// Fixture fault-injection hook (S4 §6 style): force a preflight failure.
    pub fail_preflight: Option<PreflightFailure>,
    /// Wall-clock bound on one supervised child (F-H1). On expiry the child is
    /// killed, reaped and reported as the existing typed `indeterminate`
    /// terminal; the turn never blocks on an unbounded payload.
    pub execution_timeout: Duration,
    #[doc(hidden)]
    pub debug_supervision_loss: bool,
    /// Observability for acceptance assertions (S4 §6.1: spawn count).
    pub spawn_attempts: AtomicUsize,
}

impl Executor {
    pub fn new(id: &str, fx_read_path: PathBuf) -> Executor {
        Executor {
            id: id.to_owned(),
            fx_read_digest: std::fs::read(&fx_read_path).ok().map(|b| sha256_digest(&b)),
            fx_read_path,
            platform: Platform::current(),
            bwrap_path: PathBuf::from("/usr/bin/bwrap"),
            bwrap_digest: std::fs::read("/usr/bin/bwrap")
                .ok()
                .map(|b| sha256_digest(&b)),
            fail_preflight: None,
            execution_timeout: DEFAULT_EXECUTION_TIMEOUT,
            debug_supervision_loss: false,
            spawn_attempts: AtomicUsize::new(0),
        }
    }

    /// Validate the fixed slice roots rather than ignore or broaden a manifest.
    pub fn preflight_with_read_roots(
        &self,
        argv: &[String],
        cwd: &Path,
        roots: &[String],
    ) -> Result<PreflightRecord, PreflightFailure> {
        if self.platform == Platform::Linux {
            let mut expected: Vec<String> =
                policy::READ_ROOTS.iter().map(|s| s.to_string()).collect();
            expected.push(cwd.to_string_lossy().into_owned());
            expected.sort();
            let mut supplied = roots.to_vec();
            supplied.sort();
            if supplied != expected {
                return Err(PreflightFailure {
                    stage: ConstructionStage::ArgvBuild,
                    failure_class: "read_roots_mismatch".into(),
                    code: code::ISOLATION_UNAVAILABLE,
                });
            }
        }
        self.preflight(argv, cwd)
    }

    /// Preflight (S4 §3 step 6): argv build + program resolution, then the
    /// backend probe. Never spawns the child. Fail-closed: on Linux a failed
    /// `bwrap` probe is `isolation_unavailable`; on Windows the posture is
    /// `platform_deferred` (D-18, labelled, not a weaker-backend claim).
    pub fn preflight(
        &self,
        argv: &[String],
        cwd: &Path,
    ) -> Result<PreflightRecord, PreflightFailure> {
        if let Some(f) = &self.fail_preflight {
            return Err(f.clone());
        }
        // argv build. The slice is argv-form = one segment (S4 §1): no glob
        // expansion is performed at all, so the glob_expansion stage cannot
        // fail here (glob cap 8192 is carried for the Linux backend, S4 §2).
        let Some(command) = argv.first() else {
            return Err(PreflightFailure {
                stage: ConstructionStage::ArgvBuild,
                failure_class: "empty_argv".into(),
                code: code::ISOLATION_UNAVAILABLE,
            });
        };
        let program = if command == policy::FX_READ_COMMAND {
            self.fx_read_path.clone()
        } else {
            // The executor provisions exactly one command; anything else is
            // fail-closed, never a pass-through (EXEC-32).
            return Err(PreflightFailure {
                stage: ConstructionStage::ArgvBuild,
                failure_class: "unprovisioned_command".into(),
                code: code::ISOLATION_UNAVAILABLE,
            });
        };
        match self.platform {
            Platform::Linux => {
                if cwd.file_name() != Some(std::ffi::OsStr::new("slice"))
                    || !cwd.is_absolute()
                    || cwd.canonicalize().ok().as_deref() != Some(cwd)
                {
                    return Err(PreflightFailure {
                        stage: ConstructionStage::ArgvBuild,
                        failure_class: "invalid_fixture_root".into(),
                        code: code::ISOLATION_UNAVAILABLE,
                    });
                }
                // Backend probe (S4 §3 step 6): no bwrap → fail closed, no
                // weaker backend (S4 §1).
                // Metadata only: even a helper probe must not run before the witness.
                let probed = std::fs::metadata(&self.bwrap_path)
                    .map(|m| m.is_file())
                    .unwrap_or(false);
                if !probed {
                    return Err(PreflightFailure {
                        stage: ConstructionStage::Spawn,
                        failure_class: "backend_probe".into(),
                        code: code::ISOLATION_UNAVAILABLE,
                    });
                }
                #[cfg(unix)]
                {
                    use std::os::unix::fs::PermissionsExt;
                    let mode = std::fs::metadata(&self.bwrap_path)
                        .unwrap()
                        .permissions()
                        .mode();
                    if mode & 0o111 == 0 {
                        return Err(PreflightFailure {
                            stage: ConstructionStage::ArgvBuild,
                            failure_class: "helper_unexecutable".into(),
                            code: code::SANDBOX_LAUNCH_FAILED,
                        });
                    }
                }
                let helper_bytes = std::fs::read(&self.bwrap_path).ok();
                let observed = helper_bytes.as_ref().map(|b| sha256_digest(b));
                if observed.is_none() || observed != self.bwrap_digest {
                    return Err(PreflightFailure {
                        stage: ConstructionStage::ArgvBuild,
                        failure_class: "helper_digest_mismatch".into(),
                        code: code::HELPER_VERIFICATION_FAILED,
                    });
                }
                let payload_bytes =
                    std::fs::read(&self.fx_read_path).map_err(|_| PreflightFailure {
                        stage: ConstructionStage::ArgvBuild,
                        failure_class: "payload_unavailable".into(),
                        code: code::SANDBOX_LAUNCH_FAILED,
                    })?;
                if Some(sha256_digest(&payload_bytes)) != self.fx_read_digest {
                    return Err(PreflightFailure {
                        stage: ConstructionStage::ArgvBuild,
                        failure_class: "payload_digest_mismatch".into(),
                        code: code::HELPER_VERIFICATION_FAILED,
                    });
                }
                if !cfg!(target_arch = "x86_64") {
                    return Err(PreflightFailure {
                        stage: ConstructionStage::ArgvBuild,
                        failure_class: "seccomp_abi_unavailable".into(),
                        code: code::ISOLATION_UNAVAILABLE,
                    });
                }
                Ok(PreflightRecord {
                    backend_label: policy::BACKEND_ID,
                    argv: argv.to_vec(),
                    program,
                    cwd: cwd.canonicalize().map_err(|_| PreflightFailure {
                        stage: ConstructionStage::ArgvBuild,
                        failure_class: "cwd_unavailable".into(),
                        code: code::SANDBOX_LAUNCH_FAILED,
                    })?,
                    #[cfg(target_os = "linux")]
                    linux_bytes: Some((helper_bytes.unwrap(), payload_bytes)),
                    #[cfg(target_os = "linux")]
                    linux_root: Some(std::sync::Arc::new(std::fs::File::open(cwd).map_err(
                        |_| PreflightFailure {
                            stage: ConstructionStage::ArgvBuild,
                            failure_class: "read_root_unavailable".into(),
                            code: code::ISOLATION_UNAVAILABLE,
                        },
                    )?)),
                })
            }
            Platform::Windows | Platform::Other => Ok(PreflightRecord {
                backend_label: policy::BACKEND_PLATFORM_DEFERRED,
                argv: argv.to_vec(),
                program,
                cwd: cwd.to_path_buf(),
                #[cfg(target_os = "linux")]
                linux_bytes: None,
                #[cfg(target_os = "linux")]
                linux_root: None,
            }),
        }
    }

    /// Spawn and supervise (S4 §3 step 8): argv-form, no shell; stdout and
    /// stderr piped into the typed [`ChildOutcome`]. The slice supervises
    /// cancel only (D-24): when the token fires, the child is killed and
    /// reaped and the outcome is reported with `cancelled = true` (S4 §2b
    /// row 9).
    pub fn spawn_supervise(
        &self,
        preflight: &PreflightRecord,
        cancel: &CancelToken,
    ) -> Result<Supervised, SpawnFailure> {
        self.spawn_attempts.fetch_add(1, Ordering::SeqCst);
        #[cfg(target_os = "linux")]
        let linux_files = if self.platform == Platform::Linux {
            let (helper, payload) = preflight
                .linux_bytes
                .as_ref()
                .ok_or_else(|| SpawnFailure::NotSpawned(io::Error::other("missing Linux plan")))?;
            Some((
                crate::linux_sandbox::sealed_file(c"bob-bwrap", helper)
                    .map_err(SpawnFailure::NotSpawned)?,
                crate::linux_sandbox::sealed_file(c"bob-payload", payload)
                    .map_err(SpawnFailure::NotSpawned)?,
                crate::linux_sandbox::seccomp_file().map_err(SpawnFailure::NotSpawned)?,
            ))
        } else {
            None
        };
        let mut command = if self.platform == Platform::Linux {
            #[cfg(target_os = "linux")]
            let mut c = {
                use std::os::fd::AsRawFd;
                Command::new(format!(
                    "/proc/self/fd/{}",
                    linux_files.as_ref().unwrap().0.as_raw_fd()
                ))
            };
            #[cfg(not(target_os = "linux"))]
            let mut c = Command::new(&self.bwrap_path);
            c.args([
                "--unshare-all",
                "--die-with-parent",
                "--new-session",
                "--cap-drop",
                "ALL",
                "--clearenv",
                "--tmpfs",
                "/",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
            ]);
            for root in policy::READ_ROOTS {
                c.args(["--ro-bind", root, root]);
            }
            #[cfg(target_os = "linux")]
            {
                use std::os::{fd::AsRawFd, unix::process::CommandExt};
                let root = preflight
                    .linux_root
                    .as_ref()
                    .unwrap()
                    .try_clone()
                    .map_err(SpawnFailure::NotSpawned)?;
                c.arg("--ro-bind-fd")
                    .arg(root.as_raw_fd().to_string())
                    .arg(&preflight.cwd);
                unsafe {
                    c.pre_exec(move || crate::linux_sandbox::inherit(&root));
                }
            }
            c.args(["--dir", "/run/bob"]);
            #[cfg(target_os = "linux")]
            {
                use std::os::{fd::AsRawFd, unix::process::CommandExt};
                let files = linux_files.as_ref().unwrap();
                let payload = files.1.try_clone().map_err(SpawnFailure::NotSpawned)?;
                let filter = files.2.try_clone().map_err(SpawnFailure::NotSpawned)?;
                c.args(["--perms", "0555", "--ro-bind-data"])
                    .arg(payload.as_raw_fd().to_string())
                    .arg("/run/bob/fx-read");
                c.arg("--seccomp").arg(filter.as_raw_fd().to_string());
                unsafe {
                    c.pre_exec(move || {
                        crate::linux_sandbox::inherit(&payload)?;
                        crate::linux_sandbox::inherit(&filter)
                    });
                }
            }
            c.args(["--remount-ro", "/", "--chdir"]).arg(&preflight.cwd);
            c.args([
                "--setenv",
                "BOB_SLICE_STARTED",
                "1",
                "--",
                "/run/bob/fx-read",
            ]);
            c
        } else {
            Command::new(&preflight.program)
        };
        let mut child = command
            .args(&preflight.argv[1..])
            .current_dir(&preflight.cwd)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(SpawnFailure::NotSpawned)?;
        if self.debug_supervision_loss {
            // Injection drops the typed outcome, not an orphan on the host.
            let _ = child.kill();
            let _ = child.wait();
            return Err(SpawnFailure::SupervisionLost(io::Error::other(
                "injected supervision I/O loss",
            )));
        }
        // F-H1: drain both pipes while the child runs. A payload that fills the
        // pipe buffer must never be able to block the supervisor.
        let stdout_sink = Arc::new(Mutex::new(StreamAccum::new(self.platform == Platform::Linux)));
        let stderr_sink = Arc::new(Mutex::new(StreamAccum::new(false)));
        let (done_tx, done_rx) = mpsc::channel::<()>();
        let mut readers = 0usize;
        if let Some(pipe) = child.stdout.take() {
            let sink = Arc::clone(&stdout_sink);
            let tx = done_tx.clone();
            std::thread::spawn(move || {
                drain_stream(pipe, &sink);
                let _ = tx.send(());
            });
            readers += 1;
        }
        if let Some(pipe) = child.stderr.take() {
            let sink = Arc::clone(&stderr_sink);
            let tx = done_tx.clone();
            std::thread::spawn(move || {
                drain_stream(pipe, &sink);
                let _ = tx.send(());
            });
            readers += 1;
        }
        drop(done_tx);

        let deadline = Instant::now() + self.execution_timeout;
        let mut cancelled = false;
        let mut timed_out = false;
        loop {
            if cancel.is_cancelled() {
                cancelled = true;
                break;
            }
            match child.try_wait() {
                Ok(Some(_status)) => break,
                Ok(None) => {}
                Err(err) => return Err(SpawnFailure::SupervisionLost(err)),
            }
            if Instant::now() >= deadline {
                timed_out = true;
                break;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
        if cancelled || timed_out {
            // Kill and reap: with `--die-with-parent` inside a private PID
            // namespace the whole payload tree goes with the helper. Never
            // `wait_with_output()` here: its pipe reads would block on a
            // grandchild that still holds the write end.
            let _ = child.kill();
        }
        let status = child.wait().map_err(SpawnFailure::SupervisionLost)?;
        // A grandchild may inherit the write end, so give the readers a bounded
        // grace and then read whatever they have accumulated.
        let grace = Instant::now() + Duration::from_secs(2);
        for _ in 0..readers {
            let left = grace.saturating_duration_since(Instant::now());
            if left.is_zero() || done_rx.recv_timeout(left).is_err() {
                break;
            }
        }
        let out = stdout_sink
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .snapshot();
        let err = stderr_sink
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .snapshot();
        let payload_started = out.proof_seen;
        Ok(Supervised {
            outcome: child_outcome(&status, out.content(), err.content()),
            stdout_bytes: out.retained,
            stdout_digest: out.digest,
            cancelled,
            timed_out,
            payload_started,
        })
    }

    /// Assemble the MGR-13 evidence record for a terminal `tool.result`.
    /// `read_roots` comes from the tool manifest entry (declared, S4 §1b).
    pub fn evidence(
        &self,
        read_roots: &[String],
        matches: &[RuleMatch],
        preflight: Option<&PreflightRecord>,
        preflight_failure: Option<&PreflightFailure>,
        child_spawned: ChildSpawned,
        child: Option<ChildOutcome>,
    ) -> ExecutorEvidence {
        let backend = preflight
            .map(|p| p.backend_label)
            .unwrap_or_else(|| self.platform.backend_label());
        ExecutorEvidence {
            backend: backend.to_owned(),
            capability_set: json!({
                "tools": {
                    policy::TOOL_NAME: {
                        "effect_class": "read",
                        "approval_action": policy::APPROVAL_ACTION,
                    }
                }
            }),
            policy: EvidencePolicy {
                read_roots: {
                    let mut roots = read_roots.to_vec();
                    if self.platform == Platform::Linux {
                        if let Some(p) = preflight {
                            let root = p.cwd.to_string_lossy().into_owned();
                            if !roots.contains(&root) {
                                roots.insert(0, root);
                            }
                        }
                    }
                    roots
                },
                network: policy::NETWORK_ACCESS,
                seccomp: if self.platform == Platform::Linux {
                    "deny_ptrace_process_vm_io_uring_x86_64".into()
                } else {
                    policy::BACKEND_PLATFORM_DEFERRED.into()
                },
            },
            // Verification refers to the bound bytes, not to proof of
            // confinement. Runtime payload startup and outcome are separate.
            // The helper remains absent under platform_deferred.
            helper: {
                #[cfg(target_os = "linux")]
                {
                    preflight
                        .and_then(|p| p.linux_bytes.as_ref())
                        .map(|(h, p)| HelperEvidence {
                            identity: format!(
                                "bwrap={};payload={}",
                                sha256_digest(h),
                                sha256_digest(p)
                            ),
                            verified: true,
                        })
                }
                #[cfg(not(target_os = "linux"))]
                {
                    None
                }
            },
            protected_path_violations: vec![],
            matches: matches.to_vec(),
            preflight: PreflightEvidence {
                construction_stage: preflight_failure.map(|f| f.stage),
                failure_class: preflight_failure.map(|f| f.failure_class.clone()),
            },
            child_spawned,
            child,
        }
    }
}

/// Build the typed [`ChildOutcome`] from a finished child (MGR-13). The
/// outcome is recorded exactly as the OS reported it; `signal` is always
/// null on Windows.
fn child_outcome(
    status: &std::process::ExitStatus,
    stdout: ContentValue,
    stderr: ContentValue,
) -> ChildOutcome {
    #[cfg(unix)]
    let signal = std::os::unix::process::ExitStatusExt::signal(status);
    #[cfg(not(unix))]
    let signal = None;
    ChildOutcome {
        exit_code: status.code(),
        signal,
        stdout,
        stderr,
    }
}

/// The provisioned payload's startup proof (Linux): a stdout prefix that
/// distinguishes "the payload ran" from "the helper ran and exited".
const STARTUP_PROOF: &[u8] = b"\x1eBOB_SLICE_PAYLOAD_V1\n";

/// Streaming accumulator for one child pipe (F-H1). The supervisor must read
/// while the child runs, so this is written by a reader thread and read by the
/// supervisor: it hashes and counts the entire stream while retaining at most
/// [`RETAINED_OUTPUT_CAP_BYTES`]. The startup proof is dropped from the hashed
/// bytes, not from the retained buffer afterwards, so an oversized output still
/// digests exactly the bytes the old one-shot path digested.
#[derive(Debug)]
struct StreamAccum {
    hasher: Sha256,
    total: u64,
    retained: Vec<u8>,
    /// Leading bytes whose status is undecided: they may still turn out to be
    /// the startup proof.
    holding: Vec<u8>,
    decided: bool,
    proof_seen: bool,
}

/// What the supervisor takes from a stream after the child is reaped.
struct StreamSnapshot {
    retained: Vec<u8>,
    total: u64,
    digest: String,
    proof_seen: bool,
}

impl StreamAccum {
    fn new(strip_proof: bool) -> StreamAccum {
        StreamAccum {
            hasher: Sha256::new(),
            total: 0,
            retained: Vec::new(),
            holding: Vec::new(),
            decided: !strip_proof,
            // Without a required proof (platform_deferred) startup counts as
            // observed, exactly as the previous one-shot path reported it.
            proof_seen: !strip_proof,
        }
    }

    /// Commit bytes that are definitely payload output.
    fn feed(&mut self, bytes: &[u8]) {
        if bytes.is_empty() {
            return;
        }
        self.hasher.update(bytes);
        self.total += bytes.len() as u64;
        let room = RETAINED_OUTPUT_CAP_BYTES.saturating_sub(self.retained.len());
        if room > 0 {
            self.retained.extend_from_slice(&bytes[..bytes.len().min(room)]);
        }
    }

    /// Absorb one read from the pipe, resolving the startup proof if unknown.
    fn absorb(&mut self, chunk: &[u8]) {
        if self.decided {
            self.feed(chunk);
            return;
        }
        self.holding.extend_from_slice(chunk);
        // Compare only the bytes that could still be the proof: the first read
        // usually carries the proof *and* payload output.
        let overlap = self.holding.len().min(STARTUP_PROOF.len());
        if self.holding[..overlap] == STARTUP_PROOF[..overlap] {
            if self.holding.len() >= STARTUP_PROOF.len() {
                self.proof_seen = true;
                self.decided = true;
                let rest = self.holding.split_off(STARTUP_PROOF.len());
                self.holding.clear();
                self.feed(&rest);
            }
        } else {
            self.decided = true;
            let held = std::mem::take(&mut self.holding);
            self.feed(&held);
        }
    }

    fn snapshot(&mut self) -> StreamSnapshot {
        if !self.decided {
            self.decided = true;
            let held = std::mem::take(&mut self.holding);
            self.feed(&held);
        }
        StreamSnapshot {
            retained: self.retained.clone(),
            total: self.total,
            // Same shape as `sha256_digest` / `ContentValue::Stored` (MGR-1).
            digest: format!("sha256:{:x}", self.hasher.clone().finalize()),
            proof_seen: self.proof_seen,
        }
    }
}

impl StreamSnapshot {
    /// The MGR-13 content value: inline up to [`INLINE_CAP_BYTES`], a digest
    /// ref above it (EXEC-39).
    fn content(&self) -> ContentValue {
        if self.total <= INLINE_CAP_BYTES as u64 {
            ContentValue::Inline {
                text: String::from_utf8_lossy(&self.retained).into_owned(),
            }
        } else {
            ContentValue::Stored {
                digest: self.digest.clone(),
                byte_len: self.total,
            }
        }
    }
}

/// Drain one child pipe to EOF, bounded in memory (F-H1). Runs on its own
/// thread so the child can never block on a full pipe buffer.
fn drain_stream(mut pipe: impl Read, sink: &Arc<Mutex<StreamAccum>>) {
    let mut chunk = [0u8; 16 * 1024];
    loop {
        match pipe.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => {
                let mut acc = sink.lock().unwrap_or_else(|e| e.into_inner());
                acc.absorb(&chunk[..n]);
            }
            Err(ref err) if err.kind() == io::ErrorKind::Interrupted => continue,
            Err(_) => break,
        }
    }
}

// ---- Driver: the S4 §3 ordering contract over the E2/E3 token APIs ----
//
// 1. policy resolve (load time) → 2. `tool.call` durable → 3. rule evaluate,
//    `authority.decided` durable → 4. on ask: `approval.requested` durable,
//    await `approval.resolved` (E2 recompute inside the gate) → 5. pre-effect
//    re-evaluation (E3, inside the gate) → 6. preflight (read-only, no
//    child) → 7. `tool.started` append+fsync → 8. spawn and supervise →
//    9. `tool.result` durable (E4 incorporation check after `completed_ok`).
//
// Every step consumes the token of the previous one (`AckedCall`,
// `AckedStart`); the ledger structurally refuses out-of-order writes, so the
// ordering is enforced by the E2 token APIs, not by convention.

use crate::authority::{
    ActorKind, ActorRef, ApprovalPreview, ApprovalResolved, CancelCause, DecisionState, Resolution,
};
use crate::digest::DigestError;
use crate::evaluator::{argv_of, RuleMatch as EvidenceMatch};
use crate::gate::{
    followup_decision, ApprovalOutcome, ApprovalResponse, Authorization, Gate, GateError,
    PendingDecision, PreWitness,
};
use crate::ledger::{AckedCall, AckedStart, DurableAck, Ledger, LedgerError};
use crate::payload::{
    IndeterminateOrigin, ModelDelivery, ResultError, ResultStatus, StartState, ToolCall, ToolResult,
};

/// Typed driver errors. A witness-write failure is NOT an error return — it
/// is the terminal [`CallTerminal::WitnessWriteFailed`] (S4 §2b row 5).
#[derive(Debug, thiserror::Error)]
pub enum ExecError {
    #[error(transparent)]
    Gate(#[from] GateError),
    #[error(transparent)]
    Ledger(#[from] LedgerError),
    #[error(transparent)]
    Digest(#[from] DigestError),
}

/// The terminal classification of a call (T3b S4 §2b). `Denied` covers the
/// authority-class rows 1-3 (including an E3 refusal, S1 §4a row 4);
/// `Aborted` rows 4-6; `WitnessWriteFailed` row 5 when the segment is
/// faulted and even the best-effort record cannot land (ledger-fatal);
/// `Failed` rows 7-10; `Succeeded` row 11.
#[derive(Debug)]
pub enum CallTerminal {
    /// `tool.result{denied}` durable.
    Denied { ack: DurableAck },
    /// `tool.result{aborted}` durable with the §2b error code.
    Aborted { ack: DurableAck, code: &'static str },
    /// The witness append/fsync failed (segment faulted, MGR-2): the tool
    /// did NOT run (spawn count 0). `best_effort` is `None` iff the
    /// best-effort `aborted/witness_write_failed` record landed; `Some(err)`
    /// when the faulted segment refused it too (ledger-fatal, §2b row 5).
    WitnessWriteFailed {
        witness: LedgerError,
        best_effort: Option<LedgerError>,
    },
    /// `tool.result{failed}` durable with the §2b error code.
    Failed { ack: DurableAck, code: &'static str },
    /// Helper ran but payload startup was not observed; never retried.
    Indeterminate { ack: DurableAck },
    /// `tool.result{succeeded}` durable with the merged terminal.
    Succeeded {
        ack: DurableAck,
        output_digest: String,
    },
}

/// Phase-1 outcome: either the call is authorized to attempt execution
/// (durable witness in hand) or it reached a terminal result.
#[derive(Debug)]
pub enum GatePass {
    Authorized(Box<ExecAuthorized>),
    Terminal(CallTerminal),
}

/// Everything phase 2 needs, all of it either durable or derived from
/// durable records.
#[derive(Debug)]
pub struct ExecAuthorized {
    pub started: AckedStart,
    pub pending: PendingDecision,
    pub matches: Vec<EvidenceMatch>,
    pub preflight: PreflightRecord,
}

/// The slice executor driver: binds a [`Gate`], an [`Executor`] and the
/// call context, and drives the S4 §3 order through the E2 token APIs.
pub struct SliceExec<'a> {
    pub gate: &'a Gate,
    pub executor: &'a Executor,
    pub authority_snapshot: &'a Value,
    pub cwd: &'a Path,
    pub user_id: &'a str,
}

impl SliceExec<'_> {
    /// The full pipeline (phases 1+2). The approval response is supplied by
    /// `responder` (the UI/harness affordance); the clock is read once per
    /// evaluation point (S2 §2).
    pub fn run_call(
        &self,
        ledger: &mut Ledger,
        acked: AckedCall,
        call: &ToolCall,
        clock: &dyn Fn() -> String,
        responder: &dyn Fn(&ApprovalPreview, &PendingDecision) -> ApprovalResponse,
        cancel: &CancelToken,
    ) -> Result<CallTerminal, ExecError> {
        match self.gate_and_witness(ledger, acked, call, clock, responder, cancel)? {
            GatePass::Terminal(t) => Ok(t),
            GatePass::Authorized(auth) => self.execute_witnessed(ledger, *auth, clock, cancel),
        }
    }

    /// Phase 1 (S4 §3 steps 3-7): authority → approval (if ask) → E3
    /// pre-witness check → preflight → durable witness. Cancel is supervised
    /// at the pre-witness boundaries (§2b row 4).
    pub fn gate_and_witness(
        &self,
        ledger: &mut Ledger,
        acked: AckedCall,
        call: &ToolCall,
        clock: &dyn Fn() -> String,
        responder: &dyn Fn(&ApprovalPreview, &PendingDecision) -> ApprovalResponse,
        cancel: &CancelToken,
    ) -> Result<GatePass, ExecError> {
        // Step 3: rule evaluate, `authority.decided` durable (E1).
        let auth = self.gate.authorize_call(
            ledger,
            acked,
            call,
            self.authority_snapshot,
            &clock(),
            self.cwd,
        )?;
        let (acked, pending) = match auth {
            // §2b row 1: rule/evaluator deny (denied/exec_forbidden side,
            // ledger error.code = the S2 reason, EXEC-35).
            Authorization::Denied { result, .. } => {
                return Ok(GatePass::Terminal(CallTerminal::Denied { ack: result }))
            }
            Authorization::Allow { acked, pending, .. } => (acked, pending),
            Authorization::Ask {
                acked,
                preview,
                pending,
                ..
            } => {
                // §2b row 4: cancel while the request is pending.
                if cancel.is_cancelled() {
                    let ack = self.cancel_pending(ledger, acked, &pending)?;
                    return Ok(GatePass::Terminal(CallTerminal::Aborted {
                        ack,
                        code: code::CANCELLED,
                    }));
                }
                let response = responder(&preview, &pending);
                // The token may have fired while awaiting the response.
                if cancel.is_cancelled() {
                    let ack = self.cancel_pending(ledger, acked, &pending)?;
                    return Ok(GatePass::Terminal(CallTerminal::Aborted {
                        ack,
                        code: code::CANCELLED,
                    }));
                }
                // Step 4: `approval.resolved` (E2 recompute inside the gate).
                match self.gate.resolve_approval(
                    ledger,
                    acked,
                    pending,
                    response,
                    self.user_id,
                    &clock(),
                    self.authority_snapshot,
                )? {
                    // §2b row 2/3: user deny or engine denial at the response.
                    ApprovalOutcome::Denied { result, .. } => {
                        return Ok(GatePass::Terminal(CallTerminal::Denied { ack: result }))
                    }
                    ApprovalOutcome::Allowed { acked, pending, .. } => (acked, pending),
                }
            }
        };
        let matches = self
            .gate
            .evaluator
            .rule_matches(&pending.tool_name, &pending.args);
        // §2b row 4 / S1 §4a row 6 (FX-21): cancel after allow, before the
        // witness. No `approval.resolved{cancelled}` here: RATIFY-3 closes
        // only PENDING approvals, and this one is already resolved.
        if cancel.is_cancelled() {
            let ack = self.abort_pre_witness(ledger, acked, &pending, code::CANCELLED, None)?;
            return Ok(GatePass::Terminal(CallTerminal::Aborted {
                ack,
                code: code::CANCELLED,
            }));
        }
        // Step 5: pre-effect re-evaluation (E3) inside the gate.
        let acked = match self.gate.pre_witness_check(
            ledger,
            acked,
            pending.clone(),
            &clock(),
            self.authority_snapshot,
        )? {
            PreWitness::Cleared { acked } => acked,
            // §2b row 3 / S1 §4a row 4: refused after approval, before the
            // witness (denied, terminal_decision state=refused).
            PreWitness::Refused { result } => {
                return Ok(GatePass::Terminal(CallTerminal::Denied { ack: result }))
            }
        };
        // Step 6: preflight (read-only, no child). A failure is
        // executor-class, never `denied` (S4 §3), and no `tool.started` is
        // written (§2b row 6).
        let argv = argv_of(&pending.args).unwrap_or_default();
        let roots = &self.gate.evaluator.manifest.tools[&pending.tool_name].read_roots;
        let preflight = match self
            .executor
            .preflight_with_read_roots(&argv, self.cwd, roots)
        {
            Ok(p) => p,
            Err(f) => {
                let evidence = self.evidence_for(
                    &pending,
                    &matches,
                    None,
                    Some(&f),
                    ChildSpawned::False,
                    None,
                );
                let ack =
                    self.abort_pre_witness(ledger, acked, &pending, f.code, Some(evidence))?;
                return Ok(GatePass::Terminal(CallTerminal::Aborted {
                    ack,
                    code: f.code,
                }));
            }
        };
        // Step 7: the durable start-witness.
        self.witness(ledger, acked, &pending, matches, preflight)
    }

    /// Step 7: the durable start-witness (RULING-5, I-05a). On an
    /// append/fsync failure the segment is faulted (MGR-2) and the tool MUST
    /// NOT run: a best-effort `aborted/witness_write_failed` is attempted
    /// (§2b row 5; the faulted segment may refuse that append too, which is
    /// ledger-fatal) and no spawn happens.
    pub fn witness(
        &self,
        ledger: &mut Ledger,
        acked: AckedCall,
        pending: &PendingDecision,
        matches: Vec<EvidenceMatch>,
        preflight: PreflightRecord,
    ) -> Result<GatePass, ExecError> {
        let payload = crate::authority::ToolStarted {
            call_id: pending.call_id.clone(),
            governing_decision_id: pending.decision_id.clone(),
            executor_id: self.executor.id.clone(),
            args_digest: pending.digests.args_digest.clone(),
        };
        match ledger.record_tool_started(&acked, payload) {
            Ok(started) => Ok(GatePass::Authorized(Box::new(ExecAuthorized {
                started,
                pending: pending.clone(),
                matches,
                preflight,
            }))),
            Err(witness_err) => {
                let evidence = self.evidence_for(
                    pending,
                    &matches,
                    Some(&preflight),
                    None,
                    ChildSpawned::False,
                    None,
                );
                let result = pre_witness_abort_result(
                    &pending.call_id,
                    &pending.decision_id,
                    code::WITNESS_WRITE_FAILED,
                    crate::delivery::pre_witness_abort(code::WITNESS_WRITE_FAILED),
                    Some(evidence),
                );
                let best_effort = ledger.record_pre_witness_result(acked, result).err();
                Ok(GatePass::Terminal(CallTerminal::WitnessWriteFailed {
                    witness: witness_err,
                    best_effort,
                }))
            }
        }
    }

    /// Phase 2 (S4 §3 steps 8-9): spawn, supervise (cancel only, D-24), E4
    /// incorporation, terminal `tool.result`.
    pub fn execute_witnessed(
        &self,
        ledger: &mut Ledger,
        auth: ExecAuthorized,
        clock: &dyn Fn() -> String,
        cancel: &CancelToken,
    ) -> Result<CallTerminal, ExecError> {
        let ExecAuthorized {
            started,
            pending,
            matches,
            preflight,
        } = auth;
        if argv_of(&pending.args).as_ref() != Some(&preflight.argv)
            || (self.executor.platform == Platform::Linux
                && pending.targets.get("cwd").and_then(Value::as_str) != preflight.cwd.to_str())
        {
            let evidence = self.evidence_for(
                &pending,
                &matches,
                Some(&preflight),
                None,
                ChildSpawned::False,
                None,
            );
            let result = post_witness_failed_result(
                &pending,
                code::SANDBOX_LAUNCH_FAILED,
                StartState::NotStarted,
                Some(evidence),
                None,
            );
            let ack = ledger.record_post_witness_result(started, result)?;
            return Ok(CallTerminal::Failed {
                ack,
                code: code::SANDBOX_LAUNCH_FAILED,
            });
        }
        let supervised = match self.executor.spawn_supervise(&preflight, cancel) {
            Err(SpawnFailure::SupervisionLost(_)) => {
                let ack = self.supervision_lost(ledger, started, &pending, &matches, &preflight)?;
                return Ok(CallTerminal::Indeterminate { ack });
            }
            Err(SpawnFailure::NotSpawned(_)) => {
                // §2b row 7: spawn error after the witness.
                let evidence = self.evidence_for(
                    &pending,
                    &matches,
                    Some(&preflight),
                    None,
                    ChildSpawned::False,
                    None,
                );
                let result = post_witness_failed_result(
                    &pending,
                    code::SANDBOX_LAUNCH_FAILED,
                    StartState::NotStarted,
                    Some(evidence),
                    None,
                );
                let ack = ledger.record_post_witness_result(started, result)?;
                return Ok(CallTerminal::Failed {
                    ack,
                    code: code::SANDBOX_LAUNCH_FAILED,
                });
            }
            Ok(s) => s,
        };
        if !supervised.payload_started {
            let ack = self.supervision_lost(ledger, started, &pending, &matches, &preflight)?;
            return Ok(CallTerminal::Indeterminate { ack });
        }
        if supervised.timed_out {
            // F-H1: the child outlived the executor's wall-clock bound and was
            // killed and reaped. What the payload did is unknown, so this uses
            // the existing typed `indeterminate` terminal — no new wire field,
            // event or error code.
            let ack = self.supervision_lost(ledger, started, &pending, &matches, &preflight)?;
            return Ok(CallTerminal::Indeterminate { ack });
        }
        if supervised.cancelled {
            // §2b row 9: cancel after the witness, child killed and reaped.
            let evidence = self.evidence_for(
                &pending,
                &matches,
                Some(&preflight),
                None,
                ChildSpawned::True,
                Some(supervised.outcome),
            );
            let result = post_witness_failed_result(
                &pending,
                code::CANCELLED_AFTER_START,
                StartState::Started,
                Some(evidence),
                None,
            );
            let ack = ledger.record_post_witness_result(started, result)?;
            return Ok(CallTerminal::Failed {
                ack,
                code: code::CANCELLED_AFTER_START,
            });
        }
        let completed_ok =
            supervised.outcome.exit_code == Some(0) && supervised.outcome.signal.is_none();
        if !completed_ok {
            // §2b row 8: child exit ≠ 0 or signalled.
            let evidence = self.evidence_for(
                &pending,
                &matches,
                Some(&preflight),
                None,
                ChildSpawned::True,
                Some(supervised.outcome),
            );
            let result = post_witness_failed_result(
                &pending,
                code::EXECUTION_FAILED,
                StartState::Started,
                Some(evidence),
                None,
            );
            let ack = ledger.record_post_witness_result(started, result)?;
            return Ok(CallTerminal::Failed {
                ack,
                code: code::EXECUTION_FAILED,
            });
        }
        // E4 incorporation after `completed_ok` (T3a S2 §3.12): digest
        // consistency + revocation epoch; any rejection → `approved→refused`
        // → failed/incorporation_refused, start_state=started (S1 §4a row
        // 10, FX-23).
        if let Some(reason_code) = self.gate.evaluator.revalidate(
            &pending.digests,
            &pending.tool_name,
            &pending.args,
            &pending.targets,
            self.authority_snapshot,
        )? {
            let terminal = followup_decision(
                &pending,
                DecisionState::Refused,
                reason_code,
                &crate::evaluator::explanation_for(reason_code),
                json!({}),
                ActorKind::PolicyEngine,
                "engine",
                &clock(),
            );
            let evidence = self.evidence_for(
                &pending,
                &matches,
                Some(&preflight),
                None,
                ChildSpawned::True,
                Some(supervised.outcome),
            );
            let result = post_witness_failed_result(
                &pending,
                code::INCORPORATION_REFUSED,
                StartState::Started,
                Some(evidence),
                Some(terminal),
            );
            let ack = ledger.record_post_witness_result(started, result)?;
            return Ok(CallTerminal::Failed {
                ack,
                code: code::INCORPORATION_REFUSED,
            });
        }
        // §2b row 11: exit 0 → merged + succeeded (I-14). Incorporation
        // (AUD-4): merged rides in the same atomic `tool.result` with
        // effect_incorporated{target_kind: tool_result, target_id:
        // output_digest} (T3a S2 §4, ledger-enforced; EXEC-33 flags the T3b
        // §5 own-event_id variant as a divergence).
        let output_digest = supervised.stdout_digest.clone();
        let merged = followup_decision(
            &pending,
            DecisionState::Merged,
            code::EFFECT_INCORPORATED,
            &explanation_for(code::EFFECT_INCORPORATED),
            json!({"target_kind": "tool_result", "target_id": output_digest}),
            ActorKind::PolicyEngine,
            "engine",
            &clock(),
        );
        let evidence = self.evidence_for(
            &pending,
            &matches,
            Some(&preflight),
            None,
            ChildSpawned::True,
            Some(supervised.outcome.clone()),
        );
        let result = ToolResult {
            call_id: pending.call_id.clone(),
            status: ResultStatus::Succeeded,
            output: Some(
                serde_json::to_value(&supervised.outcome.stdout).map_err(LedgerError::Json)?,
            ),
            output_digest: Some(output_digest.clone()),
            error: None,
            effect_ids: vec![],
            governing_decision_id: Some(merged.decision_id.clone()),
            terminal_decision: Some(merged),
            start_state: StartState::Started,
            model_delivery: crate::delivery::succeeded(),
            origin: None,
            recovered_by: None,
            executor_evidence: Some(evidence),
        };
        let ack = ledger.record_post_witness_result(started, result)?;
        Ok(CallTerminal::Succeeded { ack, output_digest })
    }

    /// §2b row 10 / S1 §4a row 13: after the durable witness, no typed
    /// outcome arrived (reap unconfirmed, panic, dropped future, channel
    /// closed). RATIFY-1 as amended: the live closer MAY write
    /// `indeterminate{origin: live}` — terminal, never retried.
    pub fn supervision_lost(
        &self,
        ledger: &mut Ledger,
        started: AckedStart,
        pending: &PendingDecision,
        matches: &[EvidenceMatch],
        preflight: &PreflightRecord,
    ) -> Result<DurableAck, ExecError> {
        let evidence = self.evidence_for(
            pending,
            matches,
            Some(preflight),
            None,
            ChildSpawned::Unknown,
            None,
        );
        let result = ToolResult {
            call_id: pending.call_id.clone(),
            status: ResultStatus::Indeterminate,
            output: None,
            output_digest: None,
            error: Some(ResultError {
                code: code::EXECUTION_SUPERVISION_LOST.to_owned(),
                message: explanation_for(code::EXECUTION_SUPERVISION_LOST),
                retryable: false, // indeterminate is never auto-retried (RULING-3)
            }),
            effect_ids: vec![],
            governing_decision_id: Some(pending.decision_id.clone()),
            terminal_decision: None,
            start_state: StartState::Unknown,
            model_delivery: crate::delivery::indeterminate_live(),
            origin: Some(IndeterminateOrigin::Live),
            recovered_by: None,
            executor_evidence: Some(evidence),
        };
        Ok(ledger.record_post_witness_result(started, result)?)
    }

    /// §2b row 4 (cancel while the approval is pending):
    /// `approval.resolved{cancelled, turn_cancelled}` — engine actor, no
    /// reviewer, no DecisionRecord (RATIFY-3 as amended) — then the terminal
    /// `tool.result{aborted, cancelled}`. Cancel is never recorded as a
    /// denial (AUD-6).
    pub fn cancel_pending(
        &self,
        ledger: &mut Ledger,
        acked: AckedCall,
        pending: &PendingDecision,
    ) -> Result<DurableAck, ExecError> {
        ledger.record_approval_resolved(
            &acked,
            ApprovalResolved {
                resolution: Resolution::Cancelled,
                actor: ActorRef {
                    kind: ActorKind::PolicyEngine,
                    stable_id: "engine".into(),
                },
                user_response: None,
                reason: None,
                cancel_cause: Some(CancelCause::TurnCancelled),
                decision: None,
            },
        )?;
        self.abort_pre_witness(ledger, acked, pending, code::CANCELLED, None)
    }

    /// The terminal `tool.result{aborted}` before the witness (§2b rows
    /// 4-6): `start_state=not_started` (RATIFY-2: provably never ran),
    /// `caused_by` the call's last durable event (ledger-derived).
    /// `retryable` only for `cancelled` (S5 §5).
    pub fn abort_pre_witness(
        &self,
        ledger: &mut Ledger,
        acked: AckedCall,
        pending: &PendingDecision,
        abort_code: &'static str,
        evidence: Option<ExecutorEvidence>,
    ) -> Result<DurableAck, ExecError> {
        // A-3 (SPEC-ERRATA-1): the normative status→model_delivery table.
        // Cancels deliver nothing, a witness-write failure is Fatal, a
        // preflight abort is Deliver.
        let delivery = crate::delivery::pre_witness_abort(abort_code);
        Ok(ledger.record_pre_witness_result(
            acked,
            pre_witness_abort_result(
                &pending.call_id,
                &pending.decision_id,
                abort_code,
                delivery,
                evidence,
            ),
        )?)
    }

    /// Assemble the MGR-13 evidence with the declared read roots of the
    /// call's manifest entry (S4 §1b).
    fn evidence_for(
        &self,
        pending: &PendingDecision,
        matches: &[EvidenceMatch],
        preflight: Option<&PreflightRecord>,
        failure: Option<&PreflightFailure>,
        spawned: ChildSpawned,
        child: Option<ChildOutcome>,
    ) -> ExecutorEvidence {
        let roots = self
            .gate
            .evaluator
            .manifest
            .tools
            .get(&pending.tool_name)
            .map(|e| e.read_roots.clone())
            .unwrap_or_default();
        self.executor
            .evidence(&roots, matches, preflight, failure, spawned, child)
    }
}

/// `tool.result{aborted}` pre-witness (§2b rows 4-6). Executor-class
/// explanation from the catalog (MGR-5).
fn pre_witness_abort_result(
    call_id: &str,
    governing_decision_id: &str,
    abort_code: &'static str,
    delivery: ModelDelivery,
    evidence: Option<ExecutorEvidence>,
) -> ToolResult {
    ToolResult {
        call_id: call_id.to_owned(),
        status: ResultStatus::Aborted,
        output: None,
        output_digest: None,
        error: Some(ResultError {
            code: abort_code.to_owned(),
            message: explanation_for(abort_code),
            // A-3: retryable=true only for `cancelled`.
            retryable: crate::delivery::retryable_for(abort_code),
        }),
        effect_ids: vec![],
        governing_decision_id: Some(governing_decision_id.to_owned()),
        terminal_decision: None,
        start_state: StartState::NotStarted,
        model_delivery: delivery,
        origin: None,
        recovered_by: None,
        executor_evidence: evidence,
    }
}

/// `tool.result{failed}` post-witness (§2b rows 7-10 + S1 §4a row 10).
fn post_witness_failed_result(
    pending: &PendingDecision,
    fail_code: &'static str,
    start_state: StartState,
    evidence: Option<ExecutorEvidence>,
    terminal: Option<crate::authority::DecisionRecord>,
) -> ToolResult {
    let governing = terminal
        .as_ref()
        .map(|t| t.decision_id.clone())
        .unwrap_or_else(|| pending.decision_id.clone());
    ToolResult {
        call_id: pending.call_id.clone(),
        status: ResultStatus::Failed,
        output: None,
        output_digest: None,
        error: Some(ResultError {
            code: fail_code.to_owned(),
            message: explanation_for(fail_code),
            // A-3: post-witness rows are never retryable (no `cancelled`
            // code reaches this builder — a cancel past the witness is
            // `cancelled_after_start`, S4 §2b row 9).
            retryable: crate::delivery::retryable_for(fail_code),
        }),
        effect_ids: vec![],
        governing_decision_id: Some(governing),
        terminal_decision: terminal,
        start_state,
        model_delivery: crate::delivery::post_witness_failed(),
        origin: None,
        recovered_by: None,
        executor_evidence: evidence,
    }
}
