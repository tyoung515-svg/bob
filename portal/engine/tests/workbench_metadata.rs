use bob_engine::functional::Session;
#[test]
fn rename_persists_metadata_without_rewriting_history_or_rebinding_project() {
    let root = std::env::temp_dir().join(format!("workbench-meta-{}", uuid::Uuid::new_v4()));
    let mut s = Session::create(&root).unwrap();
    let id = s.id.clone();
    let prefix = serde_json::to_value(&s.events).unwrap();
    s.rename("  A named task  ").unwrap();
    assert_eq!(serde_json::to_value(&s.events[..1]).unwrap(), prefix);
    assert_eq!(Session::saved(&root).unwrap()[0].title, "A named task");
    assert!(s.rename("bad\nname").is_err());
    Session::rename_saved(&root, &id, "Another title").unwrap();
    assert_eq!(Session::saved(&root).unwrap()[0].title, "Another title");
    assert!(Session::rename_saved(&root, "../escape", "x").is_err());
    Session::load(&root, &id).unwrap().validate().unwrap();
    std::fs::remove_dir_all(root).unwrap();
}
