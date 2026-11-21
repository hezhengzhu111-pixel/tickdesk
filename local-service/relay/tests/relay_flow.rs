use axum_server::Handle;
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use futures_util::{future::join_all, SinkExt, StreamExt};
use ring::digest::{digest, SHA256};
use serde_json::{json, Value};
use std::{
    net::SocketAddr,
    path::Path,
    sync::Arc,
    time::{SystemTime, UNIX_EPOCH},
};
use tickdesk_relay::{auth::Roles, http, relay::Relay};
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    net::TcpStream,
    task::JoinHandle,
    time::{sleep, timeout, Duration},
};
use tokio_rustls::{client::TlsStream, TlsConnector};
use tokio_tungstenite::{
    client_async,
    tungstenite::{
        client::IntoClientRequest,
        protocol::{
            frame::{coding::OpCode, Frame},
            Message,
        },
        Error,
    },
    WebSocketStream,
};

struct Server {
    addr: SocketAddr,
    client: TlsConnector,
    files: tempfile::TempDir,
    relay: Arc<Relay>,
    serve: JoinHandle<()>,
    reload: JoinHandle<()>,
}

type Endpoint = WebSocketStream<TlsStream<TcpStream>>;

fn token(index: u8) -> String {
    URL_SAFE_NO_PAD.encode([index; 32])
}

fn hash(bytes: &[u8]) -> String {
    digest(&SHA256, bytes)
        .as_ref()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn roles(app: u8, previous: Option<u8>) -> String {
    let mut roles = format!(
        "[pairs.pair_demo.app]\nsha256='{}'\n[pairs.pair_demo.local]\nsha256='{}'\n\
         [pairs.other.app]\nsha256='{}'\n[pairs.other.local]\nsha256='{}'\n",
        hash(token(app).as_bytes()),
        hash(token(2).as_bytes()),
        hash(token(3).as_bytes()),
        hash(token(4).as_bytes())
    );
    if let Some(previous) = previous {
        let expiry = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs()
            + 60;
        roles.push_str(&format!(
            "[pairs.pair_demo.app.previous]\nsha256='{}'\nexpires_at_unix={expiry}\n",
            hash(token(previous).as_bytes())
        ));
    }
    roles
}

impl Server {
    async fn start() -> Self {
        let files = tempfile::tempdir().unwrap();
        let path = files.path();
        replace(path, &roles(1, None));
        let relay = Relay::new(Roles::load(&path.join("roles.toml")).unwrap()).unwrap();
        let reload = tokio::spawn(relay.clone().watch_roles(path.join("roles.toml")));
        let cert = rcgen::generate_simple_self_signed(vec!["localhost".into(), "127.0.0.1".into()])
            .unwrap();
        std::fs::write(path.join("cert.pem"), cert.cert.pem()).unwrap();
        std::fs::write(path.join("key.pem"), cert.signing_key.serialize_pem()).unwrap();
        let _ = rustls::crypto::ring::default_provider().install_default();
        let tls = tickdesk_relay::tls::rustls_config(path.join("cert.pem"), path.join("key.pem"))
            .unwrap();
        let mut trust = rustls::RootCertStore::empty();
        trust.add(cert.cert.der().clone()).unwrap();
        let client = rustls::ClientConfig::builder()
            .with_root_certificates(trust)
            .with_no_client_auth();
        let client = TlsConnector::from(Arc::new(client));
        let tcp = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = tcp.local_addr().unwrap();
        let handle = Handle::new();
        let live_relay = relay.clone();
        let live_handle = handle.clone();
        let serve = tokio::spawn(async move {
            http::serve(tcp, Some(tls), live_relay, live_handle)
                .await
                .unwrap();
        });
        assert_eq!(
            timeout(Duration::from_secs(3), handle.listening())
                .await
                .unwrap(),
            Some(addr)
        );
        Self {
            addr,
            client,
            files,
            relay,
            serve,
            reload,
        }
    }

    async fn tls(&self) -> TlsStream<TcpStream> {
        let tcp = TcpStream::connect(self.addr).await.unwrap();
        self.client
            .connect("127.0.0.1".try_into().unwrap(), tcp)
            .await
            .unwrap()
    }

    async fn dial(&self, bearer: Option<&str>, protocol: &str) -> Result<Endpoint, Error> {
        let mut request = format!("wss://{}/relay/v2/connect", self.addr)
            .into_client_request()
            .unwrap();
        let headers = request.headers_mut();
        headers.insert("Sec-WebSocket-Protocol", protocol.parse().unwrap());
        headers.insert(
            "Sec-WebSocket-Extensions",
            "permessage-deflate".parse().unwrap(),
        );
        if let Some(bearer) = bearer {
            headers.insert("Authorization", format!("Bearer {bearer}").parse().unwrap());
        }
        let (endpoint, response) = timeout(
            Duration::from_secs(3),
            client_async(request, self.tls().await),
        )
        .await
        .unwrap()?;
        assert_eq!(response.headers()["sec-websocket-protocol"], "tickdesk.v2");
        assert!(response.headers().get("sec-websocket-extensions").is_none());
        Ok(endpoint)
    }

    async fn connect(
        &self,
        index: u8,
        incarnation: &str,
        epoch: Option<&str>,
    ) -> (Endpoint, Value, Value) {
        let mut endpoint = self.dial(Some(&token(index)), "tickdesk.v2").await.unwrap();
        send(
            &mut endpoint,
            &json!({"v":2,"type":"hello","incarnation":incarnation,"serverEpoch":epoch}),
        )
        .await;
        let welcome = message(&mut endpoint).await;
        let presence = message(&mut endpoint).await;
        assert_eq!(welcome["type"], "welcome");
        assert_eq!(presence["type"], "presence");
        (endpoint, welcome, presence)
    }

    async fn health(&self) -> String {
        let mut stream = self.tls().await;
        stream
            .write_all(
                b"GET /health/ready HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
            )
            .await
            .unwrap();
        let mut response = String::new();
        timeout(Duration::from_secs(3), stream.read_to_string(&mut response))
            .await
            .unwrap()
            .unwrap();
        response
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        self.relay.stop();
        self.serve.abort();
        self.reload.abort();
    }
}

fn replace(dir: &Path, contents: &str) {
    let next = dir.join("next.toml");
    std::fs::write(&next, contents).unwrap();
    std::fs::rename(next, dir.join("roles.toml")).unwrap();
}

fn get_case(snapshot: &str, name: &str) -> Value {
    let fixtures: Value = serde_json::from_str(snapshot).unwrap();
    fixtures["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|case| case["name"] == name)
        .unwrap()["payload"]
        .clone()
}

fn fixture(name: &str) -> Value {
    get_case(include_str!("../contracts/v2/relay-fixtures.json"), name)
}

async fn send(endpoint: &mut Endpoint, value: &Value) {
    endpoint
        .send(Message::Text(value.to_string().into()))
        .await
        .unwrap();
}

async fn next(endpoint: &mut Endpoint) -> Message {
    timeout(Duration::from_secs(3), endpoint.next())
        .await
        .unwrap()
        .unwrap()
        .unwrap()
}

async fn message(endpoint: &mut Endpoint) -> Value {
    let frame = next(endpoint).await;
    serde_json::from_str(frame.to_text().unwrap()).unwrap()
}

async fn close_code(endpoint: &mut Endpoint) -> u16 {
    match next(endpoint).await {
        Message::Close(Some(closed)) => closed.code.into(),
        other => panic!("expected close, got {other:?}"),
    }
}

async fn assert_quiet(endpoint: &mut Endpoint) {
    assert!(timeout(Duration::from_millis(100), endpoint.next())
        .await
        .is_err());
}

#[tokio::test]
async fn rapid_tls13_reconnects_complete_every_upgrade() {
    let server = Server::start().await;
    for attempt in 0..12 {
        let mut endpoint = server
            .dial(Some(&token(1)), "tickdesk.v2")
            .await
            .unwrap_or_else(|error| panic!("upgrade {attempt} failed: {error}"));
        let _ = endpoint.close(None).await;
        sleep(Duration::from_millis(100)).await;
    }
}

#[tokio::test]
async fn tls_endpoints_exchange_fixtures_ack_and_files_without_cloud_receipts() {
    let server = Server::start().await;
    assert!(server.health().await.starts_with("HTTP/1.1 200"));
    let (mut app, app_welcome, offline) = server.connect(1, "app_install_1", None).await;
    assert_eq!(app_welcome["role"], "app");
    assert_eq!(app_welcome["pairId"], "pair_demo");
    assert_eq!(app_welcome["heartbeatIntervalMs"], 15_000);
    assert_eq!(app_welcome["heartbeatTimeoutMs"], 45_000);
    assert_eq!(offline, fixture("peer_offline"));
    let command = fixture("deliver_command");
    send(&mut app, &command).await;
    assert_eq!(message(&mut app).await["code"], "PEER_OFFLINE");

    let (mut local, local_welcome, app_presence) = server
        .connect(2, "local_install_1", Some("epoch_demo_a"))
        .await;
    assert_eq!(local_welcome["role"], "local");
    assert_eq!(app_welcome["relayBootId"], local_welcome["relayBootId"]);
    assert_ne!(app_welcome["connectionId"], local_welcome["connectionId"]);
    assert_eq!(app_presence["peer"]["incarnation"], "app_install_1");
    assert_eq!(message(&mut app).await, fixture("peer_online"));

    let original_wire = serde_json::to_string_pretty(&command).unwrap();
    app.send(Message::Text(original_wire.clone().into()))
        .await
        .unwrap();
    let forwarded = next(&mut local).await;
    assert_eq!(forwarded.to_text().unwrap(), original_wire);
    assert_quiet(&mut app).await;

    let receipt = fixture("receipt");
    let mut reversed = receipt.clone();
    reversed["fromIncarnation"] = receipt["toIncarnation"].clone();
    reversed["toIncarnation"] = receipt["fromIncarnation"].clone();
    send(&mut local, &reversed).await;
    assert_eq!(message(&mut local).await["code"], "INCARNATION_MISMATCH");
    send(&mut local, &receipt).await;
    assert_eq!(message(&mut app).await, receipt);

    let ack = get_case(include_str!("../contracts/v2/fixtures.json"), "ack_act");
    let ack_hash = hash(ack.to_string().as_bytes());
    let ack_deliver = json!({"v":2,"type":"deliver","category":"business","deliveryId":"ack1",
        "fromIncarnation":"local_install_1","toIncarnation":"app_install_1",
        "payloadHash":ack_hash,"payload":ack});
    send(&mut local, &ack_deliver).await;
    assert_eq!(message(&mut app).await, ack_deliver);
    let ack_receipt = json!({"v":2,"type":"receipt","deliveryId":"ack1","payloadHash":ack_hash,
        "fromIncarnation":"local_install_1","toIncarnation":"app_install_1"});
    send(&mut app, &ack_receipt).await;
    assert_eq!(message(&mut local).await, ack_receipt);

    let upload = fixture("file_chunk");
    send(&mut app, &upload).await;
    assert_eq!(message(&mut local).await, upload);
    let mut download = upload.clone();
    download["fromIncarnation"] = json!("local_install_1");
    download["toIncarnation"] = json!("app_install_1");
    send(&mut local, &download).await;
    assert_eq!(message(&mut app).await, download);

    let retry = fixture("busy");
    send(&mut local, &retry).await;
    assert_eq!(message(&mut app).await, retry);

    let mut spoofed = ack_deliver;
    spoofed["fromIncarnation"] = json!("app_install_1");
    spoofed["toIncarnation"] = json!("local_install_1");
    send(&mut app, &spoofed).await;
    assert_eq!(message(&mut app).await["code"], "INVALID_FRAME");

    let mut bad_hash = command;
    bad_hash["payloadHash"] = json!("0".repeat(64));
    send(&mut app, &bad_hash).await;
    assert_eq!(message(&mut app).await["code"], "HASH_MISMATCH");

    local.close(None).await.unwrap();
    assert_eq!(message(&mut app).await, offline);
}

#[tokio::test]
async fn auth_subprotocol_oversize_and_pair_isolation() {
    let server = Server::start().await;
    let mut head = server.tls().await;
    let request = format!(
        "HEAD /relay/v2/connect HTTP/1.1\r\nHost: localhost\r\nConnection: Upgrade\r\n\
         Upgrade: websocket\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\
         Sec-WebSocket-Protocol: tickdesk.v2\r\nAuthorization: Bearer {}\r\n\r\n",
        token(1)
    );
    head.write_all(request.as_bytes()).await.unwrap();
    let mut status = String::new();
    timeout(
        Duration::from_secs(3),
        BufReader::new(head).read_line(&mut status),
    )
    .await
    .unwrap()
    .unwrap();
    assert!(status.starts_with("HTTP/1.1 405"), "{status}");
    for (bearer, protocol, expected) in [
        (None, "tickdesk.v2", 401),
        (Some(token(99)), "tickdesk.v2", 401),
        (Some(hash(token(1).as_bytes())), "tickdesk.v2", 401),
        (Some(token(1)), "tickdesk.v1", 426),
    ] {
        match server.dial(bearer.as_deref(), protocol).await {
            Err(Error::Http(response)) => assert_eq!(response.status().as_u16(), expected),
            _ => panic!("expected handshake denial"),
        }
    }

    let (mut app, _, _) = server.connect(1, "app_install_1", None).await;
    let (mut other, _, _) = server
        .connect(4, "local_install_1", Some("epoch_demo_a"))
        .await;
    // Even the same claimed incarnation in the other pair cannot receive this traffic.
    send(&mut app, &fixture("deliver_command")).await;
    assert_eq!(message(&mut app).await["code"], "PEER_OFFLINE");
    assert_quiet(&mut other).await;

    let mut hello = fixture("hello_app");
    hello["role"] = json!("local");
    let mut forged = server.dial(Some(&token(1)), "tickdesk.v2").await.unwrap();
    send(&mut forged, &hello).await;
    assert_eq!(close_code(&mut forged).await, 1008);

    let (local, _, _) = server
        .connect(2, "local_install_1", Some("epoch_demo_a"))
        .await;
    assert_eq!(message(&mut app).await["peer"]["online"], true);
    let mut oversized = fixture("deliver_command").to_string();
    let payload_start = oversized.find("\"payload\":{").unwrap() + "\"payload\":{".len();
    oversized.insert_str(payload_start, &" ".repeat(64 * 1024));
    app.send(Message::Text(oversized.into())).await.unwrap();
    assert_eq!(message(&mut app).await["code"], "INVALID_FRAME");

    // Each fragment fits independently; the combined message exceeds 128 KiB.
    app.send(Message::Frame(Frame::message(
        vec![b' '; 65_536],
        OpCode::Data(tokio_tungstenite::tungstenite::protocol::frame::coding::Data::Text),
        false,
    )))
    .await
    .unwrap();
    app.send(Message::Frame(Frame::message(
        vec![b' '; 65_537],
        OpCode::Data(tokio_tungstenite::tungstenite::protocol::frame::coding::Data::Continue),
        true,
    )))
    .await
    .unwrap();
    assert_eq!(close_code(&mut app).await, 1009);
    drop(local);
}

#[tokio::test]
async fn replacement_fences_old_socket_and_failed_reload_preserves_active_credentials() {
    let server = Server::start().await;
    let (mut old, old_welcome, _) = server.connect(1, "app_install_1", None).await;
    let (mut local, _, _) = server
        .connect(2, "local_install_1", Some("epoch_demo_a"))
        .await;
    message(&mut old).await;
    let (mut active, new_welcome, _) = server.connect(1, "app_install_1", None).await;
    assert_ne!(new_welcome["connectionId"], old_welcome["connectionId"]);
    assert_eq!(message(&mut local).await["peer"]["online"], true);
    assert_eq!(close_code(&mut old).await, 4409);
    drop(old);
    send(&mut active, &fixture("deliver_command")).await;
    assert_eq!(message(&mut local).await, fixture("deliver_command"));

    let invalid = roles(1, None).replace(&hash(token(2).as_bytes()), &hash(token(1).as_bytes()));
    replace(server.files.path(), &invalid);
    wait_for_metric(&server, "role_reload_rejected").await;
    assert!(server.health().await.starts_with("HTTP/1.1 200"));
    send(&mut active, &fixture("deliver_command")).await;
    assert_eq!(message(&mut local).await, fixture("deliver_command"));

    replace(server.files.path(), &roles(5, Some(1)));
    wait_for_reload(&server, &token(5)).await;
    send(&mut active, &fixture("deliver_command")).await;
    assert_eq!(message(&mut local).await, fixture("deliver_command"));

    // Keep a token-authenticated WAIT_HELLO socket during revocation.
    let mut pending = server.dial(Some(&token(1)), "tickdesk.v2").await.unwrap();
    replace(server.files.path(), &roles(5, None));
    assert_eq!(close_code(&mut active).await, 4401);
    assert_eq!(close_code(&mut pending).await, 4401);
    assert_eq!(message(&mut local).await, fixture("peer_offline"));
    match server.dial(Some(&token(1)), "tickdesk.v2").await {
        Err(Error::Http(response)) => assert_eq!(response.status().as_u16(), 401),
        _ => panic!("revoked role credential admitted"),
    }
    let (mut rotated, _, _) = server.connect(5, "app_install_1", None).await;
    assert_eq!(message(&mut local).await["peer"]["online"], true);
    send(&mut rotated, &fixture("deliver_command")).await;
    assert_eq!(message(&mut local).await, fixture("deliver_command"));

    replace(server.files.path(), "[pairs]");
    assert_eq!(close_code(&mut rotated).await, 4401);
    assert_eq!(close_code(&mut local).await, 4401);
    assert!(server.health().await.starts_with("HTTP/1.1 503"));
}

async fn wait_for_reload(server: &Server, bearer: &str) {
    timeout(Duration::from_secs(4), async {
        loop {
            if server.dial(Some(bearer), "tickdesk.v2").await.is_ok() {
                return;
            }
            tokio::time::sleep(Duration::from_millis(250)).await;
        }
    })
    .await
    .unwrap();
}

#[tokio::test]
async fn upgrade_rate_limit_replies_with_retry_after() {
    let server = Server::start().await;
    let responses = join_all((0..40).map(|_| server.dial(None, "tickdesk.v2"))).await;
    let mut limited = 0;
    for result in responses {
        match result {
            Err(Error::Http(response)) if response.status() == 429 => {
                assert_eq!(response.headers()["retry-after"], "1");
                limited += 1;
            }
            Err(Error::Http(response)) => assert_eq!(response.status(), 401),
            _ => panic!("unauthenticated handshake accepted"),
        }
    }
    assert!(limited > 0);
}

async fn wait_for_metric(server: &Server, event: &str) {
    use tower::ServiceExt;
    timeout(Duration::from_secs(4), async {
        loop {
            let app = http::metrics_router(server.relay.clone());
            let response = app
                .oneshot(
                    axum::http::Request::builder()
                        .uri("/metrics")
                        .body(axum::body::Body::empty())
                        .unwrap(),
                )
                .await
                .unwrap();
            let bytes = axum::body::to_bytes(response.into_body(), 8192)
                .await
                .unwrap();
            if std::str::from_utf8(&bytes).unwrap().contains(event) {
                return;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    })
    .await
    .unwrap();
}
