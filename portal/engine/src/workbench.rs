//! Derived workbench view. functional-app-v2 remains the only effect ledger.
use crate::functional::AppSnapshot;
use serde::{Deserialize, Serialize};
use ts_rs::TS;
#[derive(Debug, Clone, Serialize, Deserialize, TS, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Lifecycle {
    Idle,
    Running,
    WaitingApproval,
    Blocked,
    Completed,
    Failed,
    CancelRequested,
    Cancelled,
    Unknown,
}
#[derive(Debug, Clone, Serialize, Deserialize, TS)]
#[serde(rename_all = "snake_case")]
pub enum ConnectionState {
    Ready,
    Connecting,
    Disconnected,
    Unknown,
}
#[derive(Debug, Clone, Serialize, Deserialize, TS)]
#[serde(rename_all = "snake_case")]
pub enum AdapterKind {
    ModelOnly,
    AgentRuntime,
}
#[derive(Debug, Clone, Serialize, Deserialize, TS)]
#[serde(rename_all = "snake_case")]
pub enum EffectOwner {
    CleanroomLocal,
    BobRuntime,
}
#[derive(Debug, Clone, Serialize, Deserialize, TS)]
pub struct Capabilities {
    pub chat: bool,
    pub project_tools: bool,
    pub writes: bool,
    pub multi_agent: bool,
    pub workflows: bool,
    pub cancel: bool,
    pub steer: bool,
    pub reconnect: bool,
}
#[derive(Debug, Clone, Serialize, Deserialize, TS)]
pub struct RuntimeProfile {
    pub id: String,
    pub label: String,
    pub adapter_kind: AdapterKind,
    pub execution_host: String,
    pub model_label: Option<String>,
    pub memory_owner: String,
    pub effect_owner: EffectOwner,
    pub configured: bool,
    pub capabilities: Capabilities,
    pub unavailable_reason: Option<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize, TS)]
pub struct AgentView {
    pub id: String,
    pub name: String,
    pub role: String,
    pub parent_agent_id: Option<String>,
    pub runtime_profile_id: String,
    pub execution_host: String,
    pub model_label: Option<String>,
    pub run_id: String,
    pub task_id: String,
    pub step_id: Option<String>,
    pub state: Lifecycle,
    pub source_event_ids: Vec<String>,
    pub summary: String,
}
#[derive(Debug, Clone, Serialize, Deserialize, TS)]
pub struct WorkflowStep {
    pub id: String,
    pub parent_step_id: Option<String>,
    pub depends_on: Vec<String>,
    pub agent_id: Option<String>,
    pub label: String,
    pub state: Lifecycle,
    pub approval_id: Option<String>,
    pub output_ids: Vec<String>,
    pub source_event_ids: Vec<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize, TS)]
pub struct WorkflowView {
    pub id: String,
    pub run_id: String,
    pub session_id: Option<String>,
    pub title: String,
    pub state: Lifecycle,
    pub steps: Vec<WorkflowStep>,
}
#[derive(Debug, Clone, Serialize, Deserialize, TS)]
#[serde(rename_all = "snake_case")]
pub enum OutputKind {
    File,
    Diff,
    Text,
    ToolResult,
}
#[derive(Debug, Clone, Serialize, Deserialize, TS)]
pub struct OutputView {
    pub id: String,
    pub run_id: String,
    pub step_id: Option<String>,
    pub agent_id: Option<String>,
    pub kind: OutputKind,
    pub title: String,
    pub path: Option<String>,
    pub text: Option<String>,
    pub source_event_ids: Vec<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize, TS)]
#[serde(deny_unknown_fields)]
pub struct ModelChoice {
    pub id:String,pub label:String,pub model_pin:String,pub configured:bool,
    pub reachability:String,pub image_input:bool,pub reason:Option<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize, TS)]
pub struct LocalObservation {
    pub session_id: String,
    pub request_id: Option<String>,
    pub events: Vec<crate::functional::Event>,
}
#[derive(Debug, Clone, Serialize, Deserialize, TS)]
pub struct WorkbenchSnapshot {
    // Read-only durable events while app is unavailable under the model lock.
    #[serde(default)]
    pub local_observation: Option<LocalObservation>,
    pub models: Vec<ModelChoice>,
    pub schema_version: String,
    pub app: Option<AppSnapshot>,
    pub profiles: Vec<RuntimeProfile>,
    pub active_profile_id: String,
    pub connection_state: ConnectionState,
    pub agents: Vec<AgentView>,
    pub workflows: Vec<WorkflowView>,
    pub outputs: Vec<OutputView>,
    pub notices: Vec<String>,
}
pub fn bindings() -> String {
    let c = ts_rs::Config::default();
    let types = [
        Lifecycle::decl(&c),
        ConnectionState::decl(&c),
        AdapterKind::decl(&c),
        EffectOwner::decl(&c),
        Capabilities::decl(&c),
        RuntimeProfile::decl(&c),
        AgentView::decl(&c),
        WorkflowStep::decl(&c),
        WorkflowView::decl(&c),
        OutputKind::decl(&c),
        OutputView::decl(&c),
        ModelChoice::decl(&c),
        LocalObservation::decl(&c),
        WorkbenchSnapshot::decl(&c),
    ];
    format!("// GENERATED from workbench Rust types; cargo run --bin bob-workbench-wiregen\nimport type {{ AppSnapshot }} from './functional-protocol';\nimport type {{ Event }} from './functional-protocol';\n{}\n",types.iter().map(|t|format!("export {t}")).collect::<Vec<_>>().join("\n"))
}

#[path = "workbench_projection.rs"]
mod projection;
pub use projection::project_local;

#[path = "workbench_http.rs"]
mod http;
pub use http::StandaloneConfig;

#[path = "workbench_bob.rs"]
mod bob;
pub use bob::{bob_state, project_bob, valid_run_id};
