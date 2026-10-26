//! Target-local process discovery, identity validation, and leased workload supervision.
//!
//! Example: printf '%s\n' '{"id":1,"operation":"capabilities","params":{}}' | targetctl request
#[path = "../targetctl/child.rs"]
mod child;
#[path = "../targetctl/procfs.rs"]
mod procfs;
#[path = "../targetctl/protocol.rs"]
mod protocol;
#[path = "../targetctl/supervisor.rs"]
mod supervisor;

use clap::{Parser, Subcommand};
use protocol::{Request, response};
use serde_json::json;
use std::io::{self, BufRead, Read, Write};

#[derive(Parser)]
#[command(
    name = "targetctl",
    bin_name = "targetctl",
    version,
    about = "Target-local operations over JSONL stdin/stdout",
    propagate_version = true
)]
struct Args {
    #[command(subcommand)]
    mode: Mode,
}

#[derive(Subcommand)]
enum Mode {
    /// Execute one discovery, validation, or capability request from stdin.
    Request,
    /// Supervise one owned workload over a persistent JSONL control channel.
    Supervise,
}

fn main() {
    if let Err(error) = run() {
        eprintln!("targetctl: {error}");
        std::process::exit(1);
    }
}

fn run() -> io::Result<()> {
    match Args::parse().mode {
        Mode::Supervise => supervisor::run(),
        Mode::Request => {
            let mut input = io::stdin().lock().take((protocol::MAX_LINE + 1) as u64);
            let mut line = String::new();
            input.read_line(&mut line)?;
            let request = Request::parse(line.as_bytes());
            let mut out = io::stdout().lock();
            match request {
                Ok(req) => {
                    let started = protocol::clock_ns();
                    let result = match req.operation.as_str() {
                        "capabilities" => Ok(
                            json!({"operations":["capabilities","discover","validate","launch","identity","stop","renew","release"],"procfs":std::path::Path::new("/proc/self/stat").exists(),"pidfd":supervisor::pidfd_available()}),
                        ),
                        "discover" => procfs::discover(&req.params).and_then(|items| {
                            for item in items {
                                serde_json::to_writer(
                                    &mut out,
                                    &response(&req, "item", item, started),
                                )?;
                                out.write_all(b"\n")?;
                            }
                            Ok(json!({}))
                        }),
                        "validate" => procfs::validate(&req.params).map(|_| json!({})),
                        _ => Err(protocol::fault("unsupported", "unsupported operation")),
                    };
                    let event = match result {
                        Ok(value) => response(&req, "result", value, started),
                        Err(error) => protocol::error_response(&req, &error, started),
                    };
                    serde_json::to_writer(&mut out, &event)?;
                    out.write_all(b"\n")?;
                }
                Err(error) => {
                    serde_json::to_writer(
                        &mut out,
                        &json!({"id":0,"event":"error","error":protocol::error_value(&error)}),
                    )?;
                    out.write_all(b"\n")?;
                }
            }
            out.flush()
        }
    }
}
