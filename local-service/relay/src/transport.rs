use axum_server::accept::Accept;
use std::{
    future::Ready,
    io,
    pin::Pin,
    sync::Arc,
    task::{Context, Poll},
};
use tokio::{
    io::{AsyncRead, AsyncWrite, ReadBuf},
    net::TcpStream,
    sync::{OwnedSemaphorePermit, Semaphore},
};

const MAX_TCP: usize = 512;

#[derive(Clone)]
pub(crate) struct Limit(Arc<Semaphore>);

impl Limit {
    pub fn new() -> Self {
        Self(Arc::new(Semaphore::new(MAX_TCP)))
    }
}

impl<S> Accept<TcpStream, S> for Limit {
    type Stream = Guarded;
    type Service = S;
    type Future = Ready<io::Result<(Self::Stream, S)>>;

    fn accept(&self, stream: TcpStream, service: S) -> Self::Future {
        let result = (|| {
            let permit = self
                .0
                .clone()
                .try_acquire_owned()
                .map_err(|_| io::Error::other("relay TCP connection limit"))?;
            stream.set_nodelay(true)?;
            Ok((
                Guarded {
                    stream,
                    _permit: permit,
                },
                service,
            ))
        })();
        std::future::ready(result)
    }
}

pub(crate) struct Guarded {
    stream: TcpStream,
    // Kept by the underlying stream through TLS, HTTP, and the upgraded WebSocket.
    _permit: OwnedSemaphorePermit,
}

impl AsyncRead for Guarded {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        Pin::new(&mut self.stream).poll_read(cx, buf)
    }
}

impl AsyncWrite for Guarded {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        Pin::new(&mut self.stream).poll_write(cx, buf)
    }

    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.stream).poll_flush(cx)
    }

    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.stream).poll_shutdown(cx)
    }
}
