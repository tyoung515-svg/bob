//! TRANSPORT UNIT: fake timeout captures fixed argv, never dispatches SSH/provider.
#[cfg(target_os="linux")]
#[test]
fn wsl_transport_deadlines_cover_server_relay_and_windows_ssh_connect(){
 use std::os::unix::fs::PermissionsExt;
 let dir=std::env::temp_dir().join(format!("bob-deadline-{}",uuid::Uuid::new_v4()));std::fs::create_dir(&dir).unwrap();
 let program=dir.join("timeout");std::fs::write(&program,b"#!/bin/sh\n/bin/cat >/dev/null\nprintf '{\"ok\":true,\"timeout\":\"%s\"}' \"$2\"\n").unwrap();std::fs::set_permissions(&program,std::fs::Permissions::from_mode(0o700)).unwrap();
 let path=std::env::var_os("PATH");std::env::set_var("PATH",&dir);std::env::set_var("BOB_WSL_ENGINE","1");
 let messages=vec![bob_engine::functional::Message{role:"user".into(),content:"synthetic deadline capture".into()}];
 let ds=bob_engine::functional::relay(&messages).unwrap();
 let gpt=bob_engine::model_transport::request(&serde_json::json!({"op":"catalog"})).unwrap();
 std::env::remove_var("BOB_WSL_ENGINE");
 let linux_ds=bob_engine::functional::relay(&messages).unwrap();let linux_gpt=bob_engine::model_transport::request(&serde_json::json!({"op":"catalog"})).unwrap();
 if let Some(path)=path{std::env::set_var("PATH",path)}
 assert_eq!(ds["timeout"],"142");assert_eq!(gpt["timeout"],"142");
 assert_eq!(linux_ds["timeout"],"120");assert_eq!(linux_gpt["timeout"],"110");
}
