//! Explicit native settings. Endpoint configuration is never read from project/model data.
use serde::{Deserialize, Serialize};
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StandaloneConfig {
    pub endpoint: String,
    pub model: String,
    pub credential_env: Option<String>,
}
impl StandaloneConfig {
    /// Native-only bounded text transport. curl receives secrets on stdin, never argv.
    /// No curlrc, proxy, redirect, shell, tool execution, or Bob fallback.
    pub fn complete(
        &self,
        messages: &[crate::functional::Message],
    ) -> Result<serde_json::Value, String> {
        use std::{
            io::{Read, Write},
            process::{Command, Stdio},
        };
        let checked = Self::new(&self.endpoint, &self.model, self.credential_env.clone())?;
        crate::functional::validate_model_request(messages)?;
        let body = serde_json::json!({"model":checked.model,"messages":messages,"stream":false})
            .to_string();
        fn quote(s: &str) -> String {
            format!(
                "\"{}\"",
                s.replace('\\', "\\\\")
                    .replace('"', "\\\"")
                    .replace('\n', "\\n")
                    .replace('\r', "\\r")
            )
        }
        let mut config = format!(
            "url = {}\nheader = \"Content-Type: application/json\"\ndata-binary = {}\n",
            quote(&checked.endpoint),
            quote(&body)
        );
        let mut credential = None;
        if let Some(name) = &checked.credential_env {
            let token =
                std::env::var(name).map_err(|_| "standalone credential reference unavailable")?;
            if token.is_empty() || token.len() > 8192 || token.chars().any(char::is_control) {
                return Err("standalone credential invalid".into());
            }
            config.push_str(&format!(
                "header = {}\n",
                quote(&format!("Authorization: Bearer {token}"))
            ));
            credential = Some(token);
        }
        let mut child = Command::new("/usr/bin/curl")
            .args([
                "--disable",
                "--silent",
                "--noproxy",
                "*",
                "--proto",
                "=http,https",
                "--connect-timeout",
                "10",
                "--max-time",
                "120",
                "--max-filesize",
                "131072",
                "--max-redirs",
                "0",
                "--write-out",
                "\n%{http_code}",
                "--config",
                "-",
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|_| "native curl transport unavailable")?;
        let write = child.stdin.take().unwrap().write_all(config.as_bytes());
        if write.is_err() {
            let _ = child.kill();
            let _ = child.wait();
            return Err("standalone request failed".into());
        }
        let mut bytes = vec![];
        let read = child
            .stdout
            .take()
            .unwrap()
            .take(131080)
            .read_to_end(&mut bytes);
        if read.is_err() || bytes.len() >= 131080 {
            let _ = child.kill();
            let _ = child.wait();
            return Err("standalone response too large or unreadable".into());
        }
        let result = child.wait().map_err(|_| "standalone transport failed")?;
        if !result.success() {
            return Err(
                "standalone transport failed (bounded timeout/size/network); no fallback".into(),
            );
        }
        let split = bytes
            .iter()
            .rposition(|b| *b == b'\n')
            .ok_or("standalone missing HTTP status")?;
        if &bytes[split + 1..] != b"200" {
            return Err("standalone HTTP status rejected; no fallback".into());
        }
        let v: serde_json::Value = serde_json::from_slice(&bytes[..split])
            .map_err(|_| "standalone invalid JSON response")?;
        fn reflects(v: &serde_json::Value, token: &str) -> bool {
            match v {
                serde_json::Value::String(s) => s.contains(token),
                serde_json::Value::Array(a) => a.iter().any(|v| reflects(v, token)),
                serde_json::Value::Object(m) => m
                    .iter()
                    .any(|(k, v)| k.contains(token) || reflects(v, token)),
                _ => false,
            }
        }
        if credential.as_ref().is_some_and(|token| reflects(&v, token)) {
            return Err("standalone response reflected credential; response withheld".into());
        }
        let content = v["choices"][0]["message"]["content"]
            .as_str()
            .filter(|s| !s.trim().is_empty() && s.len() <= 65536)
            .ok_or("standalone missing/oversized content")?;
        Ok(
            serde_json::json!({"ok":true,"content":content,"model":v["model"],"completion_id":v["id"],"usage":v["usage"],"transport":"standalone_http","tool_execution":"cleanroom_local"}),
        )
    }

    pub fn new(
        endpoint: &str,
        model: &str,
        credential_env: Option<String>,
    ) -> Result<Self, String> {
        let u = url::Url::parse(endpoint).map_err(|_| "invalid endpoint")?;
        let loopback = match u.host() {
            Some(url::Host::Domain("localhost")) => true,
            Some(url::Host::Ipv4(ip)) => ip.is_loopback(),
            Some(url::Host::Ipv6(ip)) => ip.is_loopback(),
            _ => false,
        };
        if !matches!(u.scheme(), "http" | "https")
            || u.host().is_none()
            || (!loopback && u.scheme() != "https")
            || !u.username().is_empty()
            || u.password().is_some()
            || u.fragment().is_some()
            || u.query().is_some()
            || endpoint.chars().any(char::is_control)
        {
            return Err(
                "endpoint requires http(s), TLS for non-loopback, no credentials/query/fragment"
                    .into(),
            );
        }
        if model.is_empty()
            || model.len() > 128
            || !model
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"/_.:-".contains(&b))
        {
            return Err("invalid model identifier".into());
        }
        if let Some(n) = &credential_env {
            if !n.starts_with("BOB_STANDALONE_")
                || !(n.ends_with("_KEY") || n.ends_with("_TOKEN"))
                || n.len() > 80
                || !n
                    .bytes()
                    .all(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || b == b'_')
            {
                return Err(
                    "credential env must be an explicit BOB_STANDALONE_*_KEY or *_TOKEN reference"
                        .into(),
                );
            }
        }
        Ok(Self {
            endpoint: u.to_string(),
            model: model.into(),
            credential_env,
        })
    }
}
