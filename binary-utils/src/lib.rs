//! Shared JSONL framing, Linux sampling, and signal-driven shutdown.
pub mod process_sampling;

use serde_json::{Value, json};
use std::{
    fs,
    io::{self, Write},
    path::{Path, PathBuf},
    sync::atomic::{AtomicBool, Ordering},
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

static STOP: AtomicBool = AtomicBool::new(false);
extern "C" fn signal_stop(_: libc::c_int) {
    STOP.store(true, Ordering::Relaxed);
}
pub fn setup_signals() {
    unsafe {
        libc::signal(libc::SIGINT, signal_stop as *const () as libc::sighandler_t);
        libc::signal(
            libc::SIGTERM,
            signal_stop as *const () as libc::sighandler_t,
        );
    }
}
pub fn stopping() -> bool {
    STOP.load(Ordering::Relaxed)
}
pub fn wall_ns() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos()
}
pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
pub fn rooted(root: &Path, path: &str) -> PathBuf {
    root.join(path.trim_start_matches('/'))
}
pub fn validate(interval: u64, duration: Option<u64>) -> Result<(), String> {
    if interval == 0 || duration.is_some_and(|value| value == 0 || value > 86_400_000) {
        return Err("interval and duration must be positive; duration <= 24h".into());
    }
    Ok(())
}
/// Linux system-wide monotonic clock, shared by collectors and targetctl.
pub fn monotonic_ns() -> io::Result<u64> {
    let mut value = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    if unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut value) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(value.tv_sec as u64 * 1_000_000_000 + value.tv_nsec as u64)
}

pub struct Writer {
    start: Instant,
    sequence: u64,
    out: io::BufWriter<io::Stdout>,
}
impl Writer {
    pub fn new(tool: &str) -> io::Result<Self> {
        setup_signals();
        let before = monotonic_ns()?;
        let start = Instant::now();
        let after = monotonic_ns()?;
        let mut result = Self {
            start,
            sequence: 0,
            out: io::BufWriter::new(io::stdout()),
        };
        result.emit(json!({"type":"metadata", "schema":tool, "version":env!("CARGO_PKG_VERSION"), "wall_ns":wall_ns(), "monotonic_origin_ns":before + (after - before) / 2, "origin_uncertainty_ns":(after - before).div_ceil(2), "os":std::env::consts::OS, "architecture":std::env::consts::ARCH, "kernel":fs::read_to_string("/proc/sys/kernel/osrelease").ok().map(|s|s.trim().to_owned()), "clock_ticks_per_second":unsafe {libc::sysconf(libc::_SC_CLK_TCK)}, "page_size_bytes":unsafe {libc::sysconf(libc::_SC_PAGESIZE)}, "boot_id":fs::read_to_string("/proc/sys/kernel/random/boot_id").ok().map(|s|s.trim().to_owned())}))?;
        Ok(result)
    }
    pub fn elapsed_ns(&self) -> u128 {
        self.start.elapsed().as_nanos()
    }
    pub fn emit(&mut self, mut event: Value) -> io::Result<()> {
        event["sequence"] = json!(self.sequence);
        event["time_ns"] = json!(self.elapsed_ns());
        self.sequence += 1;
        serde_json::to_writer(&mut self.out, &event)?;
        self.out.write_all(b"\n")?;
        self.out.flush()
    }
    pub fn end(&mut self) -> io::Result<()> {
        self.emit(json!({"type":"end", "records":self.sequence, "interrupted":stopping()}))
    }
    pub fn read(&mut self, root: &Path, path: &str, tags: Value) -> io::Result<()> {
        let begin = self.elapsed_ns();
        let event = match read_bounded(&rooted(root, path), 16 * 1024 * 1024) {
            Ok(data) => {
                json!({"type":"file", "path":path, "encoding":"hex", "data":hex(&data), "started_ns":begin, "finished_ns":self.elapsed_ns(), "tags":tags})
            }
            Err(e) => {
                json!({"type":"read_error", "path":path, "error":e.to_string(), "started_ns":begin, "finished_ns":self.elapsed_ns(), "tags":tags})
            }
        };
        self.emit(event)
    }
}
pub fn read_bounded(path: &Path, limit: usize) -> io::Result<Vec<u8>> {
    use io::Read;
    use std::os::unix::fs::OpenOptionsExt;
    let mut data = Vec::new();
    let file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NONBLOCK)
        .open(path)?;
    if !file.metadata()?.is_file() {
        return Err(io::Error::other(
            "source is not a regular/procfs/sysfs file",
        ));
    }
    file.take(limit as u64 + 1).read_to_end(&mut data)?;
    if data.len() > limit {
        return Err(io::Error::other("file exceeds read limit"));
    }
    Ok(data)
}
pub fn sample_loop(
    mut writer: Writer,
    interval: u64,
    duration: Option<u64>,
    samples: Option<u64>,
    mut sample: impl FnMut(&mut Writer) -> io::Result<()>,
) -> io::Result<()> {
    validate(interval, duration).map_err(io::Error::other)?;
    let start = Instant::now();
    let step = Duration::from_millis(interval);
    let end = duration.map(Duration::from_millis);
    let mut deadline = start;
    let mut count = 0;
    while !stopping()
        && end.is_none_or(|limit| start.elapsed() < limit)
        && samples.is_none_or(|n| count < n)
    {
        sample(&mut writer)?;
        if count == 0 {
            writer.emit(json!({"type":"ready"}))?;
        }
        count += 1;
        deadline += step;
        let now = Instant::now();
        if now > deadline {
            let missed = (now.duration_since(deadline).as_nanos() / step.as_nanos()) as u64 + 1;
            deadline += step.mul_f64(missed as f64);
            writer.emit(json!({"type":"overrun", "missed":missed}))?;
        }
        while !stopping()
            && Instant::now() < deadline
            && end.is_none_or(|limit| start.elapsed() < limit)
        {
            thread::sleep(
                deadline
                    .saturating_duration_since(Instant::now())
                    .min(Duration::from_millis(20)),
            );
        }
    }
    writer.end()
}
pub fn wildcard(pattern: &str, text: &str) -> bool {
    let (p, t) = (pattern.as_bytes(), text.as_bytes());
    let (mut i, mut j, mut star, mut mark) = (0, 0, None, 0);
    while j < t.len() {
        if i < p.len() && (p[i] == b'?' || p[i] == t[j]) {
            i += 1;
            j += 1;
        } else if i < p.len() && p[i] == b'*' {
            star = Some(i);
            i += 1;
            mark = j;
        } else if let Some(s) = star {
            i = s + 1;
            mark += 1;
            j = mark;
        } else {
            return false;
        }
    }
    while i < p.len() && p[i] == b'*' {
        i += 1;
    }
    i == p.len()
}
pub fn expand(root: &Path, pattern: &str) -> Vec<String> {
    let mut paths = vec![PathBuf::from("/")];
    for part in pattern.trim_start_matches('/').split('/') {
        let mut next = vec![];
        for base in paths {
            if part.contains(['*', '?']) {
                if let Ok(entries) = fs::read_dir(rooted(root, &base.to_string_lossy())) {
                    for entry in entries.flatten() {
                        let name = entry.file_name().to_string_lossy().into_owned();
                        if wildcard(part, &name) {
                            next.push(base.join(name));
                        }
                    }
                }
            } else {
                next.push(base.join(part));
            }
        }
        paths = next;
    }
    paths.sort();
    paths
        .into_iter()
        .map(|p| p.to_string_lossy().into_owned())
        .collect()
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn byte_encoding() {
        assert_eq!(hex(&[0, 255, 10]), "00ff0a");
    }
    #[test]
    fn bounds() {
        assert!(validate(0, Some(10)).is_err());
        assert!(validate(1, Some(86_400_001)).is_err());
        assert!(validate(1, Some(1)).is_ok());
        assert!(validate(1, None).is_ok());
        assert!(validate(1, Some(0)).is_err());
    }
    #[test]
    fn patterns() {
        assert!(wildcard("cpu*/stat", "cpu12/stat"));
        assert!(wildcard("a?c", "abc"));
        assert!(!wildcard("a?c", "ac"));
        assert!(!wildcard("a*bc", "abcd"));
    }
    #[test]
    fn bounded_read() {
        let p = std::env::temp_dir().join(format!("vdk-read-{}", std::process::id()));
        fs::write(&p, b"abc").unwrap();
        assert!(read_bounded(&p, 2).is_err());
        assert_eq!(read_bounded(&p, 3).unwrap(), b"abc");
        fs::remove_file(p).unwrap();
    }
}
