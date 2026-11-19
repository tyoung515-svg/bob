//! Bounded local stdio adapter around the CURRENT native command implementations.
use std::{collections::HashSet,fs::File,io::{self,BufRead,Read,Write},os::fd::FromRawFd,path::Path,sync::{Arc,Mutex,atomic::{AtomicUsize,Ordering}}};
mod functional { include!(concat!(env!("OUT_DIR"),"/functional.rs")); }
mod workbench { include!(concat!(env!("OUT_DIR"),"/workbench.rs")); }
include!(concat!(env!("OUT_DIR"),"/dispatch.rs"));
#[cfg(test)]mod host_contract_tests{
 #[test]fn wsl_cancellation_notice_matches_native_supervisor(){
  let root=std::env::temp_dir().join(format!("bob-wsl-notice-{}",uuid::Uuid::new_v4()));
  std::env::set_var("BOB_WSL_ENGINE","1");
  let state=crate::functional::AppState::at_root(root);
  let cancel=crate::functional::app_cancel(&state).unwrap();
  let snapshot=serde_json::to_value(crate::workbench::workbench_snapshot(&state).unwrap()).unwrap();
  std::env::remove_var("BOB_WSL_ENGINE");
  assert!(cancel.contains("145 seconds"),"{cancel}");assert!(snapshot.to_string().contains("145 seconds"),"{snapshot}");
 }
}
#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Request { id:u64, command:String, args:serde_json::Value }
struct ProcessGroup;
impl ProcessGroup {
 fn own()->Result<Self,String>{
  if unsafe{libc::getpgrp()}!=unsafe{libc::getpid()} && unsafe{libc::setpgid(0,0)}!=0{return Err("cannot isolate host process group".into())}Ok(Self)
 }
}
impl Drop for ProcessGroup {fn drop(&mut self){unsafe{libc::signal(libc::SIGTERM,libc::SIG_IGN);libc::killpg(libc::getpgrp(),libc::SIGTERM)};}}
fn require_ext4_before_create(root:&Path)->Result<(),String>{
 let mut existing=root;
 loop{match std::fs::symlink_metadata(existing){Ok(_)=>break,Err(e) if e.kind()==io::ErrorKind::NotFound=>existing=existing.parent().ok_or("no existing store ancestor")?,Err(e)=>return Err(e.to_string())}}
 let file=File::open(existing).map_err(|e|e.to_string())?;
 bob_engine::functional_project::wsl_filesystem_guard(&file)
}
fn main(){if let Err(e)=run(){eprintln!("WSL workbench host unavailable: {e}");std::process::exit(2)}}
fn run()->Result<(),String>{
 let args:Vec<_>=std::env::args().collect();
 if args.len()!=3||args[1]!="--root"{return Err("usage: bob-wsl-host --root PRIVATE_EXT4_ROOT".into())}
 let root=std::path::PathBuf::from(&args[2]);if !root.is_absolute(){return Err("data root must be absolute".into())}
 // This is a WSL host, not a caller-selected security mode. Guard before writes.
 std::env::set_var("BOB_WSL_ENGINE","1");
 require_ext4_before_create(&root)?;
 bob_engine::functional::Session::saved(&root)?;
 bob_engine::functional_project::wsl_filesystem_guard(&File::open(&root).map_err(|e|e.to_string())?)?;
 std::env::set_var("BOB_FUNCTIONAL_DATA_ROOT",root);std::env::remove_var("BOB_DEVELOPER_FIXTURE");
 let _group=ProcessGroup::own()?;
 // Reserve a CLOEXEC protocol descriptor; accidental stdout prints become stderr.
 let fd=unsafe{libc::fcntl(1,libc::F_DUPFD_CLOEXEC,3)};
 if fd<0{return Err("protocol descriptor unavailable".into())}
 let protocol=unsafe{File::from_raw_fd(fd)};
 if unsafe{libc::dup2(2,1)}<0{return Err("cannot isolate protocol stdout".into())}
 let state=Arc::new(functional::AppState::default());let output=Arc::new(Mutex::new(protocol));let active=Arc::new(AtomicUsize::new(0));let mut seen=HashSet::new();
 let mut input=io::stdin().lock();
 loop{
  let mut bytes=Vec::new();let n=(&mut input).take(262145).read_until(b'\n',&mut bytes).map_err(|e|e.to_string())?;
  if n==0{break}if n>262144||bytes.last()!=Some(&b'\n'){return Err("request frame limit exceeded".into())}
  let request:Request=serde_json::from_slice(&bytes).map_err(|e|format!("invalid request: {e}"))?;
  if request.id==0||!request.args.is_object()||seen.len()>=65536||!seen.insert(request.id){return Err("invalid, duplicate or exhausted correlation; no replay".into())}
  let state=state.clone();let output=output.clone();let active=active.clone();
  if active.fetch_add(1,Ordering::SeqCst)>=8{
   active.fetch_sub(1,Ordering::SeqCst);let reply=serde_json::json!({"id":request.id,"ok":false,"error":"host busy; no dispatch attempted"});writeln!(output.lock().unwrap(),"{reply}").map_err(|e|e.to_string())?;continue
  }
  std::thread::spawn(move||{
   let result=std::panic::catch_unwind(std::panic::AssertUnwindSafe(||dispatch(&state,&request.command,&request.args)));
   let reply=match result{Ok(Ok(value))=>serde_json::json!({"id":request.id,"ok":true,"value":value}),Ok(Err(error))=>serde_json::json!({"id":request.id,"ok":false,"error":error}),Err(_)=>serde_json::json!({"id":request.id,"ok":false,"error":"host fault; outcome unknown, inspect history; no automatic retry"})};
   let mut out=output.lock().unwrap();let _=writeln!(out,"{reply}");let _=out.flush();active.fetch_sub(1,Ordering::SeqCst);
  });
 }
 Ok(()) // ProcessGroup cleans up on EOF and every error/unwind path.
}
