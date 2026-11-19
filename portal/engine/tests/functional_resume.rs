use bob_engine::functional::{Message, Session};
use serde_json::{json, Value};
#[test]
fn restart_restores_conversation_project_and_expires_pending_write() {
    let root = std::env::temp_dir().join(format!("bob-functional-store-{}", uuid::Uuid::new_v4()));
    let project = root.join("project");
    std::fs::create_dir_all(&project).unwrap();
    std::fs::write(project.join("notes.txt"), "old").unwrap();
    let mut s = Session::create(&root.join("data")).unwrap();
    s.attach(&project).unwrap();
    let id = s.id.clone();
    s.turn("Remember the story character Nori",&mut |_:&[Message]|Ok(json!({"ok":true,"content":"{\"type\":\"message\",\"content\":\"Nori remembered\"}"}))).unwrap();
    s.turn("Update notes",&mut |_:&[Message]|Ok(json!({"ok":true,"content":"{\"type\":\"action\",\"action\":{\"tool\":\"write\",\"path\":\"notes.txt\",\"content\":\"changed\"}}"}))).unwrap();
    let approval = s.pending.clone().unwrap();
    drop(s);
    let mut s = Session::load(&root.join("data"), &id).unwrap();
    assert!(s.pending.is_none());
    assert!(s.project.is_some());
    assert!(s.messages.iter().any(|m| m.content == "Nori remembered"));
    assert!(s
        .resolve(
            &approval.id,
            &approval.digest,
            true,
            &mut |_: &[Message]| -> Result<Value, String> { panic!("must not call model") }
        )
        .is_err());
    assert_eq!(
        std::fs::read_to_string(project.join("notes.txt")).unwrap(),
        "old"
    );
    s.turn("Who was the character?", &mut |m: &[Message]| {
        assert!(m.iter().any(|x| x.content == "Nori remembered"));
        Ok(json!({"ok":true,"content":"{\"type\":\"message\",\"content\":\"Nori\"}"}))
    })
    .unwrap();
    assert_eq!(Session::saved(&root.join("data")).unwrap().len(), 1);
    std::fs::remove_dir_all(root).unwrap();
}
