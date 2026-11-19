#[cfg(target_os="linux")]
#[test]
fn replaced_project_root_cannot_retarget_reads_or_approved_writes(){
 use bob_engine::functional_project::Project;
 let outer=std::env::temp_dir().join(format!("bob-root-swap-{}",uuid::Uuid::new_v4()));let root=outer.join("project");std::fs::create_dir_all(&root).unwrap();std::fs::write(root.join("notes.txt"),"original").unwrap();
 let project=Project::attach(&root).unwrap();std::fs::rename(&root,outer.join("original-project")).unwrap();std::fs::create_dir(&root).unwrap();std::fs::write(root.join("notes.txt"),"replacement protected").unwrap();
 assert!(project.read("notes.txt").unwrap_err().contains("identity changed"));
 assert!(project.write("notes.txt",Some("original"),"unauthorized").is_err());
 assert_eq!(std::fs::read_to_string(root.join("notes.txt")).unwrap(),"replacement protected");
}
