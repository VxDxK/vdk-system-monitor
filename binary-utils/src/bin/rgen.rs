//! Used by TrafficGenerator as a producer or consumer. TCP I/O calls are not
//! network packet counts; performance requires target benchmarking. JSONL includes
//! metadata, sequence, timestamps and a clean end marker.
//! Build and package binary-utils, then configure BinaryProvider.
//! Example: artifacts/rgen.bin producer --address 127.0.0.1:5000 --duration-ms 2000
//!
use binary_utils::{Writer, stopping};
use clap::{Parser, ValueEnum};
use serde_json::json;
use std::{
    io::{self, Read, Write},
    net::{SocketAddr, TcpListener, TcpStream, UdpSocket},
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    thread,
    time::{Duration, Instant},
};
#[derive(Clone, Copy, Debug, ValueEnum)]
enum Protocol {
    Tcp,
    Udp,
}
#[derive(Clone, Copy, Debug, ValueEnum)]
enum Role {
    Producer,
    Consumer,
}
#[derive(Parser)]
#[command(
    version,
    about = "TCP/UDP traffic generation with aggregate rate limit"
)]
struct Args {
    #[arg(value_enum)]
    role: Role,
    #[arg(long,value_enum,default_value_t=Protocol::Tcp)]
    protocol: Protocol,
    #[arg(long)]
    address: SocketAddr,
    /// Optional duration in milliseconds; omitted runs until signalled.
    #[arg(long)]
    duration_ms: Option<u64>,
    #[arg(long, default_value_t = 1)]
    workers: usize,
    #[arg(long, default_value_t = 1024)]
    payload_bytes: usize,
    #[arg(long)]
    rate_bps: Option<u64>,
    #[arg(long, default_value_t = 65536)]
    burst_bytes: usize,
}
struct Bucket {
    tokens: f64,
    rate: f64,
    capacity: f64,
    last: Instant,
}
impl Bucket {
    fn delay(&mut self, n: usize) -> Duration {
        let now = Instant::now();
        self.tokens = (self.tokens + now.duration_since(self.last).as_secs_f64() * self.rate)
            .min(self.capacity);
        self.last = now;
        if self.tokens >= n as f64 {
            self.tokens -= n as f64;
            Duration::ZERO
        } else {
            Duration::from_secs_f64((n as f64 - self.tokens) / self.rate)
        }
    }
}
fn permit(bucket: &Option<Arc<Mutex<Bucket>>>, n: usize, end: Option<Instant>) -> bool {
    if let Some(b) = bucket {
        loop {
            if stopping() || end.is_some_and(|limit| Instant::now() >= limit) {
                return false;
            }
            let delay = b.lock().unwrap().delay(n);
            if delay.is_zero() {
                break;
            }
            thread::sleep(delay.min(Duration::from_millis(20)));
        }
    }
    true
}
fn main() -> io::Result<()> {
    let a = Args::parse();
    if a.workers == 0
        || a.workers > 256
        || a.payload_bytes == 0
        || a.payload_bytes > 65507
        || a.duration_ms
            .is_some_and(|value| value == 0 || value > 86_400_000)
        || a.rate_bps == Some(0)
        || a.burst_bytes < a.payload_bytes
    {
        return Err(io::Error::other(
            "invalid workers, payload, duration, rate, or burst",
        ));
    }
    let mut out = Writer::new("rgen")?;
    let end = a
        .duration_ms
        .map(|value| Instant::now() + Duration::from_millis(value));
    let bytes = Arc::new(AtomicU64::new(0));
    let operations = Arc::new(AtomicU64::new(0));
    let bucket = a.rate_bps.map(|rate| {
        Arc::new(Mutex::new(Bucket {
            tokens: a.burst_bytes as f64,
            capacity: a.burst_bytes as f64,
            rate: rate as f64 / 8.0,
            last: Instant::now(),
        }))
    });
    let payload = Arc::new(vec![0x56; a.payload_bytes]);
    let mut jobs = vec![];
    let tcp_listener = if matches!((a.role, a.protocol), (Role::Consumer, Protocol::Tcp)) {
        let l = TcpListener::bind(a.address)?;
        l.set_nonblocking(true)?;
        Some(l)
    } else {
        None
    };
    let udp_consumer = if matches!((a.role, a.protocol), (Role::Consumer, Protocol::Udp)) {
        let s = UdpSocket::bind(a.address)?;
        s.set_read_timeout(Some(Duration::from_millis(100)))?;
        Some(s)
    } else {
        None
    };
    out.emit(json!({"type":"ready","role":format!("{:?}",a.role).to_lowercase(),"protocol":format!("{:?}",a.protocol).to_lowercase(),"address":a.address,"workers":a.workers}))?;
    if matches!(a.role, Role::Producer) {
        for _ in 0..a.workers {
            let (bytes, operations, payload, bucket) = (
                bytes.clone(),
                operations.clone(),
                payload.clone(),
                bucket.clone(),
            );
            let address = a.address;
            let protocol = a.protocol;
            jobs.push(thread::spawn(move || -> io::Result<()> {
                match protocol {
                    Protocol::Tcp => {
                        let mut stream =
                            TcpStream::connect_timeout(&address, Duration::from_secs(2))?;
                        stream.set_write_timeout(Some(Duration::from_millis(200)))?;
                        while !stopping() && end.is_none_or(|limit| Instant::now() < limit) {
                            if !permit(&bucket, payload.len(), end) {
                                break;
                            }
                            match stream.write(&payload) {
                                Ok(0) => break,
                                Ok(n) => {
                                    bytes.fetch_add(n as u64, Ordering::Relaxed);
                                    operations.fetch_add(1, Ordering::Relaxed);
                                }
                                Err(e)
                                    if matches!(
                                        e.kind(),
                                        io::ErrorKind::WouldBlock
                                            | io::ErrorKind::TimedOut
                                            | io::ErrorKind::Interrupted
                                    ) => {}
                                Err(e) => return Err(e),
                            }
                        }
                    }
                    Protocol::Udp => {
                        let socket = UdpSocket::bind(if address.is_ipv4() {
                            "0.0.0.0:0"
                        } else {
                            "[::]:0"
                        })?;
                        socket.set_write_timeout(Some(Duration::from_millis(200)))?;
                        socket.connect(address)?;
                        while !stopping() && end.is_none_or(|limit| Instant::now() < limit) {
                            if !permit(&bucket, payload.len(), end) {
                                break;
                            }
                            let n = match socket.send(&payload) {
                                Ok(n) => n,
                                Err(error) if error.kind() == io::ErrorKind::Interrupted => {
                                    continue;
                                }
                                Err(error) => return Err(error),
                            };
                            bytes.fetch_add(n as u64, Ordering::Relaxed);
                            operations.fetch_add(1, Ordering::Relaxed);
                        }
                    }
                }
                Ok(())
            }));
        }
    }
    let mut last = Instant::now();
    let (mut prev_bytes, mut prev_ops) = (0, 0);
    let mut buf = vec![0; 65536];
    while !stopping() && end.is_none_or(|limit| Instant::now() < limit) {
        let mut i = 0;
        while i < jobs.len() {
            if jobs[i].is_finished() {
                jobs.swap_remove(i)
                    .join()
                    .map_err(|_| io::Error::other("worker panicked"))??;
            } else {
                i += 1;
            }
        }
        if let Some(l) = &tcp_listener {
            match l.accept() {
                Ok((mut stream, _)) => {
                    if jobs.len() >= a.workers {
                        continue;
                    }
                    let (bytes, operations) = (bytes.clone(), operations.clone());
                    stream.set_read_timeout(Some(Duration::from_millis(100)))?;
                    jobs.push(thread::spawn(move || -> io::Result<()> {
                        let mut b = vec![0; 65536];
                        while !stopping() && end.is_none_or(|limit| Instant::now() < limit) {
                            match stream.read(&mut b) {
                                Ok(0) => break,
                                Ok(n) => {
                                    bytes.fetch_add(n as u64, Ordering::Relaxed);
                                    operations.fetch_add(1, Ordering::Relaxed);
                                }
                                Err(e)
                                    if matches!(
                                        e.kind(),
                                        io::ErrorKind::WouldBlock
                                            | io::ErrorKind::TimedOut
                                            | io::ErrorKind::Interrupted
                                    ) => {}
                                Err(e) => return Err(e),
                            }
                        }
                        Ok(())
                    }));
                }
                Err(e)
                    if matches!(
                        e.kind(),
                        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                    ) => {}
                Err(e) => return Err(e),
            }
        }
        if let Some(s) = &udp_consumer {
            match s.recv(&mut buf) {
                Ok(n) => {
                    bytes.fetch_add(n as u64, Ordering::Relaxed);
                    operations.fetch_add(1, Ordering::Relaxed);
                }
                Err(e)
                    if matches!(
                        e.kind(),
                        io::ErrorKind::WouldBlock
                            | io::ErrorKind::TimedOut
                            | io::ErrorKind::Interrupted
                    ) => {}
                Err(e) => return Err(e),
            }
        } else {
            thread::sleep(Duration::from_millis(10));
        }
        if last.elapsed() >= Duration::from_millis(100) {
            let (b, o) = (
                bytes.load(Ordering::Relaxed),
                operations.load(Ordering::Relaxed),
            );
            let dt = last.elapsed().as_secs_f64();
            out.emit(json!({"type":"traffic","bytes":b,"operations":o,"bits_per_second":(b-prev_bytes) as f64*8.0/dt,"operations_per_second":(o-prev_ops) as f64/dt,"operation_unit":if matches!(a.protocol,Protocol::Udp){"datagrams"}else{"io_calls"}}))?;
            prev_bytes = b;
            prev_ops = o;
            last = Instant::now();
        }
    }
    for job in jobs {
        job.join()
            .map_err(|_| io::Error::other("worker panicked"))??;
    }
    let b = bytes.load(Ordering::Relaxed);
    let o = operations.load(Ordering::Relaxed);
    let dt = last.elapsed().as_secs_f64();
    if dt > 0.0 && (b != prev_bytes || o != prev_ops) {
        out.emit(json!({"type":"traffic","bytes":b,"operations":o,"bits_per_second":(b-prev_bytes) as f64*8.0/dt,"operations_per_second":(o-prev_ops) as f64/dt,"operation_unit":if matches!(a.protocol,Protocol::Udp){"datagrams"}else{"io_calls"}}))?;
    }
    out.emit(json!({"type":"summary","bytes":bytes.load(Ordering::Relaxed),"operations":operations.load(Ordering::Relaxed)}))?;
    out.end()
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn aggregate_burst() {
        let mut b = Bucket {
            tokens: 100.0,
            rate: 100.0,
            capacity: 100.0,
            last: Instant::now(),
        };
        assert!(b.delay(100).is_zero());
        assert!(!b.delay(100).is_zero());
    }
}
