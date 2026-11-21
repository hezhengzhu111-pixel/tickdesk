use crate::{
    protocol::MAX_MESSAGE,
    queue::Item,
    rate::Bucket,
    relay::{random_bytes, Connection, Relay, PING_INTERVAL, PONG_TIMEOUT},
    wire::{self, Event},
};
use bytes::Bytes;
use std::{collections::VecDeque, future::poll_fn, pin::Pin, sync::Arc, task::Poll};
use tokio::{
    io::{self, AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt},
    sync::{mpsc, watch},
    time::{self, Duration, Instant},
};

const HELLO_TIMEOUT: Duration = Duration::from_secs(5);
const WRITE_TIMEOUT: Duration = Duration::from_secs(2);
type Control = (u8, Bytes);

pub(crate) async fn run(
    stream: impl AsyncRead + AsyncWrite + Unpin,
    relay: Arc<Relay>,
    connection: Connection,
) {
    let (mut read, write) = io::split(stream);
    let (tx, rx) = mpsc::channel(8);
    let reader = async {
        let code = receive(&mut read, &relay, &connection, tx).await;
        relay.close(connection.id, code);
        if code != 1006 {
            // Stop routing immediately. Briefly drain transport bytes so a close frame can
            // be delivered even when a rejected frame's body is still in the TCP buffer.
            let mut tail = read.take(MAX_MESSAGE as u64);
            let _ = time::timeout(
                Duration::from_millis(250),
                io::copy(&mut tail, &mut io::sink()),
            )
            .await;
        }
    };
    let writer = async {
        transmit(write, &relay, &connection, rx).await;
        relay.close(connection.id, 1006);
    };
    tokio::join!(reader, writer);
}

async fn receive(
    read: impl AsyncRead + Unpin,
    relay: &Relay,
    connection: &Connection,
    control: mpsc::Sender<Control>,
) -> u16 {
    let mut reader = wire::Reader::new(read);
    let mut closed = connection.closed.clone();
    let started = Instant::now();
    let mut active = false;
    let mut deadline = started + HELLO_TIMEOUT;
    let mut heartbeat = Heartbeat::new(started);
    let mut frames = Bucket::new(128, 64, started);
    let mut bytes_per_second = Bucket::new(2 * 1024 * 1024, 1024 * 1024, started);
    loop {
        let event = {
            let incoming = reader.next();
            tokio::pin!(incoming);
            loop {
                tokio::select! {
                    biased;
                    code = closed.wait_for(Option::is_some) => return close_code(code),
                    _ = time::sleep_until(deadline) => return if active { 1011 } else { 1008 },
                    _ = heartbeat.ticks.tick(), if active => {
                        let Ok(nonce) = heartbeat.challenge(Instant::now()) else { return 1011 };
                        if control.try_send((wire::PING, Bytes::copy_from_slice(&nonce))).is_err() {
                            return 1013;
                        }
                    }
                    result = &mut incoming => match result {
                        Ok(event) => break event,
                        Err(code) => return code,
                    },
                }
            }
        };
        let now = Instant::now();
        if !frames.allow(1, now) {
            return 1013;
        }
        match event {
            Event::Text(bytes) if !active => {
                let Ok(hello) = relay.protocol.hello(&bytes, connection.role) else {
                    return 1008;
                };
                if !relay.activate(connection.id, hello) {
                    return 4401;
                }
                active = true;
                heartbeat.ticks.reset_at(now + PING_INTERVAL);
                deadline = now + PONG_TIMEOUT;
            }
            Event::Text(bytes) => {
                if !bytes_per_second.allow(bytes.len(), now) {
                    return 1013;
                }
                match relay.protocol.incoming(&bytes, connection.role) {
                    Ok(frame) => relay.route(connection.id, frame, bytes),
                    Err(failure) => relay.reject(connection.id, failure),
                }
            }
            Event::Ping(payload) => {
                if control.try_send((wire::PONG, payload)).is_err() {
                    return 1013;
                }
            }
            Event::Pong(payload) => {
                if let Some(rtt) = heartbeat.confirm(&payload, now) {
                    deadline = now + PONG_TIMEOUT;
                    relay.pong(connection.id, now, rtt);
                }
            }
            Event::Close => return 1000,
            Event::Fragment => {}
        }
    }
}

async fn transmit(
    mut stream: impl AsyncWrite + Unpin,
    relay: &Relay,
    connection: &Connection,
    mut control: mpsc::Receiver<Control>,
) {
    let mut closed = connection.closed.clone();
    loop {
        // A single write is in flight. Let it finish or time out before writing close.
        let code = *closed.borrow_and_update();
        if let Some(code) = code {
            if code != 1006 && send(&mut stream, wire::CLOSE, &code.to_be_bytes()).await {
                let _ = time::timeout(WRITE_TIMEOUT, stream.shutdown()).await;
            }
            return;
        }
        let frame = if let Ok(control) = control.try_recv() {
            control
        } else if let Some(item) = relay.pop(connection.id) {
            if !send_text(&mut stream, relay, connection.id, item).await {
                relay.close(connection.id, 1013);
                return;
            }
            continue;
        } else {
            tokio::select! {
                biased;
                _ = closed.changed() => continue,
                Some(control) = control.recv() => control,
                _ = connection.wake.notified() => continue,
            }
        };
        if !send(&mut stream, frame.0, &frame.1).await {
            relay.close(connection.id, 1013);
            return;
        }
    }
}

async fn send_text(
    stream: &mut (impl AsyncWrite + Unpin),
    relay: &Relay,
    id: u64,
    item: Item,
) -> bool {
    let (header, size) = wire::header(wire::TEXT, item.bytes.len());
    let mut started = false;
    let write = time::timeout(WRITE_TIMEOUT, async {
        let first = poll_fn(|cx| {
            match relay.start_write(id, item.source, || {
                Pin::new(&mut *stream).poll_write(cx, &header[..size])
            }) {
                None => Poll::Ready(Ok(None)),
                Some(Poll::Ready(result)) => Poll::Ready(result.map(Some)),
                Some(Poll::Pending) => Poll::Pending,
            }
        })
        .await?;
        let Some(first) = first else { return Ok(()) };
        if first == 0 {
            return Err(io::ErrorKind::WriteZero.into());
        }
        started = true;
        stream.write_all(&header[first..size]).await?;
        stream.write_all(&item.bytes).await?;
        stream.flush().await
    })
    .await;
    match write {
        Ok(result) => result.is_ok(),
        Err(_) => !started && relay.start_write(id, item.source, || ()).is_none(),
    }
}

async fn send(stream: &mut (impl AsyncWrite + Unpin), opcode: u8, bytes: &[u8]) -> bool {
    matches!(
        time::timeout(WRITE_TIMEOUT, wire::write(stream, opcode, bytes)).await,
        Ok(Ok(()))
    )
}

fn close_code(code: Result<watch::Ref<'_, Option<u16>>, watch::error::RecvError>) -> u16 {
    code.ok().and_then(|code| *code).unwrap_or(1006)
}

struct Heartbeat {
    ticks: time::Interval,
    outstanding: VecDeque<([u8; 8], Instant)>,
}

impl Heartbeat {
    fn new(now: Instant) -> Self {
        let mut ticks = time::interval_at(now + PING_INTERVAL, PING_INTERVAL);
        ticks.set_missed_tick_behavior(time::MissedTickBehavior::Skip);
        Self {
            ticks,
            outstanding: VecDeque::with_capacity(3),
        }
    }

    fn challenge(&mut self, now: Instant) -> Result<[u8; 8], ring::error::Unspecified> {
        let mut nonce = [0; 8];
        random_bytes(&mut nonce)?;
        if self.outstanding.len() == 3 {
            self.outstanding.pop_front();
        }
        self.outstanding.push_back((nonce, now));
        Ok(nonce)
    }

    fn confirm(&mut self, payload: &[u8], now: Instant) -> Option<Duration> {
        let index = self
            .outstanding
            .iter()
            .position(|(nonce, _)| payload == nonce)?;
        let (_, sent) = self.outstanding.remove(index)?;
        Some(now.duration_since(sent))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::relay::tests::{open, relay, token};
    use tokio::io::DuplexStream;

    async fn start() -> (DuplexStream, tokio::task::JoinHandle<()>) {
        let relay = relay();
        let conn = relay
            .admit("127.0.0.1".parse().unwrap(), Some(&token(1)))
            .ok()
            .unwrap();
        let (client, server) = io::duplex(8192);
        (client, tokio::spawn(run(server, relay, conn)))
    }

    async fn masked(peer: &mut DuplexStream, opcode: u8, payload: &[u8]) {
        let key = [13, 27, 39, 45];
        let mut frame = vec![0x80 | opcode, 0x80 | payload.len() as u8];
        frame.extend_from_slice(&key);
        frame.extend(
            payload
                .iter()
                .enumerate()
                .map(|(i, byte)| byte ^ key[i % 4]),
        );
        peer.write_all(&frame).await.unwrap();
    }

    async fn next(peer: &mut DuplexStream) -> (u8, Vec<u8>) {
        time::timeout(Duration::from_secs(1), async {
            let opcode = peer.read_u8().await.unwrap();
            let tag = peer.read_u8().await.unwrap();
            let size = if tag == 126 {
                peer.read_u16().await.unwrap() as usize
            } else {
                tag as usize
            };
            let mut payload = vec![0; size];
            peer.read_exact(&mut payload).await.unwrap();
            (opcode & 0xf, payload)
        })
        .await
        .expect("server emitted a WebSocket frame")
    }

    #[tokio::test(start_paused = true)]
    async fn hello_and_matched_pong_have_independent_deadlines() {
        let (mut client, task) = start().await;
        tokio::task::yield_now().await;
        time::advance(HELLO_TIMEOUT).await;
        assert_eq!(
            next(&mut client).await,
            (wire::CLOSE, 1008u16.to_be_bytes().to_vec())
        );
        drop(client);
        task.await.unwrap();

        let (mut client, task) = start().await;
        masked(
            &mut client,
            wire::TEXT,
            br#"{"v":2,"type":"hello","incarnation":"a","serverEpoch":null}"#,
        )
        .await;
        assert_eq!(next(&mut client).await.0, wire::TEXT);
        assert_eq!(next(&mut client).await.0, wire::TEXT);
        time::advance(PING_INTERVAL).await;
        let (opcode, first) = next(&mut client).await;
        assert_eq!(opcode, wire::PING);
        assert_eq!(first.len(), 8);
        masked(&mut client, wire::PONG, &first).await;
        masked(&mut client, wire::PING, b"ping").await;
        assert_eq!(next(&mut client).await, (wire::PONG, b"ping".to_vec()));

        time::advance(PING_INTERVAL).await;
        let (opcode, second) = next(&mut client).await;
        assert_eq!(opcode, wire::PING);
        assert_ne!(first, second);
        masked(&mut client, wire::PONG, &first).await;
        time::advance(PING_INTERVAL).await;
        assert_eq!(next(&mut client).await.0, wire::PING);
        // A replayed nonce and a different valid WebSocket control do not extend the deadline.
        masked(&mut client, wire::PING, b"still here").await;
        assert_eq!(
            next(&mut client).await,
            (wire::PONG, b"still here".to_vec())
        );
        time::advance(PING_INTERVAL).await;
        assert_eq!(
            next(&mut client).await,
            (wire::CLOSE, 1011u16.to_be_bytes().to_vec())
        );
        drop(client);
        task.await.unwrap();
    }

    #[tokio::test(start_paused = true)]
    async fn stalled_first_write_is_fenced_by_either_endpoint_replacing_its_socket() {
        for replacing in [1, 2] {
            let relay = relay();
            let app = open(&relay, 1, "app1");
            let local = open(&relay, 2, "local1");
            let (mut writer, mut observer) = io::duplex(4);
            writer.write_all(b"full").await.unwrap();
            let send = send_text(
                &mut writer,
                &relay,
                local.id,
                Item {
                    source: Some(app.id),
                    bytes: Bytes::from_static(b"would be stale"),
                },
            );
            tokio::pin!(send);
            assert!(futures_util::poll!(&mut send).is_pending());

            let replacement = open(&relay, replacing, "new");
            let mut before = [0; 4];
            observer.read_exact(&mut before).await.unwrap();
            assert_eq!(&before, b"full");
            assert!(send.await);
            assert!(time::timeout(Duration::from_millis(1), observer.read_u8())
                .await
                .is_err());
            assert_eq!(*replacement.closed.borrow(), None);
        }
    }
}
