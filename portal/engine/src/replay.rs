//! Read-only replay scanner (I-23). Replay never mutates the segment. It scans
//! complete lines in append order, validates envelope and kind, and REPORTS
//! corrupt lines, unsupported schema majors, unknown kinds, duplicate/regressing
//! ordinals and ordinal gaps as diagnostics.
//!
//! Torn tail (RULING-6): a final line that fails to parse or validate — or a
//! partial unterminated tail — is NOT a record (I-01). It surfaces as
//! [`ReplayDiagnostic::LedgerTailCorrupt`] carrying the byte offset of the
//! failing tail and the last valid ordinal. Writable resume refuses it with
//! the typed `ledger_tail_corrupt` error; read-only open is permitted and
//! surfaces the corruption. There is no auto-truncate, no fuse, and no
//! synthesized gap record in the slice (repair = DEFERRED-EXT-2).
//!
//! Slice-minimal: this is the `assertions` view (I-24) plus diagnostics. The
//! `accepted` view, the joined call view and the SQLite projection live in
//! [`crate::projection`] (E8). `gap` synthesis stays out of scope (D-7).

use std::path::Path;

use time::format_description::FormatItem;
use time::macros::format_description;
use time::PrimitiveDateTime;

use crate::envelope::{EventKind, Record};

/// The schema major this engine accepts as truth (I-26: `major.minor`).
pub const SCHEMA_MAJOR: &str = "1";

/// The ratified timestamp format (RULING-8): fixed width, millisecond
/// precision, `Z` suffix. Shared with the ledger writer.
pub(crate) const TS_FORMAT: &[FormatItem<'static>] = format_description!(
    "[year]-[month]-[day]T[hour]:[minute]:[second].[subsecond digits:3]Z"
);

/// True iff `ts` matches the ratified fixed-width ms-`Z` format exactly
/// (non-`Z` offsets and variable precision are rejected, RULING-8).
pub(crate) fn is_valid_timestamp(ts: &str) -> bool {
    PrimitiveDateTime::parse(ts, TS_FORMAT).is_ok()
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReplayDiagnostic {
    /// The final line failed to parse or validate, or the file ends in an
    /// unterminated partial line. Carries the byte offset at which the
    /// corrupt tail begins and the last valid ordinal (RULING-6). Not a
    /// record; never classified.
    LedgerTailCorrupt {
        byte_offset: u64,
        last_ordinal: Option<u64>,
    },
    /// A complete non-final line that is not a valid envelope.
    CorruptLine { line: u64, error: String },
    /// Envelope parsed but `payload` is not a JSON object.
    PayloadNotObject { line: u64 },
    /// Unknown kind surfaces as an unsupported assertion (I-26); the record is
    /// still present in `assertions`.
    UnsupportedKind { line: u64, kind: String },
    /// Unsupported schema major: not accepted truth (I-26, LT-2).
    UnsupportedSchemaMajor { line: u64, found: String },
    OrdinalDuplicate { line: u64, ordinal: u64 },
    OrdinalRegression { line: u64, prev: u64, found: u64 },
    /// Gaps are permitted but diagnosed (I-03).
    OrdinalGap { after: u64, before: u64 },
}

/// Result of a read-only scan. `assertions` holds every parseable record in
/// append order (I-24 `assertions` view); `assertion_offsets` holds, in
/// lockstep, the byte offset of each assertion's line — the cursor triple
/// (segment_id, ordinal, byte offset) the projection pins per record (I-25,
/// E8). `diagnostics` holds everything the scan found wrong or noteworthy,
/// in scan order.
#[derive(Debug, Default)]
pub struct ReplayOutcome {
    pub assertions: Vec<Record>,
    pub assertion_offsets: Vec<u64>,
    pub diagnostics: Vec<ReplayDiagnostic>,
}

impl ReplayOutcome {
    /// True when any diagnostic invalidates accepted truth. A corrupt tail
    /// does NOT invalidate the valid prefix (it is simply not a record), and
    /// an ordinal gap is permitted-but-diagnosed.
    pub fn has_integrity_failures(&self) -> bool {
        self.diagnostics.iter().any(|d| {
            matches!(
                d,
                ReplayDiagnostic::CorruptLine { .. }
                    | ReplayDiagnostic::PayloadNotObject { .. }
                    | ReplayDiagnostic::UnsupportedSchemaMajor { .. }
                    | ReplayDiagnostic::OrdinalDuplicate { .. }
                    | ReplayDiagnostic::OrdinalRegression { .. }
            )
        })
    }
}

/// Scan a segment file read-only. Missing file is an `io` error (callers that
/// tolerate absence — S1.2 allows a ledger to stay physically absent — must
/// check existence themselves).
pub fn replay_segment(path: &Path) -> Result<ReplayOutcome, std::io::Error> {
    let bytes = std::fs::read(path)?;
    Ok(scan_bytes(&bytes))
}

/// Core scanner, separated for testing. Handles records over 1 MiB with no
/// truncation (I-27, LT-4): the whole file is read and split on `\n`.
pub fn scan_bytes(bytes: &[u8]) -> ReplayOutcome {
    let mut out = ReplayOutcome::default();
    if bytes.is_empty() {
        return out;
    }
    let terminated = bytes.last() == Some(&b'\n');
    let mut lines: Vec<&[u8]> = bytes.split(|b| *b == b'\n').collect();
    // `split` yields a final empty slice for a trailing '\n'; drop it. If the
    // file is unterminated, the final slice is a partial line: not a record.
    let mut torn_tail: Option<u64> = None; // byte offset where the tail begins
    if let Some(last) = lines.last() {
        if terminated {
            if last.is_empty() {
                lines.pop();
            }
        } else {
            torn_tail = Some((bytes.len() - last.len()) as u64);
            lines.pop();
        }
    }
    let last_line_idx = lines.len(); // 1-based line number of the final complete line
    let mut prev_ordinal: Option<u64> = None;
    let mut byte_offset: u64 = 0;
    for (idx, line) in lines.iter().enumerate() {
        let line_no = (idx + 1) as u64;
        let line_offset = byte_offset;
        byte_offset += line.len() as u64 + 1; // + '\n'
        let is_final_line = idx + 1 == last_line_idx;
        // A FINAL line that fails to parse or validate is `ledger_tail_corrupt`
        // (RULING-6), never a mid-file CorruptLine and never a `gap`.
        macro_rules! tail_or {
            ($diag:expr) => {
                if is_final_line {
                    out.diagnostics.push(ReplayDiagnostic::LedgerTailCorrupt {
                        byte_offset: line_offset,
                        last_ordinal: prev_ordinal,
                    });
                    return out;
                } else {
                    out.diagnostics.push($diag);
                    continue;
                }
            };
        }
        if line.is_empty() {
            // An empty line inside the file is not a valid record.
            tail_or!(ReplayDiagnostic::CorruptLine {
                line: line_no,
                error: "empty line".to_owned(),
            });
        }
        let record: Record = match serde_json::from_slice(line) {
            Ok(r) => r,
            Err(e) => {
                tail_or!(ReplayDiagnostic::CorruptLine {
                    line: line_no,
                    error: e.to_string(),
                });
            }
        };
        if !record.payload.is_object() {
            tail_or!(ReplayDiagnostic::PayloadNotObject { line: line_no });
        }
        if !is_valid_timestamp(&record.timestamp) {
            tail_or!(ReplayDiagnostic::CorruptLine {
                line: line_no,
                error: format!(
                    "timestamp '{}' is not fixed-width ms-Z (RULING-8)",
                    record.timestamp
                ),
            });
        }
        if let EventKind::Unsupported(kind) = &record.kind {
            out.diagnostics.push(ReplayDiagnostic::UnsupportedKind {
                line: line_no,
                kind: kind.clone(),
            });
        }
        match record.schema_version.split('.').next() {
            Some(major) if major == SCHEMA_MAJOR => {}
            _ => out.diagnostics.push(ReplayDiagnostic::UnsupportedSchemaMajor {
                line: line_no,
                found: record.schema_version.clone(),
            }),
        }
        match prev_ordinal {
            Some(prev) if record.ordinal == prev => {
                out.diagnostics.push(ReplayDiagnostic::OrdinalDuplicate {
                    line: line_no,
                    ordinal: record.ordinal,
                });
            }
            Some(prev) if record.ordinal < prev => {
                out.diagnostics.push(ReplayDiagnostic::OrdinalRegression {
                    line: line_no,
                    prev,
                    found: record.ordinal,
                });
            }
            Some(prev) if record.ordinal > prev + 1 => {
                out.diagnostics.push(ReplayDiagnostic::OrdinalGap {
                    after: prev,
                    before: record.ordinal,
                });
            }
            _ => {}
        }
        prev_ordinal = Some(record.ordinal);
        out.assertions.push(record);
        out.assertion_offsets.push(line_offset);
    }
    if let Some(offset) = torn_tail {
        out.diagnostics.push(ReplayDiagnostic::LedgerTailCorrupt {
            byte_offset: offset,
            last_ordinal: prev_ordinal,
        });
    }
    out
}
