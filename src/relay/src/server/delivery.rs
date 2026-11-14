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
    remaining: usize,
    header_tail: u32,
    in_body: bool,
}

impl Delivery {
    pub fn track(&self, length: usize, complete: oneshot::Sender<()>) {
        *self.0.lock().unwrap() = Some(Pending {
            complete,
            remaining: length,
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
        p.remaining = p.remaining.saturating_sub(body.len());
        if p.remaining == 0 {
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
