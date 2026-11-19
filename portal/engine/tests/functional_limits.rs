use bob_engine::functional::{DecisionV2, Message, Session};
use serde_json::json;
#[test]
fn total_turn_round_budget_survives_write_approval_pauses() {
    let p = std::env::temp_dir().join(format!("bob-functional-rounds-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir(&p).unwrap();
    std::fs::write(p.join("note.txt"), "old").unwrap();
    let mut s = Session::new();
    s.attach(&p).unwrap();
    let mut calls = 0;
    let mut model = |_: &[Message]| {
        calls += 1;
        Ok(
            json!({"ok":true,"content":"{\"type\":\"action\",\"action\":{\"tool\":\"write\",\"path\":\"note.txt\",\"content\":\"proposed\"}}"}),
        )
    };
    s.turn("edit", &mut model).unwrap();
    for _ in 0..8 {
        if let Some(a) = s.pending.clone() {
            let _ = s.resolve(&a.id, &a.digest, false, &mut model);
        }
    }
    assert!(
        s.pending.is_none(),
        "round bound cannot reset when an approval is resolved"
    );
    assert_eq!(calls, 8);
    std::fs::remove_dir_all(p).unwrap();
}
#[test]
fn decision_v2_predecessor_cannot_fork_from_an_older_record() {
    let p = std::env::temp_dir().join(format!("bob-functional-chain-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir(&p).unwrap();
    let mut s = Session::new();
    s.attach(&p).unwrap();
    let mut calls = 0;
    s.turn("list",&mut |_:&[Message]|{calls+=1;Ok(json!({"ok":true,"content":if calls==1{"{\"type\":\"action\",\"action\":{\"tool\":\"list\"}}"}else{"{\"type\":\"message\",\"content\":\"empty\"}"}}))}).unwrap();
    let proposed: DecisionV2 = serde_json::from_value(
        s.events
            .iter()
            .find(|e| e.kind == "authority.decision")
            .unwrap()
            .data
            .clone(),
    )
    .unwrap();
    let terminal = s
        .events
        .iter_mut()
        .rev()
        .find(|e| e.kind == "authority.decision")
        .unwrap();
    terminal.data["state"] = json!("approved");
    terminal.data["previous_record_id"] = json!(proposed.record_id);
    let mut prev = None;
    for e in &mut s.events {
        e.previous_digest = prev.clone();
        e.digest.clear();
        e.digest = bob_engine::jcs_sha256(&json!(e)).unwrap();
        prev = Some(e.digest.clone());
    }
    assert!(
        s.validate().is_err(),
        "valid hashes must not make a forked decision chain valid"
    );
    std::fs::remove_dir_all(p).unwrap();
}
#[test]
fn transport_request_validation_matches_private_relay_limits() {
    let many = vec![
        Message {
            role: "user".into(),
            content: "hi".into()
        };
        101
    ];
    assert!(bob_engine::functional::validate_model_request(&many).is_err());
    let big = vec![Message {
        role: "user".into(),
        content: "z".repeat(90000),
    }];
    assert!(bob_engine::functional::validate_model_request(&big).is_err());
}
