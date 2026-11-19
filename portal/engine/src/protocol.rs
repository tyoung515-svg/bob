//! S5 protocol layer (T3b S5, E5): the versioned NDJSON frame envelope of
//! the `slice-v1` event set and the harness-side request/response wire.
//!
//! - Frames: every ledger-backed frame carries `seq` (connection-level,
//!   gap-free, first event `seq=1`, independent of `ordinal`), the backing
//!   record's `event_id`, `ts`, `schema_version "1.0"`, `ordinal` (base 1,
//!   u64-string), `caused_by` per RULING-8 and `correlation_id = call_id` on
//!   the six call events (S5 §1). A frame is built ONLY from a record that
//!   is already durable (ledger-before-offer, S5 §2) — the driver reads the
//!   record back from the fdatasynced segment before offering it.
//! - Event set `slice-v1` (R4): `turn.started`, `tool.call`,
//!   `authority.decided`, `approval.requested`, `approval.resolved`,
//!   `tool.started`, `tool.result`, `turn.completed`. `stream.*`, `item.*`,
//!   `notice.diagnostic`, `interaction.requested` and `tool.completed` are
//!   RETIRED with no alias (D-43, AUD-8).
//! - Requests (S5 §2): `hello` first (`HelloAck` is the `result`, no `seq`,
//!   no credit; `selected_version` major 2; `features=[]`), `session.create`,
//!   `turn.start`, `interaction.resolve` (echoing `call_id`, `decision_id`,
//!   `args_digest`; mismatch → `approval_mismatch`, nothing appended, the
//!   request stays pending; a closed request → `approval_closed`),
//!   `event.ack` (contiguous `through_seq`). Unknown method →
//!   `method_not_supported`. Duplicate `request_id` replays the cached
//!   response for the connection's life.
//! - `hello` carries the `schema_hash`: sha256 over the JCS of the canonical
//!   slice-v1 frame-schema description, so both sides can verify they agree
//!   on the envelope before any frame flows.
//!
//! The `v2.yaml` IDL typing of §2/§3 payloads is closed in code by
//! [`crate::wire_schema`] (A-10): that model is the single normative statement
//! of the envelope, the payloads and the request set, `schema_hash` below is
//! computed from it, and the committed TypeScript client
//! (`shell/src/protocol.ts`) is generated from it with a drift check. Tauri IPC
//! byte-equivalence (STALE-9) is deferred with S10; this module is the NDJSON
//! stdio binding (S5 §1 REQUIRED).

use std::io::{self, Seek, SeekFrom};
use std::path::PathBuf;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use time::OffsetDateTime;

use crate::digest::sha256_digest;
use crate::envelope::Record;
use crate::replay::{is_valid_timestamp, TS_FORMAT};

/// Negotiated protocol version (S5 §1: `selected_version` major 2).
pub const PROTOCOL_MAJOR: u64 = 2;
pub const PROTOCOL_MINOR: u64 = 0;

/// The slice event set name (RULING-4): additive changes allowed, renames
/// need a ruling.
pub const EVENT_SET: &str = "slice-v1";

/// Advertised features: none in the slice (S5 §1).
pub const FEATURES: [&str; 0] = [];

// Carried limits (S5 §3 ranges, slice-selected values).
/// Frames must pass records of 1 MiB or more (LT-4).
pub const MAX_FRAME_BYTES: u64 = 1024 * 1024;
pub const MAX_JSON_DEPTH: u32 = 32;
pub const ID_MAX_BYTES: usize = 128;
/// RULING-7: `deny.reason` free text ≤ 1024 B.
pub const DENY_REASON_MAX_BYTES: usize = 1024;

/// The `slice-v1` event set (RULING-4 + `turn.started`/`turn.completed`
/// carriers, AUD-8). `lineage`, `turn.start`, `step.context` and `turn.end`
/// are S1 kinds with no 1:1 frame.
///
/// A-10: the list is owned by [`crate::wire_schema`] and re-exported here, so
/// the frame set the engine emits, the `schema_hash` and the generated TS
/// client cannot name different events.
pub const EVENTS: [&str; 8] = crate::wire_schema::EVENTS;

/// Retired for the slice, no alias (D-43): no `stream.*`, no `item.*`, no
/// `notice.diagnostic`, and the pre-repair interaction/result names.
pub const RETIRED_EVENTS: [&str; 7] = [
    "stream.started",
    "stream.completed",
    "item.started",
    "item.completed",
    "notice.diagnostic",
    "interaction.requested",
    "tool.completed",
];

/// True iff `name` is a `slice-v1` event. Retired names are rejected, not
/// aliased (D-43).
pub fn is_slice_event(name: &str) -> bool {
    EVENTS.contains(&name)
}

/// The canonical description of the slice-v1 frame schema: envelope field
/// set + the payload field sets of S5 §3. Hashed (JCS + sha256) into
/// [`schema_hash`]; both ends compare the hash in `hello`.
///
/// A-10 (EXEC-49): the description is DERIVED from
/// [`crate::wire_schema`] — the same model the committed TypeScript client is
/// generated from — instead of being a second field-list literal here. The
/// value is unchanged by that move
/// (`e11_codegen::schema_hash_is_unchanged_by_the_model_move` pins it).
pub fn schema_description() -> Value {
    crate::wire_schema::schema_description()
}

/// sha256 over the JCS of [`schema_description`] — the value carried in
/// `hello`/HelloAck so both ends verify the same slice-v1 frame schema.
pub fn schema_hash() -> String {
    let bytes = crate::digest::jcs_bytes(&schema_description())
        .expect("schema description is JCS-encodable");
    sha256_digest(&bytes)
}

/// Wire error codes (S5 §2 + `method_not_supported`). A-4: the spellings are
/// owned by the single normative table ([`crate::errcode`]).
pub mod wire_code {
    pub const VERSION_MISMATCH: &str = crate::errcode::VERSION_MISMATCH;
    pub const METHOD_NOT_SUPPORTED: &str = crate::errcode::METHOD_NOT_SUPPORTED;
    pub const INVALID_REQUEST: &str = crate::errcode::INVALID_REQUEST;
    pub const INVALID_LIMITS: &str = crate::errcode::INVALID_LIMITS;
    pub const APPROVAL_MISMATCH: &str = crate::errcode::APPROVAL_MISMATCH;
    pub const APPROVAL_CLOSED: &str = crate::errcode::APPROVAL_CLOSED;
    pub const INTERNAL_ERROR: &str = crate::errcode::INTERNAL_ERROR;
}

/// A typed wire error (`error` half of a response).
#[derive(Debug, Clone)]
pub struct WireError {
    pub code: &'static str,
    pub message: String,
    pub data: Option<Value>,
}

impl WireError {
    pub fn new(code: &'static str, message: impl Into<String>) -> WireError {
        WireError {
            code,
            message: message.into(),
            data: None,
        }
    }

    pub fn with_data(mut self, data: Value) -> WireError {
        self.data = Some(data);
        self
    }

    pub fn to_value(&self) -> Value {
        let mut e = json!({
            "code": self.code,
            "message": self.message,
        });
        if let Some(d) = &self.data {
            e["data"] = d.clone();
        }
        e
    }
}

/// One event frame on the wire. `seq` is the connection-level gap-free
/// counter starting at 1; `ordinal`/`event_id`/`ts`/`caused_by`/
/// `correlation_id` come verbatim from the durable backing record.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Frame {
    pub seq: u64,
    pub event: String,
    pub schema_version: String,
    pub event_id: String,
    pub ts: String,
    /// u64-string, base 1 (the backing record's ordinal).
    pub ordinal: String,
    pub correlation_id: Option<String>,
    pub caused_by: Option<String>,
    pub payload: Value,
}

impl Frame {
    /// Build a frame from a DURABLE record (ledger-before-offer, S5 §2).
    /// `event` must be a [`EVENTS`] name (retired names are rejected).
    pub fn from_record(seq: u64, record: &Record, event: &str, payload: Value) -> Frame {
        debug_assert!(
            is_slice_event(event),
            "frame event '{event}' must be slice-v1; retired names are not aliased (D-43)"
        );
        Frame {
            seq,
            event: event.to_owned(),
            schema_version: record.schema_version.clone(),
            event_id: record.event_id.clone(),
            ts: record.timestamp.clone(),
            ordinal: record.ordinal.to_string(),
            correlation_id: record.correlation_id.clone(),
            caused_by: record.caused_by.clone(),
            payload,
        }
    }

    /// One UTF-8 JSON object plus `\n` (the NDJSON binding).
    pub fn to_line(&self) -> Result<String, serde_json::Error> {
        let mut s = serde_json::to_string(self)?;
        s.push('\n');
        Ok(s)
    }
}

/// An incoming request line: exactly one of result/error is returned for
/// every well-formed request (duplicate `request_id` replays).
#[derive(Debug, Clone)]
pub struct IncomingRequest {
    pub id: String,
    pub method: String,
    pub params: Value,
}

/// Parse one NDJSON request line. Unknown additive params fields are
/// preserved and ignored (N1); ids must be non-empty opaque strings
/// ≤ [`ID_MAX_BYTES`].
pub fn parse_request(line: &str) -> Result<IncomingRequest, WireError> {
    let v: Value = serde_json::from_str(line.trim_end_matches(['\r', '\n']))
        .map_err(|e| WireError::new(wire_code::INVALID_REQUEST, format!("malformed JSON: {e}")))?;
    let obj = v
        .as_object()
        .ok_or_else(|| WireError::new(wire_code::INVALID_REQUEST, "request must be an object"))?;
    let id = obj
        .get("id")
        .and_then(Value::as_str)
        .ok_or_else(|| WireError::new(wire_code::INVALID_REQUEST, "request.id must be a string"))?;
    if id.is_empty() || id.len() > ID_MAX_BYTES {
        return Err(WireError::new(
            wire_code::INVALID_REQUEST,
            format!("request.id must be 1..={ID_MAX_BYTES} bytes"),
        ));
    }
    let method = obj
        .get("method")
        .and_then(Value::as_str)
        .ok_or_else(|| WireError::new(wire_code::INVALID_REQUEST, "request.method must be a string"))?;
    if method.is_empty() || method.len() > ID_MAX_BYTES {
        return Err(WireError::new(
            wire_code::INVALID_REQUEST,
            "request.method must be non-empty and <= 128 bytes",
        ));
    }
    let params = obj.get("params").cloned().unwrap_or_else(|| json!({}));
    if !params.is_object() {
        return Err(WireError::new(
            wire_code::INVALID_REQUEST,
            "request.params must be an object when present",
        ));
    }
    Ok(IncomingRequest {
        id: id.to_owned(),
        method: method.to_owned(),
        params,
    })
}

/// The `result` half of a response, as one NDJSON line.
pub fn result_line(id: &str, result: Value) -> String {
    let mut s = serde_json::to_string(&json!({"id": id, "result": result}))
        .expect("response is serializable");
    s.push('\n');
    s
}

/// The `error` half of a response, as one NDJSON line.
pub fn error_line(id: Option<&str>, err: &WireError) -> String {
    let idv = id.map(Value::from).unwrap_or(Value::Null);
    let mut s = serde_json::to_string(&json!({"id": idv, "error": err.to_value()}))
        .expect("response is serializable");
    s.push('\n');
    s
}

/// Validate `hello` params: `version.major` must equal [`PROTOCOL_MAJOR`]
/// (unknown minor fields ignored, N1); `limits`, when present, must be an
/// object. Any other unknown field is ignored (additive).
pub fn parse_hello(params: &Value) -> Result<(), WireError> {
    let major = params
        .get("version")
        .and_then(|v| v.get("major"))
        .and_then(Value::as_u64)
        .ok_or_else(|| {
            WireError::new(
                wire_code::INVALID_REQUEST,
                "hello.params.version.major is required",
            )
        })?;
    if major != PROTOCOL_MAJOR {
        return Err(WireError::new(
            wire_code::VERSION_MISMATCH,
            format!("selected_version major must be {PROTOCOL_MAJOR}, client sent {major}"),
        )
        .with_data(json!({"selected_major": PROTOCOL_MAJOR, "client_major": major})));
    }
    if let Some(limits) = params.get("limits") {
        if !limits.is_object() {
            return Err(WireError::new(
                wire_code::INVALID_LIMITS,
                "hello.params.limits must be an object",
            ));
        }
    }
    Ok(())
}

/// The HelloAck `result` (no `seq`, no credit, S5 §1): the negotiated
/// version, the slice-v1 `schema_hash`, empty `features`, and the
/// server-side limits.
pub fn hello_result() -> Value {
    json!({
        "selected_version": {"major": PROTOCOL_MAJOR, "minor": PROTOCOL_MINOR},
        "event_set": EVENT_SET,
        "schema_hash": schema_hash(),
        "features": FEATURES,
        "limits": {
            "max_frame_bytes": MAX_FRAME_BYTES,
            "max_json_depth": MAX_JSON_DEPTH,
            "id_max_bytes": ID_MAX_BYTES,
            "deny_reason_max_bytes": DENY_REASON_MAX_BYTES,
            "reserved_slots": 2
        }
    })
}

/// The `interaction.resolve` response affordance (RULING-7):
/// `allow_once` or `deny{reason?}`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ResolveResponse {
    AllowOnce,
    Deny { reason: Option<String> },
}

/// Parsed `interaction.resolve` params (S5 §2): the echo triple plus the
/// response.
#[derive(Debug, Clone)]
pub struct ResolveParams {
    pub call_id: String,
    pub decision_id: String,
    pub args_digest: String,
    pub response: ResolveResponse,
}

/// Parse `interaction.resolve` params. The `reason` free text is capped at
/// [`DENY_REASON_MAX_BYTES`] bytes (RULING-7).
pub fn parse_resolve(params: &Value) -> Result<ResolveParams, WireError> {
    let str_field = |name: &str| -> Result<String, WireError> {
        params
            .get(name)
            .and_then(Value::as_str)
            .map(str::to_owned)
            .ok_or_else(|| {
                WireError::new(
                    wire_code::INVALID_REQUEST,
                    format!("interaction.resolve.{name} is required"),
                )
            })
    };
    let call_id = str_field("call_id")?;
    let decision_id = str_field("decision_id")?;
    let args_digest = str_field("args_digest")?;
    let response = match params.get("response") {
        Some(Value::String(s)) if s == "allow_once" => ResolveResponse::AllowOnce,
        Some(Value::Object(o)) => match o.get("deny") {
            None => {
                return Err(WireError::new(
                    wire_code::INVALID_REQUEST,
                    "response object must carry 'deny'",
                ))
            }
            Some(Value::String(reason)) => {
                // Tolerated shorthand: {"deny": "<reason text>"}.
                if reason.len() > DENY_REASON_MAX_BYTES {
                    return Err(WireError::new(
                        wire_code::INVALID_REQUEST,
                        format!("deny.reason must be <= {DENY_REASON_MAX_BYTES} bytes"),
                    ));
                }
                let reason = reason.trim();
                ResolveResponse::Deny {
                    reason: if reason.is_empty() {
                        None
                    } else {
                        Some(reason.to_owned())
                    },
                }
            }
            Some(Value::Object(d)) => {
                let reason = d.get("reason").and_then(Value::as_str).map(str::to_owned);
                if let Some(r) = &reason {
                    if r.len() > DENY_REASON_MAX_BYTES {
                        return Err(WireError::new(
                            wire_code::INVALID_REQUEST,
                            format!("deny.reason must be <= {DENY_REASON_MAX_BYTES} bytes"),
                        ));
                    }
                }
                ResolveResponse::Deny { reason }
            }
            Some(_) => {
                return Err(WireError::new(
                    wire_code::INVALID_REQUEST,
                    "deny must be an object or a reason string",
                ))
            }
        },
        _ => {
            return Err(WireError::new(
                wire_code::INVALID_REQUEST,
                "response must be \"allow_once\" or {\"deny\": {\"reason\"?}}",
            ))
        }
    };
    Ok(ResolveParams {
        call_id,
        decision_id,
        args_digest,
        response,
    })
}

/// Parse `event.ack` params: `through_seq` must be a u64.
pub fn parse_event_ack(params: &Value) -> Result<u64, WireError> {
    params
        .get("through_seq")
        .and_then(Value::as_u64)
        .ok_or_else(|| {
            WireError::new(
                wire_code::INVALID_REQUEST,
                "event.ack.through_seq must be a non-negative integer",
            )
        })
}

/// `tool.result` frame `abort_cause` (IN-3 set) mapped from the S4 §2b wire
/// `error.code` of an `aborted` result (EXEC-34 spelling reconciliation).
///
/// A-4: the input is resolved through the one normative table first, so an S1
/// §4a or S2 spelling (`cancelled_before_start`, `sandbox_unavailable`) maps
/// exactly like its S4 §2b canonical spelling, and an unknown code maps to
/// nothing instead of being guessed at.
pub fn abort_cause(error_code: &str) -> Option<&'static str> {
    let code = crate::errcode::canonical(error_code).ok()?;
    match code {
        crate::errcode::CANCELLED => Some("cancelled"),
        crate::errcode::WITNESS_WRITE_FAILED => Some("witness_write_failed"),
        // The three preflight failure classes all surface code
        // `isolation_unavailable` in E4 (unprovisioned command, argv build,
        // backend probe).
        crate::errcode::ISOLATION_UNAVAILABLE => Some("preflight_failed"),
        _ => None,
    }
}

/// `tool.call` frame payload: `{call_id, tool_name, args}` — the record's
/// canonical arguments verbatim.
pub fn tool_call_frame_payload(record_payload: &Value) -> Value {
    json!({
        "call_id": record_payload["call_id"],
        "tool_name": record_payload["tool_name"],
        "args": record_payload["arguments"],
    })
}

/// `authority.decided` frame payload: outcome, source, and the ordered
/// `decision_chain` (proposed first, verbatim, S5 §3b).
pub fn authority_decided_frame_payload(record_payload: &Value) -> Value {
    json!({
        "outcome": record_payload["outcome"],
        "source": record_payload["source"],
        "decision_chain": [record_payload["proposed"], record_payload["decision"]],
    })
}

/// `approval.resolved` frame payload: resolution, actor, optional reason,
/// and the follow-up `decision_chain` (S5 §3b:
/// `approval_required→approved|denied`).
pub fn approval_resolved_frame_payload(record_payload: &Value) -> Value {
    let mut out = json!({
        "resolution": record_payload["resolution"],
        "actor": record_payload["actor"],
        "decision_chain": [record_payload["decision"]],
    });
    if let Some(r) = record_payload.get("reason").filter(|r| !r.is_null()) {
        out["reason"] = r.clone();
    }
    out
}

/// `tool.started` frame payload: `{call_id}` (the witness = authorized to
/// attempt, never proof of invocation).
pub fn tool_started_frame_payload(record_payload: &Value) -> Value {
    json!({"call_id": record_payload["call_id"]})
}

/// `tool.result` frame payload (S5 §3 nullability): `succeeded` ⇒
/// `error=null`, `output` a `ContentValue`; any other status ⇒ `output=null`
/// plus an `OperationError{code, message, retryable, related_ids}` whose
/// `related_ids` cite the causal predecessor. `abort_cause` only on
/// `aborted`; `executor_evidence` only when the record carries it;
/// `authority_terminal` = the record's `terminal_decision`.
pub fn tool_result_frame_payload(record_payload: &Value, caused_by: Option<&str>) -> Value {
    let status = record_payload["status"].as_str().unwrap_or_default();
    let mut out = json!({
        "status": status,
        "output": record_payload.get("output").cloned().unwrap_or(Value::Null),
        "error": Value::Null,
        "authority_terminal": record_payload
            .get("terminal_decision")
            .cloned()
            .unwrap_or(Value::Null),
    });
    if status == "aborted" {
        if let Some(cause) = record_payload["error"]["code"]
            .as_str()
            .and_then(abort_cause)
        {
            out["abort_cause"] = json!(cause);
        }
    }
    if let Some(e) = record_payload.get("error").filter(|e| !e.is_null()) {
        out["error"] = json!({
            "code": e["code"],
            "message": e["message"],
            "retryable": e["retryable"],
            "related_ids": caused_by.map(|c| json!([c])).unwrap_or(json!([])),
        });
    }
    if let Some(ev) = record_payload
        .get("executor_evidence")
        .filter(|ev| !ev.is_null())
    {
        out["executor_evidence"] = ev.clone();
    }
    out
}

/// A fresh evaluation-clock reading: fixed-width RFC 3339 UTC, millisecond
/// precision, `Z` suffix (RULING-8). Read once per evaluation point (S2 §2).
pub fn now_ts() -> String {
    let ts = OffsetDateTime::now_utc()
        .format(TS_FORMAT)
        .expect("UTC RFC3339 ms formatting is infallible");
    debug_assert!(is_valid_timestamp(&ts));
    ts
}

/// Read-only view over the segment file the driver writes: refreshed after
/// each ledger append, it yields the DURABLE record for an ack's event_id so
/// frames are built from the durable bytes (ledger-before-offer), never from
/// in-memory shadows.
#[derive(Debug)]
pub struct SegmentReader {
    path: PathBuf,
    offset: u64,
    records: Vec<Record>,
}

impl SegmentReader {
    pub fn new(path: PathBuf) -> SegmentReader {
        SegmentReader {
            path,
            offset: 0,
            records: Vec::new(),
        }
    }

    /// Read newly appended bytes and parse every COMPLETE line into records
    /// (I-01: an unterminated final line is not a record; it stays unread
    /// until the next refresh — a mid-write kill leaves it a torn tail).
    pub fn refresh(&mut self) -> io::Result<()> {
        use std::fs::OpenOptions;
        use std::io::Read;
        let mut bytes = Vec::new();
        {
            let mut file = OpenOptions::new().read(true).open(&self.path)?;
            file.seek(SeekFrom::Start(self.offset))?;
            file.read_to_end(&mut bytes)?;
        }
        let Some(complete_upto) = bytes.iter().rposition(|&b| b == b'\n') else {
            return Ok(()); // nothing complete yet
        };
        let chunk = &bytes[..=complete_upto];
        self.offset += chunk.len() as u64;
        for line in chunk.split(|&b| b == b'\n') {
            if line.is_empty() {
                continue;
            }
            if let Ok(rec) = serde_json::from_slice::<Record>(line) {
                self.records.push(rec);
            }
        }
        Ok(())
    }

    /// The durable record with this event_id, if parsed.
    pub fn by_event_id(&self, event_id: &str) -> Option<&Record> {
        self.records.iter().find(|r| r.event_id == event_id)
    }

    /// The last durable record of this kind (scan order).
    pub fn records_last_of_kind(&self, kind: &str) -> Option<&Record> {
        self.records
            .iter()
            .rev()
            .find(|r| r.kind.as_str() == kind)
    }

    pub fn len(&self) -> usize {
        self.records.len()
    }

    pub fn is_empty(&self) -> bool {
        self.records.is_empty()
    }
}
