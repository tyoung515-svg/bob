use bob_engine::functional::{Session,Message};
use serde_json::{json,Value};
use std::fs;
#[test]
fn depth_truncation_is_stable_and_does_not_hide_security_or_resource_failures() {
 use std::os::unix::fs::{symlink,PermissionsExt};
 let root=std::env::temp_dir().join(format!("bob-depth-boundaries-{}",uuid::Uuid::new_v4()));fs::create_dir(&root).unwrap();
 let deep=root.join("a/b/c/d/e/f/g/h/skipped");fs::create_dir_all(&deep).unwrap();fs::write(root.join("z.txt"),"needle").unwrap();fs::write(root.join("a.txt"),"needle").unwrap();
 symlink("/etc",root.join("outside-link")).unwrap();
 let p=bob_engine::functional_project::Project::attach(&root).unwrap();
 let first=p.list_output().unwrap();assert_eq!(first,p.list_output().unwrap());assert_eq!(first["files"],json!(["a.txt","z.txt"]));assert_eq!(first["skipped_depth"],json!(["a/b/c/d/e/f/g/h/skipped"]));
 assert!(p.read("outside-link/passwd").is_err());assert!(p.read("../etc/passwd").is_err());assert!(p.read("missing.txt").is_err());
 fs::set_permissions(&deep,fs::Permissions::from_mode(0o000)).unwrap();assert!(p.list_output().is_err(),"permission denial at the depth edge must not turn into a successful partial scan");fs::set_permissions(&deep,fs::Permissions::from_mode(0o700)).unwrap();
 for i in 0..257 {fs::write(root.join(format!("file{i}.txt")),"x").unwrap();}assert!(p.list_output().unwrap_err().contains("file limit"));
 for i in 0..257 {fs::remove_file(root.join(format!("file{i}.txt"))).unwrap();}
 for i in 0..4100 {fs::create_dir(root.join(format!("empty{i}"))).unwrap();}assert!(p.list_output().unwrap_err().contains("node/time budget"));
 fs::remove_dir_all(root).unwrap();
}
#[test]
fn deeper_tree_does_not_destroy_shallow_listing() {
 let root=std::env::temp_dir().join(format!("bob-deep-list-{}",uuid::Uuid::new_v4()));fs::create_dir(&root).unwrap();fs::write(root.join("shallow.txt"),"needle").unwrap();
 let deep=root.join("d/1/2/3/4/5/6/7/8/9");fs::create_dir_all(&deep).unwrap();
 let project=bob_engine::functional_project::Project::attach(&root).unwrap();
 assert_eq!(project.list().unwrap(),vec!["shallow.txt"]);fs::remove_dir_all(root).unwrap();
}
#[test]
fn depth_capped_list_and_search_keep_shallow_results_and_tell_model_incomplete() {
 let root=std::env::temp_dir().join(format!("bob-depth-{}",uuid::Uuid::new_v4())); fs::create_dir(&root).unwrap();
 fs::write(root.join("shallow.txt"),"needle").unwrap();
 let mut dir=root.clone();for _ in 0..8 {dir=dir.join("nested");fs::create_dir(&dir).unwrap();}
 fs::write(dir.join("threshold.txt"),"needle").unwrap();
 let mut s=Session::new();s.attach(&root).unwrap();
 for tool in ["list","search"] {
  let mut round=0;
  s.turn(tool,&mut |messages:&[Message]| {round+=1;
   if round==1 {let action=if tool=="search"{json!({"tool":tool,"query":"needle"})}else{json!({"tool":tool})};return Ok(json!({"ok":true,"content":json!({"type":"action","action":action}).to_string()}))}
   let evidence=messages.last().unwrap().content.split_once(": ").unwrap().1;
   let result:Value=serde_json::from_str(evidence).unwrap(); assert_eq!(result["status"],"succeeded");assert_eq!(result["output"]["complete"],true,"threshold is still exhaustive");
   Ok(json!({"ok":true,"content":r#"{"type":"message","content":"fixture only"}"#}))
  }).unwrap();
 }
 let deeper=dir.join("beyond");fs::create_dir(&deeper).unwrap();fs::write(deeper.join("hidden.txt"),"only_deep").unwrap();
 for (tool,query) in [("list",""),("search","needle"),("search","only_deep")] {
  let mut round=0;
  s.turn(tool,&mut |messages:&[Message]| {round+=1;
   if round==1 {let action=if tool=="search"{json!({"tool":tool,"query":query})}else{json!({"tool":tool})};return Ok(json!({"ok":true,"content":json!({"type":"action","action":action}).to_string()}))}
   let result:Value=serde_json::from_str(messages.last().unwrap().content.split_once(": ").unwrap().1).unwrap();
   assert_eq!(result["status"],"succeeded","a deeper subtree must not discard shallow results");let out=&result["output"];
   assert_eq!(out["complete"],false);assert_eq!(out["truncated"],true);assert_eq!(out["max_depth"],8);assert_eq!(out["skipped_depth"].as_array().unwrap().len(),1);
   let found=&out[if tool=="list"{"files"}else{"matches"}];
   if query=="only_deep" {assert_eq!(found.as_array().unwrap().len(),0,"empty but explicitly incomplete");} else {assert!(found.to_string().contains("shallow.txt"));assert!(found.to_string().contains("threshold.txt"));}
   assert!(!found.to_string().contains("hidden.txt"));
   Ok(json!({"ok":true,"content":r#"{"type":"message","content":"fixture only"}"#}))
  }).unwrap();s.validate().unwrap();
 }
 fs::remove_dir_all(root).unwrap();
}
