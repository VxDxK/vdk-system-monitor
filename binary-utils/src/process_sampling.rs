//! Identity-pinned procfs sampling. Lifecycle events are distinct from read failures.
use crate::{hex, read_bounded, rooted};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs::{self, File},
    io,
    os::fd::AsRawFd,
    path::{Path, PathBuf},
    str::FromStr,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Selection {
    pub pid: u32,
    pub start_ticks: u64,
}
impl FromStr for Selection {
    type Err = String;
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        let (pid, start) = s.split_once(':').ok_or("expected PID:START_TICKS")?;
        let result = Self {
            pid: pid.parse().map_err(|_| "invalid PID")?,
            start_ticks: start.parse().map_err(|_| "invalid start ticks")?,
        };
        if result.pid == 0 {
            return Err("PID must be positive".into());
        }
        Ok(result)
    }
}
fn at(dir: &File, name: &str) -> PathBuf {
    PathBuf::from(format!("/proc/self/fd/{}/{}", dir.as_raw_fd(), name))
}
fn read(dir: &File, name: &str) -> io::Result<Vec<u8>> {
    read_bounded(&at(dir, name), 16 * 1024 * 1024)
}
fn missing(e: &io::Error) -> bool {
    matches!(e.raw_os_error(), Some(libc::ENOENT | libc::ESRCH))
}
fn stat(pid: u32, bytes: &[u8]) -> io::Result<(u64, bool)> {
    let invalid = || io::Error::new(io::ErrorKind::InvalidData, "malformed process stat");
    let s = std::str::from_utf8(bytes).map_err(|_| invalid())?;
    let (begin, end) = (
        s.find('(').ok_or_else(invalid)?,
        s.rfind(')').ok_or_else(invalid)?,
    );
    if end <= begin || s[..begin].trim().parse::<u32>().ok() != Some(pid) {
        return Err(invalid());
    }
    let fields: Vec<_> = s[end + 1..].split_whitespace().collect();
    let start = fields
        .get(19)
        .ok_or_else(invalid)?
        .parse()
        .map_err(|_| invalid())?;
    Ok((start, matches!(fields.first(), Some(&"Z" | &"X" | &"x"))))
}
pub fn validate_boot(root: &Path, expected: Option<&str>) -> io::Result<()> {
    if let Some(expected) = expected {
        let actual = fs::read_to_string(rooted(root, "/proc/sys/kernel/random/boot_id"))?;
        if actual.trim() != expected {
            return Err(io::Error::other("selected process belongs to another boot"));
        }
    }
    Ok(())
}
#[derive(Debug)]
struct Entity {
    dir: Option<File>,
    pid: u32,
    tid: u32,
    start: u64,
    process_start: u64,
    thread: bool,
    active: bool,
}
impl Entity {
    fn open(root: &Path, pid: u32, expected: Option<u64>) -> io::Result<Self> {
        let dir = File::open(rooted(root, &format!("/proc/{pid}")))?;
        let (start, dead) = stat(pid, &read(&dir, "stat")?)?;
        if expected.is_some_and(|s| s != start) {
            return Err(io::Error::other("selected process identity changed"));
        }
        if dead {
            return Err(io::Error::from_raw_os_error(libc::ESRCH));
        }
        Ok(Self {
            dir: Some(dir),
            pid,
            tid: pid,
            start,
            process_start: start,
            thread: false,
            active: true,
        })
    }
    fn thread(parent: &Self, tid: u32) -> io::Result<Self> {
        let dir = File::open(at(
            parent.dir.as_ref().expect("active process"),
            &format!("task/{tid}"),
        ))?;
        let (start, dead) = stat(tid, &read(&dir, "stat")?)?;
        if dead {
            return Err(io::Error::from_raw_os_error(libc::ESRCH));
        }
        Ok(Self {
            dir: Some(dir),
            pid: parent.pid,
            tid,
            start,
            process_start: parent.start,
            thread: true,
            active: true,
        })
    }
    fn tags(&self) -> Value {
        json!({"pid":self.pid,"tid":self.tid,"scope":if self.thread {"thread"} else {"process"},"start_ticks":self.start,"process_start_ticks":self.process_start})
    }
    fn path(&self, file: &str) -> String {
        if self.thread {
            format!("/proc/{}/task/{}/{file}", self.pid, self.tid)
        } else {
            format!("/proc/{}/{file}", self.pid)
        }
    }
    fn error(&self, file: &str, operation: &str, e: &io::Error) -> Value {
        json!({"type":"read_error","path":self.path(file),"operation":operation,"errno":e.raw_os_error(),"error":e.to_string(),"tags":self.tags()})
    }
    fn end(&mut self, reason: &str) -> Value {
        self.active = false;
        self.dir.take();
        json!({"type":"entity_end","reason":reason,"tags":self.tags()})
    }
    fn check(
        &self,
        reader: &mut impl FnMut(&File, &str) -> io::Result<Vec<u8>>,
    ) -> io::Result<Option<&'static str>> {
        match reader(self.dir.as_ref().expect("active entity"), "stat") {
            Ok(bytes) => {
                let (start, dead) = stat(self.tid, &bytes)?;
                Ok(if start != self.start {
                    Some("identity_changed")
                } else if dead {
                    Some("exited")
                } else {
                    None
                })
            }
            Err(e) if missing(&e) => Ok(Some("disappeared")),
            Err(e) => Err(e),
        }
    }
    fn sample(&mut self, files: &[&str]) -> Vec<Value> {
        self.sample_with(files, read)
    }
    fn sample_with(
        &mut self,
        files: &[&str],
        mut reader: impl FnMut(&File, &str) -> io::Result<Vec<u8>>,
    ) -> Vec<Value> {
        if !self.active {
            return vec![];
        }
        match self.check(&mut reader) {
            Ok(Some(reason)) => return vec![self.end(reason)],
            Err(e) => return vec![self.error("stat", "identity", &e)],
            Ok(None) => {}
        }
        let mut records = vec![];
        for file in files {
            records.push(match reader(self.dir.as_ref().expect("active entity"), file) {
                Ok(data) => json!({"type":"file","path":self.path(file),"encoding":"hex","data":hex(&data),"tags":self.tags()}),
                Err(e) => self.error(file, "read", &e),
            });
        }
        match self.check(&mut reader) {
            Ok(Some(reason)) => {
                // Unexpected failures must not be concealed by a concurrent exit.
                records.retain(|e| e["type"] == "read_error" && !matches!(e["errno"].as_i64(), Some(n) if n == libc::ENOENT as i64 || n == libc::ESRCH as i64));
                records.push(self.end(reason));
            }
            Err(e) => {
                records.retain(|e| e["type"] == "read_error");
                records.push(self.error("stat", "identity", &e));
            }
            Ok(None) => {}
        }
        records
    }
}
struct Process {
    entity: Entity,
    threads: BTreeMap<(u32, u64), Entity>,
}
impl Process {
    fn sample(&mut self, files: &[&str], threads: bool) -> Vec<Value> {
        let mut records = self.entity.sample(files);
        if !self.entity.active {
            for thread in self.threads.values_mut().filter(|e| e.active) {
                records.push(thread.end("disappeared"));
            }
            return records;
        }
        if !threads {
            return records;
        }
        // Sample known threads even if they no longer appear in the directory.
        for thread in self.threads.values_mut() {
            records.extend(thread.sample(&["stat"]));
        }
        let entries = match fs::read_dir(at(
            self.entity.dir.as_ref().expect("active process"),
            "task",
        )) {
            Ok(entries) => entries,
            Err(e) => {
                if missing(&e) {
                    let ended = self.entity.sample(&[]);
                    let confirmed = !self.entity.active;
                    records.extend(ended);
                    if confirmed {
                        return records;
                    }
                }
                records.push(self.entity.error("task", "enumerate", &e));
                return records;
            }
        };
        for entry in entries {
            let attempt = (|| {
                let entry = entry?;
                let Some(tid) = entry
                    .file_name()
                    .to_str()
                    .and_then(|s| s.parse::<u32>().ok())
                else {
                    return Ok(None);
                };
                Entity::thread(&self.entity, tid).map(Some)
            })();
            match attempt {
                Ok(Some(mut thread)) => {
                    let key = (thread.tid, thread.start);
                    if let std::collections::btree_map::Entry::Vacant(slot) =
                        self.threads.entry(key)
                    {
                        records.extend(thread.sample(&["stat"]));
                        slot.insert(thread);
                    }
                }
                Ok(None) => {}
                Err(e) if missing(&e) => {}
                Err(e) => records.push(self.entity.error("task", "enumerate", &e)),
            }
        }
        records
    }
}
pub struct Sampler {
    root: PathBuf,
    processes: BTreeMap<(u32, u64), Process>,
}
impl Sampler {
    pub fn new(root: &Path, selected: &[Selection]) -> io::Result<Self> {
        let mut result = Self {
            root: root.into(),
            processes: BTreeMap::new(),
        };
        for s in selected {
            let entity = Entity::open(root, s.pid, Some(s.start_ticks))?;
            result.processes.insert(
                (s.pid, s.start_ticks),
                Process {
                    entity,
                    threads: BTreeMap::new(),
                },
            );
        }
        Ok(result)
    }
    pub fn discover(&mut self, pids: &[u32], pattern: Option<&str>) -> io::Result<()> {
        let candidates = if pids.is_empty() {
            fs::read_dir(rooted(&self.root, "/proc"))?
                .map(|e| e.map(|e| e.file_name().to_str().and_then(|s| s.parse::<u32>().ok())))
                .collect::<io::Result<Vec<_>>>()?
                .into_iter()
                .flatten()
                .collect()
        } else {
            pids.to_vec()
        };
        for pid in candidates {
            let attempt = (|| {
                let entity = Entity::open(&self.root, pid, None)?;
                if let Some(pattern) = pattern {
                    let bytes = read(entity.dir.as_ref().expect("active process"), "cmdline")?;
                    if !String::from_utf8_lossy(&bytes).contains(pattern) {
                        return Ok(None);
                    }
                }
                Ok(Some(entity))
            })();
            match attempt {
                Ok(Some(entity)) => {
                    self.processes
                        .entry((pid, entity.start))
                        .or_insert(Process {
                            entity,
                            threads: BTreeMap::new(),
                        });
                }
                Ok(None) => {}
                Err(e) if missing(&e) => {}
                Err(e) => return Err(e),
            }
        }
        Ok(())
    }
    pub fn sample<'a>(
        &'a mut self,
        files: &'a [&'a str],
        threads: bool,
    ) -> impl Iterator<Item = Value> + 'a {
        self.processes
            .values_mut()
            .flat_map(move |p| p.sample(files, threads))
    }
    pub fn sample_paths<'a>(&'a mut self, paths: &'a [String]) -> impl Iterator<Item = Value> + 'a {
        self.processes.values_mut().flat_map(move |p| {
            let prefix = format!("/proc/{}/", p.entity.pid);
            let files: Vec<_> = paths
                .iter()
                .filter_map(|s| s.strip_prefix(&prefix))
                .collect();
            p.sample(&files, false)
        })
    }
    pub fn validate_paths(selected: &[Selection], paths: &[String]) -> io::Result<()> {
        for path in paths {
            let Some((owner, file)) = path.strip_prefix("/proc/").and_then(|s| s.split_once('/'))
            else {
                return Err(io::Error::other(
                    "process paths must be literal /proc/PID/FILE paths",
                ));
            };
            if !selected.iter().any(|s| owner == s.pid.to_string())
                || file.is_empty()
                || file == "."
                || file == ".."
                || file.contains(['/', '*', '?'])
            {
                return Err(io::Error::other(
                    "path does not belong to a selected process",
                ));
            }
        }
        if selected.iter().any(|s| {
            !paths
                .iter()
                .any(|p| p.starts_with(&format!("/proc/{}/", s.pid)))
        }) {
            return Err(io::Error::other("each process needs a file path"));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};
    static NEXT: AtomicU64 = AtomicU64::new(0);
    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            let root = std::env::temp_dir().join(format!(
                "vsm-process-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir_all(root.join("proc/123/task/123")).unwrap();
            let fixture = Self(root);
            fixture.write("stat", &stat_text(123, 99, "S"));
            fixture.write("task/123/stat", &stat_text(123, 99, "S"));
            fixture.write("io", "read_bytes: 12\n");
            fixture
        }
        fn write(&self, name: &str, text: &str) {
            fs::write(self.0.join("proc/123").join(name), text).unwrap();
        }
        fn entity(&self) -> Entity {
            Entity::open(&self.0, 123, Some(99)).unwrap()
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }
    fn stat_text(pid: u32, start: u64, state: &str) -> String {
        let mut fields = vec!["0".to_string(); 22];
        fields[0] = state.into();
        fields[19] = start.to_string();
        format!("{pid} (name (worker)) {}", fields.join(" "))
    }
    #[test]
    fn live_missing_metric_is_an_error_not_termination() {
        let f = Fixture::new();
        let mut e = f.entity();
        let records = e.sample(&["missing"]);
        assert_eq!(records[0]["type"], "read_error");
        assert_eq!(records[0]["errno"], libc::ENOENT);
        assert!(e.active);
    }
    #[test]
    fn disappearance_emitted_once_and_never_reopens_reused_path() {
        let f = Fixture::new();
        let mut e = f.entity();
        assert_eq!(e.sample(&["stat"])[0]["type"], "file");
        fs::rename(f.0.join("proc/123"), f.0.join("old")).unwrap();
        fs::remove_dir_all(f.0.join("old")).unwrap();
        fs::create_dir_all(f.0.join("proc/123")).unwrap();
        f.write("stat", &stat_text(123, 200, "S"));
        let records = e.sample(&["stat"]);
        assert_eq!(records.len(), 1);
        assert_eq!(records[0]["reason"], "disappeared");
        assert!(e.dir.is_none());
        assert!(e.sample(&["stat"]).is_empty());
    }
    #[test]
    fn zombie_and_changed_identity_retire_without_samples() {
        for (start, state, reason) in [(99, "Z", "exited"), (200, "S", "identity_changed")] {
            let f = Fixture::new();
            let mut e = f.entity();
            f.write("stat", &stat_text(123, start, state));
            let records = e.sample(&["stat", "io"]);
            assert_eq!(records.len(), 1);
            assert_eq!(records[0]["reason"], reason);
        }
    }
    #[test]
    fn exit_during_sample_discards_buffered_files_but_keeps_permission_failure() {
        for permission in [false, true] {
            let f = Fixture::new();
            let mut e = f.entity();
            let mut checks = 0;
            let records = e.sample_with(&["io"], |dir, file| {
                if file == "stat" {
                    checks += 1;
                    if checks == 2 {
                        return Err(io::Error::from_raw_os_error(libc::ENOENT));
                    }
                }
                if permission && file == "io" {
                    return Err(io::Error::from_raw_os_error(libc::EACCES));
                }
                read(dir, file)
            });
            assert!(!records.iter().any(|e| e["type"] == "file"));
            assert_eq!(
                records.iter().filter(|e| e["type"] == "entity_end").count(),
                1
            );
            assert_eq!(
                records.iter().filter(|e| e["type"] == "read_error").count(),
                usize::from(permission)
            );
        }
    }
    #[test]
    fn malformed_or_unreadable_identity_does_not_become_disappearance() {
        let f = Fixture::new();
        let mut e = f.entity();
        f.write("stat", "malformed");
        let records = e.sample(&["io"]);
        assert_eq!(records[0]["type"], "read_error");
        assert!(e.active);
        let records = e.sample_with(&["io"], |_, _| {
            Err(io::Error::from_raw_os_error(libc::EPERM))
        });
        assert_eq!(records[0]["errno"], libc::EPERM);
        assert!(e.active);
    }
    #[test]
    fn thread_exit_and_reuse_are_separate_identities() {
        let f = Fixture::new();
        let mut sampler = Sampler::new(
            &f.0,
            &[Selection {
                pid: 123,
                start_ticks: 99,
            }],
        )
        .unwrap();
        let first: Vec<_> = sampler.sample(&["stat"], true).collect();
        assert!(first.iter().any(|e| e["tags"]["scope"] == "thread"));
        fs::remove_file(f.0.join("proc/123/task/123/stat")).unwrap();
        let gone: Vec<_> = sampler.sample(&["stat"], true).collect();
        assert_eq!(gone.iter().filter(|e| e["type"] == "entity_end").count(), 1);
        f.write("task/123/stat", &stat_text(123, 201, "S"));
        let next: Vec<_> = sampler.sample(&["stat"], true).collect();
        assert!(
            next.iter()
                .any(|e| e["type"] == "file" && e["tags"]["start_ticks"] == 201)
        );
        assert!(!next.iter().any(|e| e["type"] == "entity_end"));
    }
    #[test]
    fn missing_task_directory_of_live_process_is_an_error() {
        let f = Fixture::new();
        let mut sampler = Sampler::new(
            &f.0,
            &[Selection {
                pid: 123,
                start_ticks: 99,
            }],
        )
        .unwrap();
        fs::remove_dir_all(f.0.join("proc/123/task")).unwrap();
        let events: Vec<_> = sampler.sample(&["stat"], true).collect();
        assert!(
            events
                .iter()
                .any(|e| e["type"] == "read_error" && e["operation"] == "enumerate")
        );
    }
    #[test]
    fn startup_identity_boot_and_literal_path_validation() {
        let f = Fixture::new();
        assert!(
            Sampler::new(
                &f.0,
                &[Selection {
                    pid: 123,
                    start_ticks: 200
                }]
            )
            .is_err()
        );
        assert!("123:99".parse::<Selection>().is_ok());
        assert!("0:99".parse::<Selection>().is_err());
        for path in [
            "/proc/124/stat",
            "/proc/123/../stat",
            "/proc/123/*",
            "/setting",
        ] {
            assert!(
                Sampler::validate_paths(
                    &[Selection {
                        pid: 123,
                        start_ticks: 99
                    }],
                    &[path.into()]
                )
                .is_err()
            );
        }
        assert!(
            Sampler::validate_paths(
                &[Selection {
                    pid: 123,
                    start_ticks: 99
                }],
                &["/proc/123/stat".into()]
            )
            .is_ok()
        );
        fs::create_dir_all(f.0.join("proc/sys/kernel/random")).unwrap();
        fs::write(f.0.join("proc/sys/kernel/random/boot_id"), "boot\n").unwrap();
        assert!(validate_boot(&f.0, Some("boot")).is_ok());
        assert!(validate_boot(&f.0, Some("different")).is_err());
    }
}
