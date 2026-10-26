//! Pinned launch handshake and child/group ownership primitives.
use super::{
    procfs::{self, Identity},
    protocol::fault,
};
use serde::Deserialize;
use std::{
    collections::BTreeMap,
    ffi::CString,
    fs, io,
    os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd},
};
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Launch {
    pub argv: Vec<String>,
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    pub cwd: Option<String>,
    pub directory: String,
    pub lease_seconds: f64,
    pub grace_seconds: f64,
}
pub(super) struct Child {
    pub pid: i32,
    pub identity: Identity,
    pub out: OwnedFd,
    pub err: OwnedFd,
    pub exec: OwnedFd,
    pub reaped: bool,
}
impl Drop for Child {
    fn drop(&mut self) {
        if !self.reaped {
            unsafe {
                libc::kill(-self.pid, libc::SIGKILL);
                libc::kill(self.pid, libc::SIGKILL);
                libc::waitpid(self.pid, std::ptr::null_mut(), 0);
            }
        }
    }
}
fn pipe() -> io::Result<(OwnedFd, OwnedFd)> {
    let mut p = [0; 2];
    if unsafe { libc::pipe2(p.as_mut_ptr(), libc::O_CLOEXEC) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(unsafe { (OwnedFd::from_raw_fd(p[0]), OwnedFd::from_raw_fd(p[1])) })
}
pub(super) fn nonblock(fd: RawFd) -> io::Result<()> {
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}
fn cstr(s: &str) -> io::Result<CString> {
    CString::new(s).map_err(|_| fault("protocol", "NUL in launch argument"))
}
pub(super) fn spawn(spec: &Launch) -> io::Result<Child> {
    if spec.argv.is_empty() || spec.argv[0].is_empty() {
        return Err(fault("protocol", "empty executable"));
    }
    let args: Vec<CString> = spec
        .argv
        .iter()
        .map(|s| cstr(s))
        .collect::<io::Result<_>>()?;
    let argv: Vec<_> = args
        .iter()
        .map(|s| s.as_ptr())
        .chain(std::iter::once(std::ptr::null()))
        .collect();
    let mut env: BTreeMap<String, String> = std::env::vars().collect();
    for (name, value) in &spec.env {
        if name.is_empty() || name.contains('=') || name.contains('\0') {
            return Err(fault("protocol", "invalid environment name"));
        }
        env.insert(name.clone(), value.clone());
    }
    let candidates: Vec<CString> = if spec.argv[0].contains('/') {
        vec![cstr(&spec.argv[0])?]
    } else {
        env.get("PATH")
            .map(String::as_str)
            .unwrap_or("/usr/bin:/bin")
            .split(':')
            .map(|p| {
                cstr(&format!(
                    "{}/{}",
                    if p.is_empty() { "." } else { p },
                    spec.argv[0]
                ))
            })
            .collect::<io::Result<_>>()?
    };
    let environment: Vec<CString> = env
        .iter()
        .map(|(k, v)| cstr(&format!("{k}={v}")))
        .collect::<io::Result<_>>()?;
    let envp: Vec<_> = environment
        .iter()
        .map(|s| s.as_ptr())
        .chain(std::iter::once(std::ptr::null()))
        .collect();
    let cwd = spec.cwd.as_ref().map(|s| cstr(s)).transpose()?;
    let (out_r, out_w) = pipe()?;
    let (err_r, err_w) = pipe()?;
    let (gate_r, gate_w) = pipe()?;
    let (exec_r, exec_w) = pipe()?;
    // No other threads exist in this executable. The child uses only libc until exec/_exit.
    let pid = unsafe { libc::fork() };
    if pid < 0 {
        return Err(io::Error::last_os_error());
    }
    if pid == 0 {
        unsafe {
            let failure = || {
                let errno = *libc::__errno_location();
                libc::write(exec_w.as_raw_fd(), (&errno as *const i32).cast(), 4);
                libc::_exit(127)
            };
            libc::close(out_r.as_raw_fd());
            libc::close(err_r.as_raw_fd());
            libc::close(gate_w.as_raw_fd());
            libc::close(exec_r.as_raw_fd());
            if libc::setsid() < 0 {
                failure();
            }
            if libc::dup2(out_w.as_raw_fd(), 1) < 0 || libc::dup2(err_w.as_raw_fd(), 2) < 0 {
                failure();
            }
            let null = c"/dev/null";
            let fd = libc::open(null.as_ptr(), libc::O_RDONLY);
            if fd < 0 || libc::dup2(fd, 0) < 0 {
                failure();
            }
            libc::close(fd);
            let mut token = 0u8;
            if libc::read(gate_r.as_raw_fd(), (&mut token as *mut u8).cast(), 1) != 1 {
                libc::_exit(125);
            }
            if let Some(path) = &cwd
                && libc::chdir(path.as_ptr()) < 0
            {
                failure();
            }
            let mut denied = false;
            for path in &candidates {
                libc::execve(path.as_ptr(), argv.as_ptr(), envp.as_ptr());
                let e = *libc::__errno_location();
                if e == libc::EACCES {
                    denied = true;
                } else if e != libc::ENOENT && e != libc::ENOTDIR {
                    failure();
                }
            }
            *libc::__errno_location() = if denied { libc::EACCES } else { libc::ENOENT };
            failure();
        }
    }
    drop(out_w);
    drop(err_w);
    drop(gate_r);
    drop(exec_w);
    let identity = match procfs::identity(pid) {
        Ok(value) => value,
        Err(e) => {
            unsafe {
                libc::kill(pid, libc::SIGKILL);
                libc::waitpid(pid, std::ptr::null_mut(), 0);
            }
            return Err(e);
        }
    };
    let child = Child {
        pid,
        identity,
        out: out_r,
        err: err_r,
        exec: exec_r,
        reaped: false,
    };
    if unsafe { libc::write(gate_w.as_raw_fd(), b"x".as_ptr().cast(), 1) } != 1 {
        return Err(io::Error::last_os_error());
    }
    for fd in [
        child.out.as_raw_fd(),
        child.err.as_raw_fd(),
        child.exec.as_raw_fd(),
    ] {
        nonblock(fd)?;
    }
    Ok(child)
}
pub fn pidfd_available() -> bool {
    let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, std::process::id(), 0) };
    if fd >= 0 {
        unsafe {
            libc::close(fd as i32);
        }
        true
    } else {
        false
    }
}
pub(super) fn exit_code(pid: i32) -> io::Result<Option<i32>> {
    let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
    if unsafe {
        libc::waitid(
            libc::P_PID,
            pid as u32,
            &mut info,
            libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
        )
    } < 0
    {
        return Err(io::Error::last_os_error());
    }
    if unsafe { info.si_pid() } == 0 {
        return Ok(None);
    }
    Ok(Some(if info.si_code == libc::CLD_EXITED {
        unsafe { info.si_status() }
    } else {
        128 + unsafe { info.si_status() }
    }))
}
pub(super) fn signal_group(pid: i32, signal: i32) -> io::Result<()> {
    if unsafe { libc::kill(-pid, signal) } < 0 {
        let e = io::Error::last_os_error();
        if e.raw_os_error() != Some(libc::ESRCH) {
            return Err(e);
        }
    }
    Ok(())
}
pub(super) fn group_live(pid: i32) -> io::Result<bool> {
    for entry in fs::read_dir("/proc")? {
        let entry = entry?;
        if entry
            .file_name()
            .to_str()
            .and_then(|s| s.parse::<u32>().ok())
            .is_none()
        {
            continue;
        }
        let text = match fs::read_to_string(entry.path().join("stat")) {
            Ok(s) => s,
            Err(e) if matches!(e.raw_os_error(), Some(libc::ENOENT | libc::ESRCH)) => continue,
            Err(e) => return Err(e),
        };
        let end = text
            .rfind(')')
            .ok_or_else(|| fault("malformed", "invalid group stat"))?;
        let fields: Vec<_> = text[end + 1..].split_whitespace().collect();
        if fields.get(2).and_then(|s| s.parse::<i32>().ok()) == Some(pid)
            && !matches!(fields.first(), Some(&"Z" | &"X"))
        {
            return Ok(true);
        }
    }
    Ok(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn waitid_fallback_reserves_leader_identity() {
        let mut child = std::process::Command::new("/bin/sh")
            .args(["-c", "exit 7"])
            .spawn()
            .unwrap();
        let pid = child.id() as i32;
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
        loop {
            if let Some(code) = exit_code(pid).unwrap() {
                assert_eq!(code, 7);
                break;
            }
            assert!(std::time::Instant::now() < deadline);
            std::thread::sleep(std::time::Duration::from_millis(1));
        }
        assert_eq!(procfs::identity(pid).unwrap().pid, pid);
        assert_eq!(child.wait().unwrap().code(), Some(7));
        assert_eq!(
            exit_code(pid).unwrap_err().raw_os_error(),
            Some(libc::ECHILD)
        );
    }
}
