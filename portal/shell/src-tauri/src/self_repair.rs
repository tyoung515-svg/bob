//! Host-owned, model-free recovery. The runner is injectable; tests never touch WSL.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    fs,
    path::{Path, PathBuf},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum Health {
    Healthy,
    Degraded,
    Unreachable,
    Wedged,
    Stopped,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Observation {
    pub state: Health,
    pub observed: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Receipt {
    pub time: u64,
    pub step: String,
    pub exit: Option<i32>,
    pub verification: Observation,
}
#[derive(Clone, Debug)]
pub struct Output {
    pub ok: bool,
    pub code: Option<i32>,
    pub bytes: Vec<u8>,
}
pub trait Runner {
    fn run(&mut self, args: &[String], seconds: u64) -> Result<Output, String>;
    fn broker_ok(&mut self) -> bool {
        true
    }
    fn upgrade_pending(&mut self) -> Option<bool> {
        Some(false)
    }
    fn repair_verification(&mut self) {}
    fn pause(&mut self) {
        std::thread::sleep(Duration::from_secs(3));
    }
}
fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
fn args(items: &[&str]) -> Vec<String> {
    items.iter().map(|s| s.to_string()).collect()
}
const RESUME: &str = "import pathlib,re,sys\nsys.path.insert(0,'/usr/local/lib/bob_runtime')\nfrom runtime_upgrade import main\ni=(pathlib.Path('/var/lib/bob-upgrades')/'latest').read_text().strip()\nif not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,200}',i): raise ValueError('invalid journal')\nraise SystemExit(main(['--resume',i]))";
const INTERRUPTED: &str = r#"import json,pathlib,re,subprocess
r=pathlib.Path('/var/lib/bob-upgrades');pending=False
if (r/'latest').exists():
 i=(r/'latest').read_text().strip()
 if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,200}',i): raise ValueError('invalid journal')
 state=json.loads((r/i/'state.json').read_text())
 active=subprocess.run(['systemctl','is-active','--quiet','bob-runtime-upgrade.service'],capture_output=True).returncode==0
 pending=not active and state.get('outcome') in ('running','rolling_back','rollback_failed')
print('1' if pending else '0')
"#;
fn exec(distro: &str, items: &[&str]) -> Vec<String> {
    let mut result = args(&["-d", distro, "-u", "root", "--exec"]);
    result.extend(args(items));
    result
}
pub(crate) fn distro() -> &'static str {
    // Operator-only test override. Never take a distro name from IPC, a model or WSL.
    match std::env::var("BOB_RUNTIME_DISTRO").as_deref() {
        Ok("BoB-c7-test") => "BoB-c7-test",
        Ok("BoB-c6-test") => "BoB-c6-test",
        _ => "BoB",
    }
}
fn contains_distro(bytes: &[u8], distro: &str) -> bool {
    let text = if bytes.contains(&0) {
        String::from_utf16_lossy(
            &bytes
                .chunks_exact(2)
                .map(|x| u16::from_le_bytes([x[0], x[1]]))
                .collect::<Vec<_>>(),
        )
    } else {
        String::from_utf8_lossy(bytes).into_owned()
    };
    text.trim_start_matches('\u{feff}')
        .lines()
        .any(|s| s.trim() == distro)
}
fn hard_unexpected(result: &Result<Output, String>) -> bool {
    match result {
        Ok(out) => {
            out.code == Some(0x8000ffff_u32 as i32)
                || String::from_utf8_lossy(&out.bytes)
                    .replace('\0', "")
                    .contains("E_UNEXPECTED")
        }
        Err(error) => error.contains("E_UNEXPECTED"),
    }
}
/// Budget for a probe that opens a WSL session. It must not undercut `runtime::SESSION_FLOOR`:
/// the old 8 s probe was killed inside WSL's own first-session wait and wedged the distro.
const SESSION_SECONDS: u64 = crate::runtime::SESSION_FLOOR.as_secs();
pub fn detect(runner: &mut impl Runner, distro: &str, lane: &str) -> Observation {
    let observation = |state, text: &str| Observation {
        state,
        observed: format!("{distro}: {text}"),
    };
    // Two consecutive failures (or one hard E_UNEXPECTED) before calling WSL stuck.
    // Every session probe first checks the selected distro is already running.
    for attempt in 0..2 {
        let listing = runner.run(&args(&["--list", "--running", "--quiet"]), 8);
        match listing {
            Ok(ref out) if out.ok => {
                if !contains_distro(&out.bytes, distro) {
                    return observation(
                        Health::Stopped,
                        "Stopped. No session was started by this check.",
                    );
                }
            }
            ref failed => {
                if hard_unexpected(failed) || attempt == 1 {
                    return observation(
                        Health::Wedged,
                        "Windows could not query WSL (two failed probes or E_UNEXPECTED).",
                    );
                }
                continue;
            }
        }
        let probe = runner.run(&exec(distro, &["/bin/true"]), SESSION_SECONDS);
        if matches!(probe, Ok(ref out) if out.ok) {
            break;
        }
        if hard_unexpected(&probe) {
            return observation(Health::Wedged, "WSL session failed with E_UNEXPECTED.");
        }
        if attempt == 1 {
            return observation(Health::Wedged, "WSL reports this distro running, but two consecutive session probes failed or exceeded 45 seconds.");
        }
    }
    let out =
        match runner.run(&exec(distro, &["bob-health"]), SESSION_SECONDS) {
            Ok(out) => out,
            Err(_) => return observation(
                Health::Unreachable,
                "BoB started a session, but its health service did not reply within 45 seconds.",
            ),
        };
    let health = serde_json::from_slice::<Value>(&out.bytes).unwrap_or(Value::Null);
    if health["healthy"] == true && out.ok {
        match runner.upgrade_pending(){
            Some(true)=>return observation(Health::Degraded,"An interrupted runtime upgrade still has a saved journal. Repair can resume its checked payload."),
            None=>return observation(Health::Degraded,"BoB health passed, but its runtime upgrade journal could not be checked."),
            Some(false)=>{}
        }
        if !runner.broker_ok() {
            return observation(
                Health::Unreachable,
                "BoB services passed health checks, but this desktop's broker connection is down.",
            );
        }
        return observation(Health::Healthy, "BoB health checks passed.");
    }
    let bridge_down = health["units"].as_array().is_some_and(|units| {
        units.iter().any(|u| {
            u["name"] == format!("bob-bridge@{lane}.service") && u["ActiveState"] != "active"
        })
    });
    if health["lanes"][lane]["gateway"]["healthy"] == false || bridge_down {
        observation(
            Health::Unreachable,
            "Your BoB gateway is not answering its health check.",
        )
    } else {
        observation(
            Health::Degraded,
            "BoB health checks reported a failure or an invalid health response.",
        )
    }
}

/// Linux/container observation: what the Docker/Podman runtime actually reports. It never
/// speaks of WSL and never offers a repair; unhealthy stays unhealthy.
pub fn detect_container(runner: &mut impl Runner, container: &str, lane: &str) -> Observation {
    let observation = |state, text: &str| Observation {
        state,
        observed: format!("{container}: {text}"),
    };
    match runner.run(&args(&["inspect", "-f", "{{.State.Running}}", container]), 8) {
        Err(_) => return observation(Health::Unreachable, "The container engine could not be queried, so runtime health is unknown."),
        Ok(out) if !out.ok => return observation(Health::Stopped, "Runtime container not found. Start the Bob runtime container, then reopen this view."),
        Ok(out) if String::from_utf8_lossy(&out.bytes).trim() != "true" => {
            return observation(Health::Stopped, "Runtime container is not running. Start it to use Bob on this PC.")
        }
        Ok(_) => {}
    }
    let out = match runner.run(&args(&["exec", "-u", "root", container, "bob-health"]), 25) {
        Ok(out) => out,
        Err(_) => return observation(Health::Unreachable, "The runtime container is running, but its health service did not reply within 25 seconds."),
    };
    let health = serde_json::from_slice::<Value>(&out.bytes).unwrap_or(Value::Null);
    if health["healthy"] == true && out.ok {
        if !runner.broker_ok() {
            return observation(Health::Unreachable, "Runtime services passed health checks, but this desktop's broker connection is down.");
        }
        return observation(Health::Healthy, "Runtime health checks passed.");
    }
    let bridge_down = health["units"].as_array().is_some_and(|units| {
        units.iter().any(|u| u["name"] == format!("bob-bridge@{lane}.service") && u["ActiveState"] != "active")
    });
    if health["lanes"][lane]["gateway"]["healthy"] == false || bridge_down {
        observation(Health::Unreachable, "Your lane's gateway is not answering its health check.")
    } else {
        observation(Health::Degraded, "Runtime health checks reported a failure or an invalid health response.")
    }
}

/// Linux has WSL-free health only; "repairable": false keeps any Windows ladder out of the UI.
pub fn container_repair_status(runner: &mut impl Runner, container: &str, lane: &str) -> Value {
    json!({"observation":detect_container(runner, container, lane),"repairable":false,"receipts":[]})
}

fn save_receipt(root: &Path, receipt: &Receipt) -> Result<(), String> {
    use std::io::Write;
    fs::create_dir_all(root).map_err(|_| "Cannot create local repair receipt directory")?;
    let mut file = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(root.join("repair.jsonl"))
        .map_err(|_| "Cannot save repair receipt")?;
    writeln!(
        file,
        "{}",
        serde_json::to_string(receipt).map_err(|_| "Invalid repair receipt")?
    )
    .and_then(|_| file.sync_all())
    .map_err(|_| "Cannot persist repair receipt".into())
}
fn step(
    runner: &mut impl Runner,
    root: &Path,
    distro: &str,
    lane: &str,
    name: &str,
    command: Vec<String>,
    seconds: u64,
) -> Result<Receipt, String> {
    let exit = runner.run(&command, seconds).ok().and_then(|out| out.code);
    runner.repair_verification();
    let mut verification = detect(runner, distro, lane);
    if matches!(
        name,
        "restart_services"
            | "start_distro"
            | "start_after_shutdown"
            | "resume_upgrade"
            | "start_restored_backup"
    ) {
        for _ in 0..2 {
            if verification.state == Health::Healthy {
                break;
            }
            runner.pause();
            verification = detect(runner, distro, lane);
        }
    }
    let receipt = Receipt {
        time: now(),
        step: name.into(),
        exit,
        verification,
    };
    save_receipt(root, &receipt)?;
    Ok(receipt)
}

// The diagnostic bundle is an allowlist of host observations and exit codes.
// Never persist raw WSL stdout/stderr, journal text, env files or exception text.
pub fn diagnostics(root: &Path, observation: &Observation) -> Result<PathBuf, String> {
    fs::create_dir_all(root).map_err(|_| "Cannot create diagnostic directory")?;
    let path = root.join(format!("diagnostics-{}.json", uuid::Uuid::new_v4()));
    fs::write(
        &path,
        serde_json::to_vec_pretty(&json!({"time":now(),"observation":observation,
        "receipts":fs::read_to_string(root.join("repair.jsonl")).unwrap_or_default(),
        "includes":"host repair observations and step exits only; no environment or service logs"}))
        .unwrap(),
    )
    .map_err(|_| "Cannot write diagnostic bundle")?;
    Ok(path)
}

#[derive(Clone, Serialize, Deserialize)]
pub struct Backup {
    pub id: String,
    pub time: u64,
    pub path: PathBuf,
    pub sha256: String,
    pub distro: String,
}
pub fn backup(root: &Path) -> Result<Backup, String> {
    let entry: Backup = serde_json::from_slice(
        &fs::read(root.join("backup.json")).map_err(|_| "No pre-upgrade backup is registered")?,
    )
    .map_err(|_| "Invalid backup registry")?;
    if entry.path.parent() != Some(root)
        || entry.path.file_name().and_then(|s| s.to_str()) != Some(&format!("{}.tar", entry.id))
        || entry.id.len() != 36
        || uuid::Uuid::parse_str(&entry.id).is_err()
        || !["BoB", "BoB-c7-test", "BoB-c6-test"].contains(&entry.distro.as_str())
    {
        return Err("Invalid local backup path".into());
    }
    Ok(entry)
}
fn hash(path: &Path) -> Result<String, String> {
    use sha2::{Digest, Sha256};
    use std::io::Read;
    let mut file = fs::File::open(path).map_err(|_| "Backup archive unavailable")?;
    let mut h = Sha256::new();
    let mut buffer = vec![0u8; 1024 * 1024];
    loop {
        let n = file.read(&mut buffer).map_err(|_| "Cannot read backup")?;
        if n == 0 {
            break;
        }
        h.update(&buffer[..n]);
    }
    Ok(format!("{:x}", h.finalize()))
}
pub fn export_backup(
    runner: &mut impl Runner,
    root: &Path,
    distro: &str,
    free: u64,
) -> Result<Backup, String> {
    // df contributes only a numeric capacity estimate, never any command text.
    let space = runner.run(&exec(distro, &["df", "-B1", "--output=used", "/"]), 20)?;
    let used = String::from_utf8_lossy(&space.bytes)
        .lines()
        .last()
        .and_then(|s| s.trim().parse::<u64>().ok())
        .filter(|n| *n > 0)
        .ok_or("Cannot estimate runtime backup space")?;
    if !space.ok
        || free
            < used
                .saturating_add(used / 4)
                .saturating_add(512 * 1024 * 1024)
    {
        return Err(
            "Not enough space for a pre-upgrade backup. Free disk space before upgrading.".into(),
        );
    }
    fs::create_dir_all(root).map_err(|_| "Cannot create backup directory")?;
    let id = uuid::Uuid::new_v4().to_string();
    let path = root.join(format!("{id}.tar"));
    let out = runner.run(
        &args(&[
            "--export",
            distro,
            path.to_str().ok_or("Backup path is not Unicode")?,
        ]),
        900,
    );
    let success = matches!(out,Ok(ref out) if out.ok);
    let receipt = Receipt {
        time: now(),
        step: "pre_upgrade_export".into(),
        exit: out.ok().and_then(|o| o.code),
        verification: Observation {
            state: if success {
                Health::Healthy
            } else {
                Health::Degraded
            },
            observed: if success {
                "Export finished; checking archive hash."
            } else {
                "Export failed; update was not started."
            }
            .into(),
        },
    };
    save_receipt(root, &receipt)?;
    if !success {
        return Err("Pre-upgrade export failed. The upgrade was not started.".into());
    }
    let entry = Backup {
        id,
        time: now(),
        path: path.clone(),
        sha256: hash(&path)?,
        distro: distro.into(),
    };
    let temp = root.join("backup.json.tmp");
    fs::write(&temp, serde_json::to_vec_pretty(&entry).unwrap())
        .map_err(|_| "Cannot register backup")?;
    fs::rename(temp, root.join("backup.json")).map_err(|_| "Cannot save backup registry")?;
    prune_exports(root, &entry.id);
    Ok(entry)
}

/// Only the registered export can be restored from, so earlier ones are removed once a new
/// one is registered; otherwise every update leaves another full-size copy of the runtime.
/// Best-effort. Files not named `<uuid>.tar`, and directories, are never touched.
fn prune_exports(root: &Path, keep: &str) {
    let Ok(entries) = fs::read_dir(root) else { return };
    for entry in entries.flatten() {
        let path = entry.path();
        let earlier = path.extension().and_then(|e| e.to_str()) == Some("tar")
            && path.file_stem().and_then(|s| s.to_str())
                .is_some_and(|stem| stem != keep && uuid::Uuid::parse_str(stem).is_ok())
            && entry.file_type().is_ok_and(|kind| kind.is_file());
        if earlier {
            let _ = fs::remove_file(path);
        }
    }
}

#[derive(Serialize)]
pub struct Repair {
    pub observation: Observation,
    pub receipts: Vec<Receipt>,
    pub needs: Option<String>,
    pub diagnostics: Option<String>,
    pub backup: Option<Backup>,
}
pub fn ladder(
    runner: &mut impl Runner,
    root: &Path,
    distro: &str,
    lane: &str,
    shutdown_confirmed: bool,
) -> Result<Repair, String> {
    if !crate::portal::valid_lane(lane) {
        return Err("Select your BoB space first".into());
    }
    let mut result = Repair {
        observation: detect(runner, distro, lane),
        receipts: vec![],
        needs: None,
        diagnostics: None,
        backup: backup(root).ok(),
    };
    if result.observation.state == Health::Healthy {
        return Ok(result);
    }
    let services = [
        format!("bob-core@{lane}.service"),
        format!("bob-gateway@{lane}.service"),
        format!("bob-bridge@{lane}.service"),
    ];
    let mut restart = exec(distro, &["systemctl", "restart"]);
    restart.extend(services);
    let first = if result.observation.state == Health::Stopped {
        let receipt = Receipt {
            time: now(),
            step: "restart_services_skipped_stopped".into(),
            exit: None,
            verification: result.observation.clone(),
        };
        save_receipt(root, &receipt)?;
        receipt
    } else {
        step(runner, root, distro, lane, "restart_services", restart, 60)?
    };
    result.observation = first.verification.clone();
    result.receipts.push(first);
    if result.observation.state == Health::Healthy {
        return Ok(result);
    }
    // Terminate and boot are separately receipted; health verification follows boot.
    let terminated = runner
        .run(&args(&["--terminate", distro]), 30)
        .ok()
        .and_then(|o| o.code);
    let termination = Receipt {
        time: now(),
        step: "terminate_distro".into(),
        exit: terminated,
        verification: detect(runner, distro, lane),
    };
    save_receipt(root, &termination)?;
    result.receipts.push(termination);
    let second = step(
        runner,
        root,
        distro,
        lane,
        "start_distro",
        exec(distro, &["/bin/true"]),
        60,
    )?;
    result.observation = second.verification.clone();
    result.receipts.push(second);
    if result.observation.state == Health::Healthy {
        return Ok(result);
    }
    if !shutdown_confirmed {
        result.needs = Some("shutdown_confirmation".into());
        return Ok(result);
    }
    let third = step(
        runner,
        root,
        distro,
        lane,
        "shutdown_all_wsl",
        args(&["--shutdown"]),
        60,
    )?;
    result.receipts.push(third);
    let boot = step(
        runner,
        root,
        distro,
        lane,
        "start_after_shutdown",
        exec(distro, &["/bin/true"]),
        60,
    )?;
    result.observation = boot.verification.clone();
    result.receipts.push(boot);
    if result.observation.state == Health::Healthy {
        return Ok(result);
    }
    let fourth = step(
        runner,
        root,
        distro,
        lane,
        "health_check",
        exec(distro, &["bob-health"]),
        25,
    )?;
    result.observation = fourth.verification.clone();
    result.receipts.push(fourth);
    if result.observation.state == Health::Healthy {
        return Ok(result);
    }
    let bundle = diagnostics(root, &result.observation)?;
    let receipt = Receipt {
        time: now(),
        step: "diagnostics".into(),
        exit: Some(0),
        verification: result.observation.clone(),
    };
    save_receipt(root, &receipt)?;
    result.receipts.push(receipt);
    result.diagnostics = Some(bundle.to_string_lossy().into_owned());
    let resume = step(
        runner,
        root,
        distro,
        lane,
        "resume_upgrade",
        exec(distro, &["/opt/bob/.venv/bin/python", "-I", "-c", RESUME]),
        900,
    )?;
    result.observation = resume.verification.clone();
    result.receipts.push(resume);
    if result.observation.state != Health::Healthy {
        result.needs = Some(
            if result.backup.is_some() {
                "restore_confirmation"
            } else {
                "manual_repair"
            }
            .into(),
        );
    }
    Ok(result)
}

pub fn restore(
    runner: &mut impl Runner,
    root: &Path,
    distro: &str,
    lane: &str,
    confirm_id: &str,
) -> Result<Receipt, String> {
    let entry = backup(root)?;
    if confirm_id != entry.id || entry.distro != distro {
        return Err("Confirm this named backup before restoring".into());
    }
    if hash(&entry.path)? != entry.sha256 {
        return Err("Backup hash mismatch; restore refused".into());
    }
    let _ = runner.run(&args(&["--terminate", distro]), 30);
    // Only reached through a separate native confirmation naming the backup and loss.
    let unregistered = step(
        runner,
        root,
        distro,
        lane,
        "restore_remove_current_distro",
        args(&["--unregister", distro]),
        90,
    )?;
    if unregistered.exit != Some(0) {
        return Ok(unregistered);
    }
    let destination = root.join(format!("restored-{}", entry.id));
    let imported = step(
        runner,
        root,
        distro,
        lane,
        "restore_backup",
        args(&[
            "--import",
            distro,
            destination.to_str().ok_or("Invalid restore destination")?,
            entry.path.to_str().ok_or("Invalid backup path")?,
            "--version",
            "2",
        ]),
        900,
    )?;
    if imported.exit != Some(0) {
        return Ok(imported);
    }
    step(
        runner,
        root,
        distro,
        lane,
        "start_restored_backup",
        exec(distro, &["/bin/true"]),
        60,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Fake {
        state: Health,
        fix: &'static str,
        shutdown: bool,
        terminated: bool,
        calls: Vec<Vec<String>>,
    }
    impl Fake {
        fn new(state: Health, fix: &'static str) -> Self {
            Self {
                state,
                fix,
                shutdown: false,
                terminated: false,
                calls: vec![],
            }
        }
    }
    impl Runner for Fake {
        fn pause(&mut self) {}
        fn run(&mut self, a: &[String], seconds: u64) -> Result<Output, String> {
            assert!(seconds <= 900);
            self.calls.push(a.to_vec());
            let has = |word: &str| a.iter().any(|v| v == word);
            if has("--running") {
                return Ok(Output {
                    ok: true,
                    code: Some(0),
                    bytes: if self.state == Health::Stopped {
                        vec![]
                    } else {
                        b"BoB-c7-test\n".to_vec()
                    },
                });
            }
            if has("--terminate") {
                self.terminated = true;
                self.state = Health::Stopped;
            }
            if has("--shutdown") {
                self.shutdown = true;
                self.state = Health::Stopped;
            }
            if has("restart") && self.fix == "service" {
                self.state = Health::Healthy;
            }
            if has("/bin/true") && self.terminated && self.state == Health::Stopped {
                self.state = if self.fix == "boot" || self.fix == "shutdown" && self.shutdown {
                    Health::Healthy
                } else {
                    Health::Degraded
                };
            }
            if has(RESUME) && self.fix == "resume" {
                self.state = Health::Healthy;
            }
            if has("--import") {
                self.fix = "boot";
            }
            if has("/bin/true") && self.state == Health::Wedged {
                return Err("E_UNEXPECTED SECRET_MUST_NOT_ESCAPE".into());
            }
            let bytes = if has("bob-health") {
                serde_json::to_vec(&json!({"healthy":self.state==Health::Healthy,
                    "lanes":{"dad":{"gateway":{"healthy":self.state!=Health::Unreachable}}},
                    "untrusted":"SECRET_MUST_NOT_ESCAPE; cmd /c evil"}))
                .unwrap()
            } else {
                vec![]
            };
            Ok(Output {
                ok: !has("bob-health") || self.state == Health::Healthy,
                code: Some(if has("bob-health") && self.state != Health::Healthy {
                    1
                } else {
                    0
                }),
                bytes,
            })
        }
    }
    fn temp() -> PathBuf {
        let p = std::env::temp_dir().join(format!("bob-repair-test-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&p).unwrap();
        p
    }

    struct Scripted(Vec<(&'static str, Result<Output, String>)>, bool);
    impl Runner for Scripted {
        fn pause(&mut self) {}
        fn broker_ok(&mut self) -> bool { self.1 }
        fn run(&mut self, a: &[String], _: u64) -> Result<Output, String> {
            let key = if a[0] == "inspect" { "inspect" } else { "health" };
            assert!(a.iter().all(|word| !word.to_lowercase().contains("wsl")), "no WSL vocabulary: {a:?}");
            self.0.iter().find(|(k, _)| *k == key).map(|(_, v)| v.clone()).expect("scripted")
        }
    }
    fn out(ok: bool, text: &str) -> Result<Output, String> { Ok(Output { ok, code: Some(if ok { 0 } else { 1 }), bytes: text.as_bytes().to_vec() }) }
    #[test]
    fn linux_status_caller_reports_broker_down_and_never_repairable() {
        let healthy = r#"{"healthy":true,"lanes":{"guest":{"gateway":{"healthy":true}}},"units":[]}"#;
        let script = || vec![("inspect", out(true, "true")), ("health", out(true, healthy))];
        let down = container_repair_status(&mut Scripted(script(), false), "bob-runtime", "guest");
        assert_eq!(down["observation"]["state"], "unreachable");
        assert_eq!(down["repairable"], false);
        let up = container_repair_status(&mut Scripted(script(), true), "bob-runtime", "guest");
        assert_eq!(up["observation"]["state"], "healthy");
        assert_eq!(up["repairable"], false);
    }
    #[test]
    fn container_observation_reports_the_real_docker_runtime_not_wsl() {
        let healthy = r#"{"healthy":true,"lanes":{"guest":{"gateway":{"healthy":true}}},"units":[]}"#;
        let o = detect_container(&mut Scripted(vec![("inspect", out(true, "true\n")), ("health", out(true, healthy))], true), "bob-runtime", "guest");
        assert_eq!(o.state, Health::Healthy);
        assert!(o.observed.contains("bob-runtime") && !o.observed.to_lowercase().contains("wsl"));
        // Healthy services but a down desktop broker is not green.
        let o = detect_container(&mut Scripted(vec![("inspect", out(true, "true")), ("health", out(true, healthy))], false), "bob-runtime", "guest");
        assert_eq!(o.state, Health::Unreachable);
        // Real failures stay visible.
        let sick = r#"{"healthy":false,"lanes":{"guest":{"gateway":{"healthy":false}}},"units":[]}"#;
        let o = detect_container(&mut Scripted(vec![("inspect", out(true, "true")), ("health", out(false, sick))], true), "bob-runtime", "guest");
        assert_eq!(o.state, Health::Unreachable);
        assert!(o.observed.contains("gateway"));
        let o = detect_container(&mut Scripted(vec![("inspect", out(true, "true")), ("health", out(false, "not json"))], true), "bob-runtime", "guest");
        assert_eq!(o.state, Health::Degraded);
        // Stopped / missing container and unavailable engine are honest, never "WSL stuck".
        let o = detect_container(&mut Scripted(vec![("inspect", out(true, "false"))], true), "bob-runtime", "guest");
        assert_eq!(o.state, Health::Stopped);
        assert!(o.observed.contains("not running"));
        let o = detect_container(&mut Scripted(vec![("inspect", out(false, ""))], true), "bob-runtime", "guest");
        assert_eq!(o.state, Health::Stopped);
        assert!(o.observed.contains("not found"));
        let o = detect_container(&mut Scripted(vec![("inspect", Err("engine missing".into()))], true), "bob-runtime", "guest");
        assert_eq!(o.state, Health::Unreachable);
        assert!(!o.observed.to_lowercase().contains("wsl") && !o.observed.contains("Windows"));
    }
    #[test]
    fn watchdog_classifies_all_states_without_starting_stopped_distro() {
        for state in [
            Health::Healthy,
            Health::Degraded,
            Health::Unreachable,
            Health::Wedged,
            Health::Stopped,
        ] {
            let mut fake = Fake::new(state.clone(), "");
            assert_eq!(detect(&mut fake, "BoB-c7-test", "dad").state, state);
            if state == Health::Stopped {
                assert_eq!(fake.calls.len(), 1);
            }
        }
    }
    #[test]
    fn slow_cold_probe_retries_and_names_the_watched_distro() {
        struct Probes { fake: Fake, failures: usize, probes: usize, hard: bool }
        impl Runner for Probes {
            fn run(&mut self, a: &[String], seconds: u64) -> Result<Output, String> {
                if a.iter().any(|s| s == "/bin/true") {
                    self.probes += 1;
                    if self.probes <= self.failures {
                        return Err(if self.hard { "Wsl/Service/E_UNEXPECTED" } else { "timeout" }.into());
                    }
                }
                self.fake.run(a, seconds)
            }
        }
        for (failures, hard, expected, count) in [(1,false,Health::Healthy,2),(2,false,Health::Wedged,2),(1,true,Health::Wedged,1)] {
            let mut r=Probes{fake:Fake::new(Health::Healthy,""),failures,probes:0,hard};
            let o=detect(&mut r,"BoB-c7-test","dad");
            assert_eq!(o.state,expected); assert_eq!(r.probes,count);
            assert!(o.observed.starts_with("BoB-c7-test:"));
            assert!(!r.fake.calls.iter().any(|a| a.iter().any(|s| matches!(s.as_str(),"--shutdown"|"--terminate"|"--import"))));
        }
    }
    #[test]
    fn a_dead_desktop_broker_is_unreachable_even_when_health_is_green() {
        struct BrokerDown {
            fake: Fake,
            connected: bool,
        }
        impl Runner for BrokerDown {
            fn run(&mut self, a: &[String], s: u64) -> Result<Output, String> {
                self.fake.run(a, s)
            }
            fn broker_ok(&mut self) -> bool {
                self.connected
            }
            fn repair_verification(&mut self) {
                self.connected = true;
            }
            fn pause(&mut self) {}
        }
        let root = temp();
        let mut runner = BrokerDown {
            fake: Fake::new(Health::Healthy, "service"),
            connected: false,
        };
        assert_eq!(
            detect(&mut runner, "BoB-c7-test", "dad").state,
            Health::Unreachable
        );
        let result = ladder(&mut runner, &root, "BoB-c7-test", "dad", false).unwrap();
        assert_eq!(result.observation.state, Health::Healthy);
        assert_eq!(result.receipts.len(), 1);
        assert_eq!(result.receipts[0].step, "restart_services");
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn interrupted_upgrade_cannot_hide_behind_green_service_health() {
        struct Interrupted {
            fake: Fake,
            pending: bool,
        }
        impl Runner for Interrupted {
            fn run(&mut self, a: &[String], s: u64) -> Result<Output, String> {
                if a.iter().any(|arg| arg == RESUME) {
                    self.pending = false;
                    self.fake.state = Health::Healthy;
                }
                self.fake.run(a, s)
            }
            fn upgrade_pending(&mut self) -> Option<bool> {
                Some(self.pending)
            }
            fn pause(&mut self) {}
        }
        let root = temp();
        let mut runner = Interrupted {
            fake: Fake::new(Health::Healthy, ""),
            pending: true,
        };
        let before = detect(&mut runner, "BoB-c7-test", "dad");
        assert_eq!(before.state, Health::Degraded);
        assert!(before.observed.contains("interrupted runtime upgrade"));
        let result = ladder(&mut runner, &root, "BoB-c7-test", "dad", true).unwrap();
        assert_eq!(result.observation.state, Health::Healthy);
        assert_eq!(result.receipts.last().unwrap().step, "resume_upgrade");
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn each_rung_stops_at_first_verified_fix_and_writes_receipts() {
        for fix in ["service", "boot", "shutdown", "resume"] {
            let root = temp();
            let mut fake = Fake::new(Health::Unreachable, fix);
            let result = ladder(&mut fake, &root, "BoB-c7-test", "dad", true).unwrap();
            assert_eq!(result.observation.state, Health::Healthy, "{fix}");
            assert_eq!(result.needs, None);
            let expected = match fix {
                "service" => "restart_services",
                "boot" => "start_distro",
                "shutdown" => "start_after_shutdown",
                _ => "resume_upgrade",
            };
            assert_eq!(result.receipts.last().unwrap().step, expected);
            let saved = fs::read_to_string(root.join("repair.jsonl")).unwrap();
            assert_eq!(saved.lines().count(), result.receipts.len());
            assert!(!saved.contains("SECRET_MUST_NOT_ESCAPE"));
            if let Some(path) = result.diagnostics {
                assert!(!fs::read_to_string(path)
                    .unwrap()
                    .contains("SECRET_MUST_NOT_ESCAPE"));
            }
            fs::remove_dir_all(root).unwrap();
        }
    }
    #[test]
    fn shutdown_and_named_restore_have_separate_confirmation_boundaries() {
        let root = temp();
        let mut fake = Fake::new(Health::Unreachable, "");
        let result = ladder(&mut fake, &root, "BoB-c7-test", "dad", false).unwrap();
        assert_eq!(result.needs.as_deref(), Some("shutdown_confirmation"));
        assert!(!fake.shutdown);
        let id = uuid::Uuid::new_v4().to_string();
        let path = root.join(format!("{id}.tar"));
        fs::write(&path, b"backup").unwrap();
        let entry = Backup {
            id: id.clone(),
            time: now(),
            sha256: hash(&path).unwrap(),
            path,
            distro: "BoB-c7-test".into(),
        };
        fs::write(
            root.join("backup.json"),
            serde_json::to_vec(&entry).unwrap(),
        )
        .unwrap();
        let result = ladder(&mut fake, &root, "BoB-c7-test", "dad", true).unwrap();
        assert_eq!(result.needs.as_deref(), Some("restore_confirmation"));
        let count = fake.calls.len();
        assert!(restore(&mut fake, &root, "BoB-c7-test", "dad", "wrong").is_err());
        assert_eq!(fake.calls.len(), count);
        assert_eq!(
            restore(&mut fake, &root, "BoB-c7-test", "dad", &id)
                .unwrap()
                .verification
                .state,
            Health::Healthy
        );
        assert!(fake
            .calls
            .iter()
            .any(|a| a.iter().any(|s| s == "--unregister")));
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn external_output_never_becomes_a_command_and_invalid_lane_is_refused() {
        let root = temp();
        let mut fake = Fake::new(Health::Wedged, "");
        assert!(ladder(&mut fake, &root, "BoB-c7-test", "dad; evil", true).is_err());
        assert!(fake.calls.is_empty());
        ladder(&mut fake, &root, "BoB-c7-test", "dad", true).unwrap();
        assert!(fake
            .calls
            .iter()
            .flatten()
            .all(|s| !s.contains("SECRET_MUST_NOT_ESCAPE") && !s.contains("cmd /c evil")));
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn pre_upgrade_export_checks_capacity_and_registers_exact_archive_hash() {
        struct Export {
            exports: usize,
        }
        impl Runner for Export {
            fn run(&mut self, args: &[String], _: u64) -> Result<Output, String> {
                if args.iter().any(|s| s == "df") {
                    return Ok(Output {
                        ok: true,
                        code: Some(0),
                        bytes: b"Used\n1048576\n".to_vec(),
                    });
                }
                assert_eq!(args[0], "--export");
                assert_eq!(args[1], "BoB-c7-test");
                self.exports += 1;
                fs::write(&args[2], b"fixture-export").unwrap();
                Ok(Output {
                    ok: true,
                    code: Some(0),
                    bytes: vec![],
                })
            }
        }
        let root = temp();
        let mut runner = Export { exports: 0 };
        assert!(export_backup(&mut runner, &root, "BoB-c7-test", 1024).is_err());
        assert_eq!(runner.exports, 0);
        let saved = export_backup(&mut runner, &root, "BoB-c7-test", 1024 * 1024 * 1024).unwrap();
        assert_eq!(runner.exports, 1);
        assert_eq!(backup(&root).unwrap().id, saved.id);
        assert_eq!(saved.sha256, hash(&saved.path).unwrap());
        // A newer export replaces the earlier one; unrelated files and folders stay.
        let kept = [root.join("notes.tar"), root.join("repair.jsonl")];
        for path in &kept { fs::write(path, b"keep").unwrap(); }
        let folder = root.join(format!("restored-{}", saved.id));
        fs::create_dir_all(&folder).unwrap();
        let newer = export_backup(&mut runner, &root, "BoB-c7-test", 1024 * 1024 * 1024).unwrap();
        assert_ne!(newer.id, saved.id);
        assert!(!saved.path.exists() && newer.path.exists() && folder.is_dir());
        assert!(kept.iter().all(|path| path.exists()));
        assert_eq!(backup(&root).unwrap().id, newer.id);
        let saved = newer;
        fs::write(&saved.path, b"corrupt").unwrap();
        let count = runner.exports;
        assert!(restore(&mut runner, &root, "BoB-c7-test", "dad", &saved.id)
            .unwrap_err()
            .contains("hash mismatch"));
        assert_eq!(runner.exports, count);
        fs::remove_dir_all(root).unwrap();
    }
}

#[derive(Default)]
pub struct NativeRunner {
    portal: Option<std::sync::Arc<crate::portal::PortalState>>,
    reconnect: bool,
}
impl Runner for NativeRunner {
    fn upgrade_pending(&mut self) -> Option<bool> {
        self.run(
            &exec(
                distro(),
                &["/opt/bob/.venv/bin/python", "-I", "-c", INTERRUPTED],
            ),
            10,
        )
        .ok()
        .and_then(|out| {
            if out.ok {
                match out.bytes.as_slice() {
                    b"0\n" | b"0" => Some(false),
                    b"1\n" | b"1" => Some(true),
                    _ => None,
                }
            } else {
                None
            }
        })
    }
    fn broker_ok(&mut self) -> bool {
        self.portal
            .as_ref()
            .map(|p| {
                if self.reconnect {
                    p.repair_broker_probe()
                } else {
                    p.watchdog_broker_ok()
                }
            })
            .unwrap_or(true)
    }
    fn repair_verification(&mut self) {
        self.reconnect = true;
    }
    fn run(&mut self, args: &[String], seconds: u64) -> Result<Output, String> {
        #[cfg(windows)]
        {
            crate::runtime::platform::wsl(args, None, Duration::from_secs(seconds))
                .map(|(ok, bytes, code)| Output { ok, bytes, code })
        }
        #[cfg(not(windows))]
        {
            // Only detect_container builds these argv: fixed inspect/exec words plus a validated container.
            let (engine, _) = crate::portal::runtime_engine_container()?;
            let mut command = std::process::Command::new(engine);
            command.args(args);
            crate::runtime::bounded_output(command, None, Duration::from_secs(seconds))
                .map(|(ok, bytes, code)| Output { ok, bytes, code })
        }
    }
}
pub(crate) fn root() -> PathBuf {
    crate::portal::data_root().join("repair")
}
#[cfg(windows)]
pub(crate) fn pre_upgrade_backup() -> Result<Backup, String> {
    use std::os::windows::ffi::OsStrExt;
    let root = root();
    fs::create_dir_all(&root).map_err(|_| "Cannot create backup directory")?;
    let path: Vec<u16> = root.as_os_str().encode_wide().chain(Some(0)).collect();
    let mut free = 0;
    if unsafe {
        windows_sys::Win32::Storage::FileSystem::GetDiskFreeSpaceExW(
            path.as_ptr(),
            &mut free,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        )
    } == 0
    {
        return Err("Cannot check space for a pre-upgrade export".into());
    }
    export_backup(&mut NativeRunner::default(), &root, distro(), free)
}
#[tauri::command]
pub async fn runtime_repair_status(
    window: tauri::WebviewWindow,
    state: tauri::State<'_, std::sync::Arc<crate::portal::PortalState>>,
) -> Result<Value, String> {
    crate::runtime::main_window(&window)?;
    let lane = crate::portal::local_model_lane()?;
    #[cfg(not(windows))]
    {
        let portal = state.inner().clone();
        let (_, container) = crate::portal::runtime_engine_container()?;
        return tauri::async_runtime::spawn_blocking(move || {
            // The caller binds the real desktop-broker observation, exactly as the Windows path does.
            let mut runner = NativeRunner { portal: Some(portal), reconnect: false };
            Ok(container_repair_status(&mut runner, &container, &lane))
        })
        .await
        .map_err(|_| "Runtime health check failed")?;
    }
    #[cfg(windows)]
    {
    let portal = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let receipts: Vec<Receipt> = fs::read_to_string(root().join("repair.jsonl"))
            .unwrap_or_default()
            .lines()
            .rev()
            .take(100)
            .filter_map(|line| serde_json::from_str(line).ok())
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .collect();
        let observation = if let Ok(_guard) = crate::runtime::PROVISIONING.try_lock() {
            detect(
                &mut NativeRunner {
                    portal: Some(portal),
                    reconnect: false,
                },
                distro(),
                &lane,
            )
        } else {
            receipts
                .last()
                .map(|r| r.verification.clone())
                .unwrap_or(Observation {
                    state: Health::Degraded,
                    observed: format!("{}: runtime operation in progress.", distro()),
                })
        };
        Ok(json!({"observation":observation,"backup":backup(&root()).ok(),"receipts":receipts}))
    })
    .await
    .map_err(|_| "Repair watchdog failed")?
    }
}
#[tauri::command]
pub async fn runtime_repair_start(
    window: tauri::WebviewWindow,
    state: tauri::State<'_, std::sync::Arc<crate::portal::PortalState>>,
    shutdown_confirmed: bool,
) -> Result<Value, String> {
    crate::runtime::main_window(&window)?;
    if cfg!(not(windows)) {
        return Err("Automatic repair is available on Windows only.".into());
    }
    let lane = crate::portal::local_model_lane()?;
    let portal = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let _lock = crate::runtime::PROVISIONING
            .try_lock()
            .map_err(|_| "A runtime operation is already active")?;
        serde_json::to_value(ladder(
            &mut NativeRunner {
                portal: Some(portal),
                reconnect: false,
            },
            &root(),
            distro(),
            &lane,
            shutdown_confirmed,
        )?)
        .map_err(|_| "Invalid repair result".into())
    })
    .await
    .map_err(|_| "Repair worker failed")?
}
#[tauri::command]
pub async fn runtime_repair_restore(
    window: tauri::WebviewWindow,
    state: tauri::State<'_, std::sync::Arc<crate::portal::PortalState>>,
    backup_id: String,
) -> Result<Value, String> {
    crate::runtime::main_window(&window)?;
    if cfg!(not(windows)) {
        return Err("Automatic repair is available on Windows only.".into());
    }
    let lane = crate::portal::local_model_lane()?;
    let portal = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let _lock = crate::runtime::PROVISIONING
            .try_lock()
            .map_err(|_| "A runtime operation is already active")?;
        serde_json::to_value(restore(
            &mut NativeRunner {
                portal: Some(portal),
                reconnect: false,
            },
            &root(),
            distro(),
            &lane,
            &backup_id,
        )?)
        .map_err(|_| "Invalid restore result".into())
    })
    .await
    .map_err(|_| "Restore worker failed")?
}
