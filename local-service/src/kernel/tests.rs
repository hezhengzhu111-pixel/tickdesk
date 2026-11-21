use super::*;
use axum::{http::StatusCode, response::Redirect, routing::get, Json, Router};
use serde_json::json;
use std::sync::atomic::{AtomicBool, Ordering};
use tokio::net::TcpListener;

const CAPABILITIES: &str = "/api/settings/capability-matrix";
const STRATEGIES: &str = "/api/strategies";

async fn server(routes: Router) -> (KernelConfig, tokio::task::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let config = KernelConfig {
        address: listener.local_addr().unwrap(),
        token_file: None,
    };
    let task = tokio::spawn(async move {
        axum::serve(listener, routes).await.unwrap();
    });
    (config, task)
}

fn health() -> Router {
    Router::new().route(
        "/health",
        get(|| async { Json(json!({"status":"ok","version":"1.2.3","mode":"none","memory":{}})) }),
    )
}

#[tokio::test]
async fn connected_kernel_without_production_strategies_is_explicit() {
    let (config, task) = server(
        health()
            .route(
                CAPABILITIES,
                get(|| async {
                    Json(json!({"capabilities":[{"id":"daily","usable":true,"effective":"fuyao"}]}))
                }),
            )
            .route(
                STRATEGIES,
                get(|| async {
                    Json(json!({"tickdesk_admission_version":1,"strategies":[],"load_errors":[]}))
                }),
            ),
    )
    .await;
    let result = Probe::new(config).unwrap().sample().await;
    assert_eq!(result.status, "degraded");
    assert_eq!(result.version.as_deref(), Some("1.2.3"));
    assert_eq!(result.strategy_load_errors, 0);
    task.abort();
}

#[tokio::test]
async fn catalog_without_admission_view_cannot_make_kernel_ready() {
    let (config, task) = server(
        health()
            .route(
                CAPABILITIES,
                get(|| async {
                    Json(json!({"capabilities":[{"id":"daily","usable":true,"effective":"fuyao"}]}))
                }),
            )
            .route(
                STRATEGIES,
                get(|| async {
                    Json(json!({"strategies":[{"id":"unverified","research_only":false}],"load_errors":[]}))
                }),
            ),
    )
    .await;
    let result = Probe::new(config).unwrap().sample().await;
    assert_eq!(result.status, "degraded");
    assert!(result.strategy_ids.is_empty());
    assert_eq!(result.strategy_load_errors, 1);
    task.abort();
}

#[test]
fn accepts_literal_loopback_only() {
    for addr in ["0.0.0.0:3018", "10.2.3.4:3018", "[::]:3018", "127.0.0.1:0"] {
        let config = KernelConfig {
            address: addr.parse().unwrap(),
            token_file: None,
        };
        assert!(config.validate().is_err(), "{addr}");
    }
    for addr in ["127.0.0.1:3018", "[::1]:3018"] {
        let config = KernelConfig {
            address: addr.parse().unwrap(),
            token_file: None,
        };
        assert!(config.validate().is_ok(), "{addr}");
    }
}

#[tokio::test]
async fn reads_real_shapes_and_uses_effective_availability() {
    let (config, task) = server(
        health()
            .route(
                CAPABILITIES,
                get(|| async {
                    Json(json!({"tickflow_tier": "none", "capabilities":[
                        {"id":"minute","usable":false,"effective":"tickflow","tf_available":true},
                        {"id":"daily","usable":true,"effective":"custom","tf_available":false}
                    ]}))
                }),
            )
            .route(
                STRATEGIES,
                get(
                    |axum::extract::RawQuery(query): axum::extract::RawQuery| async move {
                        assert_eq!(
                            query.as_deref(),
                            Some("asset_type=stock&timeframe=1d&include_research=false&tickdesk_admission=true")
                        );
                        Json(
                            json!({"tickdesk_admission_version":1,
                                "strategies":[{"id":"x","version":"1.0","research_only":false}],
                                "load_errors":[]}),
                        )
                    },
                ),
            ),
    )
    .await;
    let result = Probe::new(config).unwrap().sample().await;
    assert_eq!(result.status, "available");
    assert_eq!(result.version.as_deref(), Some("1.2.3"));
    assert_eq!(result.usable_capabilities, ["daily"]);
    assert_eq!(result.strategy_ids, ["x"]);
    assert_eq!(result.strategy_load_errors, 0);
    task.abort();
}

#[tokio::test]
async fn auth_rejection_is_explicit_and_redirect_is_never_followed() {
    let followed = Arc::new(AtomicBool::new(false));
    let observed = Arc::clone(&followed);
    let (config, task) = server(
        health()
            .route(CAPABILITIES, get(|| async { StatusCode::UNAUTHORIZED }))
            .route(
                STRATEGIES,
                get(|| async { Redirect::temporary("/never-called") }),
            )
            .route(
                "/never-called",
                get(move || async move {
                    observed.store(true, Ordering::Relaxed);
                    StatusCode::OK
                }),
            ),
    )
    .await;
    let result = Probe::new(config).unwrap().sample().await;
    assert_eq!(result.status, "auth_required");
    assert!(result.usable_capabilities.is_empty());
    assert!(result.strategy_ids.is_empty());
    assert!(!followed.load(Ordering::Relaxed));
    task.abort();
}

#[tokio::test]
async fn an_oversized_chunked_body_is_a_degraded_sample() {
    use axum::{
        body::{Body, Bytes},
        response::Response,
    };
    let (config, task) = server(
        health()
            .route(
                CAPABILITIES,
                get(|| async {
                    let stream = futures_util::stream::iter((0..=8).map(|_| {
                        Ok::<_, std::convert::Infallible>(Bytes::from(vec![b' '; 65536]))
                    }));
                    Response::new(Body::from_stream(stream))
                }),
            )
            .route(
                STRATEGIES,
                get(|| async {
                    Json(json!({"tickdesk_admission_version":1,"strategies":[],"load_errors":[]}))
                }),
            ),
    )
    .await;
    assert_eq!(
        Probe::new(config).unwrap().sample().await.status,
        "degraded"
    );
    task.abort();
}

#[tokio::test]
async fn the_polling_worker_persists_only_sanitized_diagnostics() {
    let (config, server) = server(
        health()
            .route(CAPABILITIES, get(|| async { StatusCode::FORBIDDEN }))
            .route(STRATEGIES, get(|| async {
                Json(json!({
                    "tickdesk_admission_version":1,
                    "strategies": [{"id":"published","research_only":false}],
                    "load_errors":[{"file":"sensitive/path.py","error":"sensitive model output"}]
                }))
            })),
    )
    .await;
    let temp = tempfile::tempdir().unwrap();
    let storage = Arc::new(Mutex::new(Storage::open(temp.path()).unwrap()));
    let (stop, shutdown) = watch::channel(false);
    let worker = tokio::spawn(run(config, Arc::clone(&storage), shutdown));
    let (status, version, ids, errors, checked_at) = tokio::time::timeout(
        Duration::from_secs(3),
        async {
            loop {
                let row = storage.lock().unwrap().connection.query_row(
                    "SELECT status,version,strategy_ids,strategy_load_errors,checked_at FROM kernel_probe",
                    [],
                    |row| Ok((row.get::<_,String>(0)?, row.get::<_,String>(1)?,
                        row.get::<_,String>(2)?, row.get::<_,u64>(3)?, row.get::<_,u64>(4)?)),
                );
                match row {
                    Ok(row) => break row,
                    Err(rusqlite::Error::QueryReturnedNoRows) => {}
                    Err(error) => panic!("{error}"),
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        }).await.unwrap();
    stop.send(true).unwrap();
    worker.await.unwrap().unwrap();
    server.abort();
    assert_eq!(status, "auth_required");
    assert_eq!(version, "1.2.3");
    assert_eq!(ids, r#"["published"]"#);
    assert_eq!(errors, 1);
    assert!(checked_at > 0);
}
