use super::protocol::{self, MAX_ITEMS, fault};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    fs::{self, File},
    io,
    os::fd::AsRawFd,
    path::{Path, PathBuf},
};
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Identity {
    pub pid: i32,
    pub start_ticks: u64,
    pub boot_id: Option<String>,
    pub name: String,
}
fn parse(pid: i32, data: &str, boot_id: Option<String>) -> io::Result<Identity> {
    let begin = data
        .find('(')
        .ok_or_else(|| fault("malformed", "stat missing name"))?;
    let end = data
        .rfind(')')
        .filter(|end| *end > begin)
        .ok_or_else(|| fault("malformed", "stat missing name end"))?;
    if data[..begin].trim().parse::<i32>().ok() != Some(pid) {
        return Err(fault("malformed", "stat PID mismatch"));
    }
    let fields: Vec<_> = data[end + 1..].split_whitespace().collect();
    let start_ticks = fields
        .get(19)
        .ok_or_else(|| fault("malformed", "stat missing start ticks"))?
        .parse()
        .map_err(|_| fault("malformed", "invalid start ticks"))?;
    Ok(Identity {
        pid,
        start_ticks,
        boot_id,
        name: data[begin + 1..end].into(),
    })
}
fn read(dir: &File, name: &str) -> io::Result<Vec<u8>> {
    // /proc/self/fd keeps all reads tied to the already opened process directory.
    binary_utils::read_bounded(
        &PathBuf::from(format!("/proc/self/fd/{}/{}", dir.as_raw_fd(), name)),
        16 * 1024 * 1024,
    )
    .map_err(|error| {
        io::Error::new(
            error.kind(),
            protocol::ContextError {
                source: error,
                context: format!(
                    "read {}/{}",
                    fs::read_link(format!("/proc/self/fd/{}", dir.as_raw_fd()))
                        .unwrap_or_default()
                        .display(),
                    name
                ),
            },
        )
    })
}
fn identity_at(root: &Path, pid: i32, boot: Option<String>) -> io::Result<Identity> {
    let dir = File::open(root.join(pid.to_string()))?;
    let data = read(&dir, "stat")?;
    parse(
        pid,
        std::str::from_utf8(&data).map_err(|_| fault("malformed", "non UTF-8 stat"))?,
        boot,
    )
}
pub fn identity(pid: i32) -> io::Result<Identity> {
    identity_at(Path::new("/proc"), pid, Some(protocol::boot()?))
}
pub fn discover(params: &Value) -> io::Result<Vec<Value>> {
    let pattern = match params.get("pattern") {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) => Some(s.as_str()),
        _ => return Err(fault("protocol", "pattern must be string or null")),
    };
    discover_at(Path::new("/proc"), pattern, Some(protocol::boot()?))
}
fn discover_at(root: &Path, pattern: Option<&str>, boot: Option<String>) -> io::Result<Vec<Value>> {
    discover_with(root, pattern, boot, read)
}
fn discover_with(
    root: &Path,
    pattern: Option<&str>,
    boot: Option<String>,
    reader: impl Fn(&File, &str) -> io::Result<Vec<u8>>,
) -> io::Result<Vec<Value>> {
    let mut pids = Vec::new();
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        if let Some(pid) = entry
            .file_name()
            .to_str()
            .and_then(|s| s.parse::<i32>().ok())
            .filter(|p| *p > 0)
        {
            pids.push(pid);
        }
    }
    pids.sort_unstable();
    let mut result = Vec::new();
    for pid in pids {
        let attempt: io::Result<Option<Value>> = (|| {
            let dir = File::open(root.join(pid.to_string()))?;
            let stat = reader(&dir, "stat")?;
            let before = parse(
                pid,
                std::str::from_utf8(&stat).map_err(|_| fault("malformed", "non UTF-8 stat"))?,
                boot.clone(),
            )?;
            if let Some(pattern) = pattern {
                let command = reader(&dir, "cmdline")?;
                if !String::from_utf8_lossy(&command).contains(pattern) {
                    return Ok(None);
                }
                let stat = reader(&dir, "stat")?;
                let after = parse(
                    pid,
                    std::str::from_utf8(&stat).map_err(|_| fault("malformed", "non UTF-8 stat"))?,
                    boot.clone(),
                )?;
                if before.start_ticks != after.start_ticks {
                    return Ok(None);
                }
            }
            Ok(Some(serde_json::to_value(before)?))
        })();
        match attempt {
            Ok(Some(value)) => {
                if result.len() == MAX_ITEMS {
                    return Err(fault("limit", "too many discovered processes"));
                }
                result.push(value);
            }
            Ok(None) => {}
            Err(e) if matches!(protocol::errno(&e), Some(libc::ENOENT | libc::ESRCH)) => {}
            Err(e) => return Err(context(e, pid, "discover")),
        }
    }
    Ok(result)
}
pub fn validate(params: &Value) -> io::Result<()> {
    let selected: Vec<Identity> = serde_json::from_value(
        params
            .get("processes")
            .cloned()
            .ok_or_else(|| fault("protocol", "missing processes"))?,
    )
    .map_err(|e| fault("protocol", &e.to_string()))?;
    if selected.len() > MAX_ITEMS {
        return Err(fault("limit", "too many selected processes"));
    }
    for expected in selected {
        if expected.pid <= 0 {
            return Err(fault("protocol", "PID must be positive"));
        }
        let actual = identity(expected.pid).map_err(|e| context(e, expected.pid, "validate"))?;
        if actual.start_ticks != expected.start_ticks
            || expected
                .boot_id
                .as_ref()
                .zip(actual.boot_id.as_ref())
                .is_some_and(|(a, b)| a != b)
        {
            return Err(fault(
                "ownership",
                &format!("PID {} identity changed", expected.pid),
            ));
        }
    }
    Ok(())
}
fn context(error: io::Error, pid: i32, operation: &str) -> io::Error {
    io::Error::new(
        error.kind(),
        protocol::ContextError {
            source: error,
            context: format!("{operation} /proc/{pid}"),
        },
    )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn stat_names_and_identity() {
        let s = format!("12 (a ) b) S {} 123 0", vec!["0"; 18].join(" "));
        let p = parse(12, &s, Some("boot".into())).unwrap();
        assert_eq!(p.name, "a ) b");
        assert_eq!(p.start_ticks, 123);
        assert!(parse(13, &s, None).is_err());
        assert!(parse(12, "broken", None).is_err());
    }
    #[test]
    fn pinned_fixture_directory() {
        let root = std::env::temp_dir().join(format!("targetctl-test-{}", std::process::id()));
        fs::create_dir_all(root.join("12")).unwrap();
        fs::write(
            root.join("12/stat"),
            format!("12 (fixture) S {} 123", vec!["0"; 18].join(" ")),
        )
        .unwrap();
        fs::write(root.join("12/cmdline"), b"fixture\0arg\0").unwrap();
        let values = discover_at(&root, Some("\0arg\0"), None).unwrap();
        assert_eq!(values.len(), 1);
        assert!(discover_at(&root, Some("absent"), None).unwrap().is_empty());
        fs::write(root.join("12/stat"), "broken").unwrap();
        assert!(discover_at(&root, None, None).is_err());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn disappearance_permission_and_reuse_are_distinct() {
        use std::cell::Cell;
        let root = std::env::temp_dir().join(format!("targetctl-errors-{}", std::process::id()));
        fs::create_dir_all(root.join("12")).unwrap();
        let stat =
            |start| format!("12 (fixture) S {} {start}", vec!["0"; 18].join(" ")).into_bytes();
        for errno in [libc::ENOENT, libc::ESRCH] {
            let result = discover_with(&root, None, None, |_, _| {
                Err(io::Error::from_raw_os_error(errno))
            })
            .unwrap();
            assert!(result.is_empty());
        }
        for fail_name in ["stat", "cmdline"] {
            let result = discover_with(&root, Some("fixture"), None, |_, name| {
                if name == fail_name {
                    Err(io::Error::from_raw_os_error(libc::EACCES))
                } else {
                    Ok(stat(123))
                }
            })
            .unwrap_err();
            assert_eq!(protocol::error_value(&result)["errno"], libc::EACCES);
            assert!(result.to_string().contains("/proc/12"));
        }
        let calls = Cell::new(0);
        let result = discover_with(&root, Some("fixture"), None, |_, name| {
            if name == "cmdline" {
                return Ok(b"fixture\0".to_vec());
            }
            calls.set(calls.get() + 1);
            Ok(stat(123 + calls.get()))
        })
        .unwrap();
        assert!(result.is_empty());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn descriptor_does_not_follow_replacement_directory() {
        let root = std::env::temp_dir().join(format!("targetctl-reuse-{}", std::process::id()));
        let process = root.join("12");
        fs::create_dir_all(&process).unwrap();
        fs::write(process.join("stat"), "old").unwrap();
        let pinned = File::open(&process).unwrap();
        fs::remove_file(process.join("stat")).unwrap();
        fs::remove_dir(&process).unwrap();
        fs::create_dir(&process).unwrap();
        fs::write(process.join("stat"), "new").unwrap();
        let error = read(&pinned, "stat").unwrap_err();
        assert_eq!(protocol::errno(&error), Some(libc::ENOENT));
        fs::remove_dir_all(root).unwrap();
    }
}
