use bob_engine::functional::Session;
#[test]
fn pin_archive_restore_survive_restart_without_mutating_prior_audit() {
 let root=std::env::temp_dir().join(format!("usability-org-{}",uuid::Uuid::new_v4()));
 let mut s=Session::create(&root).unwrap();let id=s.id.clone();s.rename("Named synthetic conversation").unwrap();
 let prior=serde_json::to_value(&s.events).unwrap();drop(s);
 Session::organize_saved(&root,&id,true,true).unwrap();
 let listed=serde_json::to_value(Session::saved(&root).unwrap()).unwrap();
 assert_eq!(listed[0]["pinned"],true);assert_eq!(listed[0]["archived"],true);
 let loaded=Session::load(&root,&id).unwrap();loaded.validate().unwrap();
 assert_eq!(serde_json::to_value(&loaded.events[..prior.as_array().unwrap().len()]).unwrap(),prior);
 assert!(loaded.project.is_none());drop(loaded);
 Session::organize_saved(&root,&id,false,false).unwrap();
 let listed=serde_json::to_value(Session::saved(&root).unwrap()).unwrap();assert_eq!(listed[0]["pinned"],false);assert_eq!(listed[0]["archived"],false);
 assert!(Session::organize_saved(&root,"../escape",true,true).is_err());
 std::fs::remove_dir_all(root).unwrap();
}
