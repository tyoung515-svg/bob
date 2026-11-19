use bob_engine::functional::{Message, Session};
use serde_json::{json, Value};
fn fixture() -> (Session, std::path::PathBuf) {
    let p = std::env::temp_dir().join(format!("bob-functional-approval-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir(&p).unwrap();
    std::fs::write(p.join("notes.txt"), "old\n").unwrap();
    let mut s = Session::new();
    s.attach(&p).unwrap();
    (s, p)
}
fn proposal(_: &[Message]) -> Result<Value, String> {
    Ok(
        json!({"ok":true,"content":"{\"type\":\"action\",\"action\":{\"tool\":\"write\",\"path\":\"notes.txt\",\"content\":\"new\\n\"}}"}),
    )
}
#[test]
fn edit_requires_exact_explicit_approval_then_returns_verified_result_to_model() {
    let (mut s, p) = fixture();
    s.turn("Update note", &mut proposal).unwrap();
    assert_eq!(
        std::fs::read_to_string(p.join("notes.txt")).unwrap(),
        "old\n"
    );
    let pending = s.pending.clone().expect("diff approval");
    assert!(pending.diff.contains("-old"));
    assert!(pending.diff.contains("+new"));
    assert!(s
        .resolve("wrong", &pending.digest, true, &mut proposal)
        .is_err());
    s.resolve(&pending.id,&pending.digest,true,&mut |m:&[Message]|{assert!(m.last().unwrap().content.contains("succeeded"));Ok(json!({"ok":true,"content":"{\"type\":\"message\",\"content\":\"Updated verified\"}"}))}).unwrap();
    assert_eq!(
        std::fs::read_to_string(p.join("notes.txt")).unwrap(),
        "new\n"
    );
    assert!(s.pending.is_none());
    assert!(s.events.iter().any(|e| e.kind == "effect"));
    std::fs::remove_dir_all(p).unwrap();
}
