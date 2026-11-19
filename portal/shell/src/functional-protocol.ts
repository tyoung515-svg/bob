// GENERATED from functional-app-v2 Rust types; run cargo run --bin bob-app-wiregen
export type Message = { role: string, content: string, };
export type Event = { schema_version: string, event_id: string, previous_event_id: string | null, previous_digest: string | null, ordinal: string, kind: string, data: unknown, digest: string, };
export type DecisionV2 = { record_id: string, previous_record_id: string | null, decision_id: string, state: string, reason: string, };
export type Pending = { id: string, call_id: string, digest: string, path: string, before: string | null, content: string, diff: string, decision: DecisionV2, created_at: number, };
export type Project = { root: string, dev: number, ino: number, };
export type Session = { id: string, messages: Array<Message>, receipts: unknown[], project: Project | null, events: Array<Event>, pending: Pending | null, rounds_left: number, };
export type SavedSession = { group_id: string | null, pinned: boolean, archived: boolean, id: string, title: string, project: string | null, error: string | null, };
export type OrganizationalProject = { id: string, name: string, description: string, instructions: string, version: number, digest: string, collapsed: boolean, };
export type AppSnapshot = { group_id: string | null, organizational_projects: Array<OrganizationalProject>, selected_model_id: string,
/**
 * Derived from validated event order; never serialized into saved history or model wire.
 */
message_provenance: Array<string>, session: Session, saved: Array<SavedSession>, data_root: string, };
