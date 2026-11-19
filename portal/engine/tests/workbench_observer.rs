use bob_engine::functional::Session;
use serde_json::json;
#[test]
fn observer_receives_durable_events_while_transport_is_blocked() {
    let root = std::env::temp_dir().join(format!("workbench-observe-{}", uuid::Uuid::new_v4()));
    let mut s = Session::create(&root).unwrap();
    let id = s.id.clone();
    let (tx, rx) = std::sync::mpsc::channel();
    s.observe(tx);
    s.turn("hello", &mut |_| {
        let events: Vec<_> = rx.try_iter().collect();
        assert_eq!(events.last().unwrap().kind, "model.requested");
        let bytes = std::fs::read(root.join(format!("{}.json", id))).unwrap();
        let disk: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(
            disk["snapshot"]["events"]
                .as_array()
                .unwrap()
                .last()
                .unwrap()["event_id"],
            events.last().unwrap().event_id
        );
        Ok(json!({"ok":true,"content":r#"{"type":"message","content":"hello"}"#}))
    })
    .unwrap();
    assert!(rx.try_iter().any(|e| e.kind == "assistant.message"));
    assert!(!bob_engine::functional::bindings().contains("observer"));
    std::fs::remove_dir_all(root).unwrap();
}
