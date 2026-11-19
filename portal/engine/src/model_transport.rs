//! Fixed project-local server helper. No credentials or endpoint/model argv from UI.
use serde_json::{Value,json};
use crate::functional::{Message,validate_model_request};
use std::{io::Write,process::{Command,Stdio}};
pub fn relay_config(path_key: &str) -> Result<(String, String), String> {
 let target=std::env::var("BOB_RELAY_TARGET").map_err(|_|"Legacy relay unavailable: set BOB_RELAY_TARGET")?;
 let relay=std::env::var(path_key).map_err(|_|format!("Legacy relay unavailable: set {path_key}"))?;
 if target.is_empty() || target.starts_with('-') || !target.bytes().all(|b|b.is_ascii_alphanumeric()||b"@._-:".contains(&b))
  || !relay.starts_with('/') || !relay.bytes().all(|b|b.is_ascii_alphanumeric()||b"/._-".contains(&b)) {return Err("Invalid legacy relay configuration".into())}
 Ok((target,relay))
}
pub fn request(value:&Value)->Result<Value,String>{
 let data=serde_json::to_vec(value).map_err(|_|"Invalid model request")?;
 if data.len()>80000{return Err("Model request exceeds 80000 bytes".into())}
 let (target,relay)=relay_config("BOB_MODEL_RELAY")?;
 let mut child=Command::new("timeout").args(["--kill-after=5",if std::env::var("BOB_WSL_ENGINE").as_deref()==Ok("1"){"142"}else{"110"},"ssh","-o","BatchMode=yes","-o","StrictHostKeyChecking=yes","-o","ConnectTimeout=8",&target,"python3",&relay]).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().map_err(|_|"Selected model SSH transport unavailable; no fallback")?;
 child.stdin.take().ok_or("model stdin unavailable")?.write_all(&data).map_err(|_|"model request write failed; no retry")?;
 let result=child.wait_with_output().map_err(|_|"model response unknown; no retry")?;
 if result.stdout.len()>256000{return Err("Model response limit exceeded".into())}
 let value:Value=serde_json::from_slice(&result.stdout).map_err(|_|"Selected model relay response unavailable; no fallback")?;
 if !result.status.success()||value["ok"]!=true{
  let code=value["error"].as_str().filter(|s|s.len()<100&&s.bytes().all(|b|b.is_ascii_alphanumeric()||b==b'_')).unwrap_or("unavailable");
  return Err(format!("Selected model unavailable: {code}; no fallback"))
 }
 Ok(value)
}
pub fn complete(model:&str,messages:&[Message])->Result<Value,String>{
 if !["bob-ds-flash","bob-gpt-sol-61"].contains(&model){return Err("Unapproved selected model; no fallback".into())}
 validate_model_request(messages)?;
 request(&json!({"op":"complete","model":model,"messages":messages}))
}
