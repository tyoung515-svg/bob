//! S4 §1 slice policy constants (T3b): the fixed slice sandbox policy with
//! NO runtime selection surface. `SandboxPolicy = ReadOnly{network_access:
//! false}`, `SandboxablePreference = Auto`, backend `LinuxBubblewrap`
//! (`None`/Landlock unreachable), approval action `ExecCommand` only, and
//! argv-form = one segment. Fail-closed: no weaker backend, widened authority
//! or unlabelled default on any path [S4.1].
//!
//! Platform posture (D-18, EXEC-31): the Linux backend is absent on the
//! Windows build host; the executor records `platform_deferred` as the
//! backend label in `ExecutorEvidence` and never claims sandboxed execution
//! that did not happen. There is no weaker-backend fallback: on Linux a
//! missing `bwrap` probe is a preflight failure, not a downgrade.

use serde::{Deserialize, Serialize};

use crate::authority::EffectClass;

/// The slice tool (S4 §1b): one local command tool, argv-form.
pub const TOOL_NAME: &str = "exec_command";
/// The FX-READ-01 fixture command token (logical argv[0]; the host program is
/// provisioned by the executor, EXEC-32).
pub const FX_READ_COMMAND: &str = "fx-read";
/// `effect_class=read`, declared in the tool registry (never inferred from
/// argv, S4 §1b).
pub const EFFECT_CLASS: EffectClass = EffectClass::Read;
/// Approval action vocabulary: `ExecCommand` only (S4 §1, D-22).
pub const APPROVAL_ACTION: &str = "exec_command";
/// The required sandbox backend id (S4 §1); `None`/Landlock are unreachable.
pub const BACKEND_ID: &str = "linux_bubblewrap";
/// Honest backend label when the slice runs on a host without the Linux
/// sandbox backend (D-18): recorded in `ExecutorEvidence.backend`, never
/// claimed as sandboxed execution.
pub const BACKEND_PLATFORM_DEFERRED: &str = "platform_deferred";
/// Read roots (ro-bind) minus the per-fixture root (S4 §1b); see
/// [`slice_read_roots`].
pub const READ_ROOTS: [&str; 4] = ["/usr", "/lib", "/lib64", "/bin"];
/// The slice network posture (ReadOnly policy constant).
pub const NETWORK_ACCESS: bool = false;

/// `SandboxPolicy` (S4 §1): the slice fixes `ReadOnly{network_access:false}`;
/// no other variant exists in the slice vocabulary.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "policy", rename_all = "snake_case")]
pub enum SandboxPolicy {
    ReadOnly { network_access: bool },
}

/// The fixed slice sandbox policy (T3b S4 §1). A constant, not a config.
pub const SANDBOX_POLICY: SandboxPolicy = SandboxPolicy::ReadOnly {
    network_access: NETWORK_ACCESS,
};

/// `SandboxablePreference` (S4 §1): fixed to `Auto`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SandboxablePreference {
    Auto,
}

/// The fixed slice preference (T3b S4 §1).
pub const SANDBOXABLE_PREFERENCE: SandboxablePreference = SandboxablePreference::Auto;

/// The full read-root set for a fixture root: `<fx-root>/slice` plus the
/// four fixed roots (S4 §1b).
pub fn slice_read_roots(fx_root: &std::path::Path) -> Vec<String> {
    let mut roots = vec![fx_root.join("slice").to_string_lossy().into_owned()];
    roots.extend(READ_ROOTS.iter().map(|s| s.to_string()));
    roots
}
