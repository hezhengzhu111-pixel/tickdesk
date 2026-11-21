use axum::{
    body::Body,
    http::{Request, StatusCode},
};
use tickdesk_relay::{auth::Roles, config::Config, http::router, relay::Relay};
use tokio::time::{Duration, Instant};
use tower::ServiceExt;

#[test]
fn plaintext_is_loopback_only_and_unknown_options_fail() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("relay.toml");
    std::fs::write(&path, "listen='127.0.0.1:18443'\nroles_file='roles.toml'").unwrap();
    assert!(Config::load(&path).unwrap().tls.is_none());
    for text in [
        "listen='0.0.0.0:8443'\nroles_file='roles.toml'",
        "listen='127.0.0.1:18443'\nroles_file='roles.toml'\nunsafe=true",
        "listen='0.0.0.0:8443'\nroles_file='roles.toml'\n[tls]\ncert='cert.pem'",
        "listen='127.0.0.1:18443'\nroles_file='roles.toml'\nmetrics_listen='0.0.0.0:9999'",
    ] {
        std::fs::write(&path, text).unwrap();
        assert!(Config::load(&path).is_err());
    }
}

#[test]
fn tls_paths_resolve_against_the_configuration_directory() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("relay.toml");
    std::fs::write(
        &path,
        "listen='0.0.0.0:8443'\nroles_file='roles.toml'\n[tls]\ncert='cert.pem'\nkey='key.pem'",
    )
    .unwrap();
    let config = Config::load(&path).unwrap();
    assert_eq!(config.tls.unwrap().key, temp.path().join("key.pem"));
    assert_eq!(config.roles_file, temp.path().join("roles.toml"));
}

#[tokio::test]
async fn empty_role_store_never_reports_ready_and_metrics_are_off_the_main_router() {
    let relay =
        Relay::new(Roles::parse("[pairs]", Duration::ZERO, Instant::now()).unwrap()).unwrap();
    for (path, expected) in [
        ("/health/live", StatusCode::OK),
        ("/health/ready", StatusCode::SERVICE_UNAVAILABLE),
        ("/metrics", StatusCode::NOT_FOUND),
    ] {
        let response = router(relay.clone())
            .oneshot(Request::builder().uri(path).body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(response.status(), expected);
        if path == "/health/live" {
            let bytes = axum::body::to_bytes(response.into_body(), 1024)
                .await
                .unwrap();
            let body: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
            let expected_hash = ring::digest::digest(
                &ring::digest::SHA256,
                include_bytes!("../contracts/v2/manifest.json"),
            )
            .as_ref()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
            assert_eq!(body["buildVersion"], env!("CARGO_PKG_VERSION"));
            assert_eq!(body["contractManifestSha256"], expected_hash);
        }
    }
}
