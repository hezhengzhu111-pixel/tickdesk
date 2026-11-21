use axum::{
    body::Body,
    extract::State,
    http::{Request, StatusCode},
    response::Response,
    routing::post,
    Router,
};
use serde_json::{json, Value};
use std::{
    sync::{Arc, Mutex},
    time::Duration,
};
use tickdesk_local::provider::{AiProvider, AiProviderConfig, ProviderError};

type Seen = Arc<Mutex<Vec<Value>>>;

async fn server(
    status: StatusCode,
    body: String,
    delay: u64,
) -> (String, Seen, tokio::task::JoinHandle<()>) {
    let seen: Seen = Arc::new(Mutex::new(Vec::new()));
    let app = Router::new()
        .route(
            "/v1/chat/completions",
            post(move |State(seen): State<Seen>, request: Request<Body>| {
                let body = body.clone();
                async move {
                    let bytes = axum::body::to_bytes(request.into_body(), 40 * 1024 * 1024)
                        .await
                        .unwrap();
                    seen.lock()
                        .unwrap()
                        .push(serde_json::from_slice(&bytes).unwrap());
                    tokio::time::sleep(Duration::from_millis(delay)).await;
                    Response::builder()
                        .status(status)
                        .header("location", "http://127.0.0.1:1/secret")
                        .body(Body::from(body))
                        .unwrap()
                }
            }),
        )
        .with_state(seen.clone());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let endpoint = format!(
        "http://{}/v1/chat/completions",
        listener.local_addr().unwrap()
    );
    let task = tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    (endpoint, seen, task)
}

fn config(endpoint: String) -> AiProviderConfig {
    AiProviderConfig {
        endpoint,
        model: "local-test-model".into(),
        key_file: None,
        timeout_ms: 100,
        max_response_bytes: 1024,
    }
}

#[tokio::test]
async fn sends_real_openai_compatible_request_and_extracts_content() {
    let output = json!({"choices":[{"finish_reason":"stop","message":{"content":"{\"text\":\"ok\",\"sourceRefs\":[]}"}}]}).to_string();
    let (url, seen, task) = server(StatusCode::OK, output, 0).await;
    let provider = AiProvider::new(&config(url)).unwrap();
    let messages = json!([{"role":"user","content":"test only"}]);
    let reply = provider.complete(messages.clone()).await.unwrap();
    assert_eq!(serde_json::from_str::<Value>(&reply).unwrap()["text"], "ok");
    let requests = seen.lock().unwrap();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0]["messages"], messages);
    assert_eq!(requests[0]["model"], "local-test-model");
    assert_eq!(requests[0]["stream"], false);
    task.abort();
}

#[tokio::test]
async fn errors_are_bounded_redacted_and_not_retried_or_redirected() {
    for (status, body, delay, expected) in [
        (
            StatusCode::TEMPORARY_REDIRECT,
            "sensitive-token".into(),
            0,
            ProviderError::Unavailable,
        ),
        (
            StatusCode::UNAUTHORIZED,
            "sensitive-token".into(),
            0,
            ProviderError::Unavailable,
        ),
        (StatusCode::OK, "x".repeat(2048), 0, ProviderError::TooLarge),
        (
            StatusCode::OK,
            "not JSON".into(),
            0,
            ProviderError::InvalidResponse,
        ),
        (
            StatusCode::OK,
            json!({"choices":[{"finish_reason":"length","message":{"content":"partial"}}]})
                .to_string(),
            0,
            ProviderError::InvalidResponse,
        ),
        (StatusCode::OK, "{}".into(), 500, ProviderError::Timeout),
    ] {
        let (url, seen, task) = server(status, body, delay).await;
        let provider = AiProvider::new(&config(url)).unwrap();
        let error = provider
            .complete(json!([{"role":"user","content":"test"}]))
            .await
            .unwrap_err();
        assert_eq!(error, expected);
        assert!(!error.to_string().contains("sensitive"));
        assert_eq!(seen.lock().unwrap().len(), 1);
        task.abort();
    }
}

#[test]
fn configuration_rejects_cleartext_nonloopback_and_embedded_credentials() {
    for endpoint in [
        "http://192.0.2.1/v1/chat/completions",
        "https://user:secret@example.invalid/v1",
        "https://example.invalid/v1?token=secret",
        "file:///tmp/test",
    ] {
        assert!(config(endpoint.into()).validate().is_err());
    }
    assert!(config("https://api.deepseek.com/chat/completions".into())
        .validate()
        .is_ok());
    assert!(config("http://127.0.0.1:1234/v1/chat/completions".into())
        .validate()
        .is_ok());
    assert!(config("http://[::1]:1234/v1/chat/completions".into())
        .validate()
        .is_ok());
}

#[test]
fn credential_file_limits_and_errors_preserve_redaction() {
    let dir = tempfile::tempdir().unwrap();
    let key = dir.path().join("private-test-key");
    let mut config = config("http://127.0.0.1:1/v1/chat/completions".into());
    config.key_file = Some(key.clone());
    std::fs::write(&key, "x".repeat(8192)).unwrap();
    assert!(AiProvider::new(&config).is_ok());
    for bytes in [
        vec![b'x'; 8193],
        b"\n\t".to_vec(),
        b"secret\ninside".to_vec(),
        vec![255],
    ] {
        std::fs::write(&key, bytes).unwrap();
        let error = AiProvider::new(&config)
            .err()
            .expect("invalid credential must fail");
        let text = format!("{error:#}");
        assert!(!text.contains("private-test-key"));
        assert!(!text.contains("secret"));
    }
}

#[tokio::test]
async fn chunked_response_limit_does_not_depend_on_content_length() {
    let app = Router::new().route(
        "/v1/chat/completions",
        post(|| async {
            let chunks = futures_util::stream::iter([
                Ok::<_, std::io::Error>(vec![b'x'; 600]),
                Ok(vec![b'x'; 600]),
            ]);
            Response::new(Body::from_stream(chunks))
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let provider = AiProvider::new(&config(format!(
        "http://{}/v1/chat/completions",
        listener.local_addr().unwrap()
    )))
    .unwrap();
    let task = tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    let result = provider.complete(json!([])).await;
    task.abort();
    assert_eq!(result, Err(ProviderError::TooLarge));
}
