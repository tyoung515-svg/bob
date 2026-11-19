//! Linux-only seccomp transport. Unsupported syscall ABIs fail closed.
use std::fs::File;
use std::io::{self, Seek, SeekFrom, Write};
use std::os::fd::{AsRawFd, FromRawFd};

pub(crate) fn sealed_file(name: &std::ffi::CStr, bytes: &[u8]) -> io::Result<File> {
    // The fd is owned immediately; all later error paths close it.
    let fd =
        unsafe { libc::memfd_create(name.as_ptr(), libc::MFD_CLOEXEC | libc::MFD_ALLOW_SEALING) };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let mut file = unsafe { File::from_raw_fd(fd) };
    file.write_all(bytes)?;
    file.seek(SeekFrom::Start(0))?;
    let seals = libc::F_SEAL_WRITE | libc::F_SEAL_GROW | libc::F_SEAL_SHRINK | libc::F_SEAL_SEAL;
    if unsafe { libc::fcntl(fd, libc::F_ADD_SEALS, seals) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(file)
}

pub(crate) fn seccomp_file() -> io::Result<File> {
    if !cfg!(target_arch = "x86_64") {
        return Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "seccomp syscall ABI not implemented",
        ));
    }
    // Classic BPF over struct seccomp_data. Check arch and reject x32, so
    // alternate ABI syscall numbers cannot bypass the native denylist.
    let mut rules: Vec<(u16, u8, u8, u32)> = vec![
        (0x20, 0, 0, 4),
        (0x15, 1, 0, 0xc000003e),
        (0x06, 0, 0, 0x80000000),
        (0x20, 0, 0, 0),
        (0x45, 0, 1, 0x40000000),
        (0x06, 0, 0, 0x00050001),
    ];
    for syscall in [101, 310, 311, 425, 426, 427] {
        rules.extend([(0x15, 0, 1, syscall), (0x06, 0, 0, 0x00050001)]);
    }
    rules.push((0x06, 0, 0, 0x7fff0000));
    let mut bytes = Vec::new();
    for (code, jt, jf, k) in rules {
        bytes.extend(code.to_ne_bytes());
        bytes.push(jt);
        bytes.push(jf);
        bytes.extend(k.to_ne_bytes());
    }
    sealed_file(c"bob-seccomp", &bytes)
}

/// Clear CLOEXEC in the forked child only. bwrap consumes these explicit
/// descriptors, closes them before the payload, and closes unrelated fds.
pub(crate) unsafe fn inherit(file: &File) -> io::Result<()> {
    if unsafe { libc::fcntl(file.as_raw_fd(), libc::F_SETFD, 0) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}
