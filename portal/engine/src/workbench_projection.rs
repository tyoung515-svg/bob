//! Read-only projection from real functional-app-v2 events, never an execution ledger.
use super::*;
use crate::functional::Session;
fn status(s: &str) -> Lifecycle {
    match s {
        "succeeded" => Lifecycle::Completed,
        "failed" => Lifecycle::Failed,
        "denied" => Lifecycle::Blocked,
        "aborted" => Lifecycle::Cancelled,
        _ => Lifecycle::Unknown,
    }
}
pub fn project_local(
    session: &Session,
    profile: &str,
    observed: Option<Lifecycle>,
) -> (Vec<AgentView>, Vec<WorkflowView>, Vec<OutputView>) {
    let run = session.id.clone();
    let agent = format!("local:{run}");
    let mut steps: Vec<WorkflowStep> = vec![];
    let mut outputs = vec![];
    let mut phase = Lifecycle::Idle;
    let mut model = None;
    for e in &session.events {
        let src = vec![e.event_id.clone()];
        match e.kind.as_str() {
            "user.message" => phase = Lifecycle::Unknown,
            "model.requested" => {
                phase = Lifecycle::Unknown; // a durable request is not proof that it is still running after restart
                steps.push(WorkflowStep {
                    id: format!("model:{}", e.event_id),
                    parent_step_id: None,
                    depends_on: steps.last().map(|s| vec![s.id.clone()]).unwrap_or_default(),
                    agent_id: Some(agent.clone()),
                    label: "Model request".into(),
                    state: Lifecycle::Unknown,
                    approval_id: None,
                    output_ids: vec![],
                    source_event_ids: src,
                });
            }
            "model.completion" => {
                if let Some(s) = steps.iter_mut().rev().find(|s| s.id.starts_with("model:")) {
                    s.state = Lifecycle::Completed;
                    s.source_event_ids.push(e.event_id.clone());
                }
                model = e.data["receipt"]["model"].as_str().map(str::to_owned);
            }
            "model.response_rejected" => {
                if let Some(s) = steps.iter_mut().rev().find(|s| s.id.starts_with("model:")) {
                    s.state = Lifecycle::Failed;
                    s.source_event_ids.push(e.event_id.clone());
                }
            }
            "tool.call" => {
                if let Some(id) = e.data["call_id"].as_str() {
                    steps.push(WorkflowStep {
                        id: id.into(),
                        parent_step_id: None,
                        depends_on: steps.last().map(|s| vec![s.id.clone()]).unwrap_or_default(),
                        agent_id: Some(agent.clone()),
                        label: e.data["action"]["tool"]
                            .as_str()
                            .unwrap_or("Unknown tool")
                            .into(),
                        state: Lifecycle::Unknown,
                        approval_id: None,
                        output_ids: vec![],
                        source_event_ids: src,
                    });
                }
            }
            "approval.requested" => {
                phase = Lifecycle::WaitingApproval;
                let p = &e.data["pending"];
                if let Some(s) = steps.iter_mut().find(|s| p["call_id"] == s.id) {
                    s.state = Lifecycle::WaitingApproval;
                    s.approval_id = p["id"].as_str().map(str::to_owned);
                    s.source_event_ids.push(e.event_id.clone());
                    let id = format!("diff:{}", e.event_id);
                    s.output_ids.push(id.clone());
                    outputs.push(OutputView {
                        id,
                        run_id: run.clone(),
                        step_id: Some(s.id.clone()),
                        agent_id: Some(agent.clone()),
                        kind: OutputKind::Diff,
                        title: "Exact proposed write".into(),
                        path: p["path"].as_str().map(str::to_owned),
                        text: p["diff"].as_str().map(str::to_owned),
                        source_event_ids: vec![e.event_id.clone()],
                    });
                }
            }
            "tool.started" => {
                if let Some(s) = steps.iter_mut().find(|s| e.data["call_id"] == s.id) {
                    s.state = Lifecycle::Unknown;
                    s.source_event_ids.push(e.event_id.clone());
                }
            }
            "tool.result" => {
                if let Some(s) = steps.iter_mut().find(|s| e.data["call_id"] == s.id) {
                    s.state = status(e.data["status"].as_str().unwrap_or("unknown"));
                    s.source_event_ids.push(e.event_id.clone());
                    let id = format!("result:{}", e.event_id);
                    s.output_ids.push(id.clone());
                    outputs.push(OutputView {
                        id,
                        run_id: run.clone(),
                        step_id: Some(s.id.clone()),
                        agent_id: Some(agent.clone()),
                        kind: OutputKind::ToolResult,
                        title: format!("{} result", s.label),
                        path: e.data["path"].as_str().map(str::to_owned),
                        text: Some(e.data.to_string()),
                        source_event_ids: src,
                    });
                }
            }
            "effect" => {
                if e.data["verified"] == true {
                    let step = steps.iter_mut().find(|s| e.data["call_id"] == s.id);
                    let id = format!("file:{}", e.event_id);
                    let step_id = step.map(|s| {
                        s.output_ids.push(id.clone());
                        s.source_event_ids.push(e.event_id.clone());
                        s.id.clone()
                    });
                    outputs.push(OutputView {
                        id,
                        run_id: run.clone(),
                        step_id,
                        agent_id: Some(agent.clone()),
                        kind: OutputKind::File,
                        title: "Verified local file effect".into(),
                        path: e.data["path"].as_str().map(str::to_owned),
                        text: Some(e.data.to_string()),
                        source_event_ids: src,
                    });
                }
            }
            "assistant.message" => {
                phase = Lifecycle::Completed;
                outputs.push(OutputView {
                    id: format!("text:{}", e.event_id),
                    run_id: run.clone(),
                    step_id: None,
                    agent_id: Some(agent.clone()),
                    kind: OutputKind::Text,
                    title: "Assistant response".into(),
                    path: None,
                    text: e.data["content"].as_str().map(str::to_owned),
                    source_event_ids: src,
                });
            }
            "turn.cancelled" => phase = Lifecycle::Cancelled,
            _ => {}
        }
    }
    if let Some(s) = observed {
        phase = s;
    }
    let root = AgentView {
        id: agent,
        name: "Local assistant".into(),
        role: "assistant".into(),
        parent_agent_id: None,
        runtime_profile_id: profile.into(),
        execution_host: "local".into(),
        model_label: model,
        run_id: run.clone(),
        task_id: run.clone(),
        step_id: None,
        state: phase.clone(),
        source_event_ids: session.events.iter().map(|e| e.event_id.clone()).collect(),
        summary: "One actual local agent; model transport does not execute tools".into(),
    };
    let flow = WorkflowView {
        id: format!("workflow:{run}"),
        run_id: run.clone(),
        session_id: Some(run),
        title: "Local session activity".into(),
        state: phase,
        steps,
    };
    (vec![root], vec![flow], outputs)
}
