use crate::{
    relay::{AdmissionError, Relay},
    socket,
};
use axum::{
    extract::{ConnectInfo, Request, State},
    http::{header, HeaderMap, HeaderName, Method, StatusCode, Version},
    response::{IntoResponse, Response},
    routing::get,
    Json, Router,
};
use axum_server::{
    tls_rustls::{RustlsAcceptor, RustlsConfig},
    Handle,
};
use base64::{engine::general_purpose::STANDARD, Engine};
use ring::digest::{digest, SHA1_FOR_LEGACY_USE_ONLY};
use serde_json::json;
use std::{
    io,
    net::{SocketAddr, TcpListener},
    sync::Arc,
};
use tokio::time::{self, Duration};

const SUBPROTOCOL: &str = "tickdesk.v2";
const WS_GUID: &[u8] = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

pub fn router(relay: Arc<Relay>) -> Router {
    Router::new()
        .route("/health/live", get(live))
        .route("/health/ready", get(ready))
        .route("/relay/v2/connect", get(connect))
        .with_state(relay)
}

pub fn metrics_router(relay: Arc<Relay>) -> Router {
    Router::new()
        .route("/metrics", get(metrics))
        .with_state(relay)
}

pub async fn serve(
    listener: TcpListener,
    tls: Option<RustlsConfig>,
    relay: Arc<Relay>,
    handle: Handle<SocketAddr>,
) -> io::Result<()> {
    listener.set_nonblocking(true)?;
    let limit = crate::transport::Limit::new();
    let mut server = axum_server::from_tcp(listener)?.handle(handle);
    server
        .http_builder()
        .http1()
        .timer(hyper_util::rt::TokioTimer::new())
        .header_read_timeout(Duration::from_secs(10))
        .max_buf_size(16 * 1024);
    server.http_builder().http2().max_concurrent_streams(16);
    let service = router(relay).into_make_service_with_connect_info::<SocketAddr>();
    // The default axum-server connection driver enables HTTP/1.1 upgrades.
    if let Some(tls) = tls {
        server
            .acceptor(RustlsAcceptor::new(tls).acceptor(limit))
            .serve(service)
            .await
    } else {
        server.acceptor(limit).serve(service).await
    }
}

async fn live() -> impl IntoResponse {
    Json(json!({
        "service":"tickdesk-relay",
        "protocolMajor":crate::contract::PROTOCOL_MAJOR,
        "status":"alive",
        "buildVersion":env!("CARGO_PKG_VERSION"),
        "contractManifestSha256":crate::protocol::sha256_hex(crate::contract::MANIFEST.as_bytes()),
    }))
}

async fn ready(State(relay): State<Arc<Relay>>) -> Response {
    let ready = relay.is_ready();
    let body = Json(json!({"service":"tickdesk-relay","ready":ready}));
    if ready {
        body.into_response()
    } else {
        (
            StatusCode::SERVICE_UNAVAILABLE,
            [(header::RETRY_AFTER, "30")],
            body,
        )
            .into_response()
    }
}

async fn metrics(State(relay): State<Arc<Relay>>) -> impl IntoResponse {
    (
        [(
            header::CONTENT_TYPE,
            "text/plain; version=0.0.4; charset=utf-8",
        )],
        relay.metrics(),
    )
}

async fn connect(
    State(relay): State<Arc<Relay>>,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    mut request: Request,
) -> Response {
    if request.method() != Method::GET {
        return (StatusCode::METHOD_NOT_ALLOWED, [(header::ALLOW, "GET")]).into_response();
    }
    let headers = request.headers();
    if only(headers, header::SEC_WEBSOCKET_PROTOCOL) != Some(SUBPROTOCOL) {
        return (
            StatusCode::UPGRADE_REQUIRED,
            [
                (header::SEC_WEBSOCKET_PROTOCOL, SUBPROTOCOL),
                (header::SEC_WEBSOCKET_VERSION, "13"),
            ],
        )
            .into_response();
    }
    if request.version() != Version::HTTP_11
        || request.uri().query().is_some()
        || !has_token(headers, header::CONNECTION, "upgrade")
        || !has_token(headers, header::UPGRADE, "websocket")
        || only(headers, header::SEC_WEBSOCKET_VERSION) != Some("13")
    {
        return StatusCode::BAD_REQUEST.into_response();
    }
    let Some(key) = only(headers, header::SEC_WEBSOCKET_KEY) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let mut raw = [0u8; 18];
    if key.len() != 24 || STANDARD.decode_slice(key, &mut raw) != Ok(16) {
        return StatusCode::BAD_REQUEST.into_response();
    }
    if request
        .extensions()
        .get::<hyper::upgrade::OnUpgrade>()
        .is_none()
    {
        return StatusCode::BAD_REQUEST.into_response();
    }

    let token = only(headers, header::AUTHORIZATION)
        .and_then(|value| value.split_once(' '))
        .filter(|(scheme, _)| scheme.eq_ignore_ascii_case("bearer"))
        .map(|(_, token)| token);
    let connection = match relay.admit(addr.ip(), token) {
        Ok(connection) => connection,
        Err(AdmissionError::Unauthorized) => {
            return (
                StatusCode::UNAUTHORIZED,
                [(header::WWW_AUTHENTICATE, "Bearer")],
            )
                .into_response();
        }
        Err(AdmissionError::Busy) => {
            return (StatusCode::TOO_MANY_REQUESTS, [(header::RETRY_AFTER, "1")]).into_response();
        }
        Err(AdmissionError::Unavailable) => {
            return (
                StatusCode::SERVICE_UNAVAILABLE,
                [(header::RETRY_AFTER, "30")],
            )
                .into_response();
        }
    };

    // RFC 6455's accept value uses SHA-1; role credentials use SHA-256.
    let mut challenge = [0; 60];
    challenge[..24].copy_from_slice(key.as_bytes());
    challenge[24..].copy_from_slice(WS_GUID);
    let accept = STANDARD.encode(digest(&SHA1_FOR_LEGACY_USE_ONLY, &challenge));
    let upgraded = hyper::upgrade::on(&mut request);
    tokio::spawn(async move {
        if let Ok(Ok(stream)) = time::timeout(Duration::from_secs(5), upgraded).await {
            socket::run(hyper_util::rt::TokioIo::new(stream), relay, connection).await;
        }
    });
    (
        StatusCode::SWITCHING_PROTOCOLS,
        [
            (header::CONNECTION, "Upgrade"),
            (header::UPGRADE, "websocket"),
            (header::SEC_WEBSOCKET_ACCEPT, accept.as_str()),
            (header::SEC_WEBSOCKET_PROTOCOL, SUBPROTOCOL),
        ],
    )
        .into_response()
}

fn only(headers: &HeaderMap, key: HeaderName) -> Option<&str> {
    let all = headers.get_all(key);
    let mut values = all.iter();
    let value = values.next()?.to_str().ok()?;
    values.next().is_none().then_some(value)
}

fn has_token(headers: &HeaderMap, key: HeaderName, token: &str) -> bool {
    headers
        .get_all(key)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(','))
        .any(|item| item.trim().eq_ignore_ascii_case(token))
}
