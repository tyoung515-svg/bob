//! FX-READ-01 fixture command (T3b S4 §1b): a read-only local file read,
//! spawned argv-form by the slice executor. The build host is Windows, where
//! the Linux `cat` of the tier-1 spec does not exist, so the slice
//! provisions this binary as the fixture command (EXEC-32); digests and the
//! authority chain bind the logical argv as received (MGR-1), not the host
//! path.
//!
//! Usage: `fx-read [--fx-sleep-ms=N] <path>`
//! - reads `<path>` and writes its raw bytes to stdout, exit 0;
//! - on any read error, writes a message to stderr and exits 1;
//! - `--fx-sleep-ms=N` is a fixture-only affordance for the
//!   cancel-after-witness test row (S4 §2b row 9): it sleeps before reading
//!   so the harness can fire the engine-side cancel token mid-run.

use std::io::Write;

fn main() {
    // Executor-only startup proof; emitted before argv parsing, sleep or reads.
    // The Linux supervisor strips it before hashing/publishing stdout.
    if std::env::var_os("BOB_SLICE_STARTED").as_deref() == Some(std::ffi::OsStr::new("1")) {
        let mut out = std::io::stdout();
        out.write_all(b"\x1eBOB_SLICE_PAYLOAD_V1\n")
            .expect("startup proof");
        out.flush().expect("startup proof flush");
    }
    let mut sleep_ms: u64 = 0;
    let mut path: Option<String> = None;
    for arg in std::env::args().skip(1) {
        if let Some(n) = arg.strip_prefix("--fx-sleep-ms=") {
            sleep_ms = n.parse().unwrap_or(0);
        } else if path.is_none() {
            path = Some(arg);
        } else {
            eprintln!("fx-read: unexpected extra argument '{arg}'");
            std::process::exit(2);
        }
    }
    let Some(path) = path else {
        eprintln!("fx-read: usage: fx-read [--fx-sleep-ms=N] <path>");
        std::process::exit(2);
    };
    if sleep_ms > 0 {
        std::thread::sleep(std::time::Duration::from_millis(sleep_ms));
    }
    match std::fs::read(&path) {
        Ok(bytes) => {
            let mut out = std::io::stdout();
            out.write_all(&bytes).expect("fx-read: stdout write failed");
            out.flush().expect("fx-read: stdout flush failed");
        }
        Err(e) => {
            eprintln!("fx-read: {path}: {e}");
            std::process::exit(1);
        }
    }
}
