use crate::storage::Storage;
use axum::{
    extract::{DefaultBodyLimit, Path, State},
    http::{header::AUTHORIZATION, HeaderMap, HeaderValue, StatusCode},
    response::IntoResponse,
    routing::get,
    Json, Router,
};
use rusqlite::OptionalExtension;
use serde_json::{json, Value};
use std::sync::{Arc, Mutex};

pub fn router() -> Router {
    Router::new()
        .route("/health/live", get(live))
        .route("/health/ready", get(ready))
}
pub fn router_with_storage(storage: Arc<Mutex<Storage>>) -> Router {
    router_with_assistant(storage, None)
}

pub fn router_with_assistant(
    storage: Arc<Mutex<Storage>>,
    authorization: Option<HeaderValue>,
) -> Router {
    let assistant_storage = storage.clone();
    let vision_storage = storage.clone();
    let vision_auth = authorization.clone();
    let analysis_storage = storage.clone();
    let analysis_auth = authorization.clone();
    let schedule_storage = storage.clone();
    let schedule_auth = authorization.clone();
    let report_storage = storage.clone();
    let report_auth = authorization.clone();
    Router::new()
        .route("/health/live", get(live))
        .route("/health/ready", get(ready_with_storage))
        .route("/quant/observations/{scan_id}", get(quant_observations))
        .route(
            "/internal/ai/v1/scope",
            get(move |headers: HeaderMap| async move {
                let Some(key) = authorization else {
                    return (
                        StatusCode::SERVICE_UNAVAILABLE,
                        Json(json!({"error":"AI_NOT_CONFIGURED"})),
                    )
                        .into_response();
                };
                if !assistant_authorized(&headers, &key) {
                    return (
                        StatusCode::UNAUTHORIZED,
                        Json(json!({"error":"AI_AUTH_REQUIRED"})),
                    )
                        .into_response();
                }
                match assistant_storage
                    .lock()
                    .ok()
                    .and_then(|db| db.assistant_scope().ok())
                {
                    Some(scope) => Json(scope).into_response(),
                    None => (
                        StatusCode::SERVICE_UNAVAILABLE,
                        Json(json!({"error":"DATA_UNAVAILABLE"})),
                    )
                        .into_response(),
                }
            }),
        )
        .route(
            "/internal/ai/v1/vision/{job_id}/dispatch",
            axum::routing::post(
                move |Path(id): Path<String>, headers: HeaderMap| async move {
                    let Some(key) = vision_auth else {
                        return StatusCode::SERVICE_UNAVAILABLE;
                    };
                    if !assistant_authorized(&headers, &key) {
                        return StatusCode::UNAUTHORIZED;
                    }
                    let value = |name| {
                        headers
                            .get(name)
                            .and_then(|value| value.to_str().ok())
                            .filter(|id| crate::commands::valid_id(id))
                    };
                    let (Some(epoch), Some(file)) =
                        (value("x-tickdesk-epoch"), value("x-tickdesk-file"))
                    else {
                        return StatusCode::BAD_REQUEST;
                    };
                    if !crate::commands::valid_id(&id) {
                        return StatusCode::BAD_REQUEST;
                    }
                    match vision_storage.lock().ok().and_then(|mut db| {
                        db.authorize_vision_dispatch(&id, epoch, file, crate::commands::now_ms())
                            .ok()
                    }) {
                        Some(true) => StatusCode::NO_CONTENT,
                        Some(false) => StatusCode::CONFLICT,
                        None => StatusCode::SERVICE_UNAVAILABLE,
                    }
                },
            ),
        )
        .route(
            "/internal/ai/v1/analysis/{symbol}",
            get(
                move |Path(symbol): Path<String>, headers: HeaderMap| async move {
                    if let Err(status) =
                        authorize_analysis(&headers, analysis_auth.as_ref(), &symbol)
                    {
                        return (status, Json(Value::Null));
                    }
                    match analysis_storage.lock().ok().and_then(|mut db| {
                        db.analysis_snapshot(&symbol, crate::commands::now_ms())
                            .ok()
                    }) {
                        Some(Some(result)) => (StatusCode::OK, Json(result)),
                        Some(None) => (
                            StatusCode::NOT_FOUND,
                            Json(json!({"error":"DATA_UNAVAILABLE"})),
                        ),
                        None => (
                            StatusCode::SERVICE_UNAVAILABLE,
                            Json(json!({"error":"DATA_UNAVAILABLE"})),
                        ),
                    }
                },
            )
            .post(
                move |Path(symbol): Path<String>,
                      headers: HeaderMap,
                      Json(request): Json<crate::chat::scheduled::Request>| async move {
                    if let Err(status) =
                        authorize_analysis(&headers, schedule_auth.as_ref(), &symbol)
                    {
                        return (status, Json(Value::Null));
                    }
                    match schedule_storage.lock().ok().and_then(|mut db| {
                        db.queue_analysis(&symbol, request, crate::commands::now_ms())
                            .ok()
                    }) {
                        Some(Some(result)) => (StatusCode::OK, Json(result)),
                        Some(None) => (
                            StatusCode::CONFLICT,
                            Json(json!({"error":"AI_STALE_CONTEXT"})),
                        ),
                        None => (
                            StatusCode::SERVICE_UNAVAILABLE,
                            Json(json!({"error":"DATA_UNAVAILABLE"})),
                        ),
                    }
                },
            )
            .layer(DefaultBodyLimit::max(4096)),
        )
        .route(
            "/internal/ai/v1/report/{symbol}",
            get(
                move |Path(symbol): Path<String>, headers: HeaderMap| async move {
                    if let Err(status) = authorize_analysis(&headers, report_auth.as_ref(), &symbol)
                    {
                        return (status, Json(Value::Null));
                    }
                    match report_storage.lock().ok().and_then(|mut db| {
                        db.latest_analysis(&symbol, crate::commands::now_ms()).ok()
                    }) {
                        Some(Some(result)) => (StatusCode::OK, Json(result)),
                        Some(None) => (
                            StatusCode::NOT_FOUND,
                            Json(json!({"error":"AI_REPORT_UNAVAILABLE"})),
                        ),
                        None => (
                            StatusCode::SERVICE_UNAVAILABLE,
                            Json(json!({"error":"DATA_UNAVAILABLE"})),
                        ),
                    }
                },
            ),
        )
        .with_state(storage)
}

fn authorize_analysis(
    headers: &HeaderMap,
    key: Option<&HeaderValue>,
    symbol: &str,
) -> Result<(), StatusCode> {
    let Some(key) = key else {
        return Err(StatusCode::SERVICE_UNAVAILABLE);
    };
    if !assistant_authorized(headers, key) {
        return Err(StatusCode::UNAUTHORIZED);
    }
    crate::contract::validate_definition("Symbol", &json!(symbol))
        .map_err(|_| StatusCode::BAD_REQUEST)
}

fn assistant_authorized(headers: &HeaderMap, expected: &HeaderValue) -> bool {
    let key = ring::hmac::Key::new(ring::hmac::HMAC_SHA256, expected.as_bytes());
    let expected = ring::hmac::sign(&key, b"tickdesk-ai-ipc-v1");
    headers.get(AUTHORIZATION).is_some_and(|header| {
        let presented = ring::hmac::Key::new(ring::hmac::HMAC_SHA256, header.as_bytes());
        ring::hmac::verify(&presented, b"tickdesk-ai-ipc-v1", expected.as_ref()).is_ok()
    })
}

async fn quant_observations(
    State(storage): State<Arc<Mutex<Storage>>>,
    Path(scan_id): Path<String>,
) -> Result<Json<Value>, StatusCode> {
    if uuid::Uuid::parse_str(&scan_id).is_err() {
        return Err(StatusCode::BAD_REQUEST);
    }
    let row = tokio::task::spawn_blocking(move || {
        storage
            .lock()
            .map_err(|_| anyhow::anyhow!("storage unavailable"))?
            .quant_observations(&scan_id)
    })
    .await
    .map_err(|_| StatusCode::SERVICE_UNAVAILABLE)?
    .map_err(|_| StatusCode::SERVICE_UNAVAILABLE)?;
    row.map(Json).ok_or(StatusCode::NOT_FOUND)
}
async fn live() -> Json<Value> {
    Json(json!({
        "service":"tickdesk-local",
        "protocolMajor":crate::contract::PROTOCOL_MAJOR,
        "status":"alive",
        "buildVersion":env!("CARGO_PKG_VERSION"),
        "contractManifestSha256":crate::contract::sha256(include_bytes!("../contracts/v2/manifest.json")),
    }))
}
async fn ready() -> (StatusCode, Json<Value>) {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({
            "service":"tickdesk-local", "ready":false, "implementedCommands":crate::commands::SUPPORTED_COMMANDS,
            "pending":["storage", "kernel", "deepseek", "vision"]
        })),
    )
}

async fn ready_with_storage(
    State(storage): State<Arc<Mutex<Storage>>>,
) -> (StatusCode, Json<Value>) {
    let status = tokio::task::spawn_blocking(move || {
        storage
            .lock()
            .map_err(|_| anyhow::anyhow!("storage unavailable"))?
            .readiness(crate::commands::now_ms())
    })
    .await;
    match status {
        Ok(Ok(body)) => (
            if body["ready"] == true {
                StatusCode::OK
            } else {
                StatusCode::SERVICE_UNAVAILABLE
            },
            Json(body),
        ),
        _ => (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"service":"tickdesk-local", "ready":false, "pending":["storage"]})),
        ),
    }
}

impl Storage {
    pub fn readiness(&self, now: u64) -> anyhow::Result<Value> {
        let mut pending = Vec::new();
        if self.recovery_active()? {
            pending.push("recovery");
        }
        let kernel: Option<(u64, String)> = self
            .connection
            .query_row(
                "SELECT checked_at,status FROM kernel_probe WHERE singleton=1",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?;
        if kernel.is_none_or(|(at, status)| now < at || now - at > 45_000 || status != "available")
        {
            pending.push("kernel");
        }
        let mut dependencies = Vec::new();
        for name in ["deepseek", "vision"] {
            let state: Option<(String, u64, Option<String>)> = self
                .connection
                .query_row(
                    "SELECT status,checked_at,error_code FROM provider_health WHERE name=?1",
                    [name],
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
                )
                .optional()?;
            if state
                .as_ref()
                .is_none_or(|(status, at, _)| status != "online" || *at > now)
            {
                pending.push(name);
            }
            dependencies.push(match state {
                Some((status, at, code)) => {
                    json!({"name":name,"status":status,"checkedAt":at,"errorCode":code})
                }
                None => json!({"name":name,"status":"unknown","checkedAt":null,"errorCode":null}),
            });
        }
        Ok(
            json!({"service":"tickdesk-local","ready":pending.is_empty(),
            "implementedCommands":crate::commands::SUPPORTED_COMMANDS,"pending":pending,"dependencies":dependencies}),
        )
    }
}
