//! Canonical encoding and digests (MGR-1, closes A-24 / IMPL-2).
//!
//! Hash inputs are RFC 8785 JCS bytes; a digest is `sha256:<64 lowercase
//! hex>`. Inputs are I-JSON only: non-finite numbers and integers outside
//! ±(2^53−1) are rejected before encoding (MGR-1). Scope arrays are sorted by
//! UTF-8 bytes and deduplicated before hashing (MGR-1). Args are hashed as
//! received; resolved targets are hashed separately (as part of the
//! `action_digest` input object, never inside `args_digest`).

use serde_json::Value;
use sha2::{Digest, Sha256};

/// Digest string prefix (MGR-1).
pub const SHA256_PREFIX: &str = "sha256:";

/// I-JSON integer bound: |int| ≤ 2^53−1 (MGR-1).
pub const IJSON_INT_BOUND: i128 = (1i128 << 53) - 1;

/// Typed, fail-closed encoding errors. No digest API panics on bad input.
#[derive(Debug, thiserror::Error)]
pub enum DigestError {
    #[error("I-JSON violation: integer {0} outside ±(2^53-1) (MGR-1)")]
    IntOutOfRange(i128),
    #[error("I-JSON violation: non-finite number (MGR-1)")]
    NonFinite,
    #[error("JCS serialization failed: {0}")]
    Jcs(String),
}

/// Validate the I-JSON number constraints (MGR-1) before encoding.
fn check_ijson(v: &Value) -> Result<(), DigestError> {
    match v {
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                if (i as i128).abs() > IJSON_INT_BOUND {
                    return Err(DigestError::IntOutOfRange(i as i128));
                }
            } else if let Some(u) = n.as_u64() {
                if (u as i128) > IJSON_INT_BOUND {
                    return Err(DigestError::IntOutOfRange(u as i128));
                }
            } else if let Some(f) = n.as_f64() {
                if !f.is_finite() {
                    return Err(DigestError::NonFinite);
                }
            }
            Ok(())
        }
        Value::Array(a) => a.iter().try_for_each(check_ijson),
        Value::Object(o) => o.values().try_for_each(check_ijson),
        _ => Ok(()),
    }
}

/// RFC 8785 JCS bytes of a value (sorted keys, minimal string escapes,
/// ECMAScript number serialization via `serde_jcs`/`ryu-js`), after the
/// I-JSON check.
pub fn jcs_bytes(v: &Value) -> Result<Vec<u8>, DigestError> {
    check_ijson(v)?;
    serde_jcs::to_vec(v).map_err(|e| DigestError::Jcs(e.to_string()))
}

/// `sha256:<64 lowercase hex>` of raw bytes (MGR-1).
pub fn sha256_digest(bytes: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(bytes);
    format!("{SHA256_PREFIX}{:x}", h.finalize())
}

/// `sha256:<64 lowercase hex>` of the JCS bytes of a value (MGR-1).
pub fn jcs_sha256(v: &Value) -> Result<String, DigestError> {
    Ok(sha256_digest(&jcs_bytes(v)?))
}

/// Canonicalize a scope array: sort by UTF-8 bytes and dedupe (MGR-1). Rust
/// `str` ordering is byte-wise lexicographic over UTF-8, which is exactly
/// the required order.
pub fn canonical_scope(scope: &[String]) -> Vec<String> {
    let mut v = scope.to_vec();
    v.sort();
    v.dedup();
    v
}

/// Digest of a scope array after sort+dedupe (MGR-1).
pub fn scope_digest(scope: &[String]) -> Result<String, DigestError> {
    let canonical = canonical_scope(scope);
    jcs_sha256(&Value::Array(
        canonical.into_iter().map(Value::String).collect(),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn jcs_sorts_object_keys() {
        let v = serde_json::json!({"b": 1, "a": [true, null, "x"]});
        assert_eq!(
            jcs_bytes(&v).unwrap(),
            br#"{"a":[true,null,"x"],"b":1}"#.to_vec()
        );
    }

    #[test]
    fn jcs_minimal_string_escapes() {
        // RFC 8785: only the mandatory escapes; forward slash never escaped.
        let v = serde_json::json!({"s": "a/b\"c\nd"});
        assert_eq!(
            String::from_utf8(jcs_bytes(&v).unwrap()).unwrap(),
            r#"{"s":"a/b\"c\nd"}"#
        );
    }

    #[test]
    fn jcs_ecmascript_numbers() {
        assert_eq!(String::from_utf8(jcs_bytes(&serde_json::json!(0.1)).unwrap()).unwrap(), "0.1");
        assert_eq!(String::from_utf8(jcs_bytes(&serde_json::json!(-0.0)).unwrap()).unwrap(), "0");
        assert_eq!(
            String::from_utf8(jcs_bytes(&serde_json::json!(1e30)).unwrap()).unwrap(),
            "1e+30"
        );
        assert_eq!(
            String::from_utf8(jcs_bytes(&serde_json::json!(1e-7)).unwrap()).unwrap(),
            "1e-7"
        );
        assert_eq!(String::from_utf8(jcs_bytes(&serde_json::json!(42)).unwrap()).unwrap(), "42");
    }

    #[test]
    fn sha256_known_vectors() {
        assert_eq!(
            sha256_digest(b""),
            "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            sha256_digest(b"abc"),
            "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn ijson_bounds_enforced() {
        let ok = serde_json::json!(IJSON_INT_BOUND as i64);
        assert!(jcs_bytes(&ok).is_ok());
        let bad = serde_json::json!(IJSON_INT_BOUND as i64 + 1);
        assert!(matches!(jcs_bytes(&bad), Err(DigestError::IntOutOfRange(_))));
        let bad_neg = serde_json::json!(-(IJSON_INT_BOUND as i64) - 1);
        assert!(matches!(jcs_bytes(&bad_neg), Err(DigestError::IntOutOfRange(_))));
    }

    #[test]
    fn scope_sorted_and_deduped() {
        let scope = vec!["/usr".to_string(), "/bin".to_string(), "/usr".to_string()];
        assert_eq!(canonical_scope(&scope), vec!["/bin", "/usr"]);
        let a = scope_digest(&scope).unwrap();
        let b = scope_digest(&["/bin".to_string(), "/usr".to_string()]).unwrap();
        assert_eq!(a, b, "order and duplicates do not change the scope digest");
        let c = scope_digest(&["/bin".to_string(), "/etc".to_string()]).unwrap();
        assert_ne!(a, c, "mutating a scope entry changes the digest");
    }
}
