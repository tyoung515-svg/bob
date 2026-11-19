//! S1 record envelope (T3a S1 §2): all 15 frame fields are mandatory on every
//! engine-written record (S1.3). Ratified constants (RULING-8): ordinal base 1
//! (u64-string, sole ordering authority), `event_id` uuidv4 lowercase,
//! fixed-width millisecond `Z` timestamps, `schema_version` "1.0",
//! `correlation_id` = `call_id` on all six call events.

use serde::{Deserialize, Deserializer, Serialize, Serializer};

/// The slice's single schema version. Format: `major.minor`; readers reject an
/// unknown major and accept an unknown minor by ignoring unknown fields
/// (RULING-8, I-26).
pub const SCHEMA_VERSION: &str = "1.0";

/// Envelope `actor` vocabulary (S1.3).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Actor {
    Engine,
    Model,
    User,
    Hook,
    Subagent,
}

/// Dot-namespaced record kinds. The slice event set is `slice-v1` (RULING-4,
/// S5: additive changes allowed, renames need a ruling): the six-event call
/// vocabulary plus the turn/lineage carriers. `authority.transition`,
/// `approval.request`, `approval.resolve` and `effect` are retired/deferred
/// for the slice (AUD-3, D-1, D-2). Unknown kinds decode as `Unsupported` so
/// replay can surface them as assertions (I-26, LT-1) instead of failing or
/// dropping them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EventKind {
    Lineage,
    TurnStart,
    StepContext,
    ToolCall,
    AuthorityDecided,
    ApprovalRequested,
    ApprovalResolved,
    ToolStarted,
    ToolResult,
    TurnEnd,
    /// A kind this schema version does not know. Replay-only: the engine never
    /// writes it (LT-6 covers `gap`).
    Unsupported(String),
}

impl EventKind {
    pub fn as_str(&self) -> &str {
        match self {
            EventKind::Lineage => "lineage",
            EventKind::TurnStart => "turn.start",
            EventKind::StepContext => "step.context",
            EventKind::ToolCall => "tool.call",
            EventKind::AuthorityDecided => "authority.decided",
            EventKind::ApprovalRequested => "approval.requested",
            EventKind::ApprovalResolved => "approval.resolved",
            EventKind::ToolStarted => "tool.started",
            EventKind::ToolResult => "tool.result",
            EventKind::TurnEnd => "turn.end",
            EventKind::Unsupported(s) => s.as_str(),
        }
    }

    pub fn parse(s: &str) -> EventKind {
        match s {
            "lineage" => EventKind::Lineage,
            "turn.start" => EventKind::TurnStart,
            "step.context" => EventKind::StepContext,
            "tool.call" => EventKind::ToolCall,
            "authority.decided" => EventKind::AuthorityDecided,
            "approval.requested" => EventKind::ApprovalRequested,
            "approval.resolved" => EventKind::ApprovalResolved,
            "tool.started" => EventKind::ToolStarted,
            "tool.result" => EventKind::ToolResult,
            "turn.end" => EventKind::TurnEnd,
            other => EventKind::Unsupported(other.to_owned()),
        }
    }

    /// True iff this kind is known to this schema version.
    pub fn is_supported(&self) -> bool {
        !matches!(self, EventKind::Unsupported(_))
    }

    /// The six call events carry `correlation_id = call_id` (RULING-8 as
    /// extended); all other kinds carry null.
    pub fn carries_call_correlation(&self) -> bool {
        matches!(
            self,
            EventKind::ToolCall
                | EventKind::AuthorityDecided
                | EventKind::ApprovalRequested
                | EventKind::ApprovalResolved
                | EventKind::ToolStarted
                | EventKind::ToolResult
        )
    }
}

impl Serialize for EventKind {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(self.as_str())
    }
}

impl<'de> Deserialize<'de> for EventKind {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let s = String::deserialize(d)?;
        Ok(EventKind::parse(&s))
    }
}

/// Serde helper: `ordinal`-class u64 fields are strings on the wire
/// ("ordinal u64-string", S1.3; `call_index` u64-str, S1.4.2). The writer is
/// strict (always a string); the reader additionally tolerates a bare number.
pub(crate) mod u64_string {
    use serde::{Deserialize, Deserializer, Serialize, Serializer};

    pub fn serialize<S: Serializer>(v: &u64, s: S) -> Result<S::Ok, S::Error> {
        v.to_string().serialize(s)
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<u64, D::Error> {
        #[derive(Deserialize)]
        #[serde(untagged)]
        enum StrOrNum {
            Str(String),
            Num(u64),
        }
        match StrOrNum::deserialize(d)? {
            StrOrNum::Str(s) => s.parse::<u64>().map_err(serde::de::Error::custom),
            StrOrNum::Num(n) => Ok(n),
        }
    }
}

/// Serde helper for the optional ordinal-bearing field `through_ordinal`
/// (A-2, SPEC-ERRATA-1): a u64-STRING on the wire, identical to `ordinal`
/// and `call_index` — `null` when absent. No plain JSON number is permitted
/// for any ordinal-bearing field, so the reader is STRICT: a bare number is
/// rejected (unlike [`u64_string`], which keeps tolerating one so
/// pre-amendment segments stay readable).
pub(crate) mod opt_u64_string {
    use serde::{Deserialize, Deserializer, Serialize, Serializer};

    pub fn serialize<S: Serializer>(v: &Option<u64>, s: S) -> Result<S::Ok, S::Error> {
        match v {
            Some(n) => n.to_string().serialize(s),
            None => s.serialize_none(),
        }
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<Option<u64>, D::Error> {
        let opt = Option::<String>::deserialize(d)?;
        match opt {
            Some(s) => s.parse::<u64>().map(Some).map_err(serde::de::Error::custom),
            None => Ok(None),
        }
    }
}

/// The S1 record envelope. `payload` is kept as a raw JSON object so unknown
/// additive payload fields are preserved verbatim through parse and
/// re-serialization (S1.3/S1.9). Writers must guarantee it is an object;
/// `Ledger` rejects non-object payloads.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Record {
    pub schema_version: String,
    pub event_id: String,
    /// Fixed-width RFC 3339 UTC timestamp, millisecond precision, `Z` suffix
    /// (`YYYY-MM-DDTHH:MM:SS.sssZ`); non-`Z` offsets are rejected (RULING-8).
    /// Append time only; never orders events (S2 §2).
    pub timestamp: String,
    #[serde(with = "u64_string")]
    pub ordinal: u64,
    pub session_id: String,
    pub thread_id: String,
    pub lineage_id: String,
    pub segment_id: String,
    /// `null` only on session-level records (e.g. `lineage`).
    pub turn_id: Option<String>,
    /// `null` only when no step exists.
    pub step_id: Option<String>,
    pub actor: Actor,
    pub kind: EventKind,
    pub correlation_id: Option<String>,
    pub caused_by: Option<String>,
    pub payload: serde_json::Value,
}

impl Record {
    /// Serialize as one UTF-8 JSON object plus `\n` (I-01).
    pub fn to_line(&self) -> Result<String, serde_json::Error> {
        let mut s = serde_json::to_string(self)?;
        s.push('\n');
        Ok(s)
    }
}
