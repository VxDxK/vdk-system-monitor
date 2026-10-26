//! Used by PidStat and ProcessIo. Requires readable process/thread procfs entries.
//! Native JSONL includes target clock/page and process identity metadata; an end
//! marker distinguishes clean completion. PID reuse and disappearance remain possible.
//! Build and package binary-utils, then configure BinaryProvider.
//! Example: artifacts/pspider.bin --pid 1234 --duration-ms 2000
//!
use binary_utils::process_sampling::{Sampler, Selection, validate_boot};
use binary_utils::{Writer, sample_loop, validate};
use clap::Parser;
use serde_json::json;
use std::{io, path::PathBuf};
#[derive(Parser)]
#[command(
    version,
    about = "Linux process/thread raw counters with identity metadata"
)]
struct Args {
    #[arg(long)]
    pid: Vec<u32>,
    #[arg(long, conflicts_with_all = ["pid", "pattern"])]
    process: Vec<Selection>,
    #[arg(long)]
    boot_id: Option<String>,
    #[arg(long)]
    pattern: Option<String>,
    #[arg(long)]
    self_only: bool,
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
    let mut sampler = Sampler::new(&a.root, &a.process)?;
    sample_loop(
        Writer::new("pspider")?,
        a.interval_ms,
        a.duration_ms,
        a.samples,
        |w| {
            w.emit(json!({"type":"sample"}))?;
            if a.process.is_empty() {
                sampler.discover(&a.pid, a.pattern.as_deref())?;
            }
            for event in sampler.sample(&["stat", "io", "status", "cmdline"], !a.self_only) {
                w.emit(event)?;
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
        let args = Args::try_parse_from(["pspider"]).unwrap();
        assert_eq!(args.interval_ms, 250);
        assert_eq!(args.duration_ms, None);
    }
}
