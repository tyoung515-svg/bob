use super::*;
use std::{process::Command, time::Duration};

fn python(script: &str) -> Command {
    let mut command = Command::new(if cfg!(windows) { "python" } else { "python3" });
    command.args(["-I", "-c", script]);
    command
}

struct Archive(std::path::PathBuf);
impl Archive {
    fn new(bytes: &[u8]) -> Self {
        let path = std::env::temp_dir().join(format!("bob-stage-test-{}", uuid::Uuid::new_v4()));
        std::fs::write(&path, bytes).unwrap();
        Self(path)
    }
}
impl Drop for Archive {
    fn drop(&mut self) { let _ = std::fs::remove_file(&self.0); }
}

#[test]
fn runtime_upgrade_staging_args_and_paths_are_fixed() {
    let hash = "a".repeat(64);
    let args = stage_args(&hash).unwrap();
    assert_eq!(&args[..8], ["-d", "BoB", "-u", "root", "--exec", "/opt/bob/.venv/bin/python", "-I", "-c"]);
    assert_eq!(args[8], STAGE);
    assert_eq!(args[9], hash);
    assert!(stage_args("bad").is_err());
    let id = "b".repeat(32);
    let source = format!("{STAGING_PREFIX}{id}/release.tar.gz");
    assert!(staged_source(&source));
    let cleanup = cleanup_args(&source).unwrap();
    assert_eq!(&cleanup[..8], &args[..8]);
    assert_eq!(cleanup[8], CLEAN_STAGE);
    assert_eq!(cleanup[9], source);
    assert!(cleanup_args("/var/lib/bob-upgrades").is_err());
    for tail in ["", "/extra", "/../release.tar.gz", "\n", "\0"] {
        assert!(start_args(&format!("{source}{tail}x"), &hash).is_err());
    }
    for bad in [
        format!("{STAGING_PREFIX}../release.tar.gz"),
        format!("{STAGING_PREFIX}{id}/../release.tar.gz"),
        format!("{STAGING_PREFIX}{id}/nested/release.tar.gz"),
        format!("{STAGING_PREFIX}{}/release.tar.gz", "A".repeat(32)),
        format!("{STAGING_PREFIX}{}/release.tar.gz", "g".repeat(32)),
        format!("{STAGING_PREFIX}{id}/release.tar.gz\n"),
        format!("/var/lib/bob-upgrades-other/staging-{id}/release.tar.gz"),
    ] { assert!(start_args(&bad, &hash).is_err(), "{bad}"); }
}

#[test]
fn runtime_upgrade_staging_receipts_refuse_bad_paths_digests_sizes_and_errors() {
    let hash = "a".repeat(64);
    let mut row = json!({"source":format!("{STAGING_PREFIX}{}/release.tar.gz", "b".repeat(32)),"sha256":hash,"size":3});
    let parse = |row: &Value| stage_receipt(true, &serde_json::to_vec(row).unwrap(), &hash, 3);
    assert!(parse(&row).is_ok());
    row["sha256"] = json!("c".repeat(64)); assert!(parse(&row).is_err());
    row["sha256"] = json!(hash); row["size"] = json!(4); assert!(parse(&row).is_err());
    row["size"] = json!(3); row["source"] = json!("/mnt/c/bundle.tar.gz"); assert!(parse(&row).is_err());
    assert!(stage_receipt(true, b"not json", &hash, 3).is_err());
    assert!(stage_receipt(true, &[b' '; 4097], &hash, 3).is_err());
    assert_eq!(stage_receipt(false, br#"{"error":"archive digest mismatch"}"#, &hash, 3).unwrap_err(), "staged runtime archive digest mismatch");
    assert_eq!(stage_receipt(false, br#"{"error":"untrusted detail"}"#, &hash, 3).unwrap_err(), "runtime archive staging failed");
}

#[test]
fn runtime_upgrade_staging_streams_multiple_chunks_and_closes_stdin() {
    use sha2::{Digest, Sha256};
    let bytes = vec![0xab; 2 * 1024 * 1024 + 123];
    let hash = format!("{:x}", Sha256::digest(&bytes));
    let archive = Archive::new(&bytes);
    let command = python(r#"import hashlib,json,sys
h=hashlib.sha256(); size=0
while True:
 b=sys.stdin.buffer.read(17000)
 if not b: break
 size+=len(b); h.update(b)
print(json.dumps({'source':'/var/lib/bob-upgrades/staging-'+'b'*32+'/release.tar.gz','sha256':h.hexdigest(),'size':size}))
"#);
    let result = stream_archive(command, &archive.0, &hash, Duration::from_secs(10)).unwrap();
    assert!(staged_source(&result));
}

#[test]
fn runtime_upgrade_staging_fake_runner_digest_refusal_and_failures() {
    let archive = Archive::new(b"abc");
    let command = python("import json,sys; sys.stdin.buffer.read(); print(json.dumps({'error':'archive digest mismatch'})); sys.exit(1)");
    assert_eq!(stream_archive(command, &archive.0, &"0".repeat(64), Duration::from_secs(10)).unwrap_err(), "staged runtime archive digest mismatch");
    let command = python("import sys; sys.stdin.buffer.read(); print('invalid JSON')");
    assert!(stream_archive(command, &archive.0, &"0".repeat(64), Duration::from_secs(10)).is_err());
    let command = python("import sys; sys.stdin.buffer.read(); print('x'*5000)");
    assert_eq!(stream_archive(command, &archive.0, &"0".repeat(64), Duration::from_secs(10)).unwrap_err(), "oversized runtime staging receipt");
    assert!(stream_archive(Command::new("bob-nonexistent-stage-test-executable"), &archive.0, &"0".repeat(64), Duration::from_secs(1)).is_err());
}

#[test]
fn runtime_upgrade_staging_deadline_covers_blocked_pipe_writes() {
    let archive = Archive::new(&vec![0xab; 2 * 1024 * 1024]);
    let command = python("import time; time.sleep(30)");
    let began = std::time::Instant::now();
    assert!(stream_archive(command, &archive.0, &"0".repeat(64), Duration::from_millis(150)).unwrap_err().contains("timed out"));
    assert!(began.elapsed() < Duration::from_secs(5));
}

#[test]
fn runtime_upgrade_embedded_python_compiles_without_running_distro_commands() {
    let mut command = python("import sys; compile(sys.stdin.read(), '<upgrade-script>', 'exec')");
    use std::{io::Write, process::Stdio};
    for script in [STAGE, BOOTSTRAP, CLEAN_STAGE] {
        let mut child = command.stdin(Stdio::piped()).spawn().unwrap();
        child.stdin.take().unwrap().write_all(script.as_bytes()).unwrap();
        assert!(child.wait().unwrap().success());
    }
}
