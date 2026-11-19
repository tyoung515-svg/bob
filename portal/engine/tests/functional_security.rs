//! Deterministic boundary/transport fixtures, NEVER live-model acceptance.
use bob_engine::{
    functional::{Message, Session},
    functional_project::Project,
};
use serde_json::{json, Value};
use std::{fs, path::PathBuf};
fn dir() -> PathBuf {
    let p = std::env::temp_dir().join(format!("bob-functional-security-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&p).unwrap();
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&p, fs::Permissions::from_mode(0o700)).unwrap();
    }
    p
}
fn proposal(_: &[Message]) -> Result<Value, String> {
    Ok(
        json!({"ok":true,"content":"{\"type\":\"action\",\"action\":{\"tool\":\"write\",\"path\":\"notes.txt\",\"content\":\"new\"}}"}),
    )
}
fn answer(_: &[Message]) -> Result<Value, String> {
    Ok(json!({"ok":true,"content":"{\"type\":\"message\",\"content\":\"done\"}"}))
}
fn pending() -> (Session, PathBuf) {
    let p = dir();
    fs::write(p.join("notes.txt"), "old").unwrap();
    let mut s = Session::new();
    s.attach(&p).unwrap();
    s.turn("edit", &mut proposal).unwrap();
    (s, p)
}
#[test]
fn traversal_absolute_and_sensitive_relative_paths_are_refused() {
    let p = dir();
    let project = Project::attach(&p).unwrap();
    for path in [
        "../outside",
        "/etc/passwd",
        "a/../../outside",
        ".env",
        ".ssh/id_rsa",
        "secrets/token",
        "a/credentials/token",
        "a.key",
    ] {
        assert!(project.before(path).is_err(), "{path}");
        assert!(project.write(path, None, "bad").is_err(), "{path}");
    }
    fs::remove_dir_all(p).unwrap();
}
#[test]
fn symlink_files_and_symlink_directories_cannot_escape_project() {
    use std::os::unix::fs::symlink;
    let p = dir();
    let out = dir();
    fs::write(out.join("outside.txt"), "protected").unwrap();
    symlink(out.join("outside.txt"), p.join("link.txt")).unwrap();
    symlink(&out, p.join("linked-dir")).unwrap();
    let project = Project::attach(&p).unwrap();
    assert!(project.read("link.txt").is_err());
    assert!(project.write("link.txt", None, "bad").is_err());
    assert!(project.write("linked-dir/new.txt", None, "bad").is_err());
    assert_eq!(
        fs::read_to_string(out.join("outside.txt")).unwrap(),
        "protected"
    );
    assert!(!out.join("new.txt").exists());
    fs::remove_dir_all(p).unwrap();
    fs::remove_dir_all(out).unwrap();
}
#[test]
fn hardlinks_and_special_files_are_refused() {
    let p = dir();
    let out = dir();
    fs::write(out.join("outside.txt"), "protected").unwrap();
    fs::hard_link(out.join("outside.txt"), p.join("hard.txt")).unwrap();
    let project = Project::attach(&p).unwrap();
    assert!(project.read("hard.txt").is_err());
    assert!(project.write("hard.txt", Some("protected"), "bad").is_err());
    fs::create_dir(p.join("directory")).unwrap();
    assert!(project.read("directory").is_err());
    fs::remove_dir_all(p).unwrap();
    fs::remove_dir_all(out).unwrap();
}
#[test]
fn deny_preserves_bytes_and_emits_no_start_witness() {
    let (mut s, p) = pending();
    let a = s.pending.clone().unwrap();
    s.resolve(&a.id, &a.digest, false, &mut answer).unwrap();
    assert_eq!(fs::read_to_string(p.join("notes.txt")).unwrap(), "old");
    assert!(!s.events.iter().any(|e| e.kind == "tool.started"));
    assert!(!s.events.iter().any(|e| e.kind == "effect"));
    assert!(s
        .events
        .iter()
        .any(|e| e.kind == "tool.result" && e.data["status"] == "denied"));
    fs::remove_dir_all(p).unwrap();
}
#[test]
fn mismatched_digest_and_expired_approval_never_write() {
    let (mut s, p) = pending();
    let a = s.pending.clone().unwrap();
    assert!(s.resolve(&a.id, "sha256:wrong", true, &mut answer).is_err());
    s.pending.as_mut().unwrap().created_at = 0;
    assert!(s.resolve(&a.id, &a.digest, true, &mut answer).is_err());
    assert_eq!(fs::read_to_string(p.join("notes.txt")).unwrap(), "old");
    fs::remove_dir_all(p).unwrap();
}
#[test]
fn changed_file_requires_new_diff_before_any_witness() {
    let (mut s, p) = pending();
    let a = s.pending.clone().unwrap();
    fs::write(p.join("notes.txt"), "user changed").unwrap();
    assert!(s
        .resolve(&a.id, &a.digest, true, &mut answer)
        .unwrap_err()
        .contains("stale"));
    assert_eq!(
        fs::read_to_string(p.join("notes.txt")).unwrap(),
        "user changed"
    );
    assert!(!s.events.iter().any(|e| e.kind == "tool.started"));
    fs::remove_dir_all(p).unwrap();
}
#[test]
fn approval_cannot_be_rebound_to_another_project_or_content() {
    let (mut s, p) = pending();
    let other = dir();
    fs::write(other.join("notes.txt"), "old").unwrap();
    let a = s.pending.clone().unwrap();
    s.project = Some(Project::attach(&other).unwrap());
    assert!(s.resolve(&a.id, &a.digest, true, &mut answer).is_err());
    assert_eq!(fs::read_to_string(other.join("notes.txt")).unwrap(), "old");
    s.project = Some(Project::attach(&p).unwrap());
    s.pending.as_mut().unwrap().content = "forged".into();
    assert!(s.resolve(&a.id, &a.digest, true, &mut answer).is_err());
    fs::remove_dir_all(p).unwrap();
    fs::remove_dir_all(other).unwrap();
}
#[test]
fn malformed_unknown_and_extra_model_actions_fail_closed() {
    for raw in [
        "not json",
        r#"{"type":"action","action":{"tool":"shell","command":"touch /outside"}}"#,
        r#"{"type":"message","content":"ok","hidden_action":"write"}"#,
        r#"{"type":"action","action":{"tool":"list","extra":true}}"#,
    ] {
        let mut s = Session::new();
        assert!(
            s.turn("try", &mut |_: &[Message]| Ok(
                json!({"ok":true,"content":raw})
            ))
            .is_err(),
            "{raw}"
        );
        assert!(!s.events.iter().any(|e| e.kind == "tool.started"));
    }
}
#[test]
fn disconnected_transport_is_an_error_not_a_fake_answer() {
    let mut s = Session::new();
    let err = s
        .turn("hello", &mut |_: &[Message]| {
            Err("SSH disconnected (test transport)".into())
        })
        .unwrap_err();
    assert!(err.contains("disconnected"));
    assert_eq!(s.messages.len(), 1);
    assert!(s.receipts.is_empty());
}
#[test]
fn bounded_agent_stops_after_eight_read_rounds() {
    let p = dir();
    let mut s = Session::new();
    s.attach(&p).unwrap();
    let mut calls = 0;
    let err = s
        .turn("loop", &mut |_: &[Message]| {
            calls += 1;
            Ok(json!({"ok":true,"content":"{\"type\":\"action\",\"action\":{\"tool\":\"list\"}}"}))
        })
        .unwrap_err();
    assert!(err.contains("round limit"));
    assert_eq!(calls, 8);
    fs::remove_dir_all(p).unwrap();
}
#[test]
fn session_data_corruption_refuses_writable_resume() {
    let p = dir();
    let mut s = Session::create(&p).unwrap();
    s.turn("hi", &mut answer).unwrap();
    let id = s.id.clone();
    drop(s);
    let path = p.join(format!("{id}.json"));
    let mut v: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    v["snapshot"]["messages"][0]["content"] = json!("tampered");
    fs::write(&path, serde_json::to_vec(&v).unwrap()).unwrap();
    assert!(Session::load(&p, &id).unwrap_err().contains("checksum"));
    fs::remove_dir_all(p).unwrap();
}
#[test]
fn storage_failure_before_authorization_cannot_change_file() {
    let root = dir();
    let data = root.join("data");
    let project = root.join("project");
    fs::create_dir(&project).unwrap();
    fs::write(project.join("notes.txt"), "old").unwrap();
    let mut s = Session::create(&data).unwrap();
    s.attach(&project).unwrap();
    s.turn("edit", &mut proposal).unwrap();
    let a = s.pending.clone().unwrap();
    fs::rename(&data, root.join("moved-data")).unwrap();
    assert!(s.resolve(&a.id, &a.digest, true, &mut answer).is_err());
    assert_eq!(
        fs::read_to_string(project.join("notes.txt")).unwrap(),
        "old"
    );
    assert!(!s.events.iter().any(|e| e.kind == "tool.started"));
    fs::remove_dir_all(root).unwrap();
}
#[test]
fn sensitive_ancestor_project_root_is_refused() {
    let p = dir();
    let root = p.join(".ssh").join("nested");
    fs::create_dir_all(&root).unwrap();
    assert!(
        Project::attach(&root).is_err(),
        "sensitive parent must not be laundered by selecting its child"
    );
    fs::remove_dir_all(p).unwrap();
}

#[test]
fn pending_cancellation_is_an_aborted_result_not_a_denial() {
    let (mut s, p) = pending();
    s.cancel().unwrap();
    assert!(s.pending.is_none());
    assert_eq!(fs::read_to_string(p.join("notes.txt")).unwrap(), "old");
    assert!(s
        .events
        .iter()
        .any(|e| e.kind == "tool.result" && e.data["status"] == "aborted"));
    assert!(!s
        .events
        .iter()
        .any(|e| e.kind == "authority.decision" && e.data["state"] == "denied"));
    fs::remove_dir_all(p).unwrap();
}
