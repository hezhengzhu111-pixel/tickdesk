use crate::protocol::MAX_MESSAGE;
use bytes::Bytes;
use tokio::{
    io::{self, AsyncBufReadExt, AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, BufReader},
    time::{self, Duration, Instant},
};

const ASSEMBLY_TIMEOUT: Duration = Duration::from_secs(10);
pub(crate) const TEXT: u8 = 1;
pub(crate) const CLOSE: u8 = 8;
pub(crate) const PING: u8 = 9;
pub(crate) const PONG: u8 = 10;

pub(crate) enum Event {
    Text(Bytes),
    Ping(Bytes),
    Pong(Bytes),
    Close,
    Fragment,
}

pub(crate) struct Reader<R> {
    stream: BufReader<R>,
    fragment: Option<(Instant, Vec<u8>)>,
}

impl<R: AsyncRead + Unpin> Reader<R> {
    pub fn new(stream: R) -> Self {
        Self {
            stream: BufReader::new(stream),
            fragment: None,
        }
    }

    pub async fn next(&mut self) -> Result<Event, u16> {
        let assembly_deadline = self
            .fragment
            .as_ref()
            .map(|(started, _)| *started + ASSEMBLY_TIMEOUT);
        let first = self.stream.fill_buf();
        let buffer = match assembly_deadline {
            Some(deadline) => time::timeout_at(deadline, first)
                .await
                .map_err(|_| 1008u16)?,
            None => first.await,
        }
        .map_err(|_| 1006u16)?;
        if buffer.is_empty() {
            return Err(1006);
        }
        let started = Instant::now();
        let deadline = assembly_deadline.unwrap_or(started + ASSEMBLY_TIMEOUT);
        time::timeout_at(deadline, self.read_frame(started))
            .await
            .map_err(|_| 1008u16)?
    }

    async fn read_frame(&mut self, started: Instant) -> Result<Event, u16> {
        let mut prefix = [0; 2];
        self.stream
            .read_exact(&mut prefix)
            .await
            .map_err(|_| 1006u16)?;
        let [flags, length] = prefix;
        if flags & 0x70 != 0 || length & 0x80 == 0 {
            return Err(1002);
        }
        let fin = flags & 0x80 != 0;
        let opcode = flags & 0x0f;
        let length = length & 0x7f;
        let remaining = match opcode {
            0 if self.fragment.is_some() => MAX_MESSAGE - self.fragment.as_ref().unwrap().1.len(),
            TEXT if self.fragment.is_none() => MAX_MESSAGE,
            2 if self.fragment.is_none() => return Err(1003),
            CLOSE | PING | PONG if fin && length <= 125 => 125,
            _ => return Err(1002),
        };
        let size = match length {
            126 => {
                let n = self.stream.read_u16().await.map_err(|_| 1006u16)?;
                if n < 126 {
                    return Err(1002);
                }
                u64::from(n)
            }
            127 => {
                let n = self.stream.read_u64().await.map_err(|_| 1006u16)?;
                if n <= 65535 || n >> 63 != 0 {
                    return Err(1002);
                }
                n
            }
            n => u64::from(n),
        };
        if size > remaining as u64 {
            return Err(1009);
        }
        let mut mask = [0; 4];
        self.stream
            .read_exact(&mut mask)
            .await
            .map_err(|_| 1006u16)?;
        let mut payload = vec![0; size as usize];
        self.stream
            .read_exact(&mut payload)
            .await
            .map_err(|_| 1006u16)?;
        for (index, byte) in payload.iter_mut().enumerate() {
            *byte ^= mask[index % mask.len()];
        }
        match opcode {
            PING => Ok(Event::Ping(payload.into())),
            PONG => Ok(Event::Pong(payload.into())),
            CLOSE => {
                if payload.len() == 1 {
                    return Err(1002);
                }
                if !payload.is_empty() {
                    let code = u16::from_be_bytes([payload[0], payload[1]]);
                    if !matches!(code, 1000..=1003 | 1007..=1014 | 3000..=4999) {
                        return Err(1002);
                    }
                    std::str::from_utf8(&payload[2..]).map_err(|_| 1007u16)?;
                }
                Ok(Event::Close)
            }
            TEXT if fin => text(payload),
            TEXT => {
                self.fragment = Some((started, payload));
                Ok(Event::Fragment)
            }
            _ => {
                let (_, current) = self.fragment.as_mut().expect("validated continuation");
                // Use exact requested growth so geometric over-allocation does not inflate the cap.
                current.reserve_exact(payload.len());
                current.extend_from_slice(&payload);
                if fin {
                    text(self.fragment.take().expect("completed continuation").1)
                } else {
                    Ok(Event::Fragment)
                }
            }
        }
    }
}

fn text(payload: Vec<u8>) -> Result<Event, u16> {
    std::str::from_utf8(&payload).map_err(|_| 1007u16)?;
    Ok(Event::Text(payload.into()))
}

pub(crate) async fn write(
    stream: &mut (impl AsyncWrite + Unpin),
    opcode: u8,
    payload: &[u8],
) -> io::Result<()> {
    let (header, size) = header(opcode, payload.len());
    stream.write_all(&header[..size]).await?;
    stream.write_all(payload).await?;
    stream.flush().await
}

pub(crate) fn header(opcode: u8, len: usize) -> ([u8; 10], usize) {
    let mut header = [0; 10];
    header[0] = 0x80 | opcode;
    let size = match len {
        0..=125 => {
            header[1] = len as u8;
            2
        }
        126..=65535 => {
            header[1] = 126;
            header[2..4].copy_from_slice(&(len as u16).to_be_bytes());
            4
        }
        _ => {
            header[1] = 127;
            header[2..10].copy_from_slice(&(len as u64).to_be_bytes());
            10
        }
    };
    (header, size)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test(start_paused = true)]
    async fn frame_deadline_starts_with_first_byte_and_survives_control_frames() {
        let (mut tx, rx) = io::duplex(128);
        let mut reader = Reader::new(rx);
        // A fragmented text frame, then a valid masked control frame.
        tx.write_all(&[TEXT, 0x80, 1, 2, 3, 4]).await.unwrap();
        assert!(matches!(reader.next().await, Ok(Event::Fragment)));
        time::advance(Duration::from_secs(9)).await;
        tx.write_all(&[0x80 | PING, 0x80, 1, 2, 3, 4])
            .await
            .unwrap();
        assert!(matches!(reader.next().await, Ok(Event::Ping(_))));
        assert_eq!(reader.next().await.err(), Some(1008));

        let (mut tx, rx) = io::duplex(128);
        let mut reader = Reader::new(rx);
        tx.write_all(&[0x80 | TEXT]).await.unwrap();
        assert_eq!(reader.next().await.err(), Some(1008));
    }
}
