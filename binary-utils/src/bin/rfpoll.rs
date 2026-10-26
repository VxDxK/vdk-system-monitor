//! Used by PollFiles and its measurement subclasses. Requires readable target sources.
//! Native JSONL has metadata, sequence, monotonic timestamps, read errors
//! and a clean end marker. Build binary-utils, package with scripts/build_artifacts.py
//! and configure BinaryProvider for framework collection.
//! Example: artifacts/rfpoll.bin --path /proc/stat --duration-ms 2000
//!
use binary_utils::process_sampling::{Sampler, Selection, validate_boot};
use binary_utils::{Writer, expand, sample_loop, validate};
use clap::Parser;
use serde_json::json;
use std::{ffi::CString, io, path::PathBuf};
#[derive(Parser)]
#[command(version, about = "File/system sampling as JSONL")]
struct Args {
    #[arg(long, required_unless_present = "filesystem")]
    path: Vec<String>,
    #[arg(long)]
    filesystem: Vec<String>,
    #[arg(long, conflicts_with = "filesystem")]
    process: Vec<Selection>,
    #[arg(long)]
    boot_id: Option<String>,
    /// Sampling interval in milliseconds (positive integer).
    #[arg(long, default_value_t = 250)]
    interval_ms: u64,
    /// Optional duration in milliseconds; omitted runs until signalled.
    #[arg(long)]
    duration_ms: Option<u64>,
    #[arg(long)]
    samples: Option<u64>,
    #[arg(long, default_value = "/")]
    root: PathBuf,
}
fn main() -> io::Result<()> {
    let a = Args::parse();
    validate(a.interval_ms, a.duration_ms).map_err(io::Error::other)?;
    if a.samples == Some(0) {
        return Err(io::Error::other("samples must be positive"));
    }
    validate_boot(&a.root, a.boot_id.as_deref())?;
    let process_mode = !a.process.is_empty();
    if process_mode {
        Sampler::validate_paths(&a.process, &a.path)?;
    }
    let mut sampler = Sampler::new(&a.root, &a.process)?;
    sample_loop(
        Writer::new("rfpoll")?,
        a.interval_ms,
        a.duration_ms,
        a.samples,
        |w| {
            w.emit(json!({"type":"sample"}))?;
            if process_mode {
                for event in sampler.sample_paths(&a.path) {
                    w.emit(event)?;
                }
                return Ok(());
            }
            for pattern in &a.path {
                let paths = expand(&a.root, pattern);
                if paths.is_empty() {
                    w.emit(
                        json!({"type":"read_error","path":pattern,"error":"no matching paths"}),
                    )?;
                }
                for p in paths {
                    w.read(&a.root, &p, json!({}))?;
                }
            }
            for path in &a.filesystem {
                let c = CString::new(path.as_str()).map_err(io::Error::other)?;
                let mut stat = std::mem::MaybeUninit::<libc::statvfs>::uninit();
                if unsafe { libc::statvfs(c.as_ptr(), stat.as_mut_ptr()) } == 0 {
                    let s = unsafe { stat.assume_init() };
                    w.emit(json!({"type":"filesystem","path":path,"total_bytes":s.f_blocks as u128*s.f_frsize as u128,"free_bytes":s.f_bfree as u128*s.f_frsize as u128,"available_bytes":s.f_bavail as u128*s.f_frsize as u128}))?;
                } else {
                    w.emit(json!({"type":"read_error","path":path,"error":io::Error::last_os_error().to_string()}))?;
                }
            }
            Ok(())
        },
    )
}

#[cfg(test)]
mod interval_tests {
    use super::*;

    #[test]
    fn default_sampling_interval() {
        let args = Args::try_parse_from(["rfpoll", "--path", "/proc/stat"]).unwrap();
        assert_eq!(args.interval_ms, 250);
        assert_eq!(args.duration_ms, None);
    }
}
