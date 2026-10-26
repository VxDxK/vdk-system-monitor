//! Single-threaded poll loop: output backpressure cannot block the control lease.
pub use super::child::pidfd_available;
use super::{
    child::{Launch, exit_code, group_live, nonblock, signal_group, spawn},
    procfs,
    protocol::{self, Request, error_response, fault, response},
};
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    fs,
    io::{self, Read},
    os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd},
    path::{Path, PathBuf},
    time::{Duration, Instant},
};
const QUEUE_LIMIT: usize = 8 * 1024 * 1024;
const CHUNK: usize = 32768;

fn store(directory: &Path, name: &str, value: &Value) -> io::Result<()> {
    let temp = directory.join(format!(".{name}.tmp"));
    fs::write(&temp, serde_json::to_vec(value)?)?;
    fs::rename(temp, directory.join(name))
}
fn base64(bytes: &[u8]) -> String {
    const TABLE: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for b in bytes.chunks(3) {
        let n = ((b[0] as u32) << 16)
            | ((b.get(1).copied().unwrap_or(0) as u32) << 8)
            | b.get(2).copied().unwrap_or(0) as u32;
        out.push(TABLE[(n >> 18) as usize] as char);
        out.push(TABLE[((n >> 12) & 63) as usize] as char);
        out.push(if b.len() > 1 {
            TABLE[((n >> 6) & 63) as usize] as char
        } else {
            '='
        });
        out.push(if b.len() > 2 {
            TABLE[(n & 63) as usize] as char
        } else {
            '='
        });
    }
    out
}
fn enqueue(queue: &mut VecDeque<u8>, value: Value) -> io::Result<()> {
    let mut bytes = serde_json::to_vec(&value)?;
    bytes.push(b'\n');
    if bytes.len() > protocol::MAX_LINE {
        return Err(fault("limit", "response too large"));
    }
    queue.extend(bytes);
    Ok(())
}
fn read_fd(fd: RawFd, buffer: &mut [u8]) -> io::Result<Option<usize>> {
    let n = unsafe { libc::read(fd, buffer.as_mut_ptr().cast(), buffer.len()) };
    if n < 0 {
        let e = io::Error::last_os_error();
        if matches!(
            e.kind(),
            io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
        ) {
            Ok(None)
        } else {
            Err(e)
        }
    } else {
        Ok(Some(n as usize))
    }
}
fn positive(value: f64) -> bool {
    value.is_finite() && value > 0.0 && value <= 86400.0
}
pub fn run() -> io::Result<()> {
    binary_utils::setup_signals();
    // The initial line is bounded and read before any workload exists.
    let mut first = Vec::new();
    let mut byte = [0u8; 1];
    while first.len() <= protocol::MAX_LINE {
        if io::stdin().read(&mut byte)? == 0 {
            return Err(fault("protocol", "missing launch"));
        }
        first.push(byte[0]);
        if byte[0] == b'\n' {
            break;
        }
    }
    let request = Request::parse(&first)?;
    let started = protocol::clock_ns();
    if request.operation != "launch" {
        return Err(fault("protocol", "first operation must be launch"));
    }
    let spec: Launch = serde_json::from_value(request.params.clone())
        .map_err(|e| fault("protocol", &e.to_string()))?;
    if !positive(spec.lease_seconds) || !positive(spec.grace_seconds) {
        return Err(fault("protocol", "invalid lease/grace"));
    }
    let directory = PathBuf::from(&spec.directory);
    let mut builder = fs::DirBuilder::new();
    use std::os::unix::fs::DirBuilderExt;
    builder.mode(0o700).create(&directory)?;
    let mut child = match spawn(&spec) {
        Ok(c) => c,
        Err(e) => {
            let mut value = error_response(&request, &e, started);
            value["cleanup"] = json!("confirmed");
            store(
                &directory,
                "final.json",
                &json!({"cleanup":"confirmed","launch_failed":true}),
            )?;
            println!("{}", value);
            return Ok(());
        }
    };
    store(
        &directory,
        "launch.json",
        &json!({"supervisor_pid":std::process::id(),"workload":child.identity}),
    )?;
    nonblock(0)?;
    nonblock(1)?;
    let mut queue = VecDeque::new();
    let mut input = Vec::new();
    let mut exec_bytes = Vec::new();
    let mut ready = false;
    let mut launched = false;
    let mut out_open = true;
    let mut err_open = true;
    let mut control_open = true;
    let mut lease = Instant::now();
    let mut stop: Option<(Instant, f64)> = None;
    let mut killed = false;
    let mut code = None;
    let mut completion_sent = false;
    let mut release = false;
    let mut cleanup_sent = false;
    let mut stop_requests: Vec<Request> = Vec::new();
    let mut cleanup_progress: Option<Instant> = None;
    let mut output_complete = true;
    let mut group_clean_at: Option<Instant> = None;
    let mut identity = child.identity.clone();
    let mut last_request_id = request.id;
    // pidfd is an optional stable leader reference. waitid(WNOWAIT) still reserves the group leader PID.
    let pidfd = unsafe { libc::syscall(libc::SYS_pidfd_open, child.pid, 0) };
    let pidfd = if pidfd >= 0 {
        Some(unsafe { OwnedFd::from_raw_fd(pidfd as i32) })
    } else {
        None
    };
    loop {
        let now = Instant::now();
        if (!control_open
            || lease.elapsed().as_secs_f64() > spec.lease_seconds
            || binary_utils::stopping())
            && stop.is_none()
        {
            signal_group(child.pid, libc::SIGTERM)?;
            stop = Some((now, spec.grace_seconds));
            release = true;
        }
        if let Some((at, grace)) = stop
            && at.elapsed().as_secs_f64() >= grace
            && !killed
        {
            signal_group(child.pid, libc::SIGKILL)?;
            killed = true;
        }
        if code.is_none() {
            let mut descriptor = libc::pollfd {
                fd: pidfd.as_ref().map_or(-1, |fd| fd.as_raw_fd()),
                events: libc::POLLIN,
                revents: 0,
            };
            if pidfd.is_none() || unsafe { libc::poll(&mut descriptor, 1, 0) } > 0 {
                code = exit_code(child.pid)?;
            }
        }
        if !ready {
            let mut bytes = [0u8; 4];
            match read_fd(child.exec.as_raw_fd(), &mut bytes)? {
                Some(0) => {
                    if !exec_bytes.is_empty() {
                        let errno = i32::from_ne_bytes(
                            exec_bytes
                                .as_slice()
                                .try_into()
                                .map_err(|_| fault("protocol", "invalid exec failure"))?,
                        );
                        enqueue(
                            &mut queue,
                            error_response(&request, &io::Error::from_raw_os_error(errno), started),
                        )?;
                        release = true;
                        signal_group(child.pid, libc::SIGKILL)?;
                        stop = Some((now, 0.0));
                    } else {
                        launched = true;
                        identity = procfs::identity(child.pid).unwrap_or_else(|_| {
                            let mut pinned = child.identity.clone();
                            pinned.name = Path::new(&spec.argv[0]).file_name().map_or_else(
                                || spec.argv[0].clone(),
                                |name| name.to_string_lossy().into_owned(),
                            );
                            pinned
                        });
                        enqueue(
                            &mut queue,
                            response(
                                &request,
                                "launched",
                                json!({"workload":identity,"supervisor_pid":std::process::id()}),
                                started,
                            ),
                        )?;
                    }
                    ready = true;
                }
                Some(n) => exec_bytes.extend_from_slice(&bytes[..n]),
                None => {}
            }
        }
        if launched && code.is_some() && !out_open && !err_open && !completion_sent {
            enqueue(
                &mut queue,
                response(
                    &request,
                    "completed",
                    json!({"exit_code":code,"output_complete":output_complete}),
                    started,
                ),
            )?;
            completion_sent = true;
        }
        if stop.is_some() && code.is_some() && !cleanup_sent && !group_live(child.pid)? {
            // Escaped descendants can retain pipes indefinitely. Bound their drain after group cleanup.
            if (out_open || err_open)
                && group_clean_at.get_or_insert(now).elapsed() > Duration::from_secs(1)
            {
                out_open = false;
                err_open = false;
                output_complete = false;
            }
            if !out_open && !err_open {
                if launched && !completion_sent {
                    enqueue(
                        &mut queue,
                        response(
                            &request,
                            "completed",
                            json!({"exit_code":code,"output_complete":output_complete}),
                            started,
                        ),
                    )?;
                    completion_sent = true;
                }
                let final_value = json!({"workload":identity,"exit_code":code,"cleanup":"confirmed","scope":"process_group","output_complete":output_complete});
                store(&directory, "final.json", &final_value)?;
                for req in &stop_requests {
                    enqueue(
                        &mut queue,
                        response(req, "result", final_value.clone(), started),
                    )?;
                }
                enqueue(
                    &mut queue,
                    response(&request, "cleanup", final_value, started),
                )?;
                cleanup_sent = true;
                // Start the final-record stall deadline now, even for a long-silent workload.
                cleanup_progress = Some(Instant::now());
                unsafe {
                    libc::waitpid(child.pid, std::ptr::null_mut(), 0);
                }
                child.reaped = true;
            }
        }
        if cleanup_sent && release && queue.is_empty() {
            return Ok(());
        }
        // A disconnected or permanently blocked consumer cannot keep the supervisor alive after cleanup.
        if release && cleanup_progress.is_some_and(|last| last.elapsed() > Duration::from_secs(2)) {
            return Ok(());
        }
        if queue.len() > QUEUE_LIMIT && stop.is_some() && killed {
            output_complete = false;
        }
        let read_output = ready && queue.len() < QUEUE_LIMIT;
        let mut poll = [
            libc::pollfd {
                fd: 0,
                events: if control_open { libc::POLLIN } else { 0 },
                revents: 0,
            },
            libc::pollfd {
                fd: 1,
                events: if queue.is_empty() { 0 } else { libc::POLLOUT },
                revents: 0,
            },
            libc::pollfd {
                fd: child.out.as_raw_fd(),
                events: if out_open && read_output {
                    libc::POLLIN
                } else {
                    0
                },
                revents: 0,
            },
            libc::pollfd {
                fd: child.err.as_raw_fd(),
                events: if err_open && read_output {
                    libc::POLLIN
                } else {
                    0
                },
                revents: 0,
            },
        ];
        if unsafe { libc::poll(poll.as_mut_ptr(), poll.len() as _, 20) } < 0
            && io::Error::last_os_error().kind() != io::ErrorKind::Interrupted
        {
            return Err(io::Error::last_os_error());
        }
        if control_open && poll[0].revents != 0 {
            let mut buffer = [0u8; 8192];
            if let Some(n) = read_fd(0, &mut buffer)? {
                if n == 0 {
                    control_open = false;
                } else {
                    input.extend_from_slice(&buffer[..n]);
                }
            }
            while let Some(end) = input.iter().position(|b| *b == b'\n') {
                let line: Vec<_> = input.drain(..=end).collect();
                let req = match Request::parse(&line) {
                    Ok(r) => r,
                    Err(e) => {
                        enqueue(&mut queue, error_response(&request, &e, started))?;
                        control_open = false;
                        break;
                    }
                };
                if req.id <= last_request_id || queue.len() >= QUEUE_LIMIT {
                    control_open = false;
                    break;
                }
                last_request_id = req.id;
                match req.operation.as_str() {
                    "renew" => {
                        lease = Instant::now();
                    }
                    "identity" => {
                        let result = if code.is_some() {
                            Err(io::Error::from_raw_os_error(libc::ESRCH))
                        } else {
                            procfs::identity(child.pid).and_then(|actual| {
                                if actual.start_ticks == child.identity.start_ticks {
                                    Ok(serde_json::to_value(&identity)?)
                                } else {
                                    Err(fault("ownership", "workload identity changed"))
                                }
                            })
                        };
                        enqueue(
                            &mut queue,
                            match result {
                                Ok(value) => response(&req, "result", value, started),
                                Err(e) => error_response(&req, &e, started),
                            },
                        )?;
                    }
                    "stop" | "release" => {
                        let grace = match req.params.get("grace_seconds") {
                            None => spec.grace_seconds,
                            Some(value) => value.as_f64().unwrap_or(f64::NAN),
                        };
                        if !grace.is_finite() || !(0.0..=86400.0).contains(&grace) {
                            enqueue(
                                &mut queue,
                                error_response(&req, &fault("protocol", "invalid grace"), started),
                            )?;
                            continue;
                        }
                        if stop.is_none() {
                            signal_group(child.pid, libc::SIGTERM)?;
                            stop = Some((Instant::now(), grace));
                        }
                        if cleanup_sent {
                            enqueue(
                                &mut queue,
                                response(&req, "result", json!({"cleanup":"confirmed"}), started),
                            )?;
                        } else {
                            if stop_requests.len() >= 64 {
                                return Err(fault("limit", "too many pending stop requests"));
                            }
                            stop_requests.push(req);
                        }
                        release = true;
                    }
                    _ => enqueue(
                        &mut queue,
                        error_response(
                            &req,
                            &fault("unsupported", "unsupported supervisor operation"),
                            started,
                        ),
                    )?,
                }
            }
            if input.len() > protocol::MAX_LINE {
                control_open = false;
                input.clear();
            }
        }
        for (index, fd, open, stream) in [
            (2, child.out.as_raw_fd(), &mut out_open, "stdout"),
            (3, child.err.as_raw_fd(), &mut err_open, "stderr"),
        ] {
            if *open && read_output && poll[index].revents != 0 {
                let mut buffer = [0u8; CHUNK];
                if let Some(n) = read_fd(fd, &mut buffer)? {
                    if n == 0 {
                        *open = false;
                    } else {
                        enqueue(
                            &mut queue,
                            response(
                                &request,
                                "output",
                                json!({"stream":stream,"data":base64(&buffer[..n])}),
                                started,
                            ),
                        )?;
                    }
                }
            }
        }
        if !queue.is_empty() && poll[1].revents != 0 {
            let bytes = queue.make_contiguous();
            let n = unsafe { libc::write(1, bytes.as_ptr().cast(), bytes.len()) };
            if n > 0 {
                queue.drain(..n as usize);
                if cleanup_sent {
                    cleanup_progress = Some(Instant::now());
                }
            } else if n < 0 {
                let e = io::Error::last_os_error();
                if !matches!(
                    e.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                ) {
                    control_open = false;
                    output_complete = false;
                    queue.clear();
                }
            }
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn encoding() {
        assert_eq!(base64(b""), "");
        assert_eq!(base64(b"f"), "Zg==");
        assert_eq!(base64(b"fo"), "Zm8=");
        assert_eq!(base64(b"foo\0"), "Zm9vAA==");
    }
}
