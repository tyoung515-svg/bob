use bob_engine::functional::Session;
#[test]
fn per_conversation_next_turn_model_pin_preserves_old_history_and_never_falls_back(){
 let root=std::env::temp_dir().join(format!("usability-model-{}",uuid::Uuid::new_v4()));let mut s=Session::create(&root).unwrap();let id=s.id.clone();
 assert_eq!(s.selected_model(),"bob-ds-flash");
 s.turn("remember synthetic violet bridge",&mut |_|Ok(serde_json::json!({"ok":true,"model":"bob-ds-flash","requested_model":"bob-ds-flash","bob":{"model_pin":"deepseek-v4-flash"},"content":r#"{"type":"message","content":"remembered"}"#}))).unwrap();
 let old_receipts=s.receipts.clone();let old_events=serde_json::to_value(&s.events).unwrap();
 s.select_model("bob-gpt-sol-61").unwrap();assert_eq!(s.selected_model(),"bob-gpt-sol-61");assert_eq!(s.receipts,old_receipts);assert_eq!(serde_json::to_value(&s.events[..old_events.as_array().unwrap().len()]).unwrap(),old_events);
 drop(s);let mut restored=Session::load(&root,&id).unwrap();assert_eq!(restored.selected_model(),"bob-gpt-sol-61");assert_eq!(restored.messages[0].content,"remember synthetic violet bridge");
 for invalid in ["deepseek-v4-pro","bob-qwen-strata","unknown","../other"]{assert!(restored.select_model(invalid).is_err());assert_eq!(restored.selected_model(),"bob-gpt-sol-61")}
 restored.validate().unwrap();std::fs::remove_dir_all(root).unwrap();
}
