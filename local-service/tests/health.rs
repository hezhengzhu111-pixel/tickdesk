use axum::{body::Body, http::Request};
use http_body_util::BodyExt;
use serde_json::Value;
use std::sync::{Arc, Mutex};
use tickdesk_local::{commands::now_ms, http::router_with_storage, storage::Storage};
use tower::ServiceExt;

#[tokio::test]
async fn liveness_identifies_the_compiled_contract() {
    let response = tickdesk_local::http::router()
        .oneshot(
            Request::builder()
                .uri("/health/live")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    let body: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(body["buildVersion"], env!("CARGO_PKG_VERSION"));
    assert_eq!(
        body["contractManifestSha256"],
        tickdesk_local::contract::sha256(include_bytes!("../contracts/v2/manifest.json"))
    );
}

#[tokio::test]
async fn readiness_reports_actual_dependency_and_recovery_state() {
    let temp = tempfile::tempdir().unwrap();
    let storage = Arc::new(Mutex::new(Storage::open(temp.path()).unwrap()));
    let router = router_with_storage(storage.clone());
    async fn get(router: axum::Router) -> (u16, Value) {
        let response = router
            .oneshot(
                Request::builder()
                    .uri("/health/ready")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let status = response.status().as_u16();
        let body = response.into_body().collect().await.unwrap().to_bytes();
        (status, serde_json::from_slice(&body).unwrap())
    }
    let (status, body) = get(router.clone()).await;
    assert_eq!(status, 503);
    assert!(body["pending"]
        .as_array()
        .unwrap()
        .contains(&Value::from("kernel")));
    assert!(body["pending"]
        .as_array()
        .unwrap()
        .contains(&Value::from("vision")));
    assert_eq!(
        body["implementedCommands"],
        serde_json::json!(tickdesk_local::commands::SUPPORTED_COMMANDS)
    );
    let connection = rusqlite::Connection::open(temp.path().join("tickdesk.sqlite3")).unwrap();
    connection
        .execute(
            "INSERT INTO kernel_probe VALUES (1,?1,'available','test','[]','[]',0)",
            [now_ms()],
        )
        .unwrap();
    for name in ["deepseek", "vision"] {
        storage
            .lock()
            .unwrap()
            .record_ai_health(name, "online", None, now_ms())
            .unwrap();
    }
    let (status, body) = get(router.clone()).await;
    assert_eq!(status, 200);
    assert_eq!(body["ready"], true);
    storage.lock().unwrap().enter_recovery().unwrap();
    let (status, body) = get(router).await;
    assert_eq!(status, 503);
    assert!(body["pending"]
        .as_array()
        .unwrap()
        .contains(&Value::from("recovery")));
}
