//! Bounded, incremental SSE framing. The field parser is adapted from CC Switch,
//! src-tauri/src/proxy/sse.rs at 8c76bfe1e2628fe385b2c009020036d85510b01d.
//! See licenses/cc-switch-MIT.txt. Nexus validates UTF-8 and rejects incomplete EOF.
use super::ProtocolError;

#[inline]
fn strip_sse_field<'a>(line: &'a str, field: &str) -> Option<&'a str> {
    let value = line.strip_prefix(field)?.strip_prefix(':')?;
    Some(value.strip_prefix(' ').unwrap_or(value))
}

#[derive(Debug, PartialEq, Eq)]
pub struct Event {
    pub name: Option<String>,
    pub data: String,
}

pub struct Decoder {
    limit: usize,
    size: usize,
    line: Vec<u8>,
    skip_lf: bool,
    name: Option<String>,
    data: String,
    has_data: bool,
}

impl Decoder {
    pub fn new(event_limit: usize) -> Self {
        Self {
            limit: event_limit,
            size: 0,
            line: Vec::new(),
            skip_lf: false,
            name: None,
            data: String::new(),
            has_data: false,
        }
    }

    /// The limit applies per event, independently of network chunk boundaries.
    pub fn feed(
        &mut self,
        bytes: &[u8],
        mut emit: impl FnMut(Event) -> Result<(), ProtocolError>,
    ) -> Result<(), ProtocolError> {
        for &byte in bytes {
            if std::mem::take(&mut self.skip_lf) && byte == b'\n' {
                continue;
            }
            if self.size >= self.limit {
                return Err(ProtocolError::EventTooLarge);
            }
            self.size += 1;
            match byte {
                b'\r' | b'\n' => {
                    self.skip_lf = byte == b'\r';
                    if let Some(event) = self.end_line()? {
                        emit(event)?;
                    }
                }
                _ => self.line.push(byte),
            }
        }
        Ok(())
    }

    fn end_line(&mut self) -> Result<Option<Event>, ProtocolError> {
        if self.line.is_empty() {
            self.size = 0;
            let name = self.name.take();
            if std::mem::take(&mut self.has_data) {
                return Ok(Some(Event {
                    name,
                    data: std::mem::take(&mut self.data),
                }));
            }
        } else {
            let line =
                std::str::from_utf8(&self.line).map_err(|_| ProtocolError::Invalid("SSE UTF-8"))?;
            if let Some(value) =
                strip_sse_field(line, "data").or_else(|| (line == "data").then_some(""))
            {
                if self.has_data {
                    self.data.push('\n');
                }
                self.data.push_str(value);
                self.has_data = true;
            } else if let Some(value) =
                strip_sse_field(line, "event").or_else(|| (line == "event").then_some(""))
            {
                self.name = (!value.is_empty()).then(|| value.to_owned());
            }
            self.line.clear();
        }
        Ok(None)
    }

    pub fn finish(self) -> Result<(), ProtocolError> {
        if self.size != 0 {
            return Err(ProtocolError::Truncated);
        }
        Ok(())
    }
}
