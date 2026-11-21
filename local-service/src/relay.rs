use crate::{
    commands::{now_ms, valid_id},
    contract::{parse_frame, FRAME_LIMIT},
    storage::Storage,
};
use anyhow::{bail, ensure, Context};
use base64::{
    engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD},
    Engine,
};
use futures_util::{SinkExt, StreamExt};
use ring::{
    digest::{digest, SHA256},
    rand::{SecureRandom, SystemRandom},
};
use rustls::{
    client::{
        danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier},
        verify_server_name,
    },
    crypto::{verify_tls12_signature, verify_tls13_signature, WebPkiSupportedAlgorithms},
    pki_types::{CertificateDer, ServerName, UnixTime},
    CertificateError, ClientConfig, DigitallySignedStruct, SignatureScheme,
};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    collections::{HashMap, VecDeque},
    io::Read,
    net::{IpAddr, SocketAddr},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{
    io::{AsyncRead, AsyncWrite},
    net::TcpStream,
    sync::watch,
    time::{self, Instant, MissedTickBehavior},
};
use tokio_rustls::{client::TlsStream, TlsConnector};
use tokio_tungstenite::{
    client_async_with_config,
    tungstenite::{
        client::IntoClientRequest,
        http::{header, StatusCode, Uri},
        protocol::WebSocketConfig,
        Error as WsError, Message,
    },
    WebSocketStream,
};
use x509_parser::{parse_x509_certificate, time::ASN1Time};

#[cfg(test)]
mod tests;

const PROTOCOL: &str = "tickdesk.v2";
const PATH: &str = "/relay/v2/connect";
const HEARTBEAT: Duration = Duration::from_secs(15);
const HEARTBEAT_TIMEOUT: Duration = Duration::from_secs(45);
const SEND_TIMEOUT: Duration = Duration::from_secs(5);
const RETRY: Duration = Duration::from_secs(15);

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PairingConfig {
    pub relay_url: String,
    pub pair_id: String,
    pub token_file: PathBuf,
    pub pins: Vec<String>,
}

pub struct Pairing {
    endpoint: String,
    address: SocketAddr,
    pair_id: String,
    token: String,
    pins: Vec<[u8; 32]>,
}

impl PairingConfig {
    pub fn load(self, config_dir: &Path) -> anyhow::Result<Pairing> {
        let uri: Uri = self
            .relay_url
            .parse()
            .map_err(|_| anyhow::anyhow!("invalid relay URL"))?;
        let ip: IpAddr = uri
            .host()
            .context("relay URL requires IP SAN")?
            .trim_start_matches('[')
            .trim_end_matches(']')
            .parse()
            .context("relay URL requires numeric IP")?;
        ensure!(
            uri.scheme_str() == Some("wss")
                && uri.port_u16() == Some(8443)
                && uri.path_and_query().is_some_and(|pq| pq.as_str() == PATH),
            "invalid relay endpoint"
        );
        ensure!(valid_id(&self.pair_id), "invalid pair id");
        let pins: Vec<[u8; 32]> = self
            .pins
            .iter()
            .map(|p| {
                let encoded = p.strip_prefix("sha256/").context("invalid SPKI pin")?;
                let bytes = STANDARD.decode(encoded).context("invalid SPKI pin")?;
                bytes
                    .try_into()
                    .map_err(|_| anyhow::anyhow!("invalid SPKI pin length"))
            })
            .collect::<anyhow::Result<_>>()?;
        ensure!(
            (1..=2).contains(&pins.len()) && (pins.len() == 1 || pins[0] != pins[1]),
            "one or two distinct SPKI pins required"
        );
        ensure!(
            !self.token_file.as_os_str().is_empty(),
            "empty relay token path"
        );
        let mut secret = String::new();
        std::fs::File::open(config_dir.join(self.token_file))
            .context("open local relay token file")?
            .take(128)
            .read_to_string(&mut secret)
            .context("read local relay token file")?;
        ensure!(secret.len() < 128, "invalid local Bearer token length");
        let token = secret.trim_end_matches(['\r', '\n']);
        ensure!(
            token.len() == 43
                && URL_SAFE_NO_PAD
                    .decode(token)
                    .is_ok_and(|bytes| bytes.len() == 32),
            "invalid local Bearer token encoding"
        );
        Ok(Pairing {
            endpoint: self.relay_url,
            address: SocketAddr::new(ip, 8443),
            pair_id: self.pair_id,
            token: token.to_owned(),
            pins,
        })
    }
}

#[derive(Debug)]
struct PinVerifier {
    pins: Vec<[u8; 32]>,
    algorithms: WebPkiSupportedAlgorithms,
}

impl PinVerifier {
    fn new(pins: Vec<[u8; 32]>) -> Self {
        Self {
            pins,
            algorithms: rustls::crypto::ring::default_provider().signature_verification_algorithms,
        }
    }
}

fn spki_digest(cert: &CertificateDer<'_>) -> Result<[u8; 32], rustls::Error> {
    let (_, certificate) = parse_x509_certificate(cert.as_ref())
        .map_err(|_| rustls::Error::InvalidCertificate(CertificateError::BadEncoding))?;
    Ok(digest(&SHA256, certificate.public_key().raw)
        .as_ref()
        .try_into()
        .expect("SHA-256 is 32 bytes"))
}

impl ServerCertVerifier for PinVerifier {
    fn verify_server_cert(
        &self,
        end: &CertificateDer<'_>,
        _: &[CertificateDer<'_>],
        name: &ServerName<'_>,
        _: &[u8],
        now: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        let cert = rustls::server::ParsedCertificate::try_from(end)?;
        verify_server_name(&cert, name)?;
        let (_, parsed) = parse_x509_certificate(end.as_ref())
            .map_err(|_| rustls::Error::InvalidCertificate(CertificateError::BadEncoding))?;
        let timestamp = i64::try_from(now.as_secs())
            .ok()
            .and_then(|s| ASN1Time::from_timestamp(s).ok())
            .ok_or(rustls::Error::FailedToGetCurrentTime)?;
        if parsed.validity().not_before > timestamp {
            return Err(rustls::Error::InvalidCertificate(
                CertificateError::NotValidYet,
            ));
        }
        if parsed.validity().not_after < timestamp {
            return Err(rustls::Error::InvalidCertificate(CertificateError::Expired));
        }
        if !self.pins.contains(&spki_digest(end)?) {
            return Err(rustls::Error::InvalidCertificate(
                CertificateError::UnknownIssuer,
            ));
        }
        // rustls separately verifies proof of possession against this pinned key.
        Ok(ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        sig: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls12_signature(message, cert, sig, &self.algorithms)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        sig: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls13_signature(message, cert, sig, &self.algorithms)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.algorithms.supported_schemes()
    }
}

#[derive(Debug)]
enum Disconnect {
    Retry,
    RetryAfter(Duration),
    Stop(&'static str),
}

type Socket = WebSocketStream<TlsStream<TcpStream>>;
type Database = Arc<Mutex<Storage>>;

async fn connect(pairing: &Pairing) -> Result<Socket, Disconnect> {
    let config =
        ClientConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
            .with_safe_default_protocol_versions()
            .map_err(|_| Disconnect::Stop("TLS_CONFIG"))?
            .dangerous()
            .with_custom_certificate_verifier(Arc::new(PinVerifier::new(pairing.pins.clone())))
            .with_no_client_auth();
    let tcp = TcpStream::connect(pairing.address)
        .await
        .map_err(|_| Disconnect::Retry)?;
    let stream = TlsConnector::from(Arc::new(config))
        .connect(pairing.address.ip().into(), tcp)
        .await
        .map_err(|err| {
            if err.kind() == std::io::ErrorKind::InvalidData {
                Disconnect::Stop("TLS_TRUST")
            } else {
                Disconnect::Retry
            }
        })?;
    let mut request = pairing
        .endpoint
        .as_str()
        .into_client_request()
        .map_err(|_| Disconnect::Stop("RELAY_URL"))?;
    let mut authorization = format!("Bearer {}", pairing.token)
        .parse::<header::HeaderValue>()
        .map_err(|_| Disconnect::Stop("RELAY_TOKEN"))?;
    authorization.set_sensitive(true);
    request
        .headers_mut()
        .insert(header::AUTHORIZATION, authorization);
    request.headers_mut().insert(
        header::SEC_WEBSOCKET_PROTOCOL,
        header::HeaderValue::from_static(PROTOCOL),
    );
    let limits = WebSocketConfig::default()
        .max_message_size(Some(FRAME_LIMIT))
        .max_frame_size(Some(FRAME_LIMIT))
        .max_write_buffer_size(1024 * 1024);
    let (socket, response) = client_async_with_config(request, stream, Some(limits))
        .await
        .map_err(|err| match err {
            WsError::Http(response) if response.status() == StatusCode::TOO_MANY_REQUESTS => {
                let seconds = response
                    .headers()
                    .get(header::RETRY_AFTER)
                    .and_then(|s| s.to_str().ok())
                    .and_then(|s| s.parse::<u64>().ok())
                    .unwrap_or(1)
                    .clamp(1, 300);
                Disconnect::RetryAfter(Duration::from_secs(seconds))
            }
            WsError::Http(response) if response.status() == StatusCode::UNAUTHORIZED => {
                Disconnect::Stop("RELAY_AUTH")
            }
            WsError::Http(response) if response.status() == StatusCode::UPGRADE_REQUIRED => {
                Disconnect::Stop("RELAY_PROTOCOL")
            }
            WsError::Protocol(_) => Disconnect::Stop("RELAY_PROTOCOL"),
            _ => Disconnect::Retry,
        })?;
    if response
        .headers()
        .get(header::SEC_WEBSOCKET_PROTOCOL)
        .and_then(|s| s.to_str().ok())
        != Some(PROTOCOL)
        || response
            .headers()
            .contains_key(header::SEC_WEBSOCKET_EXTENSIONS)
    {
        return Err(Disconnect::Stop("RELAY_PROTOCOL"));
    }
    Ok(socket)
}

async fn send_json<T: AsyncRead + AsyncWrite + Unpin>(
    socket: &mut WebSocketStream<T>,
    body: &Value,
) -> anyhow::Result<()> {
    time::timeout(
        SEND_TIMEOUT,
        socket.send(Message::Text(serde_json::to_string(body)?.into())),
    )
    .await??;
    Ok(())
}

async fn db_task<T: Send + 'static>(
    db: &Database,
    f: impl FnOnce(&mut Storage) -> anyhow::Result<T> + Send + 'static,
) -> anyhow::Result<T> {
    let db = Arc::clone(db);
    tokio::task::spawn_blocking(move || {
        f(&mut *db
            .lock()
            .map_err(|_| anyhow::anyhow!("storage lock failed"))?)
    })
    .await?
}

async fn session(mut socket: Socket, db: &Database, config: &Pairing) -> Result<(), Disconnect> {
    let identity = db_task(db, |db| Ok(db.identity.clone()))
        .await
        .map_err(|_| Disconnect::Stop("STORAGE_INIT"))?;
    send_json(&mut socket, &json!({"v":2,"type":"hello","incarnation":identity.incarnation,"serverEpoch":identity.server_epoch}))
        .await.map_err(|_| Disconnect::Retry)?;
    let mut tick = time::interval(Duration::from_millis(80));
    tick.set_missed_tick_behavior(MissedTickBehavior::Skip);
    let start = Instant::now();
    let mut online = None::<String>;
    let mut welcomed = false;
    let mut last_pong = start;
    let mut next_ping = start + HEARTBEAT;
    let mut nonces = VecDeque::<[u8; 8]>::new();
    let mut retry_at = HashMap::<String, Instant>::new();
    let mut business_since_file = 0;
    let mut next_download_pump = start;
    let rng = SystemRandom::new();

    loop {
        tokio::select! {
            msg = socket.next() => {
                let msg = msg.ok_or(Disconnect::Retry)?.map_err(|_| Disconnect::Retry)?;
                match msg {
                    Message::Text(text) => {
                        let frame = match parse_frame(text.as_bytes()) {
                            Ok(frame) => frame,
                            Err(error) => {
                                let code = if error.to_string()=="payload hash mismatch" {"HASH_MISMATCH"} else {"INVALID_FRAME"};
                                send_json(&mut socket, &wire_error(code, false, &Value::Null))
                                    .await.map_err(|_| Disconnect::Retry)?;
                                continue;
                            }
                        };
                        match frame["type"].as_str() {
                            Some("welcome") if !welcomed => {
                                if frame["pairId"] != config.pair_id || frame["role"] != "local" {
                                    return Err(Disconnect::Stop("RELAY_IDENTITY"));
                                }
                                welcomed = true;
                            }
                            Some("presence") if welcomed => {
                                let next = frame["peer"]["incarnation"].as_str();
                                if let Some(next) = next {
                                    if online.as_deref() != Some(next) {
                                        let pair = config.pair_id.clone();
                                        let incarnation = next.to_owned();
                                        // A retired/replaced App identity is a
                                        // peer-level fence, not a reason to stop
                                        // this endpoint's relay session. Keeping
                                        // the local service online lets the new
                                        // App connect and lets the user see the
                                        // rejection instead of a dead service.
                                        if let Err(error) = db_task(db, move |db| {
                                            db.bind_peer(&pair, &incarnation)
                                        })
                                        .await
                                        {
                                            tracing::warn!(%error, "rejected peer identity; local relay stays online");
                                        } else {
                                            retry_at.clear();
                                        }
                                    }
                                } else {
                                    retry_at.clear();
                                }
                                online = next.map(str::to_owned);
                            }
                            Some("deliver") if welcomed => {
                                let id = frame["deliveryId"].clone();
                                let response = if frame["fromIncarnation"].as_str() != online.as_deref() {
                                    wire_error("INCARNATION_MISMATCH", false, &id)
                                } else {
                                    match db_task(db, move |db| db.receive(&frame)).await {
                                        Ok(value) => value,
                                        Err(err) => match err.to_string().as_str() {
                                            "INVALID_FRAME" => wire_error("INVALID_FRAME", false, &id),
                                            "INCARNATION_MISMATCH" => wire_error("INCARNATION_MISMATCH", false, &id),
                                            "DELIVERY_ID_REUSED" => wire_error("DELIVERY_ID_REUSED", false, &id),
                                            "UPLOAD_NOT_FOUND" => wire_error("UPLOAD_NOT_FOUND", false, &id),
                                            "CHUNK_CONFLICT" => wire_error("CHUNK_CONFLICT", false, &id),
                                            _ => wire_error("DISK_FULL", true, &id),
                                        },
                                    }
                                };
                                send_json(&mut socket, &response).await.map_err(|_| Disconnect::Retry)?;
                            }
                            Some("receipt") if online.as_deref().is_some_and(|inc| frame["toIncarnation"] == inc) => {
                                let id = frame["deliveryId"].as_str().unwrap().to_owned();
                                match db_task(db, move |db| db.accept_receipt(&frame)).await {
                                    Ok(true) => { retry_at.remove(&id); }
                                    Ok(false) => {}
                                    Err(_) => tracing::warn!("could not commit receipt"),
                                }
                            }
                            Some("error") if welcomed => {
                                if let Some(id) = frame["refDeliveryId"].as_str() {
                                    if retry_at.contains_key(id) {
                                        if frame["retryable"] == true {
                                            let delay = frame["retryAfterMs"].as_u64().unwrap_or_default().clamp(1_000, 300_000);
                                            retry_at.insert(id.to_owned(), Instant::now() + Duration::from_millis(delay));
                                        } else {
                                            let id = id.to_owned();
                                            db_task(db, move |db| db.quarantine_delivery(&id)).await.map_err(|_| Disconnect::Retry)?;
                                        }
                                    }
                                }
                            }
                            _ => return Err(Disconnect::Stop("RELAY_PROTOCOL")),
                        }
                    }
                    Message::Ping(_) => {
                        time::timeout(SEND_TIMEOUT, socket.flush()).await.map_err(|_| Disconnect::Retry)?
                            .map_err(|_| Disconnect::Retry)?;
                    }
                    Message::Pong(payload) => {
                        if let Some(index) = nonces.iter().position(|nonce| nonce.as_slice() == payload.as_ref()) {
                            nonces.remove(index);
                            last_pong = Instant::now();
                        }
                    }
                    Message::Close(frame) => {
                        let code = frame.map(|frame| u16::from(frame.code));
                        return Err(match code {
                            Some(4401) => Disconnect::Stop("RELAY_AUTH"),
                            Some(4409) => Disconnect::Stop("RELAY_REPLACED"),
                            Some(1002 | 1003 | 1007 | 4400 | 4426) => Disconnect::Stop("RELAY_PROTOCOL"),
                            _ => Disconnect::Retry,
                        });
                    }
                    _ => return Err(Disconnect::Stop("RELAY_PROTOCOL")),
                }
            }
            _ = tick.tick() => {
                let now = Instant::now();
                if (!welcomed && now.duration_since(start) >= SEND_TIMEOUT)
                    || now.duration_since(last_pong) >= HEARTBEAT_TIMEOUT {
                    return Err(Disconnect::Retry);
                }
                if now >= next_ping {
                    let mut nonce = [0; 8];
                    rng.fill(&mut nonce).map_err(|_| Disconnect::Stop("SYSTEM_ENTROPY"))?;
                    if nonces.len() == 3 { nonces.pop_front(); }
                    nonces.push_back(nonce);
                    time::timeout(SEND_TIMEOUT, socket.send(Message::Ping(nonce.to_vec().into())))
                        .await.map_err(|_| Disconnect::Retry)?.map_err(|_| Disconnect::Retry)?;
                    next_ping = now + HEARTBEAT;
                }
                if online.is_some() {
                    let pump = next_download_pump <= now;
                    let (business, files) = db_task(db, move |db| {
                        db.retire_obsolete_reminders(now_ms())?;
                        if pump {db.pump_downloads(now_ms())?;}
                        Ok((db.outbox(16)?,db.file_outbox()?))
                    }).await.map_err(|_| Disconnect::Retry)?;
                    if pump {next_download_pump=now+Duration::from_millis(320);}
                    retry_at.retain(|id, _| business.iter().chain(&files).any(|record| record["deliveryId"] == id.as_str()));
                    let ready = |record: &Value| retry_at.get(record["deliveryId"].as_str().unwrap()).is_none_or(|at| *at <= now);
                    let business=business.iter().find(|record|ready(record));
                    let file=files.iter().find(|record|ready(record));
                    let selected=if business_since_file>=8 {file.or(business)} else {business.or(file)};
                    if let Some(record)=selected {
                        send_json(&mut socket, record).await.map_err(|_| Disconnect::Retry)?;
                        retry_at.insert(record["deliveryId"].as_str().unwrap().to_owned(), now + RETRY);
                        business_since_file=if record["category"]=="business" {u32::min(8,business_since_file+1)} else {0};
                    }
                }
            }
        }
    }
}

fn wire_error(code: &str, retryable: bool, id: &Value) -> Value {
    json!({"v":2,"type":"error","refDeliveryId":id,"code":code,"retryable":retryable,"retryAfterMs":if retryable { 1000 } else { 0 }})
}

pub async fn run(pairing: Pairing, db: Database, shutdown: watch::Receiver<bool>) {
    run_with_kernel(pairing, db, None, shutdown).await
}

pub async fn run_with_kernel(
    pairing: Pairing,
    db: Database,
    kernel: Option<crate::kernel::KernelConfig>,
    shutdown: watch::Receiver<bool>,
) {
    tokio::join!(
        reconnect(pairing, db.clone(), shutdown.clone()),
        process_inbox(db, kernel, shutdown)
    );
}

async fn process_inbox(
    db: Database,
    kernel: Option<crate::kernel::KernelConfig>,
    mut shutdown: watch::Receiver<bool>,
) {
    let mut position = 0;
    let mut tick = time::interval(Duration::from_millis(80));
    tick.set_missed_tick_behavior(MissedTickBehavior::Skip);
    let mut last_error = None;
    let mut next_cleanup = Instant::now();
    let mut active = std::collections::BTreeSet::new();
    let mut workers = tokio::task::JoinSet::new();
    while !*shutdown.borrow() {
        tokio::select! {
            _=shutdown.changed()=>{workers.abort_all();return},
            Some(result)=workers.join_next(), if !workers.is_empty()=>{if let Ok(id)=result{active.remove(&id);}},
            _=tick.tick()=>{
                if Instant::now() >= next_cleanup {
                    if db_task(&db, |db| db.cleanup_abandoned_uploads(now_ms())).await.is_err() {
                        tracing::warn!("abandoned upload cleanup pending; will retry");
                    }
                    next_cleanup = Instant::now() + Duration::from_secs(5);
                }
                if active.len()>=8{continue;}
                match db_task(&db, move |db|db.next_job(position)).await {
                    Ok(Some(job))=>{
                        let id=job.id;position=id;
                        if !active.insert(id){continue;}
                        let db=db.clone();let kernel=kernel.clone();
                        workers.spawn(async move{
                            let prep_job=job.clone();
                            let input=db_task(&db,move |db|{
                                let Some(data)=db.pending_job_data(&prep_job)? else{return Ok(None)};
                                if !crate::market::needs_preparation(&data){return Ok(None);}
                                Ok(Some(crate::market::capture(db,&data,now_ms())?))
                            }).await;
                            let prepared=match input{Ok(Some(input))=>Some(crate::market::prepare(kernel.as_ref(),input).await),_=>None};
                            let result=db_task(&db,move |db|if prepared.is_some(){db.process_prepared(job,now_ms(),prepared)}else{db.process(job,now_ms())}).await;
                            if result.is_err(){tracing::warn!("local command transaction failed; inbox remains pending");}id
                        });
                    }
                    Ok(None)=>position=0,
                    Err(_)=>if last_error.is_none_or(|at:Instant|at.elapsed()>=Duration::from_secs(5)){tracing::warn!("inbox storage unavailable");last_error=Some(Instant::now());},
                }
            }
        }
    }
}

async fn reconnect(pairing: Pairing, db: Database, mut shutdown: watch::Receiver<bool>) {
    let rng = SystemRandom::new();
    let mut attempts = 0u32;
    while !*shutdown.borrow() {
        let start = Instant::now();
        let disconnected = tokio::select! {
            _ = shutdown.changed() => return,
            result = async {
                let socket = time::timeout(Duration::from_secs(10), connect(&pairing)).await
                    .map_err(|_| Disconnect::Retry)??;
                session(socket, &db, &pairing).await
            } => result.err().unwrap_or(Disconnect::Retry),
        };
        if start.elapsed() >= Duration::from_secs(60) {
            attempts = 0;
        }
        let pause = match disconnected {
            Disconnect::Stop(reason) => {
                tracing::error!(reason, "relay stopped");
                return;
            }
            Disconnect::RetryAfter(pause) => pause,
            Disconnect::Retry => {
                let mut random = [0; 8];
                if rng.fill(&mut random).is_err() {
                    break;
                }
                let max_ms = (1000u64 << attempts.min(5)).min(30_000);
                Duration::from_millis(u64::from_le_bytes(random) % (max_ms + 1))
            }
        };
        attempts = attempts.saturating_add(1);
        tokio::select! { _ = shutdown.changed() => return, _ = time::sleep(pause) => {} }
    }
}

pub fn ensure_pair(db: &Storage, pairing: &Pairing) -> anyhow::Result<()> {
    let mismatch: bool = db.connection.query_row(
        "SELECT EXISTS(SELECT 1 FROM peer WHERE pair_id<>?1)",
        [&pairing.pair_id],
        |r| r.get(0),
    )?;
    if mismatch {
        bail!("data directory belongs to another pairing");
    }
    Ok(())
}
