use bob_engine::{
    functional::Session,
    workbench::{project_local, Lifecycle},
};
use serde_json::json;
#[test]
fn verified_write_effect_is_an_output_not_a_claim_from_model_text() {
    let root = std::env::temp_dir().join(format!("workbench-output-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir(&root).unwrap();
    let mut s = Session::new();
    s.attach(&root).unwrap();
    s.turn("propose file",&mut |_|Ok(json!({"ok":true,"content":r#"{"type":"action","action":{"tool":"write","path":"actual.txt","content":"actual bytes"}}"#}))).unwrap();
    let p = s.pending.clone().unwrap();
    let pending = project_local(&s, "standalone_http", None);
    assert_eq!(pending.0[0].state, Lifecycle::WaitingApproval);
    assert!(pending
        .2
        .iter()
        .any(|o| matches!(o.kind, bob_engine::workbench::OutputKind::Diff)));
    s.resolve(&p.id, &p.digest, true, &mut |_| {
        Ok(json!({"ok":true,"content":r#"{"type":"message","content":"done"}"#}))
    })
    .unwrap();
    let outputs = project_local(&s, "standalone_http", None).2;
    let file = outputs
        .iter()
        .find(|o| matches!(o.kind, bob_engine::workbench::OutputKind::File))
        .expect("verified file effect must be projected");
    assert_eq!(file.path.as_deref(), Some("actual.txt"));
    assert!(s
        .events
        .iter()
        .any(|e| e.kind == "effect" && file.source_event_ids.contains(&e.event_id)));
    assert_eq!(
        std::fs::read_to_string(root.join("actual.txt")).unwrap(),
        "actual bytes"
    );
    std::fs::remove_dir_all(root).unwrap();
}
#[test]
fn local_projection_preserves_failed_steps_with_real_event_sources() {
    let mut s = Session::new();
    let mut n = 0;
    s.turn("read without a project", &mut |_| { n+=1; Ok(json!({"ok":true,"model":"observed-model","content":if n==1 {r#"{"type":"action","action":{"tool":"read","path":"missing"}}"#} else {r#"{"type":"message","content":"read failed"}"#}})) }).unwrap();
    let (agents, flows, outputs) = project_local(&s, "local_bob_model", None);
    assert_eq!(agents.len(), 1);
    assert_eq!(agents[0].model_label.as_deref(), Some("observed-model"));
    assert_eq!(agents[0].state, Lifecycle::Completed);
    assert!(flows[0].steps.iter().any(|s| s.state == Lifecycle::Failed));
    for id in agents
        .iter()
        .flat_map(|a| &a.source_event_ids)
        .chain(
            flows
                .iter()
                .flat_map(|f| f.steps.iter().flat_map(|s| &s.source_event_ids)),
        )
        .chain(outputs.iter().flat_map(|o| &o.source_event_ids))
    {
        assert!(s.events.iter().any(|e| &e.event_id == id));
    }
    assert!(!outputs.is_empty());
    assert_eq!(
        project_local(&s, "standalone_http", Some(Lifecycle::CancelRequested)).0[0].state,
        Lifecycle::CancelRequested
    );
}
