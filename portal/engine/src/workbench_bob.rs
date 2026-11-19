use super::*;
use serde_json::Value;
/// UUID validation is exact, not a lossy repair of user-supplied identifiers.
pub fn valid_run_id(s: &str) -> bool {
    uuid::Uuid::parse_str(s).is_ok_and(|u| u.hyphenated().to_string() == s)
}
pub fn bob_state(s: &str) -> Lifecycle {
    match s {
        "accepted" | "running" => Lifecycle::Running,
        "completed" => Lifecycle::Completed,
        "failed" | "interrupted" => Lifecycle::Failed,
        "cancelled" => Lifecycle::Cancelled,
        _ => Lifecycle::Unknown,
    }
}
pub fn project_bob(
    run: &Value,
    cancel_requested: bool,
) -> Result<(Vec<AgentView>, Vec<WorkflowView>, Vec<OutputView>), String> {
    let rid = run["id"]
        .as_str()
        .filter(|s| valid_run_id(s))
        .ok_or("invalid Bob run id")?;
    let mut state = bob_state(run["status"].as_str().unwrap_or("unknown"));
    if cancel_requested && matches!(state, Lifecycle::Running | Lifecycle::Unknown) {
        state = Lifecycle::CancelRequested;
    }
    let mut agents = vec![];
    let mut steps: Vec<WorkflowStep> = vec![];
    let mut outputs = vec![];
    for (i, r) in run["receipts"]
        .as_array()
        .unwrap_or(&vec![])
        .iter()
        .enumerate()
    {
        let source = format!("bob:run:{rid}:receipts:{i}");
        let id = format!("{rid}:receipt:{i}");
        let aid = format!("{rid}:agent:{i}");
        let oid = format!("{id}:output");
        let role = r["role"].as_str().unwrap_or("unknown");
        agents.push(AgentView {
            id: aid.clone(),
            name: format!("{role} (receipt {})", i + 1),
            role: role.into(),
            parent_agent_id: None,
            runtime_profile_id: "bob_team".into(),
            execution_host: "Bob server".into(),
            model_label: r["model_pin"].as_str().map(str::to_owned),
            run_id: rid.into(),
            task_id: rid.into(),
            step_id: Some(id.clone()),
            state: Lifecycle::Completed,
            source_event_ids: vec![source.clone()],
            summary: format!(
                "Observed {} receipt; {} seconds; not independently verified",
                r["model_face"].as_str().unwrap_or("unknown seat"),
                r["elapsed_seconds"]
            ),
        });
        steps.push(WorkflowStep {
            id: id.clone(),
            parent_step_id: None,
            depends_on: steps.last().map(|s| vec![s.id.clone()]).unwrap_or_default(),
            agent_id: Some(aid.clone()),
            label: format!("{role} receipt {}", i + 1),
            state: Lifecycle::Completed,
            approval_id: None,
            output_ids: vec![oid.clone()],
            source_event_ids: vec![source.clone()],
        });
        outputs.push(OutputView {
            id: oid,
            run_id: rid.into(),
            step_id: Some(id),
            agent_id: Some(aid),
            kind: OutputKind::Text,
            title: format!("{role} raw receipt"),
            path: None,
            text: Some(r.to_string()),
            source_event_ids: vec![source],
        });
    }
    if let Some(error) = run["error"].as_str() {
        outputs.push(OutputView {
            id: format!("{rid}:error"),
            run_id: rid.into(),
            step_id: None,
            agent_id: None,
            kind: OutputKind::Text,
            title: "Bob terminal error".into(),
            path: None,
            text: Some(error.into()),
            source_event_ids: vec![format!("bob:run:{rid}:status")],
        });
    }
    Ok((
        agents,
        vec![WorkflowView {
            id: format!("bob:{rid}"),
            run_id: rid.into(),
            session_id: None,
            title: "Bob bounded sequential read-only team".into(),
            state,
            steps,
        }],
        outputs,
    ))
}
