//! TypeScript wire-module codegen (A-10 / AT-S5-03, EXEC-49).
//!
//! [`generate`] renders the committed `shell/src/protocol.ts` from
//! [`crate::wire_schema`] — the engine's normative wire model. It is a pure
//! function of that model plus the crate's own constants (`EVENTS`,
//! `RETIRED_EVENTS`, the A-4 error-code table, the S5 limits): no clock, no
//! uuid, no map iteration order, so the same input always produces
//! byte-identical output.
//!
//! The artifact is generated ONCE and committed, and
//! `tests/e11_codegen.rs::generated_shell_module_matches_the_model` regenerates
//! it inside `cargo test` and fails on any difference — a hand edit of
//! `protocol.ts`, or a wire change without regeneration, is a red suite rather
//! than silent drift. `bob-wiregen --check` is the same comparison for the
//! `npm run check:protocol` convenience script.
//!
//! The shell is the second consumer this replaces the hand-written mirror for
//! (EXEC-49): `protocol.ts` had been transcribed by hand from these types,
//! which is exactly the drift A-10 forbids.

use std::path::PathBuf;

use crate::wire_schema::{self, ArrTy, Field, Presence, ShapeKind, Ty};

/// First line of the generated module; the drift check asserts it is present
/// so a truncated or replaced artifact cannot pass as "generated".
pub const HEADER_MARKER: &str = "// GENERATED FILE - DO NOT EDIT.";

/// Path of the committed artifact, resolved from the engine crate (i.e.
/// `<repo>/shell/src/protocol.ts`), so the test and the CLI agree without a
/// hard-coded absolute path.
pub fn generated_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("engine crate has a parent directory")
        .join("shell")
        .join("src")
        .join("protocol.ts")
}

/// Regenerate and return the committed TS module. Deterministic.
pub fn generate() -> String {
    let mut out = String::with_capacity(48 * 1024);

    header(&mut out);
    constants(&mut out);
    shapes(&mut out);
    frames(&mut out);
    requests(&mut out);
    response_lines(&mut out);

    out
}

fn header(out: &mut String) {
    out.push_str(HEADER_MARKER);
    out.push('\n');
    out.push_str(
        "//\n\
         // Slice-v1 wire protocol (S5 §1-§3): the frame envelope, the eight\n\
         // `slice-v1` payloads, the request/response messages and the closed\n\
         // error-code vocabulary.\n\
         //\n\
         // Source of truth: the engine's normative wire schema\n\
         // `engine/src/wire_schema.rs` — the same model `hello`'s `schema_hash`\n\
         // is computed over. `WIRE_SCHEMA_HASH` below is that hash.\n\
         //\n\
         // Regenerate (from `shell/`):\n\
         //   cargo run -q --manifest-path ../engine/Cargo.toml --bin bob-wiregen -- --out src/protocol.ts\n\
         // Verify only:\n\
         //   npm run check:protocol\n\
         //\n\
         // Drift is a test failure: `engine/tests/e11_codegen.rs` regenerates\n\
         // this file in `cargo test` and fails if the bytes differ. Do not edit\n\
         // this file by hand (A-10 / AT-S5-03, EXEC-49).\n\
         \n",
    );
}

fn constants(out: &mut String) {
    out.push_str("/// The slice schema version (RULING-8).\n");
    out.push_str(&format!(
        "export const SCHEMA_VERSION = \"{}\";\n",
        crate::envelope::SCHEMA_VERSION
    ));
    out.push_str("/// Negotiated protocol version: `selected_version` is major 2 (S5 §1).\n");
    out.push_str(&format!(
        "export const PROTOCOL_MAJOR = {};\nexport const PROTOCOL_MINOR = {};\n",
        crate::protocol::PROTOCOL_MAJOR,
        crate::protocol::PROTOCOL_MINOR
    ));
    out.push_str("/// The event-set name (RULING-4): additive changes allowed, renames need a ruling.\n");
    out.push_str(&format!(
        "export const EVENT_SET = \"{}\";\n",
        crate::protocol::EVENT_SET
    ));
    out.push_str("/// sha256 over the JCS of the engine's schema description. Both ends\n");
    out.push_str("/// compare it in `hello`/`HelloAck` before a frame flows.\n");
    out.push_str(&format!(
        "export const WIRE_SCHEMA_HASH = \"{}\";\n",
        crate::protocol::schema_hash()
    ));
    out.push_str("/// The `slice-v1` event set, in wire order.\n");
    out.push_str("export const SLICE_EVENTS = [\n");
    for e in wire_schema::EVENTS {
        out.push_str(&format!("  \"{e}\",\n"));
    }
    out.push_str("] as const;\n");
    out.push_str("/// Retired for the slice, no alias (D-43).\n");
    out.push_str("export const RETIRED_EVENTS = [\n");
    for e in crate::protocol::RETIRED_EVENTS {
        out.push_str(&format!("  \"{e}\",\n"));
    }
    out.push_str("] as const;\n");
    out.push_str("/// Carried limits (S5 §1/§3, slice-selected values).\n");
    out.push_str(&format!(
        "export const MAX_FRAME_BYTES = {};\n",
        crate::protocol::MAX_FRAME_BYTES
    ));
    out.push_str(&format!(
        "export const MAX_JSON_DEPTH = {};\n",
        crate::protocol::MAX_JSON_DEPTH
    ));
    out.push_str(&format!(
        "export const ID_MAX_BYTES = {};\n",
        crate::protocol::ID_MAX_BYTES
    ));
    out.push_str(&format!(
        "export const DENY_REASON_MAX_BYTES = {};\n\n",
        crate::protocol::DENY_REASON_MAX_BYTES
    ));
}

fn shapes(out: &mut String) {
    for shape in wire_schema::SHAPES {
        doc(out, shape.doc);
        match shape.kind {
            ShapeKind::Union(variants) => {
                let one_line = format!(
                    "export type {} = {};",
                    shape.name,
                    variants
                        .iter()
                        .map(|v| format!("\"{v}\""))
                        .collect::<Vec<_>>()
                        .join(" | ")
                );
                if one_line.len() <= 92 {
                    out.push_str(&one_line);
                    out.push('\n');
                } else {
                    out.push_str(&format!("export type {} =\n", shape.name));
                    for (i, v) in variants.iter().enumerate() {
                        let sep = if i + 1 == variants.len() { ";" } else { "" };
                        out.push_str(&format!("  | \"{v}\"{sep}\n"));
                    }
                }
            }
            ShapeKind::Custom(body) => {
                out.push_str(&format!("export type {} = {};\n", shape.name, body));
            }
            ShapeKind::ErrorCode => {
                let codes: Vec<&'static str> = crate::errcode::all_codes().collect();
                out.push_str(&format!("export type {} =\n", shape.name));
                for (i, c) in codes.iter().enumerate() {
                    let sep = if i + 1 == codes.len() { ";" } else { "" };
                    out.push_str(&format!("  | \"{c}\"{sep}\n"));
                }
            }
            ShapeKind::Object => {
                if shape.fields.is_empty() {
                    out.push_str(&format!(
                        "export type {} = Record<string, never>;\n",
                        shape.name
                    ));
                } else {
                    out.push_str(&format!("export type {} = {{\n", shape.name));
                    for f in shape.fields {
                        out.push_str(&format!(
                            "  {}{}: {};\n",
                            f.name,
                            if f.presence == Presence::Optional {
                                "?"
                            } else {
                                ""
                            },
                            ty_str(f)
                        ));
                    }
                    out.push_str("};\n");
                }
            }
        }
        out.push('\n');
    }
}

fn frames(out: &mut String) {
    doc(
        out,
        "Payload shape per `slice-v1` event: the map a consumer switches on.",
    );
    out.push_str("export interface PayloadMap {\n");
    for p in wire_schema::FRAME_PAYLOADS {
        out.push_str(&format!("  \"{}\": {};\n", p.event, p.shape));
    }
    out.push_str("}\n\n");

    doc(
        out,
        "One frame of event `E`: the S5 §1 envelope, with that event's payload.",
    );
    out.push_str("export type FrameOf<E extends SliceEvent> = {\n");
    for f in wire_schema::ENVELOPE {
        let body = match f.name {
            // The envelope's `event` is the frame's own literal, and its
            // `payload` is the payload map's entry for it: the two fields that
            // make the union discriminable.
            "event" => "E".to_owned(),
            "payload" => "PayloadMap[E]".to_owned(),
            _ => ty_str(f),
        };
        match f.name {
            "ordinal" => out.push_str("  /** u64-string, base 1 (A-2). */\n"),
            "schema_version" => out.push_str("  /** \"1.0\" (RULING-8). */\n"),
            _ => {}
        }
        out.push_str(&format!("  {}: {};\n", f.name, body));
    }
    out.push_str("};\n\n");

    doc(
        out,
        "Any frame: a discriminated union on `event`. Comparing `f.event`\n\
         * narrows `f.payload` to that event's shape, so a consumer needs no\n\
         * helper and no cast.",
    );
    out.push_str("export type Frame = { [E in SliceEvent]: FrameOf<E> }[SliceEvent];\n\n");
}

fn requests(out: &mut String) {
    doc(out, "One request's method name to its params shape (S5 §2).");
    out.push_str("export interface RequestParamsMap {\n");
    for m in wire_schema::REQUESTS {
        out.push_str(&format!("  \"{}\": {};\n", m.method, m.params));
    }
    out.push_str("}\n\n");

    doc(out, "One request's method name to its result shape.");
    out.push_str("export interface ResultMap {\n");
    for m in wire_schema::REQUESTS {
        out.push_str(&format!("  \"{}\": {};\n", m.method, m.result));
    }
    out.push_str("}\n\n");

    out.push_str("export type Method = keyof RequestParamsMap;\n");
    out.push_str("export type WireResult = ResultMap[Method];\n\n");

    doc(
        out,
        "Encode one request line. The shell never assembles a request object by\n\
         * hand: the id is opaque, the method selects the params type.",
    );
    out.push_str(
        "export function encodeRequest<M extends Method>(\n\
         \x20 id: string,\n\
         \x20 method: M,\n\
         \x20 params: RequestParamsMap[M],\n\
         ): string {\n\
         \x20 return JSON.stringify({ id, method, params });\n\
         }\n\n",
    );
}

fn response_lines(out: &mut String) {
    doc(
        out,
        "A response line: exactly one of `result`/`error` answers a request\n\
         * (S5 §2). Duplicate `request_id`s replay the same line (EXEC-43).",
    );
    out.push_str(
        "export type ResponseLine =\n\
         \x20 | { id: string; result: WireResult }\n\
         \x20 | { id: string | null; error: WireError };\n\n",
    );

    doc(out, "One parsed wire line.");
    out.push_str(
        "export type ParsedLine =\n\
         \x20 | { kind: \"frame\"; frame: Frame }\n\
         \x20 | { kind: \"response\"; response: ResponseLine };\n\n",
    );

    doc(
        out,
        "Parse one NDJSON line into a frame or a response; `null` when the line\n\
         * is not a wire object. A frame is recognised by its `event`, a response\n\
         * by `result`/`error`; nothing is coerced or repaired.",
    );
    out.push_str(
        "export function parseLine(raw: string): ParsedLine | null {\n\
         \x20 let o: any;\n\
         \x20 try {\n\
         \x20   o = JSON.parse(raw);\n\
         \x20 } catch {\n\
         \x20   return null;\n\
         \x20 }\n\
         \x20 if (o === null || typeof o !== \"object\") return null;\n\
         \x20 if (typeof o.event === \"string\") return { kind: \"frame\", frame: o as Frame };\n\
         \x20 if (typeof o.error === \"object\" || \"result\" in o) {\n\
         \x20   return { kind: \"response\", response: o as ResponseLine };\n\
         \x20 }\n\
         \x20 return null;\n\
         }\n",
    );
}

/// Render a `/** ... */` block; continuation lines in the model carry their
/// own `* ` decoration, which is stripped here so the model stays readable.
fn doc(out: &mut String, text: &str) {
    out.push_str("/**\n");
    for line in text.split('\n') {
        let trimmed = line.trim().trim_start_matches('*').trim();
        if trimmed.is_empty() {
            out.push_str(" *\n");
        } else {
            out.push_str(&format!(" * {trimmed}\n"));
        }
    }
    out.push_str(" */\n");
}

/// The TS type of one field.
fn ty_str(f: &Field) -> String {
    let base = match f.ty {
        Ty::Str => "string".to_owned(),
        Ty::Bool => "boolean".to_owned(),
        Ty::Num => "number".to_owned(),
        Ty::U64Str => "U64String".to_owned(),
        // `Json` is `unknown`, which already admits null.
        Ty::Json => "Json".to_owned(),
        Ty::Ref(name) => name.to_owned(),
        Ty::Arr(ArrTy::Str) => "string[]".to_owned(),
        Ty::Arr(ArrTy::Ref(name)) => format!("{name}[]"),
    };
    match f.presence {
        Presence::Nullable if f.ty != Ty::Json => format!("{base} | null"),
        _ => base,
    }
}

/// Normalize CRLF to LF. The committed artifact is LF-only; a Windows
/// checkout with `core.autocrlf=true` materializes CRLF, which is a git eol
/// policy rather than wire drift, so the drift check and `--check` compare
/// after this normalization.
pub fn normalize_newlines(s: &str) -> String {
    s.replace("\r\n", "\n")
}
