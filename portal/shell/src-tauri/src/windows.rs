//! Native Windows WebView. Two transports: the existing WSL local-runtime host
//! (bounded stdio, behaviour unchanged) and the native SSH portal to the server
//! Bob broker (portal.rs, CONTRACT §2/§3).
//! No listener, credentials, shell interpolation or model-owned process strings.
use std::{collections::HashMap,fs::File,io::{BufRead,BufReader,Read,Write},os::windows::io::{AsRawHandle,FromRawHandle,OwnedHandle},path::Path,process::{Child,Command,Stdio},sync::{Arc,Mutex,mpsc,atomic::{AtomicU64,Ordering}},time::{Duration,Instant}};
use serde_json::{Value,json};
use tauri::Manager;
use crate::{ide,local_store,portal,runtime,LOCAL_COMMANDS};
type Reply=Result<Value,String>;
pub(crate) const COMMANDS:&[&str]=&["app_boot","app_begin","app_release","app_send","app_attach","app_resolve","app_cancel","app_new","app_open","app_developer_mode","workbench_save_project","workbench_collapse_project","workbench_new_project_session","workbench_move_session_group","workbench_snapshot","workbench_select_profile","workbench_configure_standalone","workbench_rename_session","workbench_refresh_models","workbench_select_model","workbench_organize_session","workbench_pick_project","workbench_start_bob_run","workbench_attach_bob_run","workbench_cancel_bob_run"];
#[derive(Default)]struct Pending{dead:bool,replies:HashMap<u64,mpsc::Sender<Reply>>}
struct Proxy{input:mpsc::SyncSender<Option<Vec<u8>>>,child:Arc<Mutex<Option<Child>>>,pending:Arc<Mutex<Pending>>,next:AtomicU64,wsl_started:Option<Instant>,_job:Option<OwnedHandle>}
#[derive(Default)]
enum ProxyState{#[default]Unstarted,Started(Result<Arc<Proxy>,String>),Closed}
#[derive(Default)]
struct LazyProxy{state:Mutex<ProxyState>}
impl LazyProxy{
 fn get(&self)->Result<Arc<Proxy>,String>{self.get_or_start(Proxy::start)}
 fn get_or_start(&self,start:impl FnOnce()->Result<Arc<Proxy>,String>)->Result<Arc<Proxy>,String>{
  let mut state=self.state.lock().map_err(|_|"proxy fault")?;
  if matches!(*state,ProxyState::Unstarted){*state=ProxyState::Started(start())}
  match &*state{ProxyState::Started(result)=>result.clone(),ProxyState::Closed=>Err("candidate window closed; no dispatch".into()),ProxyState::Unstarted=>unreachable!()}
 }
 fn shutdown(&self){
  let previous={let mut state=self.state.lock().unwrap();std::mem::replace(&mut *state,ProxyState::Closed)};
  if let ProxyState::Started(Ok(proxy))=previous{proxy.shutdown()}
 }
}
fn wsl_path(path:&Path)->Result<String,String>{
 use std::path::Component;
 let mut c=path.components();let drive=match c.next(){Some(Component::Prefix(p))=>match p.kind(){std::path::Prefix::Disk(d)=>d.to_ascii_lowercase() as char,_=>return Err("UNC/device package paths unsupported".into())},_=>return Err("package path must be absolute drive path".into())};
 if !matches!(c.next(),Some(Component::RootDir)){return Err("drive-relative paths refused".into())}
 let mut result=format!("/mnt/{drive}");for part in c{match part{Component::Normal(n)=>{let s=n.to_str().ok_or("non Unicode package path")?;if s.contains(['\n','\r','\0']){return Err("invalid package path".into())}result.push('/');result.push_str(s)},_=>return Err("relative components refused".into())}}Ok(result)
}
pub(crate) fn owned_job(child:&Child)->Result<OwnedHandle,String>{
 use windows_sys::Win32::System::JobObjects::*;
 let raw=unsafe{CreateJobObjectW(std::ptr::null(),std::ptr::null())};
 if raw.is_null(){return Err("candidate Job Object unavailable".into())}
 let job=unsafe{OwnedHandle::from_raw_handle(raw)};
 let mut info=JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();info.BasicLimitInformation.LimitFlags=JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
 if unsafe{SetInformationJobObject(raw,JobObjectExtendedLimitInformation,&info as *const _ as *const _,std::mem::size_of_val(&info) as u32)}==0||unsafe{AssignProcessToJobObject(raw,child.as_raw_handle())}==0{return Err(format!("cannot supervise candidate child with Job Object: {}",std::io::Error::last_os_error()))}
 Ok(job)
}
use crate::runtime::SESSION_FLOOR;
pub(crate) const SESSION_CLOSE_GRACE:Duration=Duration::from_millis(300);
pub(crate) fn is_wsl_program(program:&std::ffi::OsStr)->bool{
 Path::new(program).file_name().is_some_and(|n|n.to_string_lossy().eq_ignore_ascii_case("wsl.exe"))
}
/// The caller closes the transport writer as well; taking the child drops our
/// process handle even when a young WSL process must be allowed to continue.
pub(crate) fn end_session(slot:&mut Option<Child>,wsl_started:Option<Instant>,grace:Duration){
 let Some(mut child)=slot.take()else{return};
 drop(child.stdin.take());
 if matches!(child.try_wait(),Ok(Some(_))){return}
 if wsl_started.is_some_and(|started|started.elapsed()<SESSION_FLOOR){
  shutdown_log(&format!("session pid={} still running: young WSL session detached; stdin closed when its writer releases",child.id()));
  return
 }
 let deadline=Instant::now()+grace;
 while Instant::now()<deadline{
  if matches!(child.try_wait(),Ok(Some(_))){return}
  std::thread::sleep(Duration::from_millis(10));
 }
 if let Err(error)=child.kill(){shutdown_log(&format!("session pid={} still running: termination failed: {error}",child.id()));}
 let _=child.wait();
}
fn shutdown_log(line:&str){
 if let Some(dir)=STARTUP_LOG_DIR.get(){let _=append_log(dir,"shutdown.log",line);}
}
fn fail_transport(pending:&Mutex<Pending>,child:&Mutex<Option<Child>>,wsl_started:Option<Instant>,reason:&str){
 {let mut p=pending.lock().unwrap();p.dead=true;for(_,tx)in p.replies.drain(){let _=tx.send(Err(reason.into()));}}
 if let Ok(mut child)=child.lock(){end_session(&mut child,wsl_started,Duration::ZERO)}
}
impl Proxy{
 fn start()->Result<Arc<Self>,String>{
  let exe=std::env::current_exe().map_err(|e|e.to_string())?;let dir=exe.parent().ok_or("package directory unavailable")?;
  let host=dir.join("bob-wsl-host");if !host.is_file(){return Err("WSL-dependent package missing bob-wsl-host; no native fallback".into())}
  let host_path=wsl_path(&host)?;let transport_path=wsl_path(&dir.join("transport"))?;
  let root=std::env::var("BOB_WSL_DATA_ROOT").unwrap_or_else(|_|"/root/bob-windows-ui-review-20261024/data".into());
  if !root.starts_with('/')||root.contains(['\0','\r','\n']){return Err("WSL store requires an absolute Linux path".into())}
  let logs=log_dir(dir);std::fs::create_dir_all(&logs).map_err(|e|e.to_string())?;
  let stderr=File::options().create(true).append(true).open(logs.join("host-stderr.log")).map_err(|e|e.to_string())?;
  use std::os::windows::process::CommandExt;
  let started=Instant::now();
  let child=Command::new("C:\\Windows\\System32\\wsl.exe").args(["--distribution","Ubuntu","--exec","env",&format!("PATH={transport_path}:/usr/bin:/bin"),&host_path,"--root",&root]).creation_flags(0x08000000).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::from(stderr)).spawn().map_err(|e|format!("WSL host unavailable: {e}"))?;
  Self::supervise_child(child,Some(started))
 }
 #[cfg(test)]
 fn from_child(child:Child)->Result<Arc<Self>,String>{Self::supervise_child(child,None)}
 fn supervise_child(mut child:Child,wsl_started:Option<Instant>)->Result<Arc<Self>,String>{
  let job=if wsl_started.is_some() {None} else {Some(match owned_job(&child){Ok(job)=>job,Err(e)=>{let _=child.kill();let _=child.wait();return Err(e)}})};
  let mut pipe=child.stdin.take().ok_or("host stdin missing")?;let output=child.stdout.take().ok_or("host stdout missing")?;
  let child=Arc::new(Mutex::new(Some(child)));let pending=Arc::new(Mutex::new(Pending::default()));
  // A bounded writer queue keeps blocking pipe writes outside IPC/shutdown locks.
  let(input,queue)=mpsc::sync_channel::<Option<Vec<u8>>>(8);let writing=pending.clone();let writer_child=child.clone();
  std::thread::spawn(move||{while let Ok(Some(mut bytes))=queue.recv(){if writing.lock().unwrap().dead{break}bytes.push(b'\n');if pipe.write_all(&bytes).and_then(|_|pipe.flush()).is_err(){fail_transport(&writing,&writer_child,wsl_started,"WSL write failed; outcome unknown; no automatic retry");break}}});
  let responses=pending.clone();let reader_child=child.clone();let reader_input=input.clone();
  std::thread::spawn(move||{
   let mut reader=BufReader::new(output);
   loop{
    let mut bytes=Vec::new();match(&mut reader).take(32*1024*1024+1).read_until(b'\n',&mut bytes){Ok(0)|Err(_)=>break,Ok(n)if n>32*1024*1024||bytes.last()!=Some(&b'\n')=>break,_=>{}}
    let value:Value=match serde_json::from_slice(&bytes){Ok(v)=>v,Err(_)=>break};let Some(id)=value["id"].as_u64()else{break};
    if !value["ok"].is_boolean(){break}
    let tx=responses.lock().unwrap().replies.remove(&id);
    if let Some(tx)=tx{let reply=if value["ok"]==true{Ok(value["value"].clone())}else{Err(value["error"].as_str().unwrap_or("host outcome unknown; no retry").to_owned())};let _=tx.send(reply);}
   }
   let _=reader_input.try_send(None);
   fail_transport(&responses,&reader_child,wsl_started,"WSL host disconnected/protocol invalid; outcome unknown, inspect history; no automatic retry");
  });
  Ok(Arc::new(Self{input,child,pending,next:AtomicU64::new(1),wsl_started,_job:job}))
 }
 fn call(&self,command:&str,args:Value)->Reply{
  if !COMMANDS.contains(&command)||!args.is_object(){return Err("unknown command or invalid arguments; no dispatch".into())}
  let id=self.next.fetch_add(1,Ordering::SeqCst);let bytes=serde_json::to_vec(&json!({"id":id,"command":command,"args":args})).map_err(|e|e.to_string())?;
  if bytes.len()>262143{return Err("request exceeds 256KiB; no dispatch".into())}
  let(tx,rx)=mpsc::channel();{
   let mut p=self.pending.lock().map_err(|_|"proxy fault")?;
   if p.dead{return Err("WSL host closed; no dispatch".into())}
   if p.replies.len()>=8{return Err("WSL host busy; no dispatch".into())}p.replies.insert(id,tx);
   if self.input.try_send(Some(bytes)).is_err(){p.replies.remove(&id);return Err("WSL writer unavailable/busy; no dispatch".into())}
  }
  match rx.recv_timeout(Duration::from_secs(145)){
   Ok(result)=>result,
   Err(_)=>{let _=self.input.try_send(None);fail_transport(&self.pending,&self.child,self.wsl_started,"WSL response deadline elapsed; outcome unknown, inspect history; no automatic retry");Err("WSL response deadline elapsed; outcome unknown; no automatic retry".into())}
  }
 }
 fn shutdown(&self){
  {let mut p=self.pending.lock().unwrap();p.dead=true;for(_,tx)in p.replies.drain(){let _=tx.send(Err("candidate window closed; outcome unknown; no replay".into()));}}
  let _=self.input.try_send(None);
  end_session(&mut self.child.lock().unwrap(),self.wsl_started,SESSION_CLOSE_GRACE);
 }
}
static STARTUP_LOG_DIR:std::sync::OnceLock<std::path::PathBuf>=std::sync::OnceLock::new();
static STARTUP_REPORTED:std::sync::atomic::AtomicBool=std::sync::atomic::AtomicBool::new(false);
fn wide(value:&str)->Vec<u16>{value.encode_utf16().chain(std::iter::once(0)).collect()}
/// Owner-facing startup dialogs are BOUNDED. An unattended launch (SSH, a scheduled task, or
/// any session without a desktop) cannot click a message box: without a bound the process parks
/// on an invisible modal forever, keeps its build-directory mutex and then tells the owner to end
/// it in Task Manager. 2026-10-24: pid 33120 and its r9 siblings were exactly that.
/// The default is generous for a human and finite for a machine; 0 skips the dialog altogether.
const DIALOG_DEFAULT_MS:u64=180_000;
const DIALOG_MAX_MS:u64=600_000;
/// Resolve the dialog budget from an override string. Unparsable values fall back to the
/// default; anything larger than the ceiling is clamped so a typo cannot reinstate the hang.
fn dialog_timeout_from(raw:Option<&str>)->u64{
 match raw.and_then(|v|v.trim().parse::<u64>().ok()){Some(ms)if ms>DIALOG_MAX_MS=>DIALOG_MAX_MS,Some(ms)=>ms,None=>DIALOG_DEFAULT_MS}
}
fn dialog_timeout_ms()->u64{dialog_timeout_from(std::env::var("BOB_PORTAL_DIALOG_TIMEOUT_MS").ok().as_deref())}
/// Run one blocking UI call (a message box) on its own thread and wait at most `budget_ms` for it
/// to RETURN. A box that was never displayed (no desktop) cannot be joined, so the thread is
/// detached and process exit tears it down; the caller always regains control.
fn wait_bounded<F:FnOnce()+Send+'static>(work:F,budget_ms:u64)->bool{
 let (tx,rx)=mpsc::channel();
 let _=std::thread::spawn(move||{work();let _=tx.send(());});
 rx.recv_timeout(Duration::from_millis(budget_ms)).is_ok()
}
/// Show a message box without waiting on it forever: the caller proceeds (and exits) when the
/// box RETURNS or when the budget elapses, whichever comes first. Waiting on the return, not on
/// the clock, is what releases the build-directory mutex immediately when the owner clicks OK
/// (R12-1): a clock-only wait kept a dismissed-but-not-exited process holding it for the whole
/// budget. 0 skips the dialog entirely.
fn bounded_dialog(body:&str,title:&str,flags:u32,timeout_ms:u64){
 use windows_sys::Win32::UI::WindowsAndMessaging::MessageBoxW;
 if timeout_ms==0{return}
 let body=wide(body);let title=wide(title);
 let _=wait_bounded(move||unsafe{MessageBoxW(std::ptr::null_mut(),body.as_ptr(),title.as_ptr(),flags);},timeout_ms);
}


/// Stable per-build-directory mutex name. Two copies of one build directory ask WebView2 for
/// the same user-data folder; on 2026-10-24 a launch from an identical copy was followed by
/// the window that was already running exiting. A named mutex - released by the OS when the
/// process ends, so it cannot go stale - refuses the second copy instead of letting WebView2
/// arbitrate. Different build directories keep different names, so revisions can coexist.
fn instance_guard_name(dir:&Path)->String{
 let mut hash:u64=0xcbf29ce484222325u64;
 for byte in dir.to_string_lossy().to_lowercase().bytes(){hash^=byte as u64;hash=hash.wrapping_mul(0x100000001b3u64);}
 format!("Local\\BoB-portal-{hash:016x}")
}
static INSTANCE_GUARD:std::sync::OnceLock<usize>=std::sync::OnceLock::new();
/// Take the per-directory single-instance mutex. Err means another copy already holds it.
fn acquire_instance_guard(dir:&Path)->Result<usize,String>{
 use windows_sys::Win32::Foundation::{CloseHandle,SetLastError};
 use windows_sys::Win32::System::Threading::CreateMutexW;
 let name=wide(&instance_guard_name(dir));
 // R9-5: clear the thread last-error first. A stale 183 left by an earlier call
 // would otherwise refuse a legitimate first launch of this directory.
 unsafe{SetLastError(0)};
 let handle=unsafe{CreateMutexW(std::ptr::null(),0,name.as_ptr())};
 if handle.is_null(){return Err(format!("the single-instance guard could not be created: {}",std::io::Error::last_os_error()))}
 // A pre-existing mutex still returns a valid handle; GetLastError is how Windows says so.
 if std::io::Error::last_os_error().raw_os_error()==Some(183){
  unsafe{CloseHandle(handle)};
  return Err("another copy of this build directory is already open; if no window is visible, end BoB in Task Manager and launch again".into())
 }
 Ok(handle as usize)
}
/// Refusal path for a second copy: log it, tell the owner, exit 4. Never returns.
fn startup_refused(dir:&Path,detail:&str)->!{
 let log_path=log_dir(dir).join("startup-error.log");
 let logged=append_log(dir,"startup-error.log",detail);
 if std::env::var_os("BOB_PORTAL_NO_DIALOG").is_none(){
  use windows_sys::Win32::UI::WindowsAndMessaging::{MB_ICONINFORMATION,MB_OK,MB_SETFOREGROUND,MB_TOPMOST};
  let note=if logged{format!("Details were written to {}",log_path.display())}else{format!("The log could NOT be written under {}",log_path.display())};
  bounded_dialog(&format!("{detail}\r\n\r\n{note}"),"BoB - already open",MB_OK|MB_ICONINFORMATION|MB_TOPMOST|MB_SETFOREGROUND,dialog_timeout_ms());
 }
 std::process::exit(4)
}

/// The WebView2 user-data folder. An explicit ABSOLUTE folder wins: two copies of one build
/// that ask WebView2 for different browser arguments (a debug port, say) cannot share a
/// profile, and the second one then fails at startup. Otherwise use LOCALAPPDATA\<bundle identifier>\webview,
/// falling back to the old executable-relative profile only if LOCALAPPDATA is unset.
/// Relative and whitespace-only overrides are ignored rather than resolved against the CWD.
fn webview_profile(dir:&Path,localappdata:Option<std::ffi::OsString>,explicit:Option<std::ffi::OsString>)->std::path::PathBuf{
 if let Some(value)=explicit{let text=value.to_string_lossy().trim().to_string();if !text.is_empty()&&Path::new(&text).is_absolute(){return Path::new(&text).to_path_buf()}}
 match localappdata{Some(root)=>Path::new(&root).join(crate::APP_DATA_DIR).join("webview"),None=>dir.join("webview-profile")}
}
fn log_dir_from(dir:&Path,localappdata:Option<std::ffi::OsString>)->std::path::PathBuf{
 match localappdata{Some(root)=>Path::new(&root).join(crate::APP_DATA_DIR).join("logs"),None=>dir.join("logs")}
}
fn log_dir(dir:&Path)->std::path::PathBuf{
 log_dir_from(dir,std::env::var_os("LOCALAPPDATA"))
}
/// Append one line to logs/<name>; false when the directory or file is not writable.
fn append_log(dir:&Path,name:&str,line:&str)->bool{
 let log_dir=log_dir(dir);
 if std::fs::create_dir_all(&log_dir).is_err(){return false}
 match std::fs::OpenOptions::new().create(true).append(true).open(log_dir.join(name)){Ok(mut file)=>file.write_all(line.as_bytes()).and_then(|_|file.write_all(b"\r\n")).is_ok(),Err(_)=>false}
}
fn set_webview_folder(dir:&Path){
 let _=STARTUP_LOG_DIR.set(dir.to_path_buf());
 std::panic::set_hook(Box::new(|info|{
  if STARTUP_REPORTED.swap(true,std::sync::atomic::Ordering::SeqCst){return}
  let dir=STARTUP_LOG_DIR.get().cloned().unwrap_or_else(||std::env::current_dir().unwrap_or_else(|_|std::path::PathBuf::from(".")));
  startup_failed(&dir,&format!("unexpected panic: {info}"))
 }));
 let folder=webview_profile(dir,std::env::var_os("LOCALAPPDATA"),std::env::var_os("WEBVIEW2_USER_DATA_FOLDER"));
 let _=append_log(dir,"startup.log",&format!("webview user-data folder: {}",folder.display()));
 std::env::set_var("WEBVIEW2_USER_DATA_FOLDER",&folder);
}
/// A fatal startup fault lands in logs/startup-error.log and on screen, then exits with
/// code 3. A GUI build with panic=abort otherwise dies as 0xc0000409 with no message at
/// all, which is indistinguishable from a hang. BOB_PORTAL_NO_DIALOG=1 keeps an
/// unattended harness from leaving a modal window open.
fn startup_failed(dir:&Path,detail:&str){
 let stamp=std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d|d.as_secs()).unwrap_or(0);
 let message=format!("[{stamp}] {detail}");
 let log_path=log_dir(dir).join("startup-error.log");
 let logged=append_log(dir,"startup-error.log",&message);
 if dialog_timeout_ms()==0{let _=append_log(dir,"startup-error.log","startup dialog suppressed (BOB_PORTAL_DIALOG_TIMEOUT_MS=0)");}
 if std::env::var_os("BOB_PORTAL_NO_DIALOG").is_none(){
  use windows_sys::Win32::UI::WindowsAndMessaging::{MB_ICONERROR,MB_OK,MB_SETFOREGROUND,MB_TOPMOST};
  let note=if logged{format!("Details were written to {}",log_path.display())}else{format!("The log could NOT be written under {}",log_path.display())};
  bounded_dialog(&format!("BoB could not start.\r\n\r\n{detail}\r\n\r\n{note}\r\n\r\nIf BoB is already open in another window, close that window and try again."),"BoB - cannot start",MB_OK|MB_ICONERROR|MB_TOPMOST|MB_SETFOREGROUND,dialog_timeout_ms());
 }
 std::process::exit(3)
}
type ShutdownStep = (&'static str, Box<dyn FnOnce()+Send>);
/// No join can extend this budget, including poisoned/startup locks and child.wait().
fn bounded_shutdown(steps:Vec<ShutdownStep>,budget:Duration)->Vec<&'static str>{
 let mut pending:Vec<_>=steps.iter().map(|(name,_)|*name).collect();
 let(tx,rx)=mpsc::channel();let deadline=Instant::now()+budget;
 for(name,work)in steps{let tx=tx.clone();let _=std::thread::Builder::new().name(format!("close-{name}")).spawn(move||{work();let _=tx.send(name);});}
 drop(tx);
 while !pending.is_empty(){
  let Some(left)=deadline.checked_duration_since(Instant::now())else{break};
  match rx.recv_timeout(left){Ok(name)=>pending.retain(|p|*p!=name),Err(_)=>break}
 }
 pending
}
static CLOSING:std::sync::atomic::AtomicBool=std::sync::atomic::AtomicBool::new(false);
fn begin_shutdown(app:&tauri::AppHandle){
 if CLOSING.swap(true,Ordering::SeqCst){return}
 // Arm before touching service locks. Even log I/O or event-loop teardown must
 // not leave a windowless process holding the build-directory instance guard.
 std::thread::spawn(||{std::thread::sleep(Duration::from_secs(3));std::process::exit(0)});
 let proxy=app.state::<Arc<LazyProxy>>().inner().clone();
 let portal=app.state::<Arc<portal::PortalState>>().inner().clone();
 std::thread::spawn(move||{
  let pending=bounded_shutdown(vec![
   ("pc-queue-and-actions",Box::new(crate::pc::revoke_all)),
   ("model-sign-in-close",Box::new(runtime::close_model_login_on_exit)),
   ("wsl-host-close",Box::new(move||proxy.shutdown())),
   ("portal-broker-close",Box::new(move||portal.close())),
  ],Duration::from_secs(2));
  let dir=STARTUP_LOG_DIR.get().map(|p|p.as_path()).unwrap_or_else(||Path::new("."));
  let stamp=std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d|d.as_secs()).unwrap_or(0);
  let _=append_log(dir,"shutdown.log",&format!("[{stamp}] close cleanup finished; still running: {}; young WSL sessions detach; older sessions receive EOF then terminate; exiting",if pending.is_empty(){"none".into()}else{pending.join(", ")}));
  std::process::exit(0)
 });
}
pub fn run(){
 let exe=match std::env::current_exe(){Ok(path)=>path,Err(error)=>{startup_failed(Path::new("."),&format!("cannot resolve own executable path: {error}"));return}};
 let dir=match exe.parent(){Some(path)=>path.to_path_buf(),None=>{startup_failed(Path::new("."),&format!("executable has no parent directory: {}",exe.display()));return}};
 let dir=dir.as_path();
 match acquire_instance_guard(dir){
  Ok(handle)=>{let _=INSTANCE_GUARD.set(handle);}
  Err(reason)=>startup_refused(dir,&format!("BoB is already open from this build directory.\r\nClose that window, then start BoB again.\r\n\r\n({reason})")),
 }
 set_webview_folder(dir);
 tauri::Builder::default().manage(Arc::new(LazyProxy::default())).manage(ide::IdeState::default()).manage(Arc::new(portal::PortalState::default())).invoke_handler({
  let local_handler:fn(tauri::ipc::Invoke<tauri::Wry>)->bool=tauri::generate_handler![
   ide::ide_pick_folder,
   ide::ide_list_dir,
   ide::ide_read_file,
   ide::ide_write_file,
   ide::ide_git_status,
   ide::ide_git_head,
   ide::ide_term_open,
   ide::ide_term_write,
   ide::ide_term_resize,
   ide::ide_term_close,
   local_store::store_load,
   local_store::store_save,
   local_store::library_launch,
   local_store::local_auth_status,
   runtime::runtime_status,
   runtime::runtime_upgrade_status,
   runtime::runtime_upgrade_start,
   crate::self_repair::runtime_repair_status,
   crate::self_repair::runtime_repair_start,
   crate::self_repair::runtime_repair_restore,
   crate::pc::pc_poll,
   crate::pc::pc_decide,
   crate::pc::pc_set_mode,
   crate::pc::pc_revoke,
   runtime::runtime_install,
   runtime::runtime_add_lane,
                runtime::runtime_model_openai_models,
                runtime::runtime_model_openai_set_model,
                runtime::runtime_model_status,
                runtime::runtime_embedder_status,
                runtime::runtime_embedder_install,
                runtime::runtime_model_providers,
                runtime::runtime_model_openai_login,
                runtime::runtime_model_claude_login,
                runtime::runtime_model_claude_submit_code,
                runtime::runtime_model_claude_models,
                runtime::runtime_model_claude_set_model,
                runtime::runtime_model_claude_logout,
                runtime::runtime_model_cancel,
                runtime::runtime_model_set_key,
                runtime::runtime_model_set_default,
                runtime::runtime_model_open_url
  ];
  move |invoke:tauri::ipc::Invoke|{
  let command=invoke.message.command().to_owned();
  if command == "app_developer_mode" {
   if invoke.message.webview_ref().label() != "main" { invoke.resolver.reject("only the main webview may inspect developer mode"); }
   else { invoke.resolver.resolve(portal::developer_mode()); }
   return true
  }
  if LOCAL_COMMANDS.contains(&command.as_str()){return local_handler(invoke)}
  // Portal commands are served natively by portal.rs; they never reach the WSL host.
  if command.starts_with("portal_"){
   if invoke.message.webview_ref().label()!="main"{invoke.resolver.reject("only the candidate main webview may invoke portal commands");return true}
   let args=match invoke.message.payload(){tauri::ipc::InvokeBody::Json(v)=>v.clone(),_=>{invoke.resolver.reject("binary IPC unsupported");return true}};
   let(app,state)={let webview=invoke.message.webview();(webview.app_handle().clone(),webview.state::<Arc<portal::PortalState>>().inner().clone())};
   std::thread::spawn(move||{let reply=portal::dispatch(&state,&app,&command,args);match reply{Ok(v)=>invoke.resolver.resolve(v),Err(e)=>invoke.resolver.reject(e)}});
   return true
  }
  if !COMMANDS.contains(&command.as_str()){return false}
  if invoke.message.webview_ref().label()!="main"{invoke.resolver.reject("only the candidate main webview may invoke workbench commands");return true}
  let args=match invoke.message.payload(){tauri::ipc::InvokeBody::Json(v)=>v.clone(),_=>{invoke.resolver.reject("binary IPC unsupported");return true}};
  let state=invoke.message.webview().state::<Arc<LazyProxy>>().inner().clone();
  std::thread::spawn(move||{let reply=state.get().and_then(|p|p.call(&command,args));match reply{Ok(v)=>invoke.resolver.resolve(v),Err(e)=>invoke.resolver.reject(e)}});true
 }}).on_window_event(|window,event|{
  if window.label()!="main"{return}
  match event{
   tauri::WindowEvent::CloseRequested{api,..}=>{api.prevent_close();begin_shutdown(window.app_handle());let _=window.hide();},
   tauri::WindowEvent::Destroyed=>begin_shutdown(window.app_handle()),
   _=>{}
  }
 })
 .build(tauri::generate_context!()).unwrap_or_else(|error|{startup_failed(dir,&format!("Tauri startup failed: {error}"));unreachable!()})
 .run(|app,event|{if let tauri::RunEvent::ExitRequested{api,..}=event{api.prevent_exit();begin_shutdown(app)}});
}
#[cfg(test)]mod tests{
 use super::*;
 // The folder policy is unit-tested; the panic hook, the log write and exit(3) are
 // process-fatal and can only be verified by a real launch (see the r4 launch receipt).
 #[test]fn webview_profile_defaults_to_localappdata(){assert_eq!(webview_profile(Path::new(r"C:\build\r4"),Some(r"C:\Users\owner\AppData\Local".into()),None),Path::new(r"C:\Users\owner\AppData\Local").join(crate::APP_DATA_DIR).join("webview"))}
 #[test]fn webview_profile_falls_back_beside_the_build_without_localappdata(){assert_eq!(webview_profile(Path::new(r"C:\build\r4"),None,None),Path::new(r"C:\build\r4").join("webview-profile"))}
 #[test]fn webview_profile_honours_an_absolute_folder(){for root in [None,Some(r"C:\local".into())]{assert_eq!(webview_profile(Path::new(r"C:\build\r4"),root,Some(std::ffi::OsString::from(r"C:\private\wv"))),std::path::PathBuf::from(r"C:\private\wv"))}}
 #[test]fn webview_profile_ignores_empty_whitespace_and_relative_overrides(){for value in ["",r"   ",r"relative\wv",r".\wv"]{assert_eq!(webview_profile(Path::new(r"C:\build\r4"),Some(r"C:\local".into()),Some(std::ffi::OsString::from(value))),Path::new(r"C:\local").join(crate::APP_DATA_DIR).join("webview"),"override {value:?} must not relocate the profile")}}
 #[test]fn logs_use_localappdata_or_fall_back_beside_the_build(){let dir=Path::new(r"C:\build\r4");assert_eq!(log_dir_from(dir,Some(r"C:\local".into())),Path::new(r"C:\local").join(crate::APP_DATA_DIR).join("logs"));assert_eq!(log_dir_from(dir,None),dir.join("logs"))}
 #[test]fn au4_shutdown_finishes_without_spending_the_budget(){
  let start=Instant::now();assert!(bounded_shutdown(vec![("ready",Box::new(||{}))],Duration::from_secs(5)).is_empty());assert!(start.elapsed()<Duration::from_secs(1));
 }
 #[test]fn au4_shutdown_reports_blocked_work_and_never_joins_it(){
  let(tx,rx)=mpsc::channel::<()>();let start=Instant::now();
  let pending=bounded_shutdown(vec![("broker-child-wait",Box::new(move||{let _=rx.recv();})),("pc-cancel",Box::new(||{}))],Duration::from_millis(100));
  assert_eq!(pending,vec!["broker-child-wait"]);assert!(start.elapsed()<Duration::from_secs(1));drop(tx);
 }
 #[test]fn au4_wsl_sessions_obey_age_on_close_and_failure(){
  use windows_sys::Win32::System::Threading::*;
  for old in [false,true]{for close in [false,true]{
   let child=Command::new("C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe").args(["-NoProfile","-Command","Start-Sleep -Seconds 30"]).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().unwrap();
   let observer=unsafe{OpenProcess(PROCESS_TERMINATE|PROCESS_SYNCHRONIZE,false as i32,child.id())};assert!(!observer.is_null());
   let observer=unsafe{OwnedHandle::from_raw_handle(observer)};
   let started=Instant::now()-if old{SESSION_FLOOR}else{Duration::ZERO};
   let proxy=Proxy::supervise_child(child,Some(started)).unwrap();assert!(proxy._job.is_none());
   if close{proxy.shutdown()}else{let _=proxy.input.try_send(None);fail_transport(&proxy.pending,&proxy.child,proxy.wsl_started,"test failure");}
   let alive=unsafe{WaitForSingleObject(observer.as_raw_handle(),0)}==windows_sys::Win32::Foundation::WAIT_TIMEOUT;
   // Test cleanup only: the young surrogate deliberately ignores EOF.
   unsafe{TerminateProcess(observer.as_raw_handle(),0);WaitForSingleObject(observer.as_raw_handle(),2000);}
   assert_eq!(alive,!old,"old={old}, close={close}");assert!(proxy.child.lock().unwrap().is_none(),"all paths release the process handle");
  }}
 }
 #[test]fn lazy_proxy_does_not_start_at_launch_or_shutdown(){let proxy=LazyProxy::default();assert!(matches!(*proxy.state.lock().unwrap(),ProxyState::Unstarted));proxy.shutdown();assert!(proxy.get_or_start(||panic!("closed proxy must not start")).is_err())}
 #[test]fn lazy_proxy_caches_startup_failure(){let proxy=LazyProxy::default();assert_eq!(proxy.get_or_start(||Err("no WSL installed".into())).err().unwrap(),"no WSL installed");assert_eq!(proxy.get_or_start(||panic!("failed startup must not retry")).err().unwrap(),"no WSL installed");proxy.shutdown()}
 #[test]fn dead_reader_refuses_later_dispatch_without_waiting_for_model_deadline(){
  let child=Command::new("C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe").args(["-NoProfile","-Command","Write-Output malformed; Start-Sleep -Milliseconds 1500"]).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().unwrap();
  let proxy=Proxy::from_child(child).unwrap();std::thread::sleep(Duration::from_millis(1000));
  let other=proxy.clone();let(tx,rx)=mpsc::channel();std::thread::spawn(move||{let _=tx.send(other.call("app_boot",json!({})));});
  let result=rx.recv_timeout(Duration::from_millis(500));proxy.shutdown();
  assert!(matches!(result,Ok(Err(_))),"a broken protocol reader must refuse subsequent work immediately, never execute without a reader");
 }
 #[test]fn instance_guard_name_is_stable_and_case_insensitive(){assert_eq!(instance_guard_name(Path::new(r"C:\build\r8")),instance_guard_name(Path::new(r"c:\BUILD\R8")))}
 #[test]fn instance_guard_name_differs_per_build_directory(){assert_ne!(instance_guard_name(Path::new(r"C:\build\r8")),instance_guard_name(Path::new(r"C:\build\r7")));assert!(instance_guard_name(Path::new(r"C:\build\r8")).starts_with("Local\\BoB-portal-"))}
 #[test]fn a_second_copy_of_one_directory_is_refused_and_another_directory_is_not(){
  let dir=std::env::temp_dir().join(format!("bob-guard-{}",std::process::id()));
  let first=acquire_instance_guard(&dir);assert!(first.is_ok(),"the first copy must be allowed: {first:?}");
  let second=acquire_instance_guard(&dir);assert!(second.is_err(),"a second copy from the same directory must be refused");
  let other=acquire_instance_guard(&dir.join("other"));assert!(other.is_ok(),"a different build directory must not be blocked: {other:?}");
 }
 #[test]fn drive_path_is_argv_not_shell(){assert_eq!(wsl_path(Path::new(r"C:\Users\owner\package space\bob-wsl-host")).unwrap(),"/mnt/c/Users/owner/package space/bob-wsl-host")}
 #[test]fn unc_device_and_relative_paths_refused(){for path in [r"\\server\share\x",r"\\?\C:\x",r"C:x",r"relative\x"]{assert!(wsl_path(Path::new(path)).is_err(),"{path}")}}
 #[test]fn command_inventory_preserves_current_workflows(){let mut actual:Vec<_>=COMMANDS.iter().map(|s|s.to_string()).collect();let mut expected:Vec<String>=serde_json::from_str(include_str!("../../../host/commands.json")).unwrap();actual.sort();expected.sort();assert_eq!(actual,expected)}
 // R12: the startup dialogs carry a budget, so an unattended launch cannot park forever.
 #[test]fn dialog_budget_defaults_to_a_finite_bound(){assert_eq!(dialog_timeout_from(None),180_000);assert!(DIALOG_DEFAULT_MS<=DIALOG_MAX_MS)}
 #[test]fn dialog_budget_zero_skips_the_dialog(){assert_eq!(dialog_timeout_from(Some("0")),0);assert_eq!(dialog_timeout_from(Some(" 0 ")),0)}
 #[test]fn dialog_budget_honours_a_small_override_and_clamps_an_absurd_one(){assert_eq!(dialog_timeout_from(Some("250")),250);assert_eq!(dialog_timeout_from(Some("999999999")),DIALOG_MAX_MS);assert_eq!(dialog_timeout_from(Some("-5")),180_000);assert_eq!(dialog_timeout_from(Some("nonsense")),180_000)}

 // R12-1: boundedness is behavioural, not arithmetic - a wait that ignores the box's return
 // would keep the mutex for the whole budget. These two cases fail on a clock-only loop.
 #[test]fn a_dialog_that_returns_releases_the_wait_at_once(){let start=Instant::now();assert!(wait_bounded(||{},5_000),"a box that returns must release the waiter");assert!(start.elapsed()<Duration::from_millis(1_000))}
 #[test]fn a_dialog_that_never_returns_is_abandoned_at_the_budget(){let start=Instant::now();assert!(!wait_bounded(||std::thread::sleep(Duration::from_millis(1_500)),200),"an unclickable box must not hold the process");assert!(start.elapsed()<Duration::from_millis(1_200))}

}
