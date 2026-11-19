use bob_engine::functional::{Message, Session};
#[test]
fn app_chat_accepts_user_input_and_keeps_followup_context() {
    let mut s = Session::new();
    let mut calls = Vec::new();
    s.turn("Remember violet comet", &mut |messages: &[Message]| {
   calls.push(messages.to_vec());
   Ok(serde_json::json!({"ok":true,"content":"{\"type\":\"message\",\"content\":\"Remembered\"}","model":"unit-fixture","completion_id":"fixture-1"}))
 }).unwrap();
    s.turn("What did I say?", &mut |messages: &[Message]| {
   assert!(messages.iter().any(|m| m.content=="Remember violet comet"));
   assert!(messages.iter().any(|m| m.content.contains("Remembered")));
   Ok(serde_json::json!({"ok":true,"content":"{\"type\":\"message\",\"content\":\"violet comet\"}","completion_id":"fixture-2"}))
 }).unwrap();
    assert_eq!(s.messages.last().unwrap().content, "violet comet");
    assert_eq!(s.receipts.len(), 2);
}

#[test]
fn model_loop_receives_actual_selected_project_tool_results() {
    let p = std::env::temp_dir().join(format!("bob-functional-loop-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir(&p).unwrap();
    std::fs::write(p.join("facts.txt"), "copper finch 463").unwrap();
    let mut s = Session::new();
    s.attach(&p).unwrap();
    let mut round = 0;
    s.turn("Read facts.txt",&mut |messages:&[Message]| {
  round+=1;
  if round==1{Ok(serde_json::json!({"ok":true,"content":"{\"type\":\"action\",\"action\":{\"tool\":\"read\",\"path\":\"facts.txt\"}}"}))}
  else {assert!(messages.last().unwrap().content.contains("copper finch 463"));Ok(serde_json::json!({"ok":true,"content":"{\"type\":\"message\",\"content\":\"copper finch 463\"}"}))}
 }).unwrap();
    assert_eq!(round, 2);
    assert!(s.events.iter().any(|e| e.kind == "tool.result"));
    std::fs::remove_dir_all(p).unwrap();
}

#[test]
fn malformed_model_json_is_not_repaired_or_executed_but_can_be_corrected_in_budget() {
    let mut s = Session::new();
    let mut calls = 0;
    s.turn("hello",&mut |m:&[Message]|{calls+=1;if calls==1{Ok(serde_json::json!({"ok":true,"content":"{\"type\":\"message\",\"content\":\"hello\"}`"}))}else{assert!(m.last().unwrap().content.contains("REJECTED"));Ok(serde_json::json!({"ok":true,"content":"{\"type\":\"message\",\"content\":\"corrected actual response\"}"}))}}).unwrap();
    assert_eq!(calls, 2);
    assert_eq!(
        s.messages.last().unwrap().content,
        "corrected actual response"
    );
    assert!(s.events.iter().any(|e| e.kind == "model.response_rejected"));
    assert!(!s.events.iter().any(|e| e.kind == "tool.started"));
}
