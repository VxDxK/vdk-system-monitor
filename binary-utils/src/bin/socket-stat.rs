//! Used by SocketStat. Requires Linux inet_diag/netlink access.
//! Kernel-dependent optional counters may be absent. JSONL includes
//! metadata, sequence, monotonic timestamps and a clean end marker.
//! Build and package binary-utils, then configure BinaryProvider.
//! Example: artifacts/socket-stat.bin --duration-ms 2000
//!
use binary_utils::{Writer, sample_loop, validate};
use clap::Parser;
use serde_json::{Value, json};
use std::{
    io,
    net::{Ipv4Addr, Ipv6Addr},
    os::fd::{AsRawFd, FromRawFd, OwnedFd},
};
#[derive(Parser)]
#[command(version, about = "Linux inet_diag TCP/UDP socket snapshots")]
struct Args {
    /// Sampling interval in milliseconds (positive integer).
    #[arg(long, default_value_t = 250)]
    interval_ms: u64,
    /// Optional duration in milliseconds; omitted runs until signalled.
    #[arg(long)]
    duration_ms: Option<u64>,
    #[arg(long)]
    samples: Option<u64>,
}
fn u32_at(b: &[u8], i: usize) -> u32 {
    u32::from_ne_bytes(b[i..i + 4].try_into().unwrap())
}
fn endpoint(b: &[u8], offset: usize, family: u8) -> String {
    if family == libc::AF_INET as u8 {
        Ipv4Addr::new(b[offset], b[offset + 1], b[offset + 2], b[offset + 3]).to_string()
    } else {
        Ipv6Addr::from(<[u8; 16]>::try_from(&b[offset..offset + 16]).unwrap()).to_string()
    }
}
fn decode(b: &[u8], protocol: u8) -> io::Result<Value> {
    if b.len() < 72 {
        return Err(io::Error::other("short inet_diag message"));
    }
    if ![libc::AF_INET as u8, libc::AF_INET6 as u8].contains(&b[0]) {
        return Err(io::Error::other("unsupported family"));
    }
    let mut result = json!({"type":"socket","protocol":if protocol==libc::IPPROTO_TCP as u8 {"tcp"} else {"udp"},"family":b[0],"state":b[1],"source":endpoint(b,8,b[0]),"destination":endpoint(b,24,b[0]),"source_port":u16::from_be_bytes([b[4],b[5]]),"destination_port":u16::from_be_bytes([b[6],b[7]]),"interface":u32_at(b,40),"cookie":[u32_at(b,44),u32_at(b,48)],"receive_queue":u32_at(b,56),"send_queue":u32_at(b,60),"uid":u32_at(b,64),"inode":u32_at(b,68)});
    let mut pos = 72;
    while pos + 4 <= b.len() {
        let size = u16::from_ne_bytes([b[pos], b[pos + 1]]) as usize;
        let kind = u16::from_ne_bytes([b[pos + 2], b[pos + 3]]) & 0x3fff;
        if size < 4 || pos + size > b.len() {
            return Err(io::Error::other("invalid netlink attribute"));
        }
        let data = &b[pos + 4..pos + size];
        if kind == 2 {
            result["tcp_info_hex"] = json!(binary_utils::hex(data));
            for (offset, name) in [
                (120, "bytes_acked"),
                (128, "bytes_received"),
                (200, "bytes_sent"),
                (208, "bytes_retransmitted"),
            ] {
                if data.len() >= offset + 8 {
                    result[name] = json!(u64::from_ne_bytes(
                        data[offset..offset + 8].try_into().unwrap()
                    ));
                }
            }
            for (offset, name) in [
                (68, "rtt_us"),
                (72, "rtt_variance_us"),
                (80, "congestion_window_segments"),
                (92, "receive_rtt_us"),
            ] {
                if data.len() >= offset + 4 {
                    result[name] = json!(u32_at(data, offset));
                }
            }
        } else if kind == 1 {
            for (offset, name) in [
                (0, "receive_memory"),
                (4, "write_memory"),
                (8, "forward_memory"),
                (12, "option_memory"),
            ] {
                if data.len() >= offset + 4 {
                    result[name] = json!(u32_at(data, offset));
                }
            }
        } else if kind == 7 {
            for (index, name) in [
                "receive_memory",
                "receive_buffer",
                "write_memory",
                "send_buffer",
                "forward_memory",
                "write_queued",
                "option_memory",
                "backlog_memory",
            ]
            .iter()
            .enumerate()
            {
                if data.len() >= (index + 1) * 4 {
                    result[*name] = json!(u32_at(data, index * 4));
                }
            }
        }
        pos += (size + 3) & !3;
    }
    Ok(result)
}
fn snapshot(w: &mut Writer, family: u8, protocol: u8) -> io::Result<()> {
    let fd = unsafe {
        libc::socket(
            libc::AF_NETLINK,
            libc::SOCK_RAW | libc::SOCK_CLOEXEC,
            libc::NETLINK_SOCK_DIAG,
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let fd = unsafe { OwnedFd::from_raw_fd(fd) };
    let timeout = libc::timeval {
        tv_sec: 1,
        tv_usec: 0,
    };
    unsafe {
        libc::setsockopt(
            fd.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_RCVTIMEO,
            &timeout as *const _ as *const libc::c_void,
            std::mem::size_of_val(&timeout) as u32,
        );
    }
    let mut addr: libc::sockaddr_nl = unsafe { std::mem::zeroed() };
    addr.nl_family = libc::AF_NETLINK as u16;
    let mut req = [0u8; 72];
    req[0..4].copy_from_slice(&72u32.to_ne_bytes());
    req[4..6].copy_from_slice(&20u16.to_ne_bytes());
    req[6..8].copy_from_slice(&0x301u16.to_ne_bytes());
    req[8..12].copy_from_slice(&1u32.to_ne_bytes());
    req[16] = family;
    req[17] = protocol;
    req[18] = 1 | 2 | 64; // INET_DIAG_MEMINFO, INFO, SKMEMINFO
    req[20..24].copy_from_slice(&u32::MAX.to_ne_bytes());
    req[64..72].fill(255);
    if unsafe {
        libc::sendto(
            fd.as_raw_fd(),
            req.as_ptr() as _,
            req.len(),
            0,
            &addr as *const _ as _,
            std::mem::size_of_val(&addr) as u32,
        )
    } < 0
    {
        return Err(io::Error::last_os_error());
    }
    let mut buffer = vec![0u8; 262144];
    loop {
        let n = unsafe { libc::recv(fd.as_raw_fd(), buffer.as_mut_ptr() as _, buffer.len(), 0) };
        if n < 0 {
            return Err(io::Error::last_os_error());
        }
        if n == 0 {
            return Err(io::Error::other("netlink EOF"));
        }
        if n as usize > buffer.len() {
            return Err(io::Error::other("truncated netlink datagram"));
        }
        let b = &buffer[..n as usize];
        let mut pos = 0;
        while pos + 16 <= b.len() {
            let len = u32_at(b, pos) as usize;
            if len < 16 || pos + len > b.len() {
                return Err(io::Error::other("truncated netlink frame"));
            }
            let kind = u16::from_ne_bytes([b[pos + 4], b[pos + 5]]);
            if kind == 3 {
                let flags = u16::from_ne_bytes([b[pos + 6], b[pos + 7]]);
                if flags & 0x10 != 0 {
                    return Err(io::Error::other("netlink dump interrupted"));
                }
                return Ok(());
            }
            if kind == 2 {
                if len < 20 {
                    return Err(io::Error::other("short netlink error"));
                }
                let errno = i32::from_ne_bytes(b[pos + 16..pos + 20].try_into().unwrap());
                if errno != 0 {
                    return Err(io::Error::from_raw_os_error(-errno));
                }
            } else if kind == 20 {
                w.emit(decode(&b[pos + 16..pos + len], protocol)?)?;
            }
            pos += (len + 3) & !3;
        }
    }
}
fn main() -> io::Result<()> {
    let a = Args::parse();
    validate(a.interval_ms, a.duration_ms).map_err(io::Error::other)?;
    if a.samples == Some(0) {
        return Err(io::Error::other("samples must be positive"));
    }
    sample_loop(
        Writer::new("socket-stat")?,
        a.interval_ms,
        a.duration_ms,
        a.samples,
        |w| {
            w.emit(json!({"type":"sample"}))?;
            for family in [libc::AF_INET as u8, libc::AF_INET6 as u8] {
                for protocol in [libc::IPPROTO_TCP as u8, libc::IPPROTO_UDP as u8] {
                    snapshot(w, family, protocol)?;
                }
            }
            Ok(())
        },
    )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn short_rejected() {
        assert!(decode(&[0; 71], 6).is_err());
    }
    #[test]
    fn socket_identity() {
        let mut b = [0u8; 72];
        b[0] = 2;
        b[1] = 1;
        b[4..6].copy_from_slice(&123u16.to_be_bytes());
        b[8..12].copy_from_slice(&[127, 0, 0, 1]);
        b[68..72].copy_from_slice(&99u32.to_ne_bytes());
        let s = decode(&b, 6).unwrap();
        assert_eq!(s["source"], "127.0.0.1");
        assert_eq!(s["source_port"], 123);
        assert_eq!(s["inode"], 99);
    }
    #[test]
    fn diagnostic_extensions_are_length_checked() {
        let mut b = vec![0u8; 72 + 4 + 96 + 4 + 32];
        b[0] = 2;
        b[72..74].copy_from_slice(&100u16.to_ne_bytes());
        b[74..76].copy_from_slice(&2u16.to_ne_bytes());
        b[156..160].copy_from_slice(&17u32.to_ne_bytes());
        b[168..172].copy_from_slice(&42u32.to_ne_bytes());
        b[172..174].copy_from_slice(&36u16.to_ne_bytes());
        b[174..176].copy_from_slice(&7u16.to_ne_bytes());
        b[180..184].copy_from_slice(&4096u32.to_ne_bytes());
        let s = decode(&b, 6).unwrap();
        assert_eq!(s["congestion_window_segments"], 17);
        assert_eq!(s["receive_rtt_us"], 42);
        assert_eq!(s["receive_buffer"], 4096);
        assert!(s.get("bytes_acked").is_none());
        b[72..74].copy_from_slice(&8u16.to_ne_bytes());
        b.truncate(80);
        let s = decode(&b, 6).unwrap();
        assert!(s.get("rtt_us").is_none());
    }
    #[test]
    fn tcp_counter_extension() {
        let mut b = vec![0u8; 212];
        b[0] = 2;
        b[72..74].copy_from_slice(&140u16.to_ne_bytes());
        b[74..76].copy_from_slice(&2u16.to_ne_bytes());
        b[196..204].copy_from_slice(&7u64.to_ne_bytes());
        b[204..212].copy_from_slice(&9u64.to_ne_bytes());
        let s = decode(&b, 6).unwrap();
        assert_eq!(s["bytes_acked"], 7);
        assert_eq!(s["bytes_received"], 9);
        assert!(s.get("bytes_sent").is_none());
    }
}

#[cfg(test)]
mod interval_tests {
    use super::*;

    #[test]
    fn default_sampling_interval() {
        let args = Args::try_parse_from(["socket-stat"]).unwrap();
        assert_eq!(args.interval_ms, 250);
        assert_eq!(args.duration_ms, None);
    }
}
