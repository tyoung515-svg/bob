//! Windows effects, called only after a native claim and local permission check.
use crate::{
    pc_policy::{checked_path, in_root},
    pc_sign::digest,
};
use serde_json::{json, Value};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};

fn guards(path: &Path, root: &Path) -> Result<Vec<File>, String> {
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        let mut handles = vec![];
        for ancestor in path.ancestors().filter(|p| p.exists() && in_root(p, root)) {
            let file = OpenOptions::new()
                .access_mode(0x80)
                .share_mode(1)
                .custom_flags(0x02000000 | 0x00200000)
                .open(ancestor)
                .map_err(|_| "Path is busy or cannot be held safely")?;
            use std::os::windows::fs::MetadataExt;
            let metadata = file
                .metadata()
                .map_err(|_| "Cannot inspect held directory")?;
            if !metadata.is_dir() || metadata.file_attributes() & 0x400 != 0 {
                return Err("Directory reparse points are refused".into());
            }
            handles.push(file);
        }
        Ok(handles)
    }
    #[cfg(not(windows))]
    {
        let _ = (path, root);
        Ok(vec![])
    }
}
fn read_file(path: &Path) -> Result<Vec<u8>, String> {
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        options.custom_flags(0x00200000).share_mode(1);
    }
    let mut file = options.open(path).map_err(|_| "File could not be opened")?;
    #[cfg(windows)]
    {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::Storage::FileSystem::*;
        let mut info = BY_HANDLE_FILE_INFORMATION::default();
        if unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut info) } == 0
            || info.nNumberOfLinks != 1
            || info.dwFileAttributes & 0x400 != 0
        {
            return Err("Reparse points and multiply linked files are refused".into());
        }
    }
    let mut data = vec![];
    Read::by_ref(&mut file)
        .take(128 * 1024 + 1)
        .read_to_end(&mut data)
        .map_err(|_| "File read failed")?;
    if data.len() > 128 * 1024 {
        return Err("File exceeds the 128 KiB PC read/edit limit".into());
    }
    Ok(data)
}
fn replace(source: &Path, target: &Path) -> Result<(), String> {
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Storage::FileSystem::*;
        let wide = |p: &Path| {
            p.as_os_str()
                .encode_wide()
                .chain(Some(0))
                .collect::<Vec<_>>()
        };
        if unsafe {
            MoveFileExW(
                wide(source).as_ptr(),
                wide(target).as_ptr(),
                MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
            )
        } == 0
        {
            return Err("Atomic Windows replacement failed".into());
        }
        Ok(())
    }
    #[cfg(not(windows))]
    {
        fs::rename(source, target).map_err(|_| "Atomic replacement failed".into())
    }
}
pub fn execute(
    id: &str,
    operation: &str,
    args: &Value,
    root: &Path,
    receipts: &Path,
    cancel: Arc<AtomicBool>,
) -> Value {
    let started = Instant::now();
    let mut result = match perform(id, operation, args, root, receipts, cancel) {
        Ok(value) => value,
        Err(reason) => json!({"status":"failed","reason":reason,"exit_code":null}),
    };
    result["elapsed_ms"] = json!(started.elapsed().as_millis() as u64);
    result
}
fn perform(
    id: &str,
    operation: &str,
    args: &Value,
    root: &Path,
    receipts: &Path,
    cancel: Arc<AtomicBool>,
) -> Result<Value, String> {
    if cancel.load(Ordering::SeqCst) {
        return Ok(json!({"status":"stopped","reason":"Task stopped before dispatch"}));
    }
    if operation == "pc.info" {
        if !args.as_object().is_some_and(|o| o.is_empty()) { return Err("Hardware summary arguments must be an empty object".into()); }
        return hardware(id, receipts, cancel);
    }
    if operation == "pc.open" {
        let raw = args["path"].as_str().ok_or("Missing open path")?;
        if raw.starts_with("https://") || raw.starts_with("http://") {
            if raw.len() > 4096 || raw.contains(['\0', '\r', '\n']) {
                return Err("Invalid URL".into());
            }
        } else {
            let path = checked_path(raw)?;
            if !in_root(&path, root) {
                return Err("Open is outside the native task folder".into());
            }
        }
        #[cfg(windows)]
        {
            use std::os::windows::ffi::OsStrExt;
            use windows_sys::Win32::UI::Shell::ShellExecuteW;
            let wide: Vec<u16> = std::ffi::OsStr::new(raw)
                .encode_wide()
                .chain(Some(0))
                .collect();
            let code = unsafe {
                ShellExecuteW(
                    std::ptr::null_mut(),
                    windows_sys::core::w!("open"),
                    wide.as_ptr(),
                    std::ptr::null(),
                    std::ptr::null(),
                    1,
                )
            } as usize;
            if code <= 32 {
                return Err("Windows could not open this result".into());
            }
            return Ok(
                json!({"status":"done","detail":"Windows accepted the default-app open request; app completion is not asserted."}),
            );
        }
        #[cfg(not(windows))]
        {
            return Err("PC opens require Windows".into());
        }
    }
    let raw = args[if operation == "pc.run" { "cwd" } else { "path" }]
        .as_str()
        .ok_or("Missing Windows path")?;
    let path = checked_path(raw)?;
    if !in_root(&path, root) {
        return Err("Path is outside this agent's native task folder".into());
    }
    if operation == "pc.run" {
        return run(id, args, &path, receipts, cancel);
    }
    let parent = if operation == "pc.list" {
        path.as_path()
    } else {
        path.parent().ok_or("File has no parent")?
    };
    let _locks = guards(parent, root)?;
    // Resolve again after locking the ancestor directories.
    if checked_path(raw)? != path {
        return Err("Path changed during inspection".into());
    }
    match operation {
        "pc.read" => {
            let data = read_file(&path)?;
            Ok(
                json!({"status":"done","content":String::from_utf8_lossy(&data),"sha256":digest(&data),"bytes":data.len()}),
            )
        }
        "pc.list" => {
            let mut entries = vec![];
            for entry in fs::read_dir(&path)
                .map_err(|_| "Directory cannot be listed")?
                .take(1001)
            {
                let entry = entry.map_err(|_| "Directory changed while listing")?;
                let kind = entry.file_type().map_err(|_| "Cannot read entry type")?;
                entries.push(json!({"name":entry.file_name().to_string_lossy(),"directory":kind.is_dir(),"symlink":kind.is_symlink()}));
            }
            let truncated = entries.len() > 1000;
            entries.truncate(1000);
            Ok(json!({"status":"done","entries":entries,"truncated":truncated}))
        }
        "pc.write" => {
            let content = args["content"].as_str().ok_or("Missing file content")?;
            if content.len() > 128 * 1024 {
                return Err("PC write exceeds 128 KiB".into());
            }
            if path.exists() {
                let existing = read_file(&path)?;
                if args["preimage_sha256"].as_str() != Some(digest(&existing).as_str()) {
                    return Err("preimage_conflict: read the current file and request a fresh native approval".into());
                }
            } else if !args["preimage_sha256"].is_null() {
                return Err("preimage_conflict: expected file is absent".into());
            }
            // Parent creation stays inside the already reviewed task root.
            fs::create_dir_all(parent).map_err(|_| "Cannot create parent directories")?;
            let _new_locks = guards(parent, root)?;
            if checked_path(raw)? != path {
                return Err("Parent changed during atomic-write preparation".into());
            }
            let temp = parent.join(format!(".bob-pc-{}.tmp", uuid::Uuid::new_v4()));
            let written = (|| {
                let mut file = OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .open(&temp)
                    .map_err(|_| "Cannot create atomic write")?;
                file.write_all(content.as_bytes())
                    .and_then(|_| file.sync_all())
                    .map_err(|_| "Cannot persist atomic write")?;
                if cancel.load(Ordering::SeqCst) {
                    return Err("Task stopped before replacement".into());
                }
                replace(&temp, &path)
            })();
            if written.is_err() {
                let _ = fs::remove_file(&temp);
            }
            written?;
            Ok(json!({"status":"done","sha256":digest(content.as_bytes()),"bytes":content.len()}))
        }
        _ => Err("Unsupported PC operation".into()),
    }
}
// Normalize only the execution copy, after policy/approval have inspected the request.
#[derive(serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct HardwareSummary {
    processors: Vec<Processor>, memory: HardwareMemory, graphics: Vec<Graphics>, disks: Vec<Disk>, windows: WindowsVersion,
}
#[derive(serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Processor { name: String, cores: u32, threads: u32 }
#[derive(serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct HardwareMemory { total_bytes: u64, free_bytes: u64 }
#[derive(serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Graphics { name: String, driver_version: String, dedicated_bytes: Option<u64>, used_bytes: Option<u64> }
#[derive(serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Disk { drive: String, total_bytes: u64, free_bytes: u64 }
#[derive(serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct WindowsVersion { version: String, build: String, edition: String }
fn hardware_result(text: &str) -> Result<Value, String> {
    let data: HardwareSummary = serde_json::from_str(text.trim_start_matches('\u{feff}').trim()).map_err(|_| "Windows hardware summary was incomplete")?;
    if data.processors.is_empty() || data.memory.total_bytes == 0 || data.windows.build.is_empty() {
        return Err("Windows hardware summary was incomplete".into());
    }
    let gib = |v: u64| format!("{:.1} GiB", v as f64 / 1073741824.0);
    let cpu = data.processors.iter().map(|p| format!("{} ({} cores / {} threads)", p.name, p.cores, p.threads)).collect::<Vec<_>>().join("; ");
    let gpu = data.graphics.iter().map(|g| format!("{} · {} dedicated · driver {}", g.name, g.dedicated_bytes.map(gib).unwrap_or("memory unavailable".into()), g.driver_version)).collect::<Vec<_>>().join("; ");
    let disks = data.disks.iter().map(|d| format!("{} {} free / {} total", d.drive, gib(d.free_bytes), gib(d.total_bytes))).collect::<Vec<_>>().join("; ");
    let summary = format!("Processor: {cpu}\nMemory: {} free / {} total\nGraphics: {gpu}\nDisks: {disks}\n{} · version {} · build {}", gib(data.memory.free_bytes), gib(data.memory.total_bytes), data.windows.edition, data.windows.version, data.windows.build);
    Ok(json!({"status":"done","hardware":data,"summary":summary,"stdout":summary,"exit_code":0}))
}
fn hardware(id: &str, receipts: &Path, cancel: Arc<AtomicBool>) -> Result<Value, String> {
    #[cfg(not(windows))]
    { let _ = (id, receipts, cancel); Err("PC hardware summaries require Windows".into()) }
    #[cfg(windows)]
    {
        // No request data enters the command, and the same job/timeout/cancel containment is used as pc.run.
        let system32 = PathBuf::from(std::env::var_os("SystemRoot").ok_or("Windows directory unavailable")?).join("System32");
        let result = run(id, &json!({"shell":"powershell","argv":["-Command",include_str!("pc_hardware.ps1")],"timeout_s":20}), &system32, receipts, cancel)?;
        if result["status"] != "done" {
            return Ok(json!({"status":result["status"],"reason":"Hardware summary could not complete within its native execution limits","exit_code":result["exit_code"]}));
        }
        let path = receipts.join(format!("{id}.stdout"));
        let mut bytes = Vec::new();
        File::open(path).map_err(|_| "Hardware receipt unavailable")?.take(128 * 1024).read_to_end(&mut bytes).map_err(|_| "Hardware receipt unavailable")?;
        hardware_result(&String::from_utf8_lossy(&bytes))
    }
}

fn shell_argv<'a>(shell: &str, argv: &'a [&'a str]) -> Result<&'a [&'a str], String> {
    let name = argv.first().and_then(|arg| arg.rsplit(['\\', '/']).next());
    let names: &[&str] = match shell {
        "powershell" => &["powershell", "powershell.exe", "pwsh", "pwsh.exe"],
        "cmd" => &["cmd", "cmd.exe"],
        _ => return Err("Choose PowerShell or cmd".into()),
    };
    let repeated =
        name.is_some_and(|name| names.iter().any(|shell| name.eq_ignore_ascii_case(shell)));
    let argv = if repeated { &argv[1..] } else { argv };
    if argv.is_empty() {
        return Err("Command argv is empty after removing the repeated shell name".into());
    }
    Ok(argv)
}

fn run(
    id: &str,
    args: &Value,
    cwd: &Path,
    receipts: &Path,
    cancel: Arc<AtomicBool>,
) -> Result<Value, String> {
    use std::process::{Command, Stdio};
    let shell = args["shell"].as_str().ok_or("Choose PowerShell or cmd")?;
    let argv: Vec<&str> = args["argv"]
        .as_array()
        .filter(|a| !a.is_empty() && a.len() <= 64)
        .ok_or("Invalid command argv")?
        .iter()
        .map(|v| {
            v.as_str()
                .filter(|s| !s.contains('\0') && s.len() <= 8192)
                .ok_or("Invalid command argument")
        })
        .collect::<Result<_, _>>()?;
    let argv = shell_argv(shell, &argv)?;
    let timeout = args["timeout_s"]
        .as_u64()
        .filter(|v| *v >= 1 && *v <= 600)
        .ok_or("Choose a timeout from 1 to 600 seconds")?;
    #[cfg(windows)]
    let program = PathBuf::from(
        std::env::var_os("SystemRoot").ok_or("Windows directory unavailable")?,
    )
    .join(match shell {
        "powershell" => r"System32\WindowsPowerShell\v1.0\powershell.exe",
        // cmd also parses its executable command line: a forward slash in
        // argv[0] changes its interpretation even though CreateProcess accepts it.
        "cmd" => r"System32\cmd.exe",
        _ => return Err("Choose PowerShell or cmd".into()),
    });
    #[cfg(not(windows))]
    let program = {
        let _ = shell;
        return Err("PC commands require Windows".into());
        #[allow(unreachable_code)]
        PathBuf::new()
    };
    fs::create_dir_all(receipts).map_err(|_| "Cannot create native command receipt directory")?;
    let stdout = receipts.join(format!("{id}.stdout"));
    let stderr = receipts.join(format!("{id}.stderr"));
    let mut command = Command::new(program);
    command
        .current_dir(cwd)
        .stdin(Stdio::null())
        .stdout(File::create(&stdout).map_err(|_| "Cannot capture stdout")?)
        .stderr(File::create(&stderr).map_err(|_| "Cannot capture stderr")?);
    if shell == "powershell" {
        command.args(["-NoLogo", "-NoProfile", "-NonInteractive"]);
    } else {
        command.arg("/d");
    }
    command.args(argv);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let mut child = command
        .spawn()
        .map_err(|_| "Windows command could not start")?;
    #[cfg(windows)]
    let job = match crate::windows::owned_job(&child) {
        Ok(job) => job,
        Err(_) => {
            let _ = child.kill();
            let _ = child.wait();
            return Err("Windows command containment unavailable".into());
        }
    };
    let deadline = Instant::now() + Duration::from_secs(timeout);
    let (status, code, reason) = loop {
        if cancel.load(Ordering::SeqCst) || Instant::now() >= deadline {
            let stopped = cancel.load(Ordering::SeqCst);
            #[cfg(windows)]
            drop(job);
            let _ = child.kill();
            let reaped = child.wait().is_ok();
            break (
                if !reaped {
                    "unknown"
                } else if stopped {
                    "stopped"
                } else {
                    "failed"
                },
                None,
                if stopped {
                    "Task stopped"
                } else {
                    "Command timeout"
                },
            );
        }
        match child.try_wait() {
            Ok(Some(status)) => {
                break (
                    if status.success() { "done" } else { "failed" },
                    status.code(),
                    if status.success() {
                        "Command exited successfully"
                    } else {
                        "Command exited with an error"
                    },
                )
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(100)),
            Err(_) => {
                break (
                    "unknown",
                    None,
                    "Windows could not establish command completion",
                )
            }
        }
    };
    let preview = |p: &Path| {
        let mut bytes = vec![];
        if let Ok(file) = File::open(p) {
            let _ = file.take(8192).read_to_end(&mut bytes);
        }
        String::from_utf8_lossy(&bytes).into_owned()
    };
    Ok(
        json!({"status":status,"exit_code":code,"reason":reason,"stdout":preview(&stdout),"stderr":preview(&stderr),
        "full_stdout_receipt":stdout,"full_stderr_receipt":stderr,"display_limit_bytes":8192}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn hardware_summary_shape_rejects_forbidden_fields_and_empty_args_only() {
        let fixture = json!({"processors":[{"name":"Fixture CPU","cores":8,"threads":16}],
            "memory":{"total_bytes":34359738368u64,"free_bytes":17179869184u64},
            "graphics":[{"name":"Fixture GPU","driver_version":"1.2","dedicated_bytes":25769803776u64,"used_bytes":1073741824u64}],
            "disks":[{"drive":"C:","total_bytes":1099511627776u64,"free_bytes":549755813888u64}],
            "windows":{"version":"10.0","build":"26100","edition":"Windows 11"}});
        let result = hardware_result(&fixture.to_string()).unwrap();
        assert_eq!(result["status"], "done");
        assert_eq!(result["hardware"], fixture);
        assert!(result["summary"].as_str().unwrap().contains("24.0 GiB dedicated"));
        for forbidden in ["user_name","computer_name","serial_number","network_identifiers","installed_software"] {
            assert!(!result.to_string().contains(forbidden));
            let mut bad = fixture.clone(); bad[forbidden] = json!("forbidden");
            assert!(hardware_result(&bad.to_string()).is_err());
            let mut bad = fixture.clone(); bad["graphics"][0][forbidden] = json!("forbidden");
            assert!(hardware_result(&bad.to_string()).is_err());
        }
        let script = include_str!("pc_hardware.ps1");
        assert!(script.contains("HardwareInformation.qwMemorySize"));
        assert!(script.contains("--query-gpu=name,memory.total,memory.used,driver_version"));
        for forbidden in ["AdapterRAM", "SerialNumber", "UserName", "ComputerName", "Win32_Product", "Win32_Network"] { assert!(!script.contains(forbidden)); }
        let result = execute("info-invalid", "pc.info", &json!({"command":"anything"}), Path::new(""), Path::new(""), Arc::new(AtomicBool::new(false)));
        assert_eq!(result["status"], "failed");
        assert!(result["reason"].as_str().unwrap().contains("empty object"));
    }
    #[test]
    fn atomic_write_readback_preimage_and_scope() {
        let root = std::env::temp_dir().join(format!("bob-pc-files-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&root).unwrap();
        let canonical = fs::canonicalize(&root).unwrap();
        let path = root.join("nested/hello.txt");
        let cancel = Arc::new(AtomicBool::new(false));
        let first = json!({"path":path,"content":"first","preimage_sha256":null});
        let result = execute(
            "test",
            "pc.write",
            &first,
            &canonical,
            &root,
            cancel.clone(),
        );
        assert_eq!(result["status"], "done", "{result}");
        let read = execute(
            "read",
            "pc.read",
            &json!({"path":path}),
            &canonical,
            &root,
            cancel.clone(),
        );
        assert_eq!(read["content"], "first");
        let conflict = execute(
            "edit",
            "pc.write",
            &json!({"path":path,"content":"bad","preimage_sha256":null}),
            &canonical,
            &root,
            cancel.clone(),
        );
        assert_eq!(conflict["status"], "failed");
        let edited = execute(
            "edit",
            "pc.write",
            &json!({"path":path,"content":"second","preimage_sha256":read["sha256"]}),
            &canonical,
            &root,
            cancel.clone(),
        );
        assert_eq!(edited["status"], "done", "{edited}");
        assert_eq!(fs::read_to_string(path).unwrap(), "second");
        let outside = execute(
            "outside",
            "pc.read",
            &json!({"path":std::env::temp_dir().join("outside")}),
            &canonical,
            &root,
            cancel,
        );
        assert_eq!(outside["status"], "failed");
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn command_argv_skips_only_one_matching_shell_name() {
        for name in [
            "powershell",
            "PowerShell.EXE",
            "pwsh",
            "PWSH.exe",
            r"C:\Program Files\PowerShell\7\pwsh.exe",
            "C:/Windows/PowerShell.EXE",
        ] {
            let argv = [name, "-NoProfile", "-Command", "$c=1; Write-Output $c"];
            assert_eq!(shell_argv("powershell", &argv).unwrap(), &argv[1..]);
        }
        for name in [
            "cmd",
            "CMD.EXE",
            r"C:\Windows\System32\cmd.exe",
            "C:/Windows/CmD",
        ] {
            let argv = [name, "/C", "echo fixture"];
            assert_eq!(shell_argv("cmd", &argv).unwrap(), &argv[1..]);
        }
        for (shell, argv) in [
            ("powershell", vec!["-Command", "$c=1"]),
            ("powershell", vec!["cmd", "/C", "echo fixture"]),
            ("cmd", vec!["powershell", "-Command", "$c=1"]),
            ("cmd", vec!["mycmd.exe", "/C", "echo fixture"]),
            ("powershell", vec!["pwsh.exe -Command", "echo fixture"]),
        ] {
            assert_eq!(shell_argv(shell, &argv).unwrap(), argv.as_slice());
        }
        let twice = ["powershell", "pwsh", "-Command", "$c=1"];
        assert_eq!(shell_argv("powershell", &twice).unwrap(), &twice[1..]);
        for (shell, argv) in [
            ("powershell", vec!["pwsh"]),
            ("cmd", vec!["CMD.EXE"]),
            ("cmd", vec![]),
        ] {
            assert!(shell_argv(shell, &argv).unwrap_err().contains("empty"));
        }
    }

    #[cfg(windows)]
    #[test]
    #[ignore = "Live hardware integration requires Windows CIM/WMI access; restricted sandboxes deny it"]
    fn windows_hardware_summary_uses_contained_fixed_command() {
        let root = std::env::temp_dir().join(format!("bob-pc-hardware-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&root).unwrap();
        let result = execute("hardware", "pc.info", &json!({}), Path::new(""), &root, Arc::new(AtomicBool::new(false)));
        assert_eq!(result["status"], "done", "{result}");
        assert!(result["hardware"]["processors"].as_array().is_some_and(|p| !p.is_empty()));
        assert!(result["hardware"]["memory"]["total_bytes"].as_u64().unwrap() > 0);
        assert!(result["hardware"]["graphics"].is_array());
        assert!(result["hardware"]["disks"].is_array());
        assert!(!result["hardware"]["windows"]["build"].as_str().unwrap().is_empty());
        fs::remove_dir_all(root).unwrap();
    }
    #[cfg(windows)]
    #[test]
    fn windows_repeated_shell_keeps_script_variables_literal() {
        let root = std::env::temp_dir().join(format!("bob-pc-argv-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&root).unwrap();
        let canonical = fs::canonicalize(&root).unwrap();
        for (id, shell, argv, expected) in [
            (
                "ps",
                "powershell",
                vec![
                    "PowerShell.EXE",
                    "-NoProfile",
                    "-Command",
                    "$c=41; Write-Output ($c + 1)",
                ],
                "42",
            ),
            (
                "cmd",
                "cmd",
                vec![r"C:\Windows\System32\CMD.EXE", "/C", "echo fixture"],
                "fixture",
            ),
        ] {
            let result = execute(
                id,
                "pc.run",
                &json!({"shell":shell,"argv":argv,"cwd":root,"timeout_s":10}),
                &canonical,
                &root,
                Arc::new(AtomicBool::new(false)),
            );
            assert_eq!(result["status"], "done", "{result}");
            assert_eq!(result["stdout"].as_str().unwrap().trim(), expected);
        }
        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(windows)]
    #[test]
    fn windows_commands_keep_full_output_nonzero_timeout_and_stop_distinct() {
        let root = std::env::temp_dir().join(format!("bob-pc-command-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&root).unwrap();
        let canonical = fs::canonicalize(&root).unwrap();
        let args = |script: &str, timeout: u64| json!({"shell":"powershell","argv":["-Command",script],"cwd":root,"timeout_s":timeout});
        let result = execute(
            "success",
            "pc.run",
            &args("Write-Output ('x' * 12000)", 10),
            &canonical,
            &root,
            Arc::new(AtomicBool::new(false)),
        );
        assert_eq!(result["status"], "done", "{result}");
        assert_eq!(result["exit_code"], 0);
        assert!(result["stdout"].as_str().unwrap().len() <= 8192);
        assert!(fs::metadata(root.join("success.stdout")).unwrap().len() > 12000);
        let failed = execute(
            "failed",
            "pc.run",
            &args("exit 7", 10),
            &canonical,
            &root,
            Arc::new(AtomicBool::new(false)),
        );
        assert_eq!(failed["status"], "failed");
        assert_eq!(failed["exit_code"], 7);
        let cmd = execute(
            "cmd-failed",
            "pc.run",
            &json!({"shell":"cmd","argv":["/C","exit","7"],"cwd":root,"timeout_s":10}),
            &canonical,
            &root,
            Arc::new(AtomicBool::new(false)),
        );
        assert_eq!(cmd["status"], "failed", "{cmd}");
        assert_eq!(cmd["exit_code"], 7, "{cmd}");
        let timed = execute(
            "timed",
            "pc.run",
            &args("Start-Sleep -Seconds 5", 1),
            &canonical,
            &root,
            Arc::new(AtomicBool::new(false)),
        );
        assert_eq!(timed["status"], "failed");
        assert_eq!(timed["reason"], "Command timeout");
        let cancel = Arc::new(AtomicBool::new(false));
        let trigger = cancel.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(300));
            trigger.store(true, Ordering::SeqCst);
        });
        let stopped = execute(
            "stopped",
            "pc.run",
            &args("Start-Sleep -Seconds 5", 10),
            &canonical,
            &root,
            cancel,
        );
        assert_eq!(stopped["status"], "stopped");
        fs::remove_dir_all(root).unwrap();
    }
}
