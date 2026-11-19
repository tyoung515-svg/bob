//! Linux descriptor-relative local file capability. No model-controlled process execution.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    ffi::CString,
    fs::{File, OpenOptions},
    io::{Read, Write},
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::fs::{MetadataExt, OpenOptionsExt},
    },
    path::{Component, Path, PathBuf},
};
const MAX_FILE: u64 = 65536;
/// WSL fallback supports only ext4. Inspect the actual open descriptor, never
/// a canonicalized string or `/mnt` prefix; bind/junction aliases cannot opt in.
/// This does not claim native NTFS confinement or change ordinary Linux policy.
pub fn wsl_filesystem_guard(file: &File) -> Result<(), String> {
    let mut fs = std::mem::MaybeUninit::<libc::statfs>::uninit();
    if unsafe { libc::fstatfs(file.as_raw_fd(), fs.as_mut_ptr()) } != 0 {
        return Err("WSL filesystem identity unavailable; access refused".into());
    }
    if unsafe { fs.assume_init() }.f_type != 0xef53 {
        return Err("WSL-dependent candidate requires an ext4 project/store; Windows folders, drvfs and other filesystems are not supported".into());
    }
    Ok(())
}
#[derive(Debug, Clone, Serialize, Deserialize, ts_rs::TS)]
pub struct Project {
    pub root: PathBuf,
    #[ts(type = "number")]
    dev: u64,
    #[ts(type = "number")]
    ino: u64,
}
fn err(e: impl std::fmt::Display) -> String {
    e.to_string()
}
fn forbidden(s: &str) -> bool {
    s.starts_with('.')
        || [
            "credentials",
            "secrets",
            "id_rsa",
            "id_ed25519",
            "node_modules",
            "target",
        ]
        .contains(&s)
        || s.ends_with(".pem")
        || s.ends_with(".key")
}
fn parts(path: &str) -> Result<Vec<String>, String> {
    if std::env::var("BOB_WSL_ENGINE").as_deref() == Ok("1") {
        if path.contains(['\\', ':']) || path.starts_with("//") {
            return Err("Windows-shaped device, ADS, UNC or separator path refused in WSL candidate".into());
        }
        for name in path.split('/') {
            let stem=name.split('.').next().unwrap_or("").to_ascii_uppercase();
            let device=matches!(stem.as_str(),"CON"|"PRN"|"AUX"|"NUL"|"CLOCK$")
                || ["COM","LPT"].iter().any(|prefix|stem.strip_prefix(prefix).is_some_and(|digit|matches!(digit,"1"|"2"|"3"|"4"|"5"|"6"|"7"|"8"|"9"|"¹"|"²"|"³")));
            if device || name.ends_with(['.', ' ']) {
                return Err("Windows-shaped reserved device or trailing-dot/space path refused in WSL candidate".into());
            }
        }
    }
    let p = Path::new(path);
    let mut out = vec![];
    for c in p.components() {
        match c {
            Component::Normal(n) => {
                let n = n.to_str().ok_or("non UTF-8 path")?;
                if forbidden(n) {
                    return Err("sensitive or generated path refused".into());
                }
                out.push(n.into())
            }
            _ => return Err("absolute, dot and traversal paths refused".into()),
        }
    }
    if out.is_empty() {
        return Err("empty file path".into());
    }
    Ok(out)
}
fn at(parent: &File, name: &str, flags: i32) -> Result<File, String> {
    let n = CString::new(name).map_err(err)?;
    let fd = unsafe {
        libc::openat(
            parent.as_raw_fd(),
            n.as_ptr(),
            flags | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK,
            0o600,
        )
    };
    if fd < 0 {
        return Err(err(std::io::Error::last_os_error()));
    }
    Ok(unsafe { File::from_raw_fd(fd) })
}
impl Project {
    pub fn attach(path: &Path) -> Result<Self, String> {
        if std::fs::symlink_metadata(path)
            .map_err(err)?
            .file_type()
            .is_symlink()
        {
            return Err(
                "symlink project roots refused; select the real directory explicitly".into(),
            );
        }
        let root = path.canonicalize().map_err(err)?;
        if root.components().any(|c| match c {
            Component::Normal(n) => n.to_str().is_some_and(|n| {
                [
                    ".ssh",
                    ".gnupg",
                    ".aws",
                    ".hermes",
                    ".config",
                    ".git",
                    "credentials",
                    "secrets",
                ]
                .contains(&n)
            }),
            _ => false,
        }) {
            return Err("sensitive ancestor project root refused".into());
        }
        if root == Path::new("/")
            || std::env::var_os("HOME").is_some_and(|h| root == PathBuf::from(h))
            || ["/etc", "/proc", "/sys", "/dev", "/usr", "/run", "/var"]
                .iter()
                .any(|p| root.starts_with(p))
        {
            return Err("choose a specific private project directory".into());
        }
        // Hidden parent cache directories are permitted for disposable tests; the selected
        // root itself and tool-relative paths may not name sensitive components.
        if root
            .file_name()
            .and_then(|x| x.to_str())
            .is_none_or(forbidden)
        {
            return Err("sensitive project root refused".into());
        }
        let f = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(&root)
            .map_err(err)?;
        let m = f.metadata().map_err(err)?;
        if std::env::var("BOB_WSL_ENGINE").as_deref() == Ok("1") { wsl_filesystem_guard(&f)?; }
        Ok(Self {
            root,
            dev: m.dev(),
            ino: m.ino(),
        })
    }
    fn dir(&self) -> Result<File, String> {
        let f = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(&self.root)
            .map_err(err)?;
        let m = f.metadata().map_err(err)?;
        if std::env::var("BOB_WSL_ENGINE").as_deref() == Ok("1") { wsl_filesystem_guard(&f)?; }
        if m.dev() != self.dev || m.ino() != self.ino {
            return Err("project identity changed; reattach explicitly".into());
        }
        Ok(f)
    }
    fn parent(&self, path: &str) -> Result<(File, String), String> {
        let mut names = parts(path)?;
        let name = names.pop().unwrap();
        let mut f = self.dir()?;
        for n in names {
            f = at(&f, &n, libc::O_RDONLY | libc::O_DIRECTORY)?;
        }
        Ok((f, name))
    }
    pub fn read(&self, path: &str) -> Result<String, String> {
        let (parent, name) = self.parent(path)?;
        let mut f = at(&parent, &name, libc::O_RDONLY)?;
        let m = f.metadata().map_err(err)?;
        if !m.is_file() || m.nlink() != 1 || m.len() > MAX_FILE {
            return Err("only single-link UTF-8 regular files <=64KiB can be read".into());
        }
        let mut bytes = vec![];
        Read::by_ref(&mut f)
            .take(MAX_FILE + 1)
            .read_to_end(&mut bytes)
            .map_err(err)?;
        if bytes.len() as u64 > MAX_FILE {
            return Err("file grew past 64KiB".into());
        }
        String::from_utf8(bytes).map_err(|_| "non UTF-8 file refused".into())
    }
    pub fn before(&self, path: &str) -> Result<Option<String>, String> {
        let (parent, name) = self.parent(path)?;
        let n = CString::new(name).map_err(err)?;
        let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
        if unsafe {
            libc::fstatat(
                parent.as_raw_fd(),
                n.as_ptr(),
                stat.as_mut_ptr(),
                libc::AT_SYMLINK_NOFOLLOW,
            )
        } < 0
        {
            let e = std::io::Error::last_os_error();
            if e.raw_os_error() == Some(libc::ENOENT) {
                return Ok(None);
            }
            return Err(err(e));
        }
        self.read(path).map(Some)
    }
    pub fn write(&self, path: &str, before: Option<&str>, content: &str) -> Result<String, String> {
        if content.len() > MAX_FILE as usize {
            return Err("write content exceeds 64KiB".into());
        }
        let (parent, name) = self.parent(path)?;
        if self.before(path)?.as_deref() != before {
            return Err("stale file: preview no longer matches; request a new proposal".into());
        }
        let temp = format!(".bob-write-{}", uuid::Uuid::new_v4());
        let t = CString::new(temp.clone()).map_err(err)?;
        let n = CString::new(name).map_err(err)?;
        let result = (|| {
            let mut f = at(
                &parent,
                &temp,
                libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL,
            )?;
            f.write_all(content.as_bytes()).map_err(err)?;
            f.sync_all().map_err(err)?;
            // Validate again immediately before installing; no symlink is followed.
            if self.before(path)?.as_deref() != before {
                return Err("stale file changed during write preparation".into());
            }
            let rc = unsafe {
                if before.is_none() {
                    libc::linkat(
                        parent.as_raw_fd(),
                        t.as_ptr(),
                        parent.as_raw_fd(),
                        n.as_ptr(),
                        0,
                    )
                } else {
                    libc::renameat(
                        parent.as_raw_fd(),
                        t.as_ptr(),
                        parent.as_raw_fd(),
                        n.as_ptr(),
                    )
                }
            };
            if rc < 0 {
                return Err(err(std::io::Error::last_os_error()));
            }
            if before.is_none() {
                unsafe { libc::unlinkat(parent.as_raw_fd(), t.as_ptr(), 0) };
            }
            parent.sync_all().map_err(err)?;
            let actual = self.read(path)?;
            if actual != content {
                return Err("write verification failed: outcome requires review".into());
            }
            Ok(crate::sha256_digest(actual.as_bytes()))
        })();
        unsafe { libc::unlinkat(parent.as_raw_fd(), t.as_ptr(), 0) };
        result
    }
    pub fn list(&self) -> Result<Vec<String>, String> {
        Ok(self.scan()?.0)
    }
    pub fn list_output(&self) -> Result<Value, String> {
        let (files, skipped_depth) = self.scan()?;
        Ok(json!({"files":files,"complete":skipped_depth.is_empty(),"truncated":!skipped_depth.is_empty(),"max_depth":8,"skipped_depth":skipped_depth,"excludes":"hidden, sensitive, generated paths and symlinks"}))
    }
    fn scan(&self) -> Result<(Vec<String>, Vec<String>), String> {
        let mut out = vec![];
        let mut skipped = vec![];
        let mut budget = (0usize, std::time::Instant::now());
        Self::walk(&self.dir()?, "", &mut out, &mut skipped, &mut budget, 0)?;
        out.sort();skipped.sort();
        Ok((out, skipped))
    }
    fn walk(dir: &File, prefix: &str, out: &mut Vec<String>, skipped: &mut Vec<String>, budget: &mut (usize, std::time::Instant), depth: usize) -> Result<(), String> {
        if depth > 8 {
            skipped.push(prefix.trim_end_matches('/').to_owned());
            return Ok(());
        }
        for entry in std::fs::read_dir(format!("/proc/self/fd/{}", dir.as_raw_fd())).map_err(err)? {
            budget.0 += 1;
            if budget.0 > 4096 || budget.1.elapsed() > std::time::Duration::from_secs(2) {
                return Err("project traversal node/time budget exceeded; select a smaller project".into());
            }
            let e = entry.map_err(err)?;
            let n = e.file_name().to_string_lossy().into_owned();
            if forbidden(&n) {
                continue;
            }
            let ty = e.file_type().map_err(err)?;
            if ty.is_symlink() {
                continue;
            }
            let path = format!("{prefix}{n}");
            if ty.is_dir() {
                Self::walk(
                    &at(dir, &n, libc::O_RDONLY | libc::O_DIRECTORY)?,
                    &format!("{path}/"),
                    out,
                    skipped,
                    budget,
                    depth + 1,
                )?
            } else if ty.is_file() {
                out.push(path);
                if out.len() > 256 {
                    return Err("project file limit 256 exceeded; select a smaller project".into());
                }
            }
        }
        Ok(())
    }
    pub fn search(&self, query: &str) -> Result<Value, String> {
        if query.is_empty() || query.len() > 256 {
            return Err("search query must be 1..256 bytes".into());
        }
        let mut matches = vec![];
        let mut skipped = vec![];
        let mut bytes = 0;
        let (paths, skipped_depth) = self.scan()?;
        for path in paths {
            let text = match self.read(&path) {
                Ok(text) => text,
                Err(reason) => {
                    skipped.push(json!({"path":path,"reason":reason}));
                    continue;
                }
            };
            bytes += text.len();
            if bytes > 1048576 {
                return Err("search budget 1MiB exceeded; select a smaller project".into());
            }
            for (i, line) in text.lines().enumerate() {
                if line.contains(query) {
                    matches.push(json!({"path":path,"line":i+1,"text":line}));
                    if matches.len() > 128 {
                        return Err("search match limit 128 exceeded; refine query".into());
                    }
                }
            }
        }
        Ok(
            json!({"matches":matches,"complete":skipped_depth.is_empty()&&skipped.is_empty(),"truncated":!skipped_depth.is_empty(),"max_depth":8,"skipped_depth":skipped_depth,"skipped":skipped,"excludes":"hidden, sensitive, generated paths and symlinks"}),
        )
    }
}
