//! Trusted recovery state. No factors cross the webview IPC boundary.
//! Only safe receipt metadata is persisted; credential frames are sent once.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{fs::{self, OpenOptions}, io::{Read, Write}, path::Path, time::{SystemTime, UNIX_EPOCH}};

#[cfg(windows)]
#[path = "recovery_windows.rs"]
mod platform;
#[cfg(not(windows))]
mod platform {
    use super::*;
    pub fn protect() -> Result<(), String> { Err("native_recovery_unavailable".into()) }
    pub fn entry(_: &str, _: bool) -> Result<String, String> { Err("native_recovery_unavailable".into()) }
    pub fn display_seed(_: &str) -> Result<(), String> { Err("native_recovery_unavailable".into()) }
    pub fn read_stick(_: &Binding, _: bool) -> Result<Stick, String> { Err("native_recovery_unavailable".into()) }
    pub fn save(_: &Stick, _: bool) -> Result<(), String> { Err("native_recovery_unavailable".into()) }
}
const LIMIT: u64 = 4096;
fn fail() -> String { "invalid_recovery_material".into() }
fn now() -> u64 { SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs() }
fn ident(s: &str) -> bool { !s.is_empty() && s.len() <= 128 && s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-') }
fn hex(s: &str, n: usize) -> bool { s.len() == n && s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) }
#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Binding { pub installation_id: String, pub lane_id: String, pub owner: String }
impl Binding {
    fn validate(&self) -> Result<(), String> {
        if ident(&self.installation_id) && hex(&self.lane_id, 12) && ident(&self.owner) { Ok(()) } else { Err(fail()) }
    }
}
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Stick {
    pub format: String, pub version: u8, pub installation_id: String, pub lane_id: String,
    pub owner: String, pub recovery_id: String, pub issued_at: String, pub secret: String, pub checksum: String,
}
fn wipe(s: &mut String) {
    // Volatile writes prevent dead-store elimination. Never Debug/Display secrets.
    unsafe { for b in s.as_bytes_mut() { std::ptr::write_volatile(b, 0); } }
    s.clear();
}
impl Drop for Stick { fn drop(&mut self) { wipe(&mut self.secret); } }
fn base32(bytes: &[u8]) -> String {
    const ABC: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
    let mut out = String::new(); let mut bits = 0; let mut acc = 0u32;
    for &b in bytes { acc = (acc << 8) | b as u32; bits += 8;
        while bits >= 5 { bits -= 5; out.push(ABC[((acc >> bits) & 31) as usize] as char); }
    }
    if bits > 0 { out.push(ABC[((acc << (5-bits)) & 31) as usize] as char); } out
}
fn canonical_secret(s: &str) -> bool {
    // 256 bits -> 52 symbols; only one bit in the final symbol is significant.
    s.len() == 52 && s.bytes().all(|b| b.is_ascii_uppercase() || (b'2'..=b'7').contains(&b))
        && matches!(s.as_bytes()[51], b'A' | b'Q')
}
fn hash(parts: &[&str]) -> String {
    // All bound identifiers and codes are ASCII; serde_json performs exact JSON
    // ASCII escaping (quotes/backslashes/control chars included in test vector).
    let bytes = serde_json::to_vec(parts).expect("string array serialization");
    format!("{:x}", Sha256::digest(bytes))
}
impl Stick {
    pub fn verifier(&self) -> String { hash(&["bob-recovery-v1", &self.installation_id, &self.lane_id, &self.owner, &self.recovery_id, &self.secret]) }
    fn checksum_value(&self) -> String { hash(&["bob-stick-checksum-v1", &self.installation_id, &self.lane_id, &self.owner, &self.recovery_id, &self.issued_at, &self.secret])[..16].into() }
    fn validate(&self, b: &Binding) -> Result<(), String> {
        b.validate()?;
        if self.issued_at.len()!=20 || !self.issued_at.is_ascii() || self.issued_at.starts_with("0000") || &self.issued_at[17..19]>"59" {return Err(fail());}
        let date = chrono::NaiveDateTime::parse_from_str(&self.issued_at, "%Y-%m-%dT%H:%M:%SZ").map_err(|_| fail())?;
        if self.format != "bob-recovery-stick" || self.version != 1 || !ident(&self.recovery_id)
            || self.installation_id != b.installation_id || self.lane_id != b.lane_id || self.owner != b.owner
            || date.format("%Y-%m-%dT%H:%M:%SZ").to_string() != self.issued_at || !canonical_secret(&self.secret)
            || !hex(&self.checksum, 16) || self.checksum != self.checksum_value() { return Err(fail()); }
        Ok(())
    }
    fn reduced(&self) -> Value { json!({"installation_id":self.installation_id,"lane_id":self.lane_id,"owner":self.owner,"recovery_id":self.recovery_id,"secret":self.secret}) }
}
pub fn parse(bytes: &[u8], b: &Binding) -> Result<Stick, String> {
    if bytes.len() > LIMIT as usize { return Err(fail()); }
    // Derived struct rejects duplicate keys, unknown keys, missing keys and wrong
    // types. serde_json rejects invalid UTF-8 and trailing non-whitespace bytes.
    let s: Stick = serde_json::from_slice(bytes).map_err(|_| fail())?; s.validate(b)?; Ok(s)
}
fn generate(b: &Binding) -> Result<Stick, String> {
    b.validate()?;
    let mut random = vec![0u8; 32]; getrandom::fill(&mut random).map_err(|_| "native_random_unavailable")?;
    let secret = base32(&random); for x in &mut random { unsafe { std::ptr::write_volatile(x, 0) }; }
    let mut s = Stick { format:"bob-recovery-stick".into(), version:1, installation_id:b.installation_id.clone(), lane_id:b.lane_id.clone(), owner:b.owner.clone(),
        recovery_id:uuid::Uuid::new_v4().to_string(), issued_at:chrono::Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string(), secret, checksum:String::new() };
    s.checksum = s.checksum_value(); Ok(s)
}
fn new_seed() -> Result<String, String> {
    let mut bytes = vec![0u8; 20]; getrandom::fill(&mut bytes).map_err(|_| "native_random_unavailable")?;
    let seed = base32(&bytes); for b in &mut bytes { unsafe { std::ptr::write_volatile(b, 0) }; } Ok(seed)
}


/// Exclusive same-volume staging, durable flush, no-replace rename, full reopen
/// comparison and strict parse. Production always supplies a pinned volume guard.
fn verified_write(dir: &Path, stick: &Stick, mut revalidate: impl FnMut()->Result<(),String>) -> Result<(),String> {
    revalidate()?;
    let name=format!("bob-recovery-{}-PENDING.json",stick.recovery_id);
    let destination=dir.join(name);let temp=dir.join(format!(".bob-pending-{}",uuid::Uuid::new_v4()));
    if fs::symlink_metadata(&destination).is_ok() {return Err("recovery_destination_exists".into());}
    let mut bytes=serde_json::to_vec(stick).map_err(|_|fail())?;
    let result=(|| {
        #[cfg(windows)] let mut f=platform::secure_create(&temp)?;
        #[cfg(not(windows))] let mut f={use std::os::unix::fs::OpenOptionsExt;OpenOptions::new().write(true).create_new(true).mode(0o600).open(&temp).map_err(|_|"recovery_write_failed")?};
        revalidate()?;
        f.write_all(&bytes).and_then(|_|f.sync_all()).map_err(|_|"recovery_write_failed")?;drop(f);revalidate()?;
        // Windows rename fails if destination exists. Linux uses a hard link for
        // atomic no-replace publication rather than overwriting an unexpected file.
        #[cfg(windows)] platform::publish(&temp,&destination)?;
        #[cfg(not(windows))] {fs::hard_link(&temp,&destination).map_err(|_|"recovery_write_failed")?;fs::remove_file(&temp).map_err(|_|"recovery_write_failed")?;}
        revalidate()?;
        let mut read=bounded_read(&destination)?;let equal=read==bytes;
        let binding=Binding{installation_id:stick.installation_id.clone(),lane_id:stick.lane_id.clone(),owner:stick.owner.clone()};
        let valid=parse(&read,&binding).map(|s|s.verifier()==stick.verifier()).unwrap_or(false);
        for b in &mut read {unsafe{std::ptr::write_volatile(b,0)}}
        if !equal||!valid{return Err("recovery_readback_failed".into());}revalidate()?;Ok(())
    })();
    for b in &mut bytes{unsafe{std::ptr::write_volatile(b,0)}}
    if result.is_err(){let _=fs::remove_file(&temp);}result
}
fn bounded_read(path:&Path)->Result<Vec<u8>,String>{
    let m=fs::symlink_metadata(path).map_err(|_|fail())?;
    if !m.is_file()||m.file_type().is_symlink()||m.len()>LIMIT{return Err(fail());}
    #[cfg(windows)]{use std::os::windows::fs::MetadataExt;if m.file_attributes() & (0x400|0x1000|0x400000|0x40000)!=0{return Err(fail());}}
    let mut opts=OpenOptions::new();opts.read(true);
    #[cfg(windows)]{use std::os::windows::fs::OpenOptionsExt;opts.share_mode(1).custom_flags(0x00200000);}
    #[cfg(unix)]{use std::os::unix::fs::OpenOptionsExt;opts.custom_flags(libc::O_NOFOLLOW|libc::O_NONBLOCK);}
    let mut file=opts.open(path).map_err(|_|fail())?;
    #[cfg(windows)] {use std::os::windows::io::AsRawHandle;use windows_sys::Win32::Storage::FileSystem::{GetFileType,FILE_TYPE_DISK};if unsafe{GetFileType(file.as_raw_handle())}!=FILE_TYPE_DISK{return Err(fail());}}
    let actual=file.metadata().map_err(|_|fail())?;
    if !actual.is_file()||actual.len()>LIMIT{return Err(fail());}
    #[cfg(windows)]{use std::os::windows::fs::MetadataExt;if actual.file_attributes() & 0x400!=0{return Err(fail());}}
    let mut bytes=Vec::new();Read::by_ref(&mut file).take(LIMIT+1).read_to_end(&mut bytes).map_err(|_|fail())?;
    if bytes.len()>LIMIT as usize{return Err(fail());}Ok(bytes)
}

// The caller supplies no credential data, path, binding, challenge or request key.
// One handle is bound to one selected connection and one server challenge.
#[derive(Default)]
pub struct Recovery { pending: Option<Pending> }
struct Pending {
    handle: String, pair: String, stick: Option<Stick>, seed: String,
    challenge: Option<String>, expires: u64, saved: bool, phase: String,
    operation: String, request_key: String, outcome: Option<Value>,
}
impl Drop for Pending { fn drop(&mut self) { wipe(&mut self.seed); } }
fn safe_error(_: impl std::fmt::Display) -> String { "recovery_outcome_unknown".into() }
fn prepared(v: &Value) -> Result<(String,u64), String> {
    let id = v["challenge_id"].as_str().filter(|s| ident(s)).ok_or_else(fail)?;
    let expires = v["expires_at"].as_u64().ok_or_else(fail)?;
    if v["state"] != "proof_verified" { return Err(fail()); } Ok((id.into(),expires))
}
fn outcome(v: &Value) -> Result<Value, String> {
    if !v["changed"].is_boolean() || !v["restarted"].is_boolean() || (v["restarted"] == true && v["changed"] != true) { return Err(fail()); }
    Ok(json!({"changed":v["changed"],"restarted":v["restarted"]}))
}
fn totp_entry(label:&str)->Result<String,String>{
    let mut code=platform::entry(label,true)?;
    if code.len()!=6||!code.bytes().all(|b|b.is_ascii_digit()){wipe(&mut code);return Err("six_digit_code_required".into());}Ok(code)
}
fn definitely_refused(error: &str) -> bool {
    let v:Value=serde_json::from_str(error).unwrap_or(Value::Null);
    matches!(v["code"].as_str(),Some("invalid_args"|"invalid_factors"|"authentication_throttled"|"owner_portal_required"|"owner_context_invalid"|"client_upgrade_required"|"operation_unavailable"|"not_dispatched"|"bad_request"|"recovery_conflict"))
}
pub(crate) fn scrub(v: &mut Value) { match v { Value::String(s) => wipe(s), Value::Array(a) => a.iter_mut().for_each(scrub), Value::Object(m) => m.values_mut().for_each(scrub), _=>() } }
impl Pending {
    fn safe(&self) -> Value { json!({"handle":self.handle,"pair":self.pair,"phase":self.phase,"expires_at":self.expires,"saved":self.saved,"material_available":self.stick.is_some(),"outcome":self.outcome}) }
    fn journal(&self, path: &Path, replace: bool) -> Result<(), String> {
        // A closed metadata document: never secret, verifier, factor input, body
        // hash, seed, pending plaintext path or challenge payload.
        let data = json!({"operation":self.operation,"request_key":self.request_key,"status":"unknown"}).to_string();
        let mut opts = OpenOptions::new(); opts.write(true).create_new(true);
        #[cfg(unix)] { use std::os::unix::fs::OpenOptionsExt; opts.mode(0o600); }
        let target=if replace {path.with_extension(format!("{}-pending",uuid::Uuid::new_v4()))}else{path.to_path_buf()};
        let mut f = opts.open(&target).map_err(|_| "recovery_pending_receipt")?;
        if f.write_all(data.as_bytes()).and_then(|_|f.sync_all()).is_err(){drop(f);let _=fs::remove_file(&target);return Err("recovery_journal_failed".into());}
        drop(f);
        if replace {if fs::rename(&target,path).is_err(){let _=fs::remove_file(&target);return Err("recovery_journal_failed".into());}}
        Ok(())
    }
}
impl Recovery {
    pub fn forget(&mut self) { self.pending = None; }
    pub fn status(&mut self, journal: &Path) -> Result<Value, String> {
        if let Some(p) = &self.pending { return Ok(p.safe()); }
        if let Ok(meta)=fs::symlink_metadata(journal){if !meta.is_file()||meta.file_type().is_symlink()||meta.len()>1024{return Err(fail());}}
        let mut f = match OpenOptions::new().read(true).open(journal) { Ok(f)=>f, Err(e) if e.kind()==std::io::ErrorKind::NotFound=>return Ok(Value::Null), Err(_)=>return Err("recovery_pending_receipt".into()) };
        let mut data = Vec::new(); Read::by_ref(&mut f).take(1025).read_to_end(&mut data).map_err(safe_error)?;
        if data.len()>1024 { return Err(fail()); }
        let v: Value = serde_json::from_slice(&data).map_err(safe_error)?;
        if !matches!(v["operation"].as_str(),Some("auth.factors.prepare"|"auth.factors.commit"|"auth.factors.cancel"))
            || !v["request_key"].as_str().is_some_and(ident) || v["status"]!="unknown" { return Err(fail()); }
        self.pending = Some(Pending { handle:uuid::Uuid::new_v4().to_string(), pair:"restored".into(), stick:None, seed:String::new(), challenge:None, expires:0, saved:false,
            phase:"unknown".into(), operation:v["operation"].as_str().unwrap().into(), request_key:v["request_key"].as_str().unwrap().into(), outcome:None });
        Ok(self.pending.as_ref().unwrap().safe())
    }
    pub fn start(&mut self, pair: &str, printed: bool, journal: &Path, mut send: impl FnMut(&str, Value)->Result<Value,String>) -> Result<Value,String> {
        platform::protect()?;
        if !matches!(pair,"setup"|"password_stick"|"password_totp"|"totp_stick") { return Err(fail()); }
        if self.status(journal)?.is_object() { return Err("recovery_pending_receipt".into()); }
        // Binding is fetched over the selected live link, never supplied by JS.
        let b: Binding = serde_json::from_value(send("auth.factors.binding", json!({"contract_version":2})).map_err(safe_error)?).map_err(|_|fail())?;
        b.validate()?;
        let stick = generate(&b)?;
        let mut body = json!({"contract_version":2,"pair":pair,"replacement":{"recovery_id":stick.recovery_id,"verifier":stick.verifier()}});
        let mut seed = String::new();
        let collect = (|| -> Result<(),String> {
            if pair=="setup" { body["setup_capability"] = json!(platform::entry("Provisioning setup capability", true)?); }
            if pair.starts_with("password_") { body["password"] = json!(platform::entry("Confirm it's you: current password", true)?); }
            if matches!(pair,"password_totp"|"totp_stick") { body["totp"] = json!(totp_entry("Confirm it's you: current authenticator code")?); }
            if matches!(pair,"password_stick"|"totp_stick") { body["old_stick"] = platform::read_stick(&b, printed)?.reduced(); }
            if pair=="totp_stick" {
                let mut pw = platform::entry("New password", true)?;
                let mut again = platform::entry("New password again", true)?;
                let matches = pw == again && !pw.is_empty(); wipe(&mut again);
                if !matches { wipe(&mut pw); return Err("password_mismatch".into()); } body["new_password"] = json!(pw); wipe(&mut pw);
            }
            if matches!(pair,"setup"|"password_stick") { seed = new_seed()?; body["new_seed"] = json!(seed); }
            Ok(())
        })();
        if let Err(e) = collect { scrub(&mut body); wipe(&mut seed); return Err(e); }
        let mut p = Pending { handle:uuid::Uuid::new_v4().to_string(),pair:pair.into(),stick:Some(stick),seed,challenge:None,expires:0,saved:false,
            phase:"unknown".into(),operation:"auth.factors.prepare".into(),request_key:uuid::Uuid::new_v4().to_string(),outcome:None };
        body["request_key"] = json!(p.request_key);
        if let Err(e) = p.journal(journal, false) { scrub(&mut body); return Err(e); }
        // Move to transport once. There is no retry loop and no body in journal.
        let result = send("auth.factors.prepare",body);
        match result {
            Ok(v)=>{if let Ok((id,expires))=prepared(&v){p.challenge=Some(id);p.expires=expires;p.phase="proof_verified".into();}},
            Err(e) if definitely_refused(&e)=>{p.phase="not_changed".into();p.stick=None;wipe(&mut p.seed);let _=fs::remove_file(journal);},
            _=>(),
        }
        self.pending=Some(p); Ok(self.pending.as_ref().unwrap().safe())
    }
    pub fn act(&mut self, handle: &str, action: &str, journal: &Path, mut send: impl FnMut(&str,Value)->Result<Value,String>) -> Result<Value,String> {
        let p=self.pending.as_mut().filter(|p|p.handle==handle).ok_or_else(||"recovery_handle_invalid".to_string())?;
        if action=="check" {
            if p.phase!="unknown" { return Ok(p.safe()); }
            let v=send("settings.status",json!({"request_key":p.request_key})).map_err(safe_error)?;
            if !matches!(v["phase"].as_str(),Some("ready"|"failed")) { return Ok(p.safe()); }
            if p.operation=="auth.factors.prepare" {
                if let Ok((id,expires))=prepared(&v["result"]) { p.challenge=Some(id);p.expires=expires;p.phase="proof_verified".into(); }
                else if v["phase"]=="failed" { p.phase="not_changed".into(); } else { return Err(fail()); }
            } else { let result=outcome(&v["result"])?; if p.operation=="auth.factors.cancel" && result["changed"]!=false{return Err(fail());} p.phase=if result["changed"]==true {"activated"}else{"not_changed"}.into();p.outcome=Some(result);p.stick=None;wipe(&mut p.seed); }
            return Ok(p.safe());
        }
        if action=="finish" && matches!(p.phase.as_str(),"activated"|"not_changed"|"cancelled") {
            match fs::remove_file(journal){Ok(())=>(),Err(e) if e.kind()==std::io::ErrorKind::NotFound=>(),Err(_)=>return Err("recovery_journal_failed".into())}
            self.pending=None;return Ok(Value::Null);
        }
        if p.phase!="proof_verified" && p.phase!="material_saved" { return Err("reconcile_original_receipt".into()); }
        if action!="cancel" && p.expires<=now() { return Err("recovery_challenge_expired".into()); }
        match action {
            "seed" => { if p.seed.is_empty() { return Err("seed_unavailable".into()); } platform::display_seed(&p.seed)?; }
            "usb"|"print" => { platform::save(p.stick.as_ref().ok_or_else(||"replacement_material_lost".to_string())?,action=="print")?; p.saved=true;p.phase="material_saved".into(); }
            "commit"|"cancel" => {
                if action=="commit" && (!p.saved || p.stick.is_none()) { return Err("verified_material_required".into()); }
                let mut body=json!({"contract_version":2,"challenge_id":p.challenge.as_ref().ok_or_else(fail)?});
                if action=="commit" {
                    body["material_saved"]=json!(true);
                    let mut code=if p.seed.is_empty(){String::new()}else{totp_entry("New authenticator six-digit proof")?};
                    body["new_totp"]=json!(code);wipe(&mut code);
                }
                p.operation=format!("auth.factors.{}",action);p.request_key=uuid::Uuid::new_v4().to_string();body["request_key"]=json!(p.request_key);
                if let Err(e)=p.journal(journal, true) { scrub(&mut body);return Err(e); } p.phase="unknown".into();
                match send(&p.operation,body) { Ok(v)=>{if let Ok(result)=outcome(&v) {
                    if action=="cancel" && result["changed"]!=false{return Err(fail());}
                    p.phase=if result["changed"]==true {"activated"}else if action=="cancel" {"cancelled"}else{"not_changed"}.into();p.outcome=Some(result);p.stick=None;wipe(&mut p.seed);
                }}, Err(e) if definitely_refused(&e)=>{p.phase="not_changed".into();p.stick=None;wipe(&mut p.seed);let _=fs::remove_file(journal);return Err("recovery_not_activated".into());}, _=>() }
            }
            _=>return Err("invalid_recovery_action".into()),
        }
        Ok(p.safe())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn binding()->Binding { Binding{installation_id:"install-1".into(),lane_id:"012345abcdef".into(),owner:"owner".into()} }
    fn fixed()->Stick { let mut s=Stick{format:"bob-recovery-stick".into(),version:1,installation_id:"install-1".into(),lane_id:"012345abcdef".into(),owner:"owner".into(),recovery_id:"recovery-1".into(),issued_at:"2026-11-04T00:00:00Z".into(),secret:base32(&[0;32]),checksum:String::new()};s.checksum=s.checksum_value();s }
    #[test] fn fixed_base32_vectors_and_canonical_bits(){assert_eq!(base32(b"foobar"),"MZXW6YTBOI");assert_eq!(base32(&[0;32]),"A".repeat(52));assert_eq!(base32(&[255;32]),format!("{}Q","7".repeat(51)));assert_eq!(base32(&[0;20]),"A".repeat(32));for c in b"ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"{assert_eq!(canonical_secret(&format!("{}{}","A".repeat(51),*c as char)),matches!(*c,b'A'|b'Q'));}}
    #[test] fn fixed_hash_vectors(){let s=fixed();assert_eq!(s.verifier(),"b628885086023e19ff5c750d00bf034084730900ac87efddf45edbb1e1753962");assert_eq!(s.checksum,"85b60bec10fec3e8");assert_eq!(hash(&["quote\"", "slash\\", "line\n"]),"649c446effc6b2a7fd22fe311e607745066121af689a9222bb66905cb4032d2e");}
    #[test] fn strict_validation(){let b=binding();let s=fixed();let raw=serde_json::to_vec(&s).unwrap();assert!(parse(&raw,&b).is_ok());
        for (key,value) in [("format",json!("wrong")),("version",json!(2)),("version",json!(true)),("version",json!("1")),("installation_id",json!("other")),("lane_id",json!("123456abcdef")),("owner",json!("other")),("recovery_id",json!("../bad")),("issued_at",json!("2026-02-30T00:00:00Z")),("issued_at",json!("2026-11-04T00:00:00+00:00")),("secret",json!("A".repeat(51)+"B")),("secret",json!("a".repeat(52))),("secret",json!("A".repeat(52)+"=")),("checksum",json!("0".repeat(16))),("extra",json!(true))]{let mut v=serde_json::to_value(&s).unwrap();v[key]=value;assert!(parse(&serde_json::to_vec(&v).unwrap(),&b).is_err(),"{key}");}
        for key in ["format","version","installation_id","lane_id","owner","recovery_id","issued_at","secret","checksum"]{let mut v=serde_json::to_value(&s).unwrap();v.as_object_mut().unwrap().remove(key);assert!(parse(&serde_json::to_vec(&v).unwrap(),&b).is_err());}
        for key in ["format","version","installation_id","lane_id","owner","recovery_id","issued_at","secret","checksum"]{
            let mut wrong=serde_json::to_value(&s).unwrap();wrong[key]=Value::Null;assert!(parse(&serde_json::to_vec(&wrong).unwrap(),&b).is_err());
            let text=String::from_utf8(raw.clone()).unwrap();let field=format!("{{{}:{},",serde_json::to_string(key).unwrap(),serde_json::to_string(&serde_json::to_value(&s).unwrap()[key]).unwrap());let duplicate=text.replacen('{',&field,1);assert!(parse(duplicate.as_bytes(),&b).is_err());
        }
        let mut boundary=raw.clone();boundary.resize(4096,b' ');assert!(parse(&boundary,&b).is_ok());boundary.push(b' ');assert!(parse(&boundary,&b).is_err());
        for date in ["0000-01-01T00:00:00Z","2026-11-04T00:00:60Z","2026-11-04T00:00:00z"]{let mut v=serde_json::to_value(&s).unwrap();v["issued_at"]=json!(date);assert!(parse(&serde_json::to_vec(&v).unwrap(),&b).is_err());}
        let text=String::from_utf8(raw.clone()).unwrap();let dup=text.replacen('{',"{\"version\":1,",1);assert!(parse(dup.as_bytes(),&b).is_err());assert!(parse(&[255],&b).is_err());assert!(parse(&vec![b' ';4097],&b).is_err());assert!(parse(format!("{} true",text).as_bytes(),&b).is_err());}
    #[test] fn exclusive_write_readback_and_volume_failure(){let d=std::env::temp_dir().join(uuid::Uuid::new_v4().to_string());fs::create_dir(&d).unwrap();let s=fixed();verified_write(&d,&s,||Ok(())).unwrap();let f=d.join(format!("bob-recovery-{}-PENDING.json",s.recovery_id));assert_eq!(parse(&bounded_read(&f).unwrap(),&binding()).unwrap().verifier(),s.verifier());assert!(verified_write(&d,&s,||Ok(())).is_err());fs::remove_file(&f).unwrap();let mut count=0;assert!(verified_write(&d,&s,||{count+=1;if count==3{Err("unplugged".into())}else{Ok(())}}).is_err());assert!(!f.exists());assert!(bounded_read(&d).is_err());fs::write(d.join("big"),vec![0;4097]).unwrap();assert!(bounded_read(&d.join("big")).is_err());fs::remove_dir_all(d).unwrap();}
    #[test] fn readback_corruption_is_not_saved(){let d=std::env::temp_dir().join(uuid::Uuid::new_v4().to_string());fs::create_dir(&d).unwrap();let s=fixed();let f=d.join(format!("bob-recovery-{}-PENDING.json",s.recovery_id));let mut count=0;let result=verified_write(&d,&s,||{count+=1;if count==4{fs::write(&f,b"corrupt").map_err(|_|"corruption_injection_refused".to_string())?;}Ok(())});assert!(result.is_err());fs::remove_dir_all(d).unwrap();}
    #[cfg(unix)]
    #[test] fn symlink_and_device_input_are_refused(){let d=std::env::temp_dir().join(uuid::Uuid::new_v4().to_string());fs::create_dir(&d).unwrap();fs::write(d.join("regular"),b"{}").unwrap();std::os::unix::fs::symlink(d.join("regular"),d.join("link")).unwrap();assert!(bounded_read(&d.join("link")).is_err());assert!(bounded_read(Path::new("/dev/null")).is_err());fs::remove_dir_all(d).unwrap();}
    #[cfg(windows)]
    #[test] fn native_devices_are_refused(){assert!(bounded_read(Path::new(r"\\.\NUL")).is_err());assert!(bounded_read(Path::new("NUL")).is_err());}
    #[test] fn preparation_receipt_survives_restart_without_material(){let d=std::env::temp_dir().join(uuid::Uuid::new_v4().to_string());fs::create_dir(&d).unwrap();let path=d.join("safe.json");fs::write(&path,r#"{"operation":"auth.factors.prepare","request_key":"prepare-1","status":"unknown"}"#).unwrap();let mut r=Recovery::default();let status=r.status(&path).unwrap();assert_eq!(status["phase"],"unknown");let handle=status["handle"].as_str().unwrap();let safe=r.act(handle,"check",&path,|op,args|{assert_eq!(op,"settings.status");assert_eq!(args,json!({"request_key":"prepare-1"}));Ok(json!({"phase":"ready","result":{"challenge_id":"c","expires_at":now()+200,"state":"proof_verified"}}))}).unwrap();assert_eq!(safe["material_available"],false);assert!(path.exists());assert!(r.act(handle,"commit",&path,|_,_|panic!("no resend")).is_err());r.act(handle,"cancel",&path,|op,_|{assert_eq!(op,"auth.factors.cancel");Ok(json!({"changed":false,"restarted":false}))}).unwrap();r.act(handle,"finish",&path,|_,_|panic!("no send")).unwrap();assert!(!path.exists());fs::remove_dir_all(d).unwrap();}
    #[test] fn csprng_material_is_fresh(){let a=generate(&binding()).unwrap();let b=generate(&binding()).unwrap();assert!(a.secret!=b.secret);assert_ne!(a.recovery_id,b.recovery_id);assert!(canonical_secret(&a.secret));assert_eq!(new_seed().unwrap().len(),32);}
    #[test] fn lost_commit_reply_reconciles_without_resend(){let dir=std::env::temp_dir().join(uuid::Uuid::new_v4().to_string());fs::create_dir(&dir).unwrap();let journal=dir.join("safe.json");let mut r=Recovery{pending:Some(Pending{handle:"h".into(),pair:"password_totp".into(),stick:Some(fixed()),seed:String::new(),challenge:Some("c".into()),expires:now()+300,saved:true,phase:"material_saved".into(),operation:String::new(),request_key:String::new(),outcome:None})};
        assert_eq!(r.act("h","commit",&journal,|op,_|{assert_eq!(op,"auth.factors.commit");Err("timeout".into())}).unwrap()["phase"],"unknown");let metadata=fs::read_to_string(&journal).unwrap();assert!(!metadata.contains(&"A".repeat(52)));assert!(!metadata.contains("verifier"));assert!(r.act("h","commit",&journal,|_,_|panic!("no resend")).is_err());
        assert_eq!(r.act("h","check",&journal,|op,_|{assert_eq!(op,"settings.status");Ok(json!({"phase":"committed"}))}).unwrap()["phase"],"unknown");
        assert_eq!(r.act("h","check",&journal,|op,_|{assert_eq!(op,"settings.status");Ok(json!({"phase":"failed","result":{"changed":true,"restarted":false}}))}).unwrap()["phase"],"activated");assert!(journal.exists());r.act("h","finish",&journal,|_,_|panic!("no send")).unwrap();assert!(!journal.exists());fs::remove_dir_all(dir).unwrap();}
    #[test] fn commit_requires_save_and_unexpired_challenge(){let mut r=Recovery{pending:Some(Pending{handle:"h".into(),pair:"password_totp".into(),stick:Some(fixed()),seed:String::new(),challenge:Some("c".into()),expires:now()+300,saved:false,phase:"proof_verified".into(),operation:String::new(),request_key:String::new(),outcome:None})};assert!(r.act("h","commit",Path::new("unused"),|_,_|panic!("no send")).is_err());r.pending.as_mut().unwrap().expires=0;assert!(r.act("h","usb",Path::new("unused"),|_,_|panic!("no send")).is_err());}
}
