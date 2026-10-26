use serde::Deserialize;
use serde_json::{Value, json};
use std::{io, sync::OnceLock, time::Instant};
pub const MAX_LINE: usize = 1024 * 1024;
pub const MAX_ITEMS: usize = 65_536;
#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub id: u64,
    pub operation: String,
    #[serde(default)]
    pub params: Value,
}
impl Request {
    pub fn parse(data: &[u8]) -> io::Result<Self> {
        if data.len() > MAX_LINE || !data.ends_with(b"\n") {
            return Err(fault("protocol", "oversized or truncated request"));
        }
        let request: Self =
            serde_json::from_slice(data).map_err(|e| fault("protocol", &e.to_string()))?;
        if !request.params.is_object() {
            return Err(fault("protocol", "params must be an object"));
        }
        Ok(request)
    }
}
pub fn clock_ns() -> u64 {
    static START: OnceLock<Instant> = OnceLock::new();
    START.get_or_init(Instant::now).elapsed().as_nanos() as u64
}
pub fn boot() -> io::Result<String> {
    Ok(std::fs::read_to_string("/proc/sys/kernel/random/boot_id")?
        .trim()
        .into())
}
pub fn fault(category: &str, message: &str) -> io::Error {
    io::Error::other(format!("{category}: {message}"))
}
#[derive(Debug)]
pub struct ContextError {
    pub source: io::Error,
    pub context: String,
}
impl std::fmt::Display for ContextError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.context, self.source)
    }
}
impl std::error::Error for ContextError {}
pub fn errno(error: &io::Error) -> Option<i32> {
    error.raw_os_error().or_else(|| {
        error
            .get_ref()
            .and_then(|e| e.downcast_ref::<ContextError>())
            .and_then(|e| errno(&e.source))
    })
}
pub fn error_value(error: &io::Error) -> Value {
    if let Some(context) = error
        .get_ref()
        .and_then(|e| e.downcast_ref::<ContextError>())
    {
        let mut value = error_value(&context.source);
        value["message"] = json!(context.to_string());
        return value;
    }
    let text = error.to_string();
    let category = if error.raw_os_error().is_some() {
        "os"
    } else {
        text.split_once(": ").map_or("io", |v| v.0)
    };
    json!({"category":category,"errno":error.raw_os_error(),"message":text})
}
pub fn response(req: &Request, event: &str, result: Value, started: u64) -> Value {
    json!({"id":req.id,"operation":req.operation,"event":event,"boot_id":boot().ok(),"started_ns":started,"finished_ns":clock_ns(),"result":result})
}
pub fn error_response(req: &Request, error: &io::Error, started: u64) -> Value {
    let mut value = response(req, "error", json!({}), started);
    value["error"] = error_value(error);
    value
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn strict_requests() {
        for input in [
            b"{}\n".as_slice(),
            b"{\"version\":2,\"id\":1,\"operation\":\"x\",\"params\":{}}\n",
            b"{\"id\":true,\"operation\":\"x\",\"params\":{}}\n",
            b"{\"id\":1,\"operation\":\"x\",\"params\":[] }\n",
        ] {
            assert!(Request::parse(input).is_err());
        }
        assert!(Request::parse(&vec![b'x'; MAX_LINE + 1]).is_err());
        assert!(
            Request::parse(b"{\"id\":1,\"operation\":\"capabilities\",\"params\":{}}\n").is_ok()
        );
    }
}
