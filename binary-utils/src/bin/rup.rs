//! Used by Rup and RupProcessor, targeting upstream rup 0.15.1 CLI/wire behavior.
//! Client JSONL is rup.ping/3, wrapped by framework rup. Server service logs are
//! not client measurements. Terminal responses count as received. ICMP needs ping
//! socket permissions. Framework readiness and SIGTERM draining are opt-in.
//! Build and package binary-utils, then configure BinaryProvider.
//! Example: artifacts/rup.bin --protocol tcp --output jsonl 127.0.0.1:5000
//!
//! Standalone protocol-compatible RTT probe. All implementation lives in this file.
use clap::{CommandFactory, FromArgMatches, Parser, Subcommand, ValueEnum};
use serde_json::{Value, json};
use socket2::{Domain, Protocol as SocketProtocol, Socket, Type};
use std::{
    collections::{BTreeMap, HashMap},
    io::{self, Write},
    net::{IpAddr, SocketAddr},
    os::fd::AsRawFd,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt, Interest},
    net::{TcpListener, TcpSocket, TcpStream, UdpSocket, tcp::OwnedWriteHalf},
    sync::{mpsc, oneshot},
    task::JoinSet,
    time::{Instant, sleep_until, timeout},
};

const HEADER: usize = 12;
const QUEUE: usize = 256;
#[derive(Clone, Copy, Debug, PartialEq, Eq, ValueEnum)]
enum Protocol {
    Tcp,
    Udp,
    Icmp,
}
impl Protocol {
    fn name(self) -> &'static str {
        match self {
            Self::Tcp => "tcp",
            Self::Udp => "udp",
            Self::Icmp => "icmp",
        }
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, ValueEnum)]
enum Output {
    Human,
    Jsonl,
}
#[derive(Subcommand, Debug)]
enum Command {
    Server { local_address: SocketAddr },
}
#[derive(Parser, Debug)]
#[command(
    name = "rup",
    version,
    about = "Universal TCP/UDP/ICMP RTT probe",
    subcommand_negates_reqs = true,
    arg_required_else_help = true
)]
struct Args {
    #[arg(value_name = "remote-address", required = true)]
    remote: Option<String>,
    #[command(subcommand)]
    command: Option<Command>,
    #[arg(short = 'p', long, value_enum)]
    protocol: Option<Protocol>,
    #[arg(long)]
    local_address: Option<SocketAddr>,
    #[arg(short='i', long, default_value="1000", value_parser=positive, conflicts_with="adaptive_interval")]
    interval: u64,
    #[arg(short = 'A', long, conflicts_with = "interval")]
    adaptive_interval: bool,
    #[arg(short='W', long, default_value="1000", value_parser=positive)]
    wait_time: u64,
    #[arg(long, value_parser=packet_size)]
    request_size: Option<u16>,
    #[arg(long, value_parser=packet_size)]
    response_size: Option<u16>,
    #[arg(long)]
    tos: Option<u8>,
    #[arg(short='n', long, value_parser=positive)]
    ping_number: Option<u64>,
    #[arg(short='t', long, value_parser=positive)]
    run_time: Option<u64>,
    #[arg(long, value_enum, default_value = "human")]
    output: Output,
    #[arg(long, value_name = "template")]
    format: Option<String>,
}
fn positive(s: &str) -> Result<u64, String> {
    s.parse::<u64>()
        .ok()
        .filter(|n| *n > 0)
        .ok_or_else(|| "expected a positive integer".into())
}
fn packet_size(s: &str) -> Result<u16, String> {
    s.parse::<u16>()
        .ok()
        .filter(|n| *n >= HEADER as u16)
        .ok_or_else(|| "packet size must be in 12..65535".into())
}
fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message.into())
}

// Explicit byte encoding; Rust struct layout never determines the wire format.
fn request(seq: u64, size: u16, response: u16) -> Vec<u8> {
    let mut bytes = vec![0; usize::from(size)];
    bytes[..8].copy_from_slice(&seq.to_le_bytes());
    bytes[8..10].copy_from_slice(&size.to_le_bytes());
    bytes[10..12].copy_from_slice(&response.to_le_bytes());
    bytes
}
fn header(bytes: &[u8]) -> io::Result<(u64, usize, u16)> {
    if bytes.len() < HEADER {
        return Err(invalid("short echo header"));
    }
    let seq = u64::from_le_bytes(bytes[..8].try_into().unwrap());
    let length = u16::from_le_bytes(bytes[8..10].try_into().unwrap());
    let response = u16::from_le_bytes(bytes[10..12].try_into().unwrap());
    if length < HEADER as u16 || (response != 0 && response < HEADER as u16) {
        return Err(invalid("invalid echo length"));
    }
    Ok((seq, usize::from(length), response))
}
fn echo(bytes: &[u8]) -> io::Result<Vec<u8>> {
    let (seq, _, response) = header(bytes)?;
    let length = if response == 0 {
        bytes.len() as u16
    } else {
        response
    };
    Ok(request(seq, length, 0))
}
fn checksum(bytes: &[u8]) -> u16 {
    let mut sum: u32 = bytes
        .chunks(2)
        .map(|b| (u32::from(b[0]) << 8) | u32::from(*b.get(1).unwrap_or(&0)))
        .sum();
    while sum > 0xffff {
        sum = (sum & 0xffff) + (sum >> 16);
    }
    !(sum as u16)
}
fn icmp_request(seq: u64, size: u16, ipv6: bool) -> Vec<u8> {
    let mut bytes = vec![0; 8];
    bytes[0] = if ipv6 { 128 } else { 8 };
    bytes[6..8].copy_from_slice(&(seq as u16).to_be_bytes());
    bytes.extend(request(seq, size, 0));
    if !ipv6 {
        let check = checksum(&bytes);
        bytes[2..4].copy_from_slice(&check.to_be_bytes());
    }
    bytes
}
fn option(fd: i32, level: i32, name: i32, value: i32) -> io::Result<()> {
    let result = unsafe {
        libc::setsockopt(
            fd,
            level,
            name,
            (&value as *const i32).cast(),
            std::mem::size_of::<i32>() as _,
        )
    };
    if result < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}
fn set_tos(fd: i32, ipv6: bool, value: u8) -> io::Result<()> {
    option(
        fd,
        if ipv6 {
            libc::IPPROTO_IPV6
        } else {
            libc::IPPROTO_IP
        },
        if ipv6 {
            libc::IPV6_TCLASS
        } else {
            libc::IP_TOS
        },
        i32::from(value),
    )
}
fn unspecified(remote: SocketAddr) -> SocketAddr {
    SocketAddr::new(
        if remote.is_ipv4() {
            "0.0.0.0".parse().unwrap()
        } else {
            "::".parse().unwrap()
        },
        0,
    )
}
fn datagram(
    local: SocketAddr,
    protocol: Protocol,
    tos: Option<u8>,
    errors: bool,
) -> io::Result<UdpSocket> {
    let ipv6 = local.is_ipv6();
    let socket = Socket::new(
        Domain::for_address(local),
        Type::DGRAM,
        Some(if protocol == Protocol::Icmp {
            if ipv6 {
                SocketProtocol::ICMPV6
            } else {
                SocketProtocol::ICMPV4
            }
        } else {
            SocketProtocol::UDP
        }),
    )?;
    socket.set_nonblocking(true)?;
    socket.bind(&local.into())?;
    if let Some(tos) = tos {
        set_tos(socket.as_raw_fd(), ipv6, tos)?;
    }
    if errors {
        option(
            socket.as_raw_fd(),
            if ipv6 {
                libc::IPPROTO_IPV6
            } else {
                libc::IPPROTO_IP
            },
            if ipv6 {
                libc::IPV6_RECVERR
            } else {
                libc::IP_RECVERR
            },
            1,
        )?;
    }
    // Received TTL/TCLASS are optional, unlike requested outgoing TOS.
    let _ = option(
        socket.as_raw_fd(),
        if ipv6 {
            libc::IPPROTO_IPV6
        } else {
            libc::IPPROTO_IP
        },
        if ipv6 {
            libc::IPV6_RECVHOPLIMIT
        } else {
            libc::IP_RECVTTL
        },
        1,
    );
    let _ = option(
        socket.as_raw_fd(),
        if ipv6 {
            libc::IPPROTO_IPV6
        } else {
            libc::IPPROTO_IP
        },
        if ipv6 {
            libc::IPV6_RECVTCLASS
        } else {
            libc::IP_RECVTOS
        },
        1,
    );
    UdpSocket::from_std(socket.into())
}
struct Datagram {
    bytes: Vec<u8>,
    address: Option<SocketAddr>,
    ttl: Option<u8>,
    tos: Option<u8>,
    terminal: bool,
}
fn receive_message(fd: i32, error_queue: bool) -> io::Result<Datagram> {
    let mut bytes = vec![0; 65535];
    let mut control = [0usize; 128]; // Aligned storage for ancillary message headers.
    let mut address: libc::sockaddr_storage = unsafe { std::mem::zeroed() };
    let mut vector = libc::iovec {
        iov_base: bytes.as_mut_ptr().cast(),
        iov_len: bytes.len(),
    };
    let mut msg: libc::msghdr = unsafe { std::mem::zeroed() };
    msg.msg_name = (&mut address as *mut libc::sockaddr_storage).cast();
    msg.msg_namelen = std::mem::size_of_val(&address) as _;
    msg.msg_iov = &mut vector;
    msg.msg_iovlen = 1;
    msg.msg_control = control.as_mut_ptr().cast();
    // musl uses socklen_t here while glibc uses size_t. This fixed buffer
    // fits both representations; infer the libc field type at the boundary.
    msg.msg_controllen = std::mem::size_of_val(&control) as _;
    let n = unsafe {
        libc::recvmsg(
            fd,
            &mut msg,
            libc::MSG_DONTWAIT | if error_queue { libc::MSG_ERRQUEUE } else { 0 },
        )
    };
    if n < 0 {
        return Err(io::Error::last_os_error());
    }
    if msg.msg_flags & (libc::MSG_TRUNC | libc::MSG_CTRUNC) != 0 {
        return Err(invalid("truncated packet or control message"));
    }
    bytes.truncate(n as usize);
    let mut result = Datagram {
        bytes,
        address: None,
        ttl: None,
        tos: None,
        terminal: false,
    };
    if usize::from(address.ss_family) == libc::AF_INET as usize
        && msg.msg_namelen as usize >= std::mem::size_of::<libc::sockaddr_in>()
    {
        let a = unsafe {
            (&address as *const libc::sockaddr_storage)
                .cast::<libc::sockaddr_in>()
                .read_unaligned()
        };
        result.address = Some(SocketAddr::new(
            IpAddr::V4(a.sin_addr.s_addr.to_ne_bytes().into()),
            u16::from_be(a.sin_port),
        ));
    } else if i32::from(address.ss_family) == libc::AF_INET6
        && msg.msg_namelen as usize >= std::mem::size_of::<libc::sockaddr_in6>()
    {
        let a = unsafe {
            (&address as *const libc::sockaddr_storage)
                .cast::<libc::sockaddr_in6>()
                .read_unaligned()
        };
        result.address = Some(SocketAddr::new(
            IpAddr::V6(a.sin6_addr.s6_addr.into()),
            u16::from_be(a.sin6_port),
        ));
    }
    let mut current = unsafe { libc::CMSG_FIRSTHDR(&msg) };
    while !current.is_null() {
        let h = unsafe { &*current };
        let length = h.cmsg_len.saturating_sub(unsafe { libc::CMSG_LEN(0) as _ });
        let data = unsafe { libc::CMSG_DATA(current) };
        match (h.cmsg_level, h.cmsg_type) {
            (libc::IPPROTO_IP, libc::IP_TTL) | (libc::IPPROTO_IPV6, libc::IPV6_HOPLIMIT)
                if length >= 4 =>
            {
                result.ttl = u8::try_from(unsafe { data.cast::<i32>().read_unaligned() }).ok();
            }
            (libc::IPPROTO_IP, libc::IP_TOS) if length >= 1 => result.tos = Some(unsafe { *data }),
            (libc::IPPROTO_IPV6, libc::IPV6_TCLASS) if length >= 4 => {
                result.tos = u8::try_from(unsafe { data.cast::<i32>().read_unaligned() }).ok()
            }
            (libc::IPPROTO_IP, libc::IP_RECVERR) | (libc::IPPROTO_IPV6, libc::IPV6_RECVERR)
                if length >= 16 =>
            {
                // Linux sock_extended_err.ee_origin is byte four; accept remote ICMP only.
                result.terminal = matches!(unsafe { *data.add(4) }, 2 | 3);
            }
            _ => {}
        }
        current = unsafe { libc::CMSG_NXTHDR(&msg, current) };
    }
    Ok(result)
}
async fn receive_datagram(socket: &UdpSocket, errors: bool) -> io::Result<Datagram> {
    loop {
        socket.readable().await?;
        let result = socket.try_io(Interest::READABLE, || {
            if errors {
                match receive_message(socket.as_raw_fd(), true) {
                    Ok(d) if d.terminal => return Ok(d),
                    Ok(_) => {}
                    Err(e) if e.kind() == io::ErrorKind::WouldBlock => {}
                    Err(e) => return Err(e),
                }
            }
            receive_message(socket.as_raw_fd(), false)
        });
        match result {
            Err(e)
                if e.kind() == io::ErrorKind::WouldBlock
                    || e.kind() == io::ErrorKind::ConnectionRefused =>
            {
                continue;
            }
            other => return other,
        }
    }
}
async fn read_frame<R: AsyncReadExt + Unpin>(reader: &mut R) -> io::Result<Vec<u8>> {
    let mut bytes = vec![0; HEADER];
    reader.read_exact(&mut bytes).await?;
    let (_, length, _) = header(&bytes)?;
    bytes.resize(length, 0);
    reader.read_exact(&mut bytes[HEADER..]).await?;
    Ok(bytes)
}
fn ready() -> io::Result<()> {
    if let Some(path) = std::env::var_os("VSM_RUP_READY_FILE") {
        std::fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(path)?
            .write_all(b"ready\n")?;
    }
    Ok(())
}
async fn term(signal: &mut Option<tokio::signal::unix::Signal>) {
    if let Some(s) = signal {
        s.recv().await;
    } else {
        std::future::pending::<()>().await;
    }
}
fn term_signal() -> io::Result<Option<tokio::signal::unix::Signal>> {
    if std::env::var_os("VSM_RUP_READY_FILE").is_some() {
        Ok(Some(tokio::signal::unix::signal(
            tokio::signal::unix::SignalKind::terminate(),
        )?))
    } else {
        Ok(None)
    }
}
async fn server(address: SocketAddr, protocol: Protocol) -> io::Result<()> {
    let managed = std::env::var_os("VSM_RUP_READY_FILE").is_some();
    let mut signal = term_signal()?;
    let mut interrupt = if managed {
        Some(tokio::signal::unix::signal(
            tokio::signal::unix::SignalKind::interrupt(),
        )?)
    } else {
        None
    };
    match protocol {
        Protocol::Icmp => return Err(invalid("there is no server for ICMP")),
        Protocol::Udp => {
            let socket = datagram(address, protocol, None, false)?;
            println!("Running UDP server listening {address}");
            ready()?;
            loop {
                tokio::select! {
                    result = receive_datagram(&socket, false) => {
                        let d = match result {
                            Ok(d) => d,
                            Err(error) => { eprintln!("UDP receive: {error}"); continue; }
                        };
                        if let (Some(peer), Ok(reply)) = (d.address, echo(&d.bytes)) {
                            if let Some(value) = d.tos { set_tos(socket.as_raw_fd(), peer.is_ipv6(), value)?; }
                            if let Err(error) = socket.send_to(&reply, peer).await { eprintln!("UDP echo: {error}"); }
                        }
                    }
                    _ = term(&mut signal) => break,
                    _ = term(&mut interrupt), if managed => break,
                }
            }
        }
        Protocol::Tcp => {
            let listener = TcpListener::bind(address).await?;
            let mut workers = JoinSet::new();
            println!("Running TCP server listening {address}");
            ready()?;
            loop {
                tokio::select! {
                    incoming = listener.accept() => {
                        let (mut stream, _) = incoming?;
                        if workers.len() >= QUEUE { continue; }
                        workers.spawn(async move {
                            while let Ok(bytes) = read_frame(&mut stream).await {
                                let reply = echo(&bytes)?;
                                stream.write_all(&reply).await?;
                            }
                            Ok::<(), io::Error>(())
                        });
                    }
                    _ = workers.join_next(), if !workers.is_empty() => {},
                    _ = term(&mut signal) => break,
                    _ = term(&mut interrupt), if managed => break,
                }
            }
            workers.abort_all();
            while workers.join_next().await.is_some() {}
        }
    }
    Ok(())
}

#[derive(Clone, Debug)]
struct Reply {
    seq: u64,
    at: Instant,
    size: usize,
    ttl: Option<u8>,
}
#[derive(Debug)]
enum Event {
    Sent(u64, Instant),
    Reply(Reply),
    Failure(io::Error),
    Transmitted,
}
enum Connection {
    Datagram(std::sync::Arc<UdpSocket>),
    TcpInitial(TcpSocket),
    TcpEcho(OwnedWriteHalf),
    TcpProbe,
}
fn tcp_socket(local: SocketAddr, tos: Option<u8>) -> io::Result<TcpSocket> {
    let s = if local.is_ipv4() {
        TcpSocket::new_v4()?
    } else {
        TcpSocket::new_v6()?
    };
    s.bind(local)?;
    if let Some(value) = tos {
        set_tos(s.as_raw_fd(), local.is_ipv6(), value)?;
    }
    Ok(s)
}
fn spawn_reader(
    stream: TcpStream,
    events: mpsc::Sender<Event>,
    jobs: &mut JoinSet<()>,
) -> OwnedWriteHalf {
    let (mut reader, writer) = stream.into_split();
    jobs.spawn(async move {
        loop {
            match read_frame(&mut reader).await {
                Ok(bytes) => {
                    let seq = header(&bytes).unwrap().0;
                    if events
                        .send(Event::Reply(Reply {
                            seq,
                            at: Instant::now(),
                            size: bytes.len(),
                            ttl: None,
                        }))
                        .await
                        .is_err()
                    {
                        break;
                    }
                }
                Err(e) => {
                    if e.kind() != io::ErrorKind::UnexpectedEof {
                        let _ = events.send(Event::Failure(e)).await;
                    }
                    break;
                }
            }
        }
    });
    writer
}
#[derive(Clone, Copy)]
struct Network {
    remote: SocketAddr,
    local: SocketAddr,
    protocol: Protocol,
    wait: Duration,
    size: u16,
    response: u16,
    tos: Option<u8>,
}
async fn transport(
    net: Network,
    mut requests: mpsc::Receiver<u64>,
    events: mpsc::Sender<Event>,
    mut connection: Connection,
    mut shutdown: oneshot::Receiver<()>,
) {
    let mut readers = JoinSet::new();
    if let Connection::Datagram(socket) = &connection {
        let socket = socket.clone();
        let tx = events.clone();
        readers.spawn(async move {
            loop {
                match receive_datagram(&socket, net.protocol == Protocol::Udp).await {
                    Ok(d) => {
                        let bytes = if net.protocol == Protocol::Icmp {
                            if d.bytes.len() < 8
                                || d.bytes[0] != if net.remote.is_ipv6() { 129 } else { 0 }
                                || d.bytes[1] != 0
                            {
                                continue;
                            }
                            &d.bytes[8..]
                        } else {
                            &d.bytes[..]
                        };
                        if let Ok((seq, length, _)) = header(bytes) {
                            if !d.terminal && length != bytes.len() {
                                continue;
                            }
                            if tx
                                .send(Event::Reply(Reply {
                                    seq,
                                    at: Instant::now(),
                                    size: if d.terminal { 0 } else { bytes.len() },
                                    ttl: if d.terminal { None } else { d.ttl },
                                }))
                                .await
                                .is_err()
                            {
                                break;
                            }
                        }
                    }
                    Err(e) => {
                        let _ = tx.send(Event::Failure(e)).await;
                        break;
                    }
                }
            }
        });
    }
    {
        let sending = async {
            while let Some(seq) = requests.recv().await {
                let packet = if net.protocol == Protocol::Icmp {
                    icmp_request(seq, net.size, net.remote.is_ipv6())
                } else {
                    request(seq, net.size, net.response)
                };
                let mut started = Instant::now();
                let mut terminal = false;
                let send: io::Result<()> = async {
                    if matches!(connection, Connection::TcpInitial(_)) {
                        let state = std::mem::replace(&mut connection, Connection::TcpProbe);
                        if let Connection::TcpInitial(socket) = state {
                            match timeout(net.wait, socket.connect(net.remote)).await {
                                Ok(Ok(stream)) => {
                                    connection = Connection::TcpEcho(spawn_reader(
                                        stream,
                                        events.clone(),
                                        &mut readers,
                                    ));
                                }
                                Ok(Err(_)) => terminal = true,
                                Err(_) => {}
                            }
                            if !matches!(connection, Connection::TcpEcho(_)) {
                                return Ok(());
                            }
                        }
                    }
                    match &mut connection {
                        Connection::TcpEcho(writer) => {
                            started = Instant::now();
                            timeout(Duration::from_secs(30), writer.write_all(&packet))
                                .await
                                .map_err(|_| {
                                    io::Error::new(io::ErrorKind::TimedOut, "TCP write timeout")
                                })??;
                        }
                        Connection::TcpProbe => {
                            let socket = tcp_socket(net.local, net.tos)?;
                            started = Instant::now();
                            terminal = timeout(net.wait, socket.connect(net.remote)).await.is_ok();
                        }
                        Connection::Datagram(socket) => {
                            socket.send(&packet).await?;
                        }
                        Connection::TcpInitial(_) => unreachable!(),
                    }
                    Ok(())
                }
                .await;
                if let Err(error) = send {
                    let _ = events.send(Event::Failure(error)).await;
                    break;
                }
                if events.send(Event::Sent(seq, started)).await.is_err() {
                    break;
                }
                if terminal
                    && events
                        .send(Event::Reply(Reply {
                            seq,
                            at: Instant::now(),
                            size: 0,
                            ttl: None,
                        }))
                        .await
                        .is_err()
                {
                    break;
                }
            }
            let _ = events.send(Event::Transmitted).await;
            // The receiver must stay alive until the tracker settles pending outcomes.
            std::future::pending::<()>().await;
        };
        tokio::select! { _ = sending => {}, _ = &mut shutdown => {} }
    }
    readers.abort_all();
    while readers.join_next().await.is_some() {}
}

#[derive(Debug, Clone)]
enum Part {
    Text(String),
    Field(String),
}
fn template(text: &str) -> Result<Vec<Part>, String> {
    let mut parts = Vec::new();
    let mut literal = String::new();
    let mut chars = text.chars().peekable();
    while let Some(ch) = chars.next() {
        match ch {
            '{' if chars.peek() == Some(&'{') => {
                chars.next();
                literal.push('{');
            }
            '}' if chars.peek() == Some(&'}') => {
                chars.next();
                literal.push('}');
            }
            '{' => {
                if !literal.is_empty() {
                    parts.push(Part::Text(std::mem::take(&mut literal)));
                }
                let mut name = String::new();
                loop {
                    match chars.next() {
                        Some('}') => break,
                        Some('{') | None => return Err("unmatched brace".into()),
                        Some(c) => name.push(c),
                    }
                }
                let valid = [
                    "target",
                    "ip",
                    "seq",
                    "rtt",
                    "rtt_ms",
                    "inter_packet",
                    "inter_packet_ms",
                    "size",
                    "ttl",
                    "status",
                    "protocol",
                ]
                .contains(&name.as_str())
                    || ["rtt_ms:.", "inter_packet_ms:."].iter().any(|prefix| {
                        name.strip_prefix(prefix)
                            .is_some_and(|n| n.len() == 1 && matches!(n.as_bytes()[0], b'0'..=b'6'))
                    });
                if !valid {
                    return Err(format!("invalid format field: {name}"));
                }
                parts.push(Part::Field(name));
            }
            '}' => return Err("unmatched brace".into()),
            '\\' => literal.push(match chars.next() {
                Some('n') => '\n',
                Some('t') => '\t',
                Some('\\') => '\\',
                _ => return Err("unsupported format escape".into()),
            }),
            other => literal.push(other),
        }
    }
    if !literal.is_empty() {
        parts.push(Part::Text(literal));
    }
    Ok(parts)
}
struct Printer {
    output: Output,
    started: Instant,
    epoch: u64,
    target: String,
    remote: SocketAddr,
    protocol: Protocol,
    format: Option<Vec<Part>>,
    previous: Option<Instant>,
}
impl Printer {
    fn json(&self, value: Value) {
        println!("{value}");
    }
    fn timestamp(&self) -> u64 {
        self.epoch
            .saturating_add(self.started.elapsed().as_millis() as u64)
    }
    fn outcome(&mut self, seq: u64, kind: &str, reply: Option<(&Reply, Duration)>) {
        if let Some((reply, duration)) = reply {
            let ms = duration.as_secs_f64() * 1000.0;
            let gap = self
                .previous
                .replace(reply.at)
                .map(|last| reply.at.saturating_duration_since(last).as_secs_f64() * 1000.0);
            if self.output == Output::Jsonl {
                self.json(json!({"record":kind,"timestamp_ms":self.timestamp(),"seq":seq,"rtt_ms":ms,"size_bytes":reply.size,"ttl":reply.ttl}));
            } else if let Some(parts) = &self.format {
                let mut line = String::new();
                for part in parts {
                    match part {
                        Part::Text(s) => line.push_str(s),
                        Part::Field(field) => {
                            let (base, precision) = field
                                .split_once(":.")
                                .map_or((field.as_str(), 3), |(b, n)| {
                                    (b, n.parse::<usize>().unwrap())
                                });
                            line.push_str(&match base {
                                "target" => self.target.clone(),
                                "ip" => self.remote.ip().to_string(),
                                "seq" => seq.to_string(),
                                "rtt" => format!("{ms:.3} ms"),
                                "rtt_ms" => format!("{ms:.precision$}"),
                                "inter_packet" => gap.map_or("-".into(), |g| format!("{g:.3} ms")),
                                "inter_packet_ms" => {
                                    gap.map_or("-".into(), |g| format!("{g:.precision$}"))
                                }
                                "size" => reply.size.to_string(),
                                "ttl" => reply.ttl.map_or("-".into(), |n| n.to_string()),
                                "status" => kind.into(),
                                "protocol" => self.protocol.name().into(),
                                _ => unreachable!(),
                            });
                        }
                    }
                }
                println!("{line}");
            } else if reply.size == 0 {
                println!(
                    "terminal response from {}: seq={seq} time={ms:.3} ms",
                    self.remote.ip()
                );
            } else {
                let ttl = reply.ttl.map_or(String::new(), |n| format!(" ttl={n}"));
                println!(
                    "{} bytes from {}: seq={seq}{ttl} time={ms:.3} ms",
                    reply.size,
                    self.remote.ip()
                );
            }
        } else if self.output == Output::Jsonl {
            self.json(json!({"record":kind,"timestamp_ms":self.timestamp(),"seq":seq}));
        }
    }
}
#[derive(Default)]
struct Tracker {
    sent: u64,
    pending: BTreeMap<u64, Instant>,
    early: HashMap<u64, Reply>,
    rtts: Vec<Duration>,
}
impl Tracker {
    fn reply(&mut self, reply: Reply, printer: &mut Printer) -> bool {
        if !self.pending.contains_key(&reply.seq) {
            if reply.seq >= self.sent && self.early.len() < QUEUE {
                self.early.insert(reply.seq, reply);
            }
            return false;
        }
        let older: Vec<u64> = self
            .pending
            .range(..reply.seq)
            .map(|(&seq, _)| seq)
            .collect();
        for seq in older {
            self.pending.remove(&seq);
            printer.outcome(seq, "reorder_or_loss", None);
        }
        let start = self.pending.remove(&reply.seq).unwrap();
        let rtt = reply.at.saturating_duration_since(start);
        printer.outcome(
            reply.seq,
            if reply.size == 0 {
                "terminal_reply"
            } else {
                "reply"
            },
            Some((&reply, rtt)),
        );
        self.rtts.push(rtt);
        true
    }
    fn event(&mut self, event: Event, printer: &mut Printer) -> io::Result<bool> {
        match event {
            Event::Sent(seq, at) => {
                self.sent += 1;
                self.pending.insert(seq, at);
                if let Some(reply) = self.early.remove(&seq) {
                    return Ok(self.reply(reply, printer));
                }
            }
            Event::Reply(reply) => return Ok(self.reply(reply, printer)),
            Event::Failure(error) => return Err(error),
            Event::Transmitted => {}
        }
        Ok(false)
    }
    fn expire(&mut self, now: Instant, wait: Duration, printer: &mut Printer) -> bool {
        let expired: Vec<_> = self
            .pending
            .iter()
            .filter(|(_, at)| now.saturating_duration_since(**at) >= wait)
            .map(|(&id, _)| id)
            .collect();
        for id in &expired {
            self.pending.remove(id);
            printer.outcome(*id, "timeout", None);
        }
        !expired.is_empty()
    }
}
fn statistics(rtts: &[Duration]) -> [Option<f64>; 5] {
    if rtts.is_empty() {
        return [None; 5];
    }
    let mut ns: Vec<u128> = rtts.iter().map(Duration::as_nanos).collect();
    ns.sort_unstable();
    let mean = ns.iter().sum::<u128>() / ns.len() as u128;
    let deviation = (ns
        .iter()
        .map(|n| (*n as f64 - mean as f64).powi(2))
        .sum::<f64>()
        / ns.len() as f64)
        .sqrt();
    [
        Some(ns[0] as f64 / 1e6),
        Some(mean as f64 / 1e6),
        Some(ns[ns.len() / 2] as f64 / 1e6),
        Some(ns[ns.len() - 1] as f64 / 1e6),
        Some(Duration::from_secs_f64(deviation / 1e9).as_secs_f64() * 1000.0),
    ]
}
fn next_tick(previous: Instant, now: Instant, interval: Duration) -> Instant {
    previous
        .checked_add(interval)
        .filter(|at| *at > now)
        .unwrap_or_else(|| now.checked_add(interval).expect("validated interval"))
}
fn target_address(input: &str, protocol: Protocol) -> io::Result<String> {
    if let Ok(ip) = input.parse::<IpAddr>() {
        if protocol != Protocol::Icmp {
            return Err(invalid("TCP/UDP requires a port"));
        }
        return Ok(SocketAddr::new(ip, 0).to_string());
    }
    if protocol == Protocol::Icmp && input.starts_with('[') && input.ends_with(']') {
        let ip = input[1..input.len() - 1]
            .parse::<IpAddr>()
            .map_err(|error| invalid(error.to_string()))?;
        return Ok(SocketAddr::new(ip, 0).to_string());
    }
    if !input.contains(':') {
        return if protocol == Protocol::Icmp {
            Ok(format!("{input}:0"))
        } else {
            Err(invalid("TCP/UDP requires a port"))
        };
    }
    Ok(input.to_owned())
}
async fn resolve(input: &str, protocol: Protocol) -> io::Result<SocketAddr> {
    let address = target_address(input, protocol)?;
    let mut result = tokio::net::lookup_host(address)
        .await?
        .next()
        .ok_or_else(|| invalid("no address found"))?;
    if protocol == Protocol::Icmp {
        result.set_port(0);
    }
    Ok(result)
}
async fn client(args: Args) -> io::Result<()> {
    let protocol = args.protocol.unwrap_or(Protocol::Icmp);
    let target = args.remote.clone().unwrap();
    let remote = resolve(&target, protocol).await?;
    let local = args.local_address.unwrap_or_else(|| unspecified(remote));
    if local.is_ipv4() != remote.is_ipv4() {
        return Err(invalid("local and remote address families differ"));
    }
    let mut signal = term_signal()?;
    let size = args.request_size.unwrap_or(HEADER as u16);
    let epoch = u64::try_from(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(io::Error::other)?
            .as_millis(),
    )
    .map_err(io::Error::other)?;
    let mut printer = Printer {
        output: args.output,
        started: Instant::now(),
        epoch,
        target,
        remote,
        protocol,
        format: args
            .format
            .as_deref()
            .map(template)
            .transpose()
            .map_err(invalid)?,
        previous: None,
    };
    let packet_size = usize::from(size)
        + if remote.is_ipv4() { 20 } else { 40 }
        + if protocol == Protocol::Tcp { 20 } else { 8 };
    if args.output == Output::Jsonl {
        printer.json(json!({"schema":"rup.ping","version":3,"record":"metadata","started_at_ms":epoch,"target":printer.target,"address":remote.ip(),"protocol":protocol.name(),"request_size_bytes":size,"packet_size_bytes":packet_size,"interval_ms":args.interval,"adaptive":args.adaptive_interval}));
    } else {
        println!(
            "PING {} ({}) {}({packet_size}) bytes of data.",
            printer.target,
            remote.ip(),
            size
        );
    }
    let connection = if protocol == Protocol::Tcp {
        Connection::TcpInitial(tcp_socket(local, args.tos)?)
    } else {
        let socket = datagram(local, protocol, args.tos, protocol == Protocol::Udp)?;
        socket.connect(remote).await?;
        Connection::Datagram(std::sync::Arc::new(socket))
    };
    let (requests, rx) = mpsc::channel(QUEUE);
    let mut requests = Some(requests);
    let (events, mut incoming) = mpsc::channel(QUEUE);
    let net = Network {
        remote,
        local,
        protocol,
        wait: Duration::from_millis(args.wait_time),
        size,
        response: args.response_size.unwrap_or(0),
        tos: args.tos,
    };
    let (stop_transport, shutdown) = oneshot::channel();
    let mut interrupt = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())?;
    let worker = tokio::spawn(transport(net, rx, events, connection, shutdown));
    // Always abort and join the transport and its receivers, even on output/setup failure.
    let result = async {
        ready()?;
        let mut tracker = Tracker::default();
        let mut generated = 0;
        let mut transmitted = false;
        let mut next = Some(Instant::now());
        let interval = Duration::from_millis(args.interval);
        let deadline = args.run_time.and_then(|seconds| Instant::now().checked_add(Duration::from_secs(seconds)));
        loop {
            let now = Instant::now();
            let limit_reached = args.ping_number.is_some_and(|n| generated >= n)
                || deadline.is_some_and(|d| now >= d);
            if requests.is_some() && limit_reached {
                requests.take();
                next = None;
            }
            if transmitted && tracker.pending.is_empty() { break; }
            let wake = next.into_iter()
                .chain(deadline.filter(|_| requests.is_some()))
                .chain(tracker.pending.values().filter_map(|at| at.checked_add(net.wait)))
                .min().unwrap_or(now + Duration::from_secs(86400));
            tokio::select! {
                event = incoming.recv() => {
                    let event = event.ok_or_else(|| io::Error::other("transport ended unexpectedly"))?;
                    if matches!(event, Event::Transmitted) { transmitted = true; }
                    let settled = tracker.event(event, &mut printer)?;
                    if settled && args.adaptive_interval && requests.is_some() {
                        next = Some(Instant::now());
                    }
                }
                _ = sleep_until(wake) => {
                    let now = Instant::now();
                    if tracker.expire(now, net.wait, &mut printer) && args.adaptive_interval && requests.is_some() {
                        next = Some(now);
                    }
                    if deadline.is_some_and(|d| now >= d) { requests.take(); next = None; }
                    if next.is_some_and(|at| now >= at) && let Some(sender) = &requests {
                        match sender.try_send(generated) {
                            Ok(()) => {
                                generated += 1;
                                next = if args.adaptive_interval { None } else {
                                    Some(next_tick(next.unwrap(), now, interval))
                                };
                            }
                            Err(mpsc::error::TrySendError::Full(_)) => next = Some(now + Duration::from_millis(1)),
                            Err(_) => return Err(io::Error::other("transmitter closed")),
                        }
                    }
                }
                _ = interrupt.recv(), if requests.is_some() => { requests.take(); next = None; }
                _ = term(&mut signal), if requests.is_some() => { requests.take(); next = None; }
            }
        }
        let [min, mean, median, max, stddev] = statistics(&tracker.rtts);
        let received = tracker.rtts.len() as u64;
        let loss = if tracker.sent == 0 { 0.0 } else {
            (tracker.sent - received) as f64 / tracker.sent as f64 * 100.0
        };
        if args.output == Output::Jsonl {
            printer.json(json!({
                "record": "summary", "timestamp_ms": printer.timestamp(),
                "sent": tracker.sent, "received": received, "loss_percent": loss,
                "rtt_min_ms": min, "rtt_mean_ms": mean, "rtt_median_ms": median,
                "rtt_max_ms": max, "rtt_std_dev_ms": stddev
            }));
        } else {
            println!("\n--- {} ping statistics ---", printer.target);
            println!("{} packets transmitted, {received} received, {loss:.0}% packet loss, time {}ms",
                tracker.sent, printer.started.elapsed().as_millis());
            if let [Some(min), Some(mean), _, Some(max), Some(stddev)] = [min, mean, median, max, stddev] {
                println!("rtt min/avg/max/mdev = {min:.3}/{mean:.3}/{max:.3}/{stddev:.3} ms");
            }
        }
        Ok(())
    }.await;
    let _ = stop_transport.send(());
    worker.await.map_err(io::Error::other)?;
    result
}
fn validate_cli(args: &Args) -> Result<(), String> {
    for duration in [
        Duration::from_millis(args.interval),
        Duration::from_millis(args.wait_time),
        Duration::from_secs(args.run_time.unwrap_or(0)),
    ] {
        if Instant::now().checked_add(duration).is_none() {
            return Err("time value is too large".into());
        }
    }
    if Instant::now()
        .checked_add(Duration::from_millis(args.interval))
        .is_none()
    {
        return Err("interval is too large".into());
    }
    if args.output == Output::Jsonl && args.format.is_some() {
        return Err("--format cannot be used with --output jsonl".into());
    }
    if let Some(text) = &args.format {
        template(text)?;
    }
    if args.command.is_some()
        && (args.remote.is_some()
            || args.local_address.is_some()
            || args.adaptive_interval
            || args.interval != 1000
            || args.wait_time != 1000
            || args.request_size.is_some()
            || args.response_size.is_some()
            || args.tos.is_some()
            || args.ping_number.is_some()
            || args.run_time.is_some()
            || args.format.is_some()
            || args.output != Output::Human)
    {
        return Err("client options cannot be used with the server subcommand".into());
    }
    Ok(())
}
#[tokio::main(flavor = "current_thread")]
async fn main() -> std::process::ExitCode {
    let matches = Args::command().get_matches();
    if matches.subcommand_name() == Some("server") {
        let client_fields = [
            "remote",
            "local_address",
            "interval",
            "adaptive_interval",
            "wait_time",
            "request_size",
            "response_size",
            "tos",
            "ping_number",
            "run_time",
            "output",
            "format",
        ];
        if client_fields.iter().any(|field| {
            matches.value_source(field) == Some(clap::parser::ValueSource::CommandLine)
        }) {
            clap::Error::raw(
                clap::error::ErrorKind::ArgumentConflict,
                "client options cannot be used with the server subcommand",
            )
            .exit();
        }
    }
    let args = Args::from_arg_matches(&matches).unwrap_or_else(|error| error.exit());
    if let Err(error) = validate_cli(&args) {
        clap::Error::raw(clap::error::ErrorKind::ArgumentConflict, error).exit();
    }
    let result = if let Some(Command::Server { local_address }) = args.command {
        server(local_address, args.protocol.unwrap_or(Protocol::Udp)).await
    } else {
        client(args).await
    };
    match result {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("{error}");
            std::process::ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn original_header_and_different_response_size() {
        let bytes = request(0x0807060504030201, 16, 24);
        assert_eq!(&bytes[..12], &[1, 2, 3, 4, 5, 6, 7, 8, 16, 0, 24, 0]);
        let reply = echo(&bytes).unwrap();
        assert_eq!(reply.len(), 24);
        assert_eq!(&reply[..12], &[1, 2, 3, 4, 5, 6, 7, 8, 24, 0, 0, 0]);
        assert_eq!(header(&reply).unwrap().0, 0x0807060504030201);
    }
    #[test]
    fn framing_rejects_bad_sizes() {
        for n in 0..HEADER {
            assert!(header(&vec![0; n]).is_err());
        }
        assert!(header(&[0; 12]).is_err());
        assert_eq!(echo(&request(2, 12, 0)).unwrap(), request(2, 12, 0));
    }
    #[test]
    fn icmp_checksum_and_sequence() {
        let bytes = icmp_request(65537, 32, false);
        assert_eq!(checksum(&bytes), 0);
        assert_eq!(bytes.len(), 40);
        assert_eq!(header(&bytes[8..]).unwrap().0, 65537);
        assert_eq!(icmp_request(1, 12, true)[0], 128);
    }
    #[test]
    fn report_statistics_match_integer_duration_semantics() {
        let values = [Duration::from_micros(1), Duration::from_micros(2)];
        let s = statistics(&values);
        assert_eq!(s[1], Some(0.0015));
        assert_eq!(s[2], Some(0.002));
        assert_eq!(statistics(&[]), [None; 5]);
    }
    #[test]
    fn format_accepts_documented_fields_only() {
        assert!(template("{{{ip}}} {rtt_ms:.2}\\n{seq} {inter_packet_ms:.6}").is_ok());
        for text in ["{bad}", "{rtt_ms:.7}", "{rtt:.1}", "}", "{", r"\q"] {
            assert!(template(text).is_err(), "{text}");
        }
    }
    #[test]
    fn cli_defaults_and_conflicts() {
        let args = Args::try_parse_from(["rup", "127.0.0.1"]).unwrap();
        assert_eq!(args.protocol, None);
        assert_eq!(args.interval, 1000);
        assert!(Args::try_parse_from(["rup", "-i", "10", "-A", "127.0.0.1"]).is_err());
        assert!(Args::try_parse_from(["rup", "--request-size", "11", "127.0.0.1"]).is_err());
        assert!(
            validate_cli(
                &Args::try_parse_from([
                    "rup",
                    "--output",
                    "jsonl",
                    "--format",
                    "{seq}",
                    "127.0.0.1"
                ])
                .unwrap()
            )
            .is_err()
        );
    }
    fn printer() -> Printer {
        Printer {
            output: Output::Jsonl,
            started: Instant::now(),
            epoch: 1000,
            target: "127.0.0.1:1".into(),
            remote: "127.0.0.1:1".parse().unwrap(),
            protocol: Protocol::Udp,
            format: None,
            previous: None,
        }
    }
    #[test]
    fn response_before_send_registration_is_matched_once() {
        let mut p = printer();
        let mut tracker = Tracker::default();
        let at = Instant::now();
        let reply = Reply {
            seq: 0,
            at: at + Duration::from_millis(3),
            size: 12,
            ttl: Some(64),
        };
        assert!(!tracker.event(Event::Reply(reply.clone()), &mut p).unwrap());
        assert!(tracker.event(Event::Sent(0, at), &mut p).unwrap());
        assert_eq!(tracker.rtts, vec![Duration::from_millis(3)]);
        assert!(!tracker.reply(reply, &mut p));
        assert_eq!(tracker.rtts.len(), 1);
        assert!(tracker.early.is_empty());
    }
    #[test]
    fn terminal_responses_count_as_received_and_late_older_replies_are_ignored() {
        let mut p = printer();
        let mut tracker = Tracker::default();
        let at = Instant::now();
        for seq in 0..2 {
            tracker.event(Event::Sent(seq, at), &mut p).unwrap();
        }
        assert!(tracker.reply(
            Reply {
                seq: 1,
                at: at + Duration::from_millis(4),
                size: 0,
                ttl: None
            },
            &mut p
        ));
        assert!(tracker.pending.is_empty());
        assert_eq!(tracker.sent, 2);
        assert_eq!(tracker.rtts.len(), 1);
        assert!(!tracker.reply(
            Reply {
                seq: 0,
                at: at + Duration::from_millis(5),
                size: 12,
                ttl: None
            },
            &mut p
        ));
        assert_eq!(tracker.rtts.len(), 1);
    }
    #[test]
    fn timeout_settles_at_deadline_without_creating_rtt_samples() {
        let mut p = printer();
        let mut tracker = Tracker::default();
        let at = Instant::now();
        let wait = Duration::from_millis(10);
        tracker.event(Event::Sent(0, at), &mut p).unwrap();
        assert!(!tracker.expire(at + Duration::from_millis(9), wait, &mut p));
        assert!(tracker.expire(at + wait, wait, &mut p));
        assert!(tracker.pending.is_empty());
        assert_eq!(tracker.sent, 1);
        assert!(tracker.rtts.is_empty());
        assert!(!tracker.expire(at + wait, wait, &mut p));
    }
    #[test]
    fn fixed_schedule_does_not_accumulate_small_delays_or_burst_after_stalls() {
        let start = Instant::now();
        let interval = Duration::from_millis(10);
        assert_eq!(
            next_tick(start, start + Duration::from_millis(3), interval),
            start + interval
        );
        assert_eq!(
            next_tick(start, start + Duration::from_millis(35), interval),
            start + Duration::from_millis(45)
        );
    }
    #[test]
    fn receive_races_have_bounded_storage() {
        let mut tracker = Tracker::default();
        let mut p = printer();
        for seq in 0..(QUEUE as u64 + 100) {
            tracker.reply(
                Reply {
                    seq,
                    at: Instant::now(),
                    size: 12,
                    ttl: None,
                },
                &mut p,
            );
        }
        assert_eq!(tracker.early.len(), QUEUE);
    }
    #[tokio::test]
    async fn tcp_stream_framing_handles_partial_reads_and_multiple_packets() {
        let (mut writer, mut reader) = tokio::io::duplex(16);
        let task = tokio::spawn(async move {
            let a = request(1, 24, 32);
            let b = request(2, 12, 0);
            writer.write_all(&a[..3]).await.unwrap();
            writer.write_all(&a[3..]).await.unwrap();
            writer.write_all(&b).await.unwrap();
        });
        assert_eq!(read_frame(&mut reader).await.unwrap(), request(1, 24, 32));
        assert_eq!(read_frame(&mut reader).await.unwrap(), request(2, 12, 0));
        assert_eq!(
            read_frame(&mut reader).await.unwrap_err().kind(),
            io::ErrorKind::UnexpectedEof
        );
        task.await.unwrap();
    }
    #[test]
    fn address_normalization_preserves_families_and_requires_udp_tcp_ports() {
        for input in ["::1", "[::1]", "[::1]:123"] {
            let normalized = target_address(input, Protocol::Icmp).unwrap();
            assert!(normalized.starts_with("[::1]:"));
        }
        assert_eq!(
            target_address("host.example", Protocol::Icmp).unwrap(),
            "host.example:0"
        );
        assert_eq!(
            target_address("127.0.0.1", Protocol::Icmp).unwrap(),
            "127.0.0.1:0"
        );
        for input in ["127.0.0.1", "::1", "host.example"] {
            assert!(target_address(input, Protocol::Udp).is_err());
        }
        assert_eq!(
            target_address("[::1]:5000", Protocol::Tcp).unwrap(),
            "[::1]:5000"
        );
        assert!(Args::try_parse_from(["rup", "-A", "127.0.0.1"]).is_ok());
    }
}
