//! Read Python's actual MCP probe. A configured key alone never means online.
use crate::{commands::now_ms, provider::AiProvider, storage::Storage};
use anyhow::Result;
use serde_json::Value;
use std::{
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{sync::watch, time::MissedTickBehavior};

fn mcp_health(body: &Value, now: u64) -> (&'static str, u64, Option<&'static str>) {
    if body["version"] != 1 {
        return ("unknown", now, Some("MCP_INVALID_RESPONSE"));
    }
    let mcp = &body["mcp"];
    let Some(at) = mcp["checkedAt"].as_u64() else {
        return ("unknown", now, Some("MCP_NOT_CONFIGURED"));
    };
    if now < at || now - at > 660_000 {
        return ("unknown", at.min(now), Some("MCP_PROBE_STALE"));
    }
    if mcp["status"] == "online" && mcp["errorCode"].is_null() {
        return ("online", at, None);
    }
    let code = match mcp["errorCode"].as_str() {
        Some("MCP_NOT_CONFIGURED") => "MCP_NOT_CONFIGURED",
        Some("MCP_AUTH_REQUIRED") => "MCP_AUTH_REQUIRED",
        Some("MCP_TIMEOUT") => "MCP_TIMEOUT",
        Some("MCP_TOOL_FAILED") => "MCP_TOOL_FAILED",
        Some("MCP_TOOL_UNAVAILABLE") => "MCP_TOOL_UNAVAILABLE",
        Some("MCP_RESPONSE_TOO_LARGE") => "MCP_RESPONSE_TOO_LARGE",
        Some("MCP_UNAVAILABLE") => "MCP_UNAVAILABLE",
        _ => "MCP_INVALID_RESPONSE",
    };
    ("offline", at, Some(code))
}

pub async fn run(
    provider: Option<AiProvider>,
    storage: Arc<Mutex<Storage>>,
    mut shutdown: watch::Receiver<bool>,
) -> Result<()> {
    let Some(provider) = provider.filter(AiProvider::is_bridge) else {
        return Ok(());
    };
    let mut ticks = tokio::time::interval(Duration::from_secs(15));
    ticks.set_missed_tick_behavior(MissedTickBehavior::Skip);
    while !*shutdown.borrow() {
        tokio::select! {
            biased;
            _ = shutdown.changed() => break,
            _ = ticks.tick() => {}
        }
        let body = tokio::select! {
            biased;
            _ = shutdown.changed() => break,
            value = provider.diagnostic_status() => value,
        };
        let now = now_ms();
        let (status, at, code) = match body {
            Ok(body) => mcp_health(&body, now),
            Err(_) => ("offline", now, Some("MCP_ASSISTANT_UNAVAILABLE")),
        };
        let storage = storage.clone();
        tokio::task::spawn_blocking(move || {
            storage
                .lock()
                .map_err(|_| anyhow::anyhow!("storage lock poisoned"))?
                .record_ai_health("mcp", status, code, at)
        })
        .await??;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn never_promotes_missing_stale_failed_or_future_evidence() {
        assert_eq!(mcp_health(&json!({"version":1}), 1000).0, "unknown");
        let mut body =
            json!({"version":1,"mcp":{"status":"online","checkedAt":1000,"errorCode":null}});
        assert_eq!(mcp_health(&body, 1100), ("online", 1000, None));
        assert_eq!(mcp_health(&body, 700000).2, Some("MCP_PROBE_STALE"));
        assert_eq!(mcp_health(&body, 900).0, "unknown");
        body["mcp"]["errorCode"] = json!("sensitive provider error");
        assert_eq!(mcp_health(&body, 1100).2, Some("MCP_INVALID_RESPONSE"));
    }

    #[tokio::test]
    async fn authenticated_worker_persists_real_diagnostic_then_stops() {
        use axum::{http::HeaderMap, routing::get, Json, Router};
        let token = "a".repeat(43);
        let expected = format!("Bearer {token}");
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let routes = Router::new().route("/api/tickdesk/ai/v1/status", get(move |headers: HeaderMap| async move {
            assert_eq!(headers.get("authorization").unwrap(), expected.as_str());
            Json(json!({"version":1,"mcp":{"status":"online","checkedAt":now_ms(),"errorCode":null}}))
        }));
        let server = tokio::spawn(async move { axum::serve(listener, routes).await.unwrap() });
        let temp = tempfile::tempdir().unwrap();
        let token_file = temp.path().join("token");
        std::fs::write(&token_file, token).unwrap();
        let provider = AiProvider::bridge(&crate::provider::AssistantConfig {
            address,
            token_file,
        })
        .unwrap();
        let storage = Arc::new(Mutex::new(
            Storage::open(&temp.path().join("data")).unwrap(),
        ));
        let (stop, shutdown) = watch::channel(false);
        let worker = tokio::spawn(run(Some(provider), storage.clone(), shutdown));
        tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                let status = storage.lock().unwrap().connection.query_row(
                    "SELECT status FROM provider_health WHERE name='mcp'",
                    [],
                    |row| row.get::<_, String>(0),
                );
                if let Ok(status) = status {
                    assert_eq!(status, "online");
                    break;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
        stop.send(true).unwrap();
        worker.await.unwrap().unwrap();
        server.abort();
    }
}
