use bob_engine::workbench::{project_bob, Lifecycle};
use serde_json::json;
#[test]
fn bob_projection_only_shows_observed_receipts_and_preserves_terminal_failure() {
    let run = json!({"id":"00000000-0000-0000-0000-000000000001","status":"running","receipts":[{"role":"manager","model_face":"actual-seat","model_pin":"actual-pin","output":"plan","elapsed_seconds":1.2}]});
    let (agents, flows, outputs) = project_bob(&run, false).unwrap();
    assert_eq!(agents.len(), 1);
    assert_eq!(agents[0].state, Lifecycle::Completed);
    assert_eq!(agents[0].model_label.as_deref(), Some("actual-pin"));
    assert_eq!(flows[0].state, Lifecycle::Running);
    assert_eq!(flows[0].steps.len(), 1);
    assert_eq!(outputs.len(), 1);
    assert_eq!(
        project_bob(&run, true).unwrap().1[0].state,
        Lifecycle::CancelRequested
    );
    let mut failed = run.clone();
    failed["status"] = json!("failed");
    failed["error"] = json!("wall_time_exhausted");
    assert_eq!(
        project_bob(&failed, true).unwrap().1[0].state,
        Lifecycle::Failed
    );
    assert!(project_bob(&json!({"id":"../bad"}), false).is_err());
}
