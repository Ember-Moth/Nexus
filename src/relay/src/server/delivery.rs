//! Confirm a buffered HTTP/1 response only after the final body byte was written
//! to the downstream socket. Hyper may poll its Body long before a write succeeds.
//!
//! Streams also count the bytes written to the socket, so a wait on a slow client times out
//! only after `idle` without any write progress (`progressing`), not after a fixed time.
use std::{
    future::Future,
    io,
    pin::Pin,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    task::{Context, Poll},
    time::Duration,
};

use tokio::{
    io::{AsyncRead, AsyncWrite, ReadBuf},
    net::TcpStream,
    sync::oneshot,
    time::Instant,
};

#[derive(Clone, Default)]
pub struct Delivery(Arc<Mutex<State>>);
#[derive(Default)]
struct State {
    pending: Option<Pending>,
    /// Bytes written to the socket since a stream or an upgraded WebSocket started counting.
    written: Option<Arc<AtomicU64>>,
}
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
    /// Confirm the chunked stream's terminator on `complete`; count its bytes in `written`.
    pub fn track_stream(&self, complete: oneshot::Sender<()>, written: Arc<AtomicU64>) {
        let mut state = self.0.lock().unwrap();
        state.pending = Some(Pending {
            complete,
            body: Framing::Chunked(Chunk::Size(0)),
            header_tail: 0,
            in_body: false,
        });
        state.written = Some(written);
    }
    pub fn track(&self, length: usize, complete: oneshot::Sender<()>) {
        self.0.lock().unwrap().pending = Some(Pending {
            complete,
            body: Framing::Fixed(length),
            header_tail: 0,
            in_body: false,
        });
    }
    /// Count every byte written to this connection from now on (an upgraded WebSocket).
    pub fn count(&self) -> Arc<AtomicU64> {
        let written = Arc::new(AtomicU64::new(0));
        self.0.lock().unwrap().written = Some(written.clone());
        written
    }

    fn wrote(&self, bytes: &[u8]) {
        let mut guard = self.0.lock().unwrap();
        let state = &mut *guard;
        if let Some(written) = &state.written {
            written.fetch_add(bytes.len() as u64, Ordering::Relaxed);
        }
        let Some(p) = &mut state.pending else { return };
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
            let _ = state.pending.take().unwrap().complete.send(());
        }
    }
}

tokio::task_local! {
    /// Bytes written to the upgraded connection of the WebSocket session running in this task.
    pub static WEBSOCKET_WRITTEN: Arc<AtomicU64>;
}

/// Wait for `work`, a write towards the session's client, while the client keeps reading;
/// outside a session this is a plain `idle` timeout.
pub async fn client_write<F: Future>(idle: Duration, work: F) -> Result<F::Output, ()> {
    match WEBSOCKET_WRITTEN.try_with(Arc::clone) {
        Ok(written) => progressing(&written, idle, idle * 10, work).await,
        Err(_) => tokio::time::timeout(idle, work).await.map_err(|_| ()),
    }
}

/// Wait for `work` while the client keeps reading: fail once `idle` passes without any byte
/// written to its socket (`written`), or once `cap` passes in all.
pub async fn progressing<F: Future>(
    written: &AtomicU64,
    idle: Duration,
    cap: Duration,
    work: F,
) -> Result<F::Output, ()> {
    tokio::pin!(work);
    let started = Instant::now();
    let check = (idle / 8).max(Duration::from_millis(1));
    let mut seen = written.load(Ordering::Relaxed);
    let mut progressed = started;
    loop {
        let wake = (Instant::now() + check)
            .min(progressed + idle)
            .min(started + cap);
        tokio::select! {biased;
            output = &mut work => return Ok(output),
            _ = tokio::time::sleep_until(wake) => {}
        }
        let now = Instant::now();
        let total = written.load(Ordering::Relaxed);
        if total != seen {
            seen = total;
            progressed = now;
        }
        if now >= progressed + idle || now >= started + cap {
            return Err(());
        }
    }
}
pub struct Socket {
    pub stream: TcpStream,
    pub delivery: Delivery,
}
impl Drop for Socket {
    fn drop(&mut self) {
        self.delivery.0.lock().unwrap().pending.take();
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
mod progress_tests {
    use super::*;

    #[tokio::test(start_paused = true)]
    async fn a_slow_reader_keeps_the_wait_alive_and_a_stalled_one_ends_it() {
        let written = Arc::new(AtomicU64::new(0));
        let idle = Duration::from_secs(30);
        let cap = Duration::from_secs(300);
        // The client reads a little every 20 s; the receipt arrives after 100 s.
        let reader = written.clone();
        let feeder = tokio::spawn(async move {
            for _ in 0..5 {
                tokio::time::sleep(Duration::from_secs(20)).await;
                reader.fetch_add(4096, Ordering::Relaxed);
            }
        });
        let done = tokio::time::sleep(Duration::from_secs(100));
        assert!(progressing(&written, idle, cap, done).await.is_ok());
        feeder.await.unwrap();
        // Nothing is written for 30 s: the wait ends at idle, not at the receipt.
        let started = Instant::now();
        let never = tokio::time::sleep(Duration::from_secs(1000));
        assert!(progressing(&written, idle, cap, never).await.is_err());
        let waited = started.elapsed();
        assert!(waited >= idle && waited < idle + idle / 4, "{waited:?}");
        // Progress never stops, but the cap bounds the wait.
        let reader = written.clone();
        let drip = tokio::spawn(async move {
            loop {
                tokio::time::sleep(Duration::from_secs(10)).await;
                reader.fetch_add(1, Ordering::Relaxed);
            }
        });
        let started = Instant::now();
        let never = tokio::time::sleep(Duration::from_secs(10_000));
        assert!(progressing(&written, idle, cap, never).await.is_err());
        assert!(started.elapsed() <= cap + Duration::from_secs(1));
        drip.abort();
    }

    #[test]
    fn a_counted_connection_counts_headers_and_body() {
        let wire = Delivery::default();
        let (tx, mut rx) = oneshot::channel();
        let written = Arc::new(AtomicU64::new(0));
        wire.track_stream(tx, written.clone());
        wire.wrote(b"HTTP/1.1 200 OK\r\n\r\n3\r\nabc\r\n");
        assert_eq!(written.load(Ordering::Relaxed), 27);
        wire.wrote(b"0\r\n\r\n");
        assert!(rx.try_recv().is_ok());
        assert_eq!(written.load(Ordering::Relaxed), 32);
    }
}

#[cfg(test)]
mod stream_tests {
    use super::*;
    #[test]
    fn partial_chunk_and_terminator_inside_payload_never_acknowledge() {
        let wire = Delivery::default();
        let (tx, mut rx) = oneshot::channel();
        wire.track_stream(tx, Arc::default());
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
