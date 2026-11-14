//! Confirm a buffered HTTP/1 response only after the final body byte was written
//! to the downstream socket. Hyper may poll its Body long before a write succeeds.
use std::{
    io,
    pin::Pin,
    sync::{Arc, Mutex},
    task::{Context, Poll},
};

use tokio::{
    io::{AsyncRead, AsyncWrite, ReadBuf},
    net::TcpStream,
    sync::oneshot,
};

#[derive(Clone, Default)]
pub struct Delivery(Arc<Mutex<Option<Pending>>>);
struct Pending {
    complete: oneshot::Sender<()>,
    body: Framing,
    header_tail: u32,
    in_body: bool,
}

enum Framing {
    Fixed(usize),
    Chunked(Chunk),
}
enum Chunk {
    Size(usize),
    SizeLf(usize),
    Data(usize),
    DataCr,
    DataLf,
    EndCr,
    EndLf,
    Invalid,
}
impl Chunk {
    fn wrote(&mut self, mut data: &[u8]) -> bool {
        while !data.is_empty() {
            if let Self::Data(n) = self {
                let used = data.len().min(*n);
                *n -= used;
                data = &data[used..];
                if *n == 0 {
                    *self = Self::DataCr;
                }
                continue;
            }
            let byte = data[0];
            data = &data[1..];
            *self = match (&self, byte) {
                (Self::Size(n), b'\r') => Self::SizeLf(*n),
                (Self::Size(n), b) if b.is_ascii_hexdigit() => {
                    Self::Size(n * 16 + (b as char).to_digit(16).unwrap() as usize)
                }
                (Self::SizeLf(0), b'\n') => Self::EndCr,
                (Self::SizeLf(n), b'\n') => Self::Data(*n),
                (Self::DataCr, b'\r') => Self::DataLf,
                (Self::DataLf, b'\n') => Self::Size(0),
                (Self::EndCr, b'\r') => Self::EndLf,
                (Self::EndLf, b'\n') => return true,
                _ => Self::Invalid,
            }
        }
        false
    }
}
impl Delivery {
    pub fn track_stream(&self, complete: oneshot::Sender<()>) {
        *self.0.lock().unwrap() = Some(Pending {
            complete,
            body: Framing::Chunked(Chunk::Size(0)),
            header_tail: 0,
            in_body: false,
        });
    }
    pub fn track(&self, length: usize, complete: oneshot::Sender<()>) {
        *self.0.lock().unwrap() = Some(Pending {
            complete,
            body: Framing::Fixed(length),
            header_tail: 0,
            in_body: false,
        });
    }

    fn wrote(&self, bytes: &[u8]) {
        let mut state = self.0.lock().unwrap();
        let Some(p) = &mut *state else { return };
        let mut body = bytes;
        if !p.in_body {
            let Some(end) = bytes.iter().position(|&b| {
                p.header_tail = (p.header_tail << 8) | u32::from(b);
                p.header_tail == 0x0d0a0d0a
            }) else {
                return;
            };
            p.in_body = true;
            body = &bytes[end + 1..];
        }
        let finished = match &mut p.body {
            Framing::Fixed(n) => {
                *n = n.saturating_sub(body.len());
                *n == 0
            }
            Framing::Chunked(parser) => parser.wrote(body),
        };
        if finished {
            let _ = state.take().unwrap().complete.send(());
        }
    }
}
pub struct Socket {
    pub stream: TcpStream,
    pub delivery: Delivery,
}
impl Drop for Socket {
    fn drop(&mut self) {
        self.delivery.0.lock().unwrap().take();
    }
}
impl AsyncRead for Socket {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        Pin::new(&mut self.stream).poll_read(cx, buf)
    }
}
impl AsyncWrite for Socket {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        let result = Pin::new(&mut self.stream).poll_write(cx, buf);
        if let Poll::Ready(Ok(n)) = result {
            self.delivery.wrote(&buf[..n]);
        }
        result
    }
    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.stream).poll_flush(cx)
    }
    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.stream).poll_shutdown(cx)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn fragmented_header_and_partial_body_do_not_confirm_delivery() {
        let wire = Delivery::default();
        let (tx, mut result) = oneshot::channel();
        wire.track(5, tx);
        for chunk in [
            b"HTTP/1.1 200 OK\r\nContent-Length: 5\r".as_slice(),
            b"\n\r",
            b"\nab",
            b"cd",
        ] {
            wire.wrote(chunk);
            assert!(matches!(
                result.try_recv(),
                Err(oneshot::error::TryRecvError::Empty)
            ));
        }
        wire.wrote(b"e");
        assert!(result.try_recv().is_ok());
    }
}

#[cfg(test)]
mod stream_tests {
    use super::*;
    #[test]
    fn partial_chunk_and_terminator_inside_payload_never_acknowledge() {
        let wire = Delivery::default();
        let (tx, mut rx) = oneshot::channel();
        wire.track_stream(tx);
        for chunk in [
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r".as_slice(),
            b"\n\r\n7\r\n",
            b"x0\r\n\r\ny",
            b"\r\n0\r\n\r",
        ] {
            wire.wrote(chunk);
            assert!(matches!(
                rx.try_recv(),
                Err(oneshot::error::TryRecvError::Empty)
            ));
        }
        wire.wrote(b"\n");
        assert!(rx.try_recv().is_ok());
    }
}
