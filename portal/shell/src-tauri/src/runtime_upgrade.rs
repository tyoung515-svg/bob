//! Fixed-resource, root-mediated upgrades. No webview paths, digests, shell or force flag.
use serde_json::{json, Value};
use std::path::Path;
#[cfg(windows)]
use tauri::Manager;

fn digest(value: &Value) -> bool {
    value.as_str().is_some_and(|s| s.len() == 64 && s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)))
}
fn version(value: &Value, legacy: bool) -> Result<Value, String> {
    let id = value["build_id"].as_str().ok_or("missing runtime build ID")?;
    if id.is_empty() || id.len() > 128 || !id.as_bytes()[0].is_ascii_alphanumeric()
        || !id.bytes().all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b)) {
        return Err("invalid runtime build ID".into());
    }
    let epoch = value["build_epoch"].as_u64().ok_or("invalid runtime version order")?;
    let rev = value["git_rev"].as_str().ok_or("missing runtime revision")?;
    let is_legacy = legacy && id == "legacy" && epoch == 0 && rev == "unknown";
    if is_legacy && !value["payload_sha256"].is_null() { return Err("invalid legacy payload receipt".into()); }
    if !is_legacy
        && (!digest(&value["payload_sha256"]) || epoch == 0 || rev.len() != 40 || !rev.bytes().all(|b| b.is_ascii_hexdigit())) {
        return Err("invalid runtime version receipt".into());
    }
    Ok(json!({"build_id":id,"build_epoch":epoch,"git_rev":rev,"payload_sha256":value["payload_sha256"]}))
}
fn release(bytes: &[u8]) -> Result<Value, String> {
    if bytes.len() > 4096 { return Err("oversized bundled release metadata".into()); }
    let raw: Value = serde_json::from_slice(bytes).map_err(|_| "invalid bundled release metadata")?;
    let mut result = version(&raw, false)?;
    if !digest(&raw["archive_sha256"]) { return Err("invalid archive digest".into()); }
    result["archive_sha256"] = raw["archive_sha256"].clone();
    Ok(result)
}
const FAILURE_CODES: [&str; 4] = ["runtime_too_old", "runtime_unhealthy", "insufficient_space", "unsupported_layout"];
pub(super) const DAMAGED_INSTALL: &str = "The installed copy of BoB is damaged. Reinstall BoB and try again.";
pub(super) fn verify_install_archive(path: &Path, bundled: &Value) -> Result<(), String> {
    if !digest(&bundled["archive_sha256"]) { return Err(DAMAGED_INSTALL.into()); }
    hash_archive(path, bundled["archive_sha256"].as_str().unwrap()).map_err(|_| DAMAGED_INSTALL.into())
}
fn receipt(bytes: &[u8], bundled: Value) -> Result<Value, String> {
    if bytes.len() > 16384 { return Err("oversized runtime upgrade status".into()); }
    let raw: Value = serde_json::from_slice(bytes).map_err(|_| "invalid runtime upgrade status")?;
    let installed = version(&raw["installed"], true)?;
    let running = raw["running"].as_bool().ok_or("invalid runtime job state")?;
    let upgrade = if raw["upgrade"].is_null() { Value::Null } else {
        let row = &raw["upgrade"];
        let id = row["id"].as_str().ok_or("missing upgrade ID")?;
        if id.is_empty() || id.len() > 201 || !id.as_bytes()[0].is_ascii_alphanumeric() || !id.bytes().all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b)) {
            return Err("invalid upgrade ID".into());
        }
        let phase = row["phase"].as_str().ok_or("missing upgrade phase")?;
        let outcome = row["outcome"].as_str().ok_or("missing upgrade outcome")?;
        if !["preflight","backup","stop","swap","units","migrations","refresh-env","hooks","start","health","publish","done","rollback","rolled_back"].contains(&phase)
            || !["running","rolling_back","completed","rolled_back","rollback_failed","failed"].contains(&outcome) {
            return Err("invalid upgrade phase/outcome".into());
        }
        let mut entry = json!({"id":id,"phase":phase,"outcome":outcome});
        if let Some(code) = row["failure_code"].as_str().filter(|code| FAILURE_CODES.contains(code)) {
            entry["reason"] = json!(code);
        }
        entry
    };
    let available = bundled["build_epoch"].as_u64() > installed["build_epoch"].as_u64()
        && bundled["build_id"] != installed["build_id"];
    let mut result = json!({"installed":installed,"bundled":bundled,"available":available,"running":running,"upgrade":upgrade});
    if raw["blocked"] == "runtime_too_old" { result["blocked"] = json!("runtime_too_old"); }
    Ok(result)
}

// Works before C5 is installed. The root-private job survives closing the desktop.
const STATUS: &str = r#"import json,pathlib,subprocess
r=pathlib.Path('/var/lib/bob-upgrades'); v=pathlib.Path('/etc/bob/runtime-version')
s={'installed':json.loads(v.read_text()) if v.exists() else {'build_id':'legacy','build_epoch':0,'git_rev':'unknown','payload_sha256':None},'upgrade':None,'running':subprocess.run(['systemctl','is-active','--quiet','bob-runtime-upgrade.service']).returncode==0}
if not pathlib.Path('/etc/bob/execution-boundary').exists(): s['blocked']='runtime_too_old'
i=None
if (r/'latest').exists():
 i=(r/'latest').read_text().strip()
 if not i or not i[0].isalnum() or len(i)>201 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-' for c in i): raise ValueError('invalid journal')
 j=json.loads((r/i/'state.json').read_text()); s['upgrade']={k:j[k] for k in ('id','phase','outcome','failure_code') if k in j}
if (r/'incoming.json').exists():
 j=json.loads((r/'incoming.json').read_text())
 if j['previous']==i: s['upgrade']={k:j[k] for k in ('id','phase','outcome','failure_code') if k in j}
print(json.dumps(s))
"#;

// No automount/interop dependency: only stdin carries bytes across the Windows boundary.
const MAX_ARCHIVE: u64 = 4 * 1024 * 1024 * 1024;
const STAGING_PREFIX: &str = "/var/lib/bob-upgrades/staging-";
const STAGE: &str = r#"import hashlib,json,os,pathlib,shutil,sys,uuid
os.umask(0o077)
r=pathlib.Path('/var/lib/bob-upgrades'); p=None; complete=False
try:
 digest=sys.argv[1]
 if len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest): raise ValueError('invalid archive digest')
 r.mkdir(mode=0o700,exist_ok=True)
 root=os.open(r,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
 try:
  info=os.fstat(root)
  if info.st_uid or info.st_mode&0o077: raise ValueError('upgrade directory must be private')
  name='staging-'+uuid.uuid4().hex
  os.mkdir(name,mode=0o700,dir_fd=root); p=r/name
  directory=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root)
  try:
   fd=os.open('release.tar.gz',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
   with os.fdopen(fd,'wb') as f:
    total=0; h=hashlib.sha256()
    while True:
     chunk=sys.stdin.buffer.read(1024*1024)
     if not chunk: break
     total+=len(chunk)
     if total>4*1024*1024*1024: raise ValueError('runtime archive exceeds size limit')
     f.write(chunk); h.update(chunk)
    if total==0 or h.hexdigest()!=digest: raise ValueError('archive digest mismatch')
    f.flush(); os.fsync(f.fileno())
   os.fsync(directory); os.fsync(root)
  finally: os.close(directory)
 finally: os.close(root)
 print(json.dumps({'source':str(p/'release.tar.gz'),'sha256':digest,'size':total}),flush=True)
 complete=True
except Exception as error:
 message=str(error) if isinstance(error,ValueError) else 'runtime archive staging failed'
 print(json.dumps({'error':message}),flush=True)
 raise SystemExit(1)
finally:
 if p is not None and not complete: shutil.rmtree(p)
"#;

const BOOTSTRAP: &str = r#"import hashlib,json,os,pathlib,shutil,stat,sys,tarfile,tempfile
os.umask(0o077)
source,digest=sys.argv[1:]
r=pathlib.Path('/var/lib/bob-upgrades')
if r.is_symlink(): raise ValueError('unsafe upgrade directory')
r.mkdir(mode=0o700,exist_ok=True)
if r.stat().st_uid or r.stat().st_mode&0o077: raise ValueError('upgrade directory must be private')
a=pathlib.Path(source); stage=a.parent; p=None; j=None
if a.name!='release.tar.gz' or stage.parent!=r or not stage.name.startswith('staging-') or len(stage.name)!=40 or any(c not in '0123456789abcdef' for c in stage.name[8:]): raise ValueError('invalid staged source')
info=stage.lstat()
if not stat.S_ISDIR(info.st_mode) or info.st_uid or info.st_mode&0o077: raise ValueError('staging directory must be private')
try:
 p=pathlib.Path(tempfile.mkdtemp(prefix='incoming-',dir=r))
 j={'id':p.name,'phase':'preflight','outcome':'running','previous':(r/'latest').read_text().strip() if (r/'latest').exists() else None}
 def save():
  q=r/'.incoming.json.new'; q.write_text(json.dumps(j)); os.replace(q,r/'incoming.json')
 save()
 with os.fdopen(os.open(a,os.O_RDONLY|os.O_NOFOLLOW),'rb') as f:
  info=os.fstat(f.fileno())
  if not stat.S_ISREG(info.st_mode) or info.st_uid or info.st_mode&0o077 or info.st_nlink!=1: raise ValueError('unsafe staged archive')
  if not 0<info.st_size<=4*1024*1024*1024: raise ValueError('runtime archive exceeds size limit')
  if info.st_size*4>shutil.disk_usage(r).free: raise ValueError('insufficient staging space')
  if hashlib.file_digest(f,'sha256').hexdigest()!=digest: raise ValueError('archive digest mismatch')
  f.seek(0)
  with tarfile.open(fileobj=f) as t:
   for name in ('runtime_upgrade','upgrade_payload'):
    key='usr/local/lib/bob_runtime/'+name+'.py'; matches=[m for m in t if m.name.removeprefix('./')==key]
    if len(matches)!=1 or not matches[0].isfile() or matches[0].size>1048576: raise ValueError('invalid upgrade bootstrap')
    (p/(name+'.py')).write_bytes(t.extractfile(matches[0]).read())
 sys.path[:0]=[str(p),'/usr/local/lib/bob_runtime','/opt/bob/bobclaw-core']
 from runtime_upgrade import main
 sys.argv=['bob-runtime','--payload',str(a),'--expected-sha256',digest]
 code=main()
 (r/'incoming.json').unlink(missing_ok=True)
 raise SystemExit(code)
except Exception:
 if j is not None: j['outcome']='failed'; save()
 raise
finally:
 try:
  if p is not None: shutil.rmtree(p)
 finally: shutil.rmtree(stage)
"#;

// A rejected launch has no BOOTSTRAP to clean up. On an ambiguous timeout, leave
// an active/activating job's source alone; its finally block owns that directory.
const CLEAN_STAGE: &str = r#"import pathlib,shutil,stat,subprocess,sys
r=pathlib.Path('/var/lib/bob-upgrades'); a=pathlib.Path(sys.argv[1]); p=a.parent
if a.name!='release.tar.gz' or p.parent!=r or not p.name.startswith('staging-') or len(p.name)!=40 or any(c not in '0123456789abcdef' for c in p.name[8:]): raise ValueError('invalid staged source')
if r.is_symlink() or r.stat().st_uid or r.stat().st_mode&0o077: raise ValueError('unsafe upgrade directory')
state=subprocess.run(['systemctl','show','--property=ActiveState','--value','bob-runtime-upgrade.service'],capture_output=True,text=True)
if state.returncode or state.stdout.strip() not in ('inactive','failed'): raise SystemExit(0)
if not p.exists(): raise SystemExit(0)
info=p.lstat()
if not stat.S_ISDIR(info.st_mode) or info.st_uid or info.st_mode&0o077: raise ValueError('unsafe staging directory')
shutil.rmtree(p)
"#;

fn staged_source(source: &str) -> bool {
    source.strip_prefix(STAGING_PREFIX)
        .and_then(|tail| tail.strip_suffix("/release.tar.gz"))
        .is_some_and(|id| id.len() == 32 && id.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)))
}
fn stage_args(hash: &str) -> Result<Vec<String>, String> {
    if !digest(&json!(hash)) { return Err("invalid archive digest".into()); }
    Ok(["-d",crate::self_repair::distro(),"-u","root","--exec","/opt/bob/.venv/bin/python","-I","-c",STAGE,hash]
        .map(String::from).to_vec())
}
fn cleanup_args(source: &str) -> Result<Vec<String>, String> {
    if !staged_source(source) { return Err("invalid staged runtime path".into()); }
    Ok(["-d",crate::self_repair::distro(),"-u","root","--exec","/opt/bob/.venv/bin/python","-I","-c",CLEAN_STAGE,source]
        .map(String::from).to_vec())
}
fn start_args(source: &str, hash: &str) -> Result<Vec<String>, String> {
    if !staged_source(source) || !digest(&json!(hash)) { return Err("invalid bundled upgrade source".into()); }
    Ok(["-d",crate::self_repair::distro(),"-u","root","--exec","systemd-run","--unit=bob-runtime-upgrade",
        "--collect","--property=UMask=0077","--property=TimeoutStartSec=infinity",
        "/opt/bob/.venv/bin/python","-I","-c",BOOTSTRAP,source,hash].map(String::from).to_vec())
}
pub(super) fn hash_archive(path: &Path, expected: &str) -> Result<(), String> {
    use sha2::{Digest, Sha256};
    use std::io::Read;
    let mut file = std::fs::File::open(path).map_err(|_| "bundled runtime archive unavailable")?;
    let size = file.metadata().map_err(|_| "bundled runtime archive unavailable")?.len();
    if size == 0 || size > MAX_ARCHIVE { return Err("runtime archive exceeds size limit".into()); }
    let mut buffer = [0u8; 1024 * 1024];
    let mut hash = Sha256::new();
    let mut total = 0u64;
    loop {
        let count = file.read(&mut buffer).map_err(|_| "cannot read bundled runtime")?;
        if count == 0 { break; }
        total += count as u64;
        if total > MAX_ARCHIVE { return Err("runtime archive exceeds size limit".into()); }
        hash.update(&buffer[..count]);
    }
    if format!("{:x}", hash.finalize()) != expected { return Err("bundled runtime archive digest mismatch".into()); }
    Ok(())
}

fn stage_receipt(ok: bool, bytes: &[u8], hash: &str, size: u64) -> Result<String, String> {
    if bytes.len() > 4096 { return Err("oversized runtime staging receipt".into()); }
    let row: Value = serde_json::from_slice(bytes).map_err(|_| "invalid runtime staging receipt")?;
    if !ok {
        return Err(match row["error"].as_str() {
            Some("archive digest mismatch") => "staged runtime archive digest mismatch",
            Some("runtime archive exceeds size limit") => "runtime archive exceeds size limit",
            _ => "runtime archive staging failed",
        }.into());
    }
    let source = row["source"].as_str().filter(|s| staged_source(s)).ok_or("invalid staged runtime path")?;
    if row["sha256"].as_str() != Some(hash) || row["size"].as_u64() != Some(size) {
        return Err("staged runtime archive receipt mismatch".into());
    }
    Ok(source.into())
}

// A separate writer keeps pipe backpressure inside the same deadline as the child.
// Both input chunks and the JSON response are bounded, independent of archive size.
fn stream_archive(mut command: std::process::Command, archive: &Path, hash: &str, budget: std::time::Duration) -> Result<String, String> {
    use std::{io::{Read, Write}, process::Stdio, sync::mpsc, time::Instant};
    let mut file = std::fs::File::open(archive).map_err(|_| "bundled runtime archive unavailable")?;
    let size = file.metadata().map_err(|_| "bundled runtime archive unavailable")?.len();
    if size == 0 || size > MAX_ARCHIVE { return Err("runtime archive exceeds size limit".into()); }
    let mut child = command.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null())
        .spawn().map_err(|_| "runtime staging process could not start")?;
    let deadline = Instant::now() + budget;
    let mut input = child.stdin.take().ok_or("runtime staging stdin unavailable")?;
    let mut output = child.stdout.take().ok_or("runtime staging stdout unavailable")?;
    let (write_tx, write_rx) = mpsc::channel();
    std::thread::spawn(move || {
        let result = (|| -> std::io::Result<()> {
            let mut buffer = [0u8; 64 * 1024];
            let mut total = 0u64;
            loop {
                let count = file.read(&mut buffer)?;
                if count == 0 { return Ok(()); }
                total += count as u64;
                if total > MAX_ARCHIVE { return Err(std::io::Error::other("archive size limit")); }
                input.write_all(&buffer[..count])?;
            }
        })();
        drop(input); // EOF is required before STAGE hashes/returns its receipt.
        let _ = write_tx.send(result);
    });
    let (read_tx, read_rx) = mpsc::channel();
    std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let result = output.by_ref().take(4097).read_to_end(&mut bytes);
        let _ = read_tx.send(result.map(|_| bytes));
    });
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                let remaining = || deadline.saturating_duration_since(Instant::now());
                let bytes = read_rx.recv_timeout(remaining()).map_err(|_| "runtime staging output timed out")?
                    .map_err(|_| "cannot read runtime staging receipt")?;
                let written = write_rx.recv_timeout(remaining()).map_err(|_| "runtime archive copy timed out")?;
                // Prefer the distro's digest/size refusal over a secondary broken pipe.
                let source = stage_receipt(status.success(), &bytes, hash, size)?;
                written.map_err(|_| "cannot copy bundled runtime archive")?;
                return Ok(source);
            }
            Ok(None) if Instant::now() < deadline => std::thread::sleep(std::time::Duration::from_millis(25)),
            _ => {
                let _ = child.kill(); let _ = child.wait();
                return Err("runtime archive copy failed or timed out; refresh upgrade status".into());
            }
        }
    }
}

#[cfg(windows)]
pub(super) fn bundle(app: &tauri::AppHandle) -> Result<(std::path::PathBuf, Value), String> {
    let resolve = |name| app.path().resolve(name, tauri::path::BaseDirectory::Resource)
        .map_err(|_| "runtime resource path unavailable".to_string());
    let path = resolve("runtime/bob-runtime.tar.gz")?;
    let meta = resolve("runtime/bob-runtime.release.json")?;
    if std::fs::metadata(&meta).map_err(|_| "bundled runtime release metadata missing")?.len() > 4096 {
        return Err("oversized release metadata".into());
    }
    let data = std::fs::read(meta).map_err(|_| "cannot read runtime metadata")?;
    Ok((path, release(&data)?))
}
#[cfg(windows)]
fn checked(args: &[String], budget: std::time::Duration) -> Result<Vec<u8>, String> {
    let (ok, bytes, _) = super::platform::wsl(args, None, budget)?;
    if !ok { return Err("runtime upgrade command failed; refresh upgrade status".into()); }
    Ok(bytes)
}
#[cfg(windows)]
pub(super) fn status(app: &tauri::AppHandle) -> Result<Value, String> {
    super::model_lane()?; // Only a configured This PC connection can control its distro.
    let (_, bundled) = bundle(app)?;
    let bytes = checked(&super::exec_args(Some("root"), &["/opt/bob/.venv/bin/python","-I","-c",STATUS]), super::SHORT)?;
    receipt(&bytes, bundled)
}
#[cfg(windows)]
pub(super) fn start(app: &tauri::AppHandle) -> Result<Value, String> {
    let _guard = super::PROVISIONING.try_lock().map_err(|_| "runtime provisioning already in progress")?;
    let before = status(app)?;
    if before["running"] == true { return Ok(before); }
    if before["blocked"] == "runtime_too_old" {
        return Err("This runtime is too old to update in place. Nothing was changed. Back up your data, remove the old runtime and set up BoB again.".into());
    }
    let pending = matches!(before["upgrade"]["outcome"].as_str(), Some("running" | "rolling_back" | "rollback_failed"));
    if before["available"] != true && !pending { return Err("no newer bundled runtime available".into()); }
    let (archive, bundled) = bundle(app)?;
    let hash = bundled["archive_sha256"].as_str().ok_or("missing archive digest")?;
    hash_archive(&archive, hash)?;
    // Keep the last pre-upgrade export when resuming a partial upgrade.
    if !pending { crate::self_repair::pre_upgrade_backup()?; }
    use std::os::windows::process::CommandExt;
    let program = std::path::PathBuf::from(std::env::var_os("SystemRoot").ok_or("SystemRoot unavailable")?)
        .join("System32/wsl.exe");
    let mut command = std::process::Command::new(program);
    command.args(stage_args(hash)?).creation_flags(0x0800_0000);
    let source = stream_archive(command, &archive, hash, std::time::Duration::from_secs(900))?;
    if let Err(error) = checked(&start_args(&source, hash)?, super::SHORT) {
        let _ = checked(&cleanup_args(&source)?, super::SHORT);
        return Err(error);
    }
    status(app)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn bundled() -> Value { json!({"build_id":"c5","build_epoch":2,"git_rev":"a".repeat(40),"payload_sha256":"b".repeat(64),"archive_sha256":"c".repeat(64)}) }
    #[test]
    fn runtime_upgrade_metadata_and_receipts_are_strict_and_strip_extra_fields() {
        let b = bundled();
        assert!(release(&serde_json::to_vec(&b).unwrap()).is_ok());
        for field in ["build_id","build_epoch","git_rev","payload_sha256","archive_sha256"] {
            let mut bad=b.clone(); bad[field]=json!(";bad\n");
            assert!(release(&serde_json::to_vec(&bad).unwrap()).is_err(),"{field}");
        }
        let raw=json!({"installed":{"build_id":"legacy","build_epoch":0,"git_rev":"unknown","payload_sha256":null},"running":false,"upgrade":null,"secret":"hidden"});
        let result=receipt(&serde_json::to_vec(&raw).unwrap(),b.clone()).unwrap();
        assert_eq!(result["available"],true);
        assert!(!result.to_string().contains("hidden"));
        let mut same=raw.clone(); same["installed"]=b.clone();
        assert_eq!(receipt(&serde_json::to_vec(&same).unwrap(),b.clone()).unwrap()["available"],false);
        same["installed"]["build_epoch"]=json!(3);
        assert_eq!(receipt(&serde_json::to_vec(&same).unwrap(),b.clone()).unwrap()["available"],false);
        let mut bad=raw; bad["upgrade"]=json!({"id":"../escape","phase":"swap","outcome":"running"});
        assert!(receipt(&serde_json::to_vec(&bad).unwrap(),b).is_err());
    }
    #[test]
    fn first_install_refuses_missing_or_corrupt_archive_digest() {
        let path = std::env::temp_dir().join(format!("bob-install-hash-{}", uuid::Uuid::new_v4()));
        std::fs::write(&path, b"abc").unwrap();
        let mut meta = bundled();
        meta["archive_sha256"] = json!("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
        assert!(verify_install_archive(&path, &meta).is_ok());
        std::fs::write(&path, b"damaged").unwrap();
        assert_eq!(verify_install_archive(&path, &meta).unwrap_err(), DAMAGED_INSTALL);
        for value in [Value::Null, json!(""), json!("invalid")] {
            meta["archive_sha256"] = value;
            assert_eq!(verify_install_archive(&path, &meta).unwrap_err(), DAMAGED_INSTALL);
        }
        std::fs::remove_file(&path).unwrap();
        assert_eq!(verify_install_archive(&path, &meta).unwrap_err(), DAMAGED_INSTALL);
        let install = include_str!("runtime.rs").split("pub fn install(").nth(1).unwrap().split("pub fn add_lane(").next().unwrap();
        assert!(install.find("upgrade::verify_install_archive").unwrap() < install.find("import_args(").unwrap());
        assert!(install.contains("Duration::from_secs(180)"));
        assert!(install.contains("progress(\"verify\""));
    }
    #[test]
    fn upgrade_block_and_failure_reason_are_closed_codes_only() {
        let mut raw = json!({"installed":{"build_id":"legacy","build_epoch":0,"git_rev":"unknown","payload_sha256":null},"running":false,"upgrade":{"id":"job-1","phase":"rolled_back","outcome":"rolled_back"}});
        assert!(receipt(&serde_json::to_vec(&raw).unwrap(), bundled()).unwrap().get("blocked").is_none());
        raw["blocked"] = json!("runtime_too_old");
        assert_eq!(receipt(&serde_json::to_vec(&raw).unwrap(), bundled()).unwrap()["blocked"], "runtime_too_old");
        for code in FAILURE_CODES {
            raw["upgrade"]["failure_code"] = json!(code);
            let result = receipt(&serde_json::to_vec(&raw).unwrap(), bundled()).unwrap();
            assert_eq!(result["upgrade"]["reason"], code);
            assert!(result["upgrade"].get("failure_code").is_none());
        }
        for unknown in [json!("unknown-private-detail"), Value::Null, json!(123), json!({"secret":"private"})] {
            raw["upgrade"]["failure_code"] = unknown;
            raw["blocked"] = json!("unknown-private-detail");
            let result = receipt(&serde_json::to_vec(&raw).unwrap(), bundled()).unwrap();
            assert!(result.get("blocked").is_none());
            assert!(result["upgrade"].get("reason").is_none());
            assert!(!result.to_string().contains("private"));
        }
        assert!(STATUS.contains("if not pathlib.Path('/etc/bob/execution-boundary').exists(): s['blocked']='runtime_too_old'"));
        assert_eq!(STATUS.matches("('id','phase','outcome','failure_code') if k in j").count(), 2);
    }
    #[test]
    fn runtime_upgrade_fixed_argv_has_no_shell_or_frontend_control() {
        let source=format!("{STAGING_PREFIX}{}/release.tar.gz", "b".repeat(32));
        let args=start_args(&source,&"a".repeat(64)).unwrap();
        assert_eq!(&args[..6],["-d","BoB","-u","root","--exec","systemd-run"]);
        assert_eq!(args[args.len()-2],source);
        assert!(!args.iter().any(|s| s=="sh" || s=="--force"));
        for source in ["/var/lib/bob/secret","../bad","/mnt/c/bad\nfile", "/mnt/c/Program Files/BoB/runtime.tar.gz"] { assert!(start_args(source,&"a".repeat(64)).is_err()); }
        assert!(start_args(&source,"bad").is_err());
    }
    #[test]
    fn runtime_upgrade_archive_hash_refuses_corruption() {
        let path=std::env::temp_dir().join(format!("bob-upgrade-hash-{}",uuid::Uuid::new_v4()));
        std::fs::write(&path,b"abc").unwrap();
        assert!(hash_archive(&path,"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad").is_ok());
        assert!(hash_archive(&path,&"0".repeat(64)).is_err());
        std::fs::remove_file(path).unwrap();
    }
}

#[cfg(test)]
#[path = "runtime_upgrade_staging_tests.rs"]
mod staging_tests;
