//! Owned process-group supervision for the portal; this is not an OS sandbox.
use std::{
    io,
    os::unix::process::CommandExt,
    process::{Child, Command},
    sync::mpsc,
};

pub fn configure(command: &mut Command) {
    command.process_group(0);
    #[cfg(target_os = "linux")]
    {
        let parent = std::process::id() as i32;
        // Only async-signal-safe libc calls after fork. The dedicated spawn
        // thread below stays alive for the whole transport lifetime: Linux's
        // parent-death notification is tied to that thread, not just its PID.
        unsafe {
            command.pre_exec(move || {
                if prctl(1, 9usize, 0usize, 0usize, 0usize) != 0 {
                    return Err(io::Error::last_os_error());
                }
                if getppid() != parent {
                    return Err(io::Error::from_raw_os_error(10)); // Linux ECHILD
                }
                Ok(())
            });
        }
    }
}

/// Never expose wait/try_wait: the unreaped child reserves the group leader PID
/// until the sole cleanup operation has signalled the group. The Link mutex
/// serializes this operation. Only use with a child spawned by configure().
pub struct OwnedChild {
    child: Option<Child>,
    keep_alive: Option<mpsc::SyncSender<()>>,
}
impl OwnedChild {
    pub fn spawn(mut command: Command) -> io::Result<Self> {
        let (ready_tx, ready_rx) = mpsc::sync_channel(1);
        let (stop_tx, stop_rx) = mpsc::sync_channel(0);
        std::thread::Builder::new()
            .name("portal-child-owner".into())
            .spawn(move || {
                configure(&mut command);
                match command.spawn() {
                    Ok(child) => match ready_tx.send(Ok(child)) {
                        Ok(()) => {
                            let _ = stop_rx.recv();
                        }
                        Err(error) => {
                            if let Ok(mut child) = error.0 {
                                unsafe {
                                    kill(-(child.id() as i32), 9);
                                }
                                let _ = child.wait();
                            }
                        }
                    },
                    Err(error) => {
                        let _ = ready_tx.send(Err(error));
                    }
                }
            })?;
        let child = ready_rx
            .recv()
            .map_err(|_| io::Error::from(io::ErrorKind::BrokenPipe))??;
        Ok(Self {
            child: Some(child),
            keep_alive: Some(stop_tx),
        })
    }
    #[cfg(test)]
    pub fn new(child: Child) -> Self {
        Self {
            child: Some(child),
            keep_alive: None,
        }
    }
    #[cfg(test)]
    pub fn id(&self) -> Option<u32> {
        self.child.as_ref().map(Child::id)
    }
    pub fn take_stdin(&mut self) -> Option<std::process::ChildStdin> {
        self.child.as_mut().expect("owned child").stdin.take()
    }
    pub fn take_stdout(&mut self) -> Option<std::process::ChildStdout> {
        self.child.as_mut().expect("owned child").stdout.take()
    }
    pub fn cleanup(&mut self) {
        if let Some(mut child) = self.child.take() {
            // configure() made this child its own group leader. No wait or
            // try_wait has occurred, so this ID cannot have been reused.
            unsafe {
                kill(-(child.id() as i32), 9);
            }
            let _ = child.wait();
        }
        // Releasing the owner thread comes AFTER signalling and reaping.
        self.keep_alive.take();
    }
}
extern "C" {
    fn kill(pid: i32, signal: i32) -> i32;
    #[cfg(target_os = "linux")]
    fn prctl(option: i32, ...) -> i32;
    #[cfg(target_os = "linux")]
    fn getppid() -> i32;
}
impl Drop for OwnedChild {
    fn drop(&mut self) {
        self.cleanup();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        io::{BufRead, BufReader},
        process::{Command, Stdio},
        time::Duration,
    };

    #[test]
    fn cleanup_kills_owned_child_and_grandchild_but_preserves_other_child() {
        let mut other = Command::new("sleep").arg("30").spawn().unwrap();
        let mut command = Command::new("python3");
        command.args(["-c", "import subprocess,time; p=subprocess.Popen(['sleep','30']); print(p.pid,flush=True); time.sleep(30)"])
            .stdout(Stdio::piped());
        configure(&mut command);
        let mut owned = OwnedChild::new(command.spawn().unwrap());
        let mut line = String::new();
        BufReader::new(owned.take_stdout().unwrap())
            .read_line(&mut line)
            .unwrap();
        let grandchild: u32 = line.trim().parse().unwrap();
        owned.cleanup();
        owned.cleanup();
        for _ in 0..100 {
            let status = std::fs::read_to_string(format!("/proc/{grandchild}/stat"));
            if status
                .as_ref()
                .map(|s| s.split_whitespace().nth(2) == Some("Z"))
                .unwrap_or(true)
            {
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        if let Ok(status) = std::fs::read_to_string(format!("/proc/{grandchild}/stat")) {
            assert_eq!(
                status.split_whitespace().nth(2),
                Some("Z"),
                "grandchild still running"
            );
        }
        assert!(
            other.try_wait().unwrap().is_none(),
            "unrelated child was killed"
        );
        other.kill().unwrap();
        other.wait().unwrap();
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn parent_death_probe() {
        if std::env::var_os("BOB_PORTAL_PARENT_DEATH_PROBE").is_none() {
            return;
        }
        let mut command = Command::new("sleep");
        command.arg("30");
        let owned = OwnedChild::spawn(command).unwrap();
        println!("OWNED_TRANSPORT:{}", owned.id().unwrap());
        let _owned = owned;
        loop {
            std::thread::park();
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn abrupt_parent_death_ends_direct_owned_transport() {
        let mut parent = Command::new(std::env::current_exe().unwrap())
            .args(["parent_death_probe", "--nocapture", "--test-threads=1"])
            .env("BOB_PORTAL_PARENT_DEATH_PROBE", "1")
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let mut lines = BufReader::new(parent.stdout.take().unwrap()).lines();
        let mut pid = None;
        for _ in 0..32 {
            let Some(Ok(line)) = lines.next() else {
                break;
            };
            if let Some((_, value)) = line.split_once("OWNED_TRANSPORT:") {
                pid = Some(value.trim().parse::<u32>().unwrap());
                break;
            }
        }
        let pid = pid.expect("probe must publish its actual child");
        parent.kill().unwrap();
        parent.wait().unwrap();
        let mut stopped = false;
        for _ in 0..100 {
            let status = std::fs::read_to_string(format!("/proc/{pid}/stat"));
            if status
                .as_ref()
                .map(|s| s.split_whitespace().nth(2) == Some("Z"))
                .unwrap_or(true)
            {
                stopped = true;
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        if !stopped {
            unsafe {
                kill(pid as i32, 9);
            }
        }
        assert!(
            stopped,
            "direct transport survived abrupt application death"
        );
    }

    #[test]
    fn transport_survives_the_short_lived_request_thread() {
        let mut owned = std::thread::spawn(|| {
            let mut command = Command::new("sleep");
            command.arg("30");
            OwnedChild::spawn(command).unwrap()
        })
        .join()
        .unwrap();
        std::thread::sleep(Duration::from_millis(50));
        let pid = owned.id().unwrap();
        let status = std::fs::read_to_string(format!("/proc/{pid}/stat")).unwrap();
        assert_ne!(
            status.split_whitespace().nth(2),
            Some("Z"),
            "IPC thread exit killed the live transport"
        );
        owned.cleanup();
    }

    #[test]
    fn final_drop_reaps_owned_child() {
        let mut command = Command::new("sleep");
        command.arg("30");
        configure(&mut command);
        let child = command.spawn().unwrap();
        let pid = child.id();
        drop(OwnedChild::new(child));
        assert!(!std::path::Path::new(&format!("/proc/{pid}")).exists());
    }
}
