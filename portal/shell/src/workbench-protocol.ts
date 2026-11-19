// GENERATED from workbench Rust types; cargo run --bin bob-workbench-wiregen
import type { AppSnapshot } from './functional-protocol';
import type { Event } from './functional-protocol';
export type Lifecycle = "idle" | "running" | "waiting_approval" | "blocked" | "completed" | "failed" | "cancel_requested" | "cancelled" | "unknown";
export type ConnectionState = "ready" | "connecting" | "disconnected" | "unknown";
export type AdapterKind = "model_only" | "agent_runtime";
export type EffectOwner = "cleanroom_local" | "bob_runtime";
export type Capabilities = { chat: boolean, project_tools: boolean, writes: boolean, multi_agent: boolean, workflows: boolean, cancel: boolean, steer: boolean, reconnect: boolean, };
export type RuntimeProfile = { id: string, label: string, adapter_kind: AdapterKind, execution_host: string, model_label: string | null, memory_owner: string, effect_owner: EffectOwner, configured: boolean, capabilities: Capabilities, unavailable_reason: string | null, };
export type AgentView = { id: string, name: string, role: string, parent_agent_id: string | null, runtime_profile_id: string, execution_host: string, model_label: string | null, run_id: string, task_id: string, step_id: string | null, state: Lifecycle, source_event_ids: Array<string>, summary: string, };
export type WorkflowStep = { id: string, parent_step_id: string | null, depends_on: Array<string>, agent_id: string | null, label: string, state: Lifecycle, approval_id: string | null, output_ids: Array<string>, source_event_ids: Array<string>, };
export type WorkflowView = { id: string, run_id: string, session_id: string | null, title: string, state: Lifecycle, steps: Array<WorkflowStep>, };
export type OutputKind = "file" | "diff" | "text" | "tool_result";
export type OutputView = { id: string, run_id: string, step_id: string | null, agent_id: string | null, kind: OutputKind, title: string, path: string | null, text: string | null, source_event_ids: Array<string>, };
export type ModelChoice = { id: string, label: string, model_pin: string, configured: boolean, reachability: string, image_input: boolean, reason: string | null, };
export type LocalObservation = { session_id: string, request_id: string | null, events: Array<Event>, };
export type WorkbenchSnapshot = { local_observation: LocalObservation | null, models: Array<ModelChoice>, schema_version: string, app: AppSnapshot | null, profiles: Array<RuntimeProfile>, active_profile_id: string, connection_state: ConnectionState, agents: Array<AgentView>, workflows: Array<WorkflowView>, outputs: Array<OutputView>, notices: Array<string>, };
