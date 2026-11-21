use anyhow::{bail, Context};
use std::{path::PathBuf, sync::Arc, time::Duration};
use tickdesk_relay::{auth::Roles, config::Config, contract, http, relay::Relay};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter("off,tickdesk_relay=info")
        .with_target(false)
        .init();
    let mut path = PathBuf::from("config/dev.toml");
    let mut check = false;
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--config" => path = args.next().context("--config requires a path")?.into(),
            "--check-config" => check = true,
            "--help" | "-h" => {
                println!("tickdesk-relay [--config PATH] [--check-config]");
                return Ok(());
            }
            _ => bail!("unknown argument"),
        }
    }
    let config = Config::load(&path)?;
    contract::verify_contract()?;
    let roles = Roles::load(&config.roles_file)?;
    let relay = Relay::new(roles)?;
    let _ = rustls::crypto::ring::default_provider().install_default();
    // Validate actual PEM files even for --check-config; never start a listener in check mode.
    let tls = match config.tls {
        Some(tls) => Some(
            tickdesk_relay::tls::rustls_config(&tls.cert, &tls.key)
                .map_err(|_| anyhow::anyhow!("invalid or unreadable TLS certificate/key"))?,
        ),
        None => None,
    };
    if check {
        println!("configuration, TLS (when configured), and v2 contract valid");
        return Ok(());
    }
    let listener = std::net::TcpListener::bind(config.listen).context("bind relay listener")?;
    let metrics = match config.metrics_listen {
        Some(addr) => Some(
            tokio::net::TcpListener::bind(addr)
                .await
                .context("bind metrics listener")?,
        ),
        None => None,
    };
    let handle = axum_server::Handle::new();
    let shutdown = handle.clone();
    let closing = Arc::clone(&relay);
    let signal = tokio::spawn(async move {
        tickdesk_relay::lifecycle::shutdown_signal().await;
        closing.stop();
        shutdown.graceful_shutdown(Some(Duration::from_secs(5)));
    });
    let reloader = tokio::spawn(Arc::clone(&relay).watch_roles(config.roles_file));
    let metrics = metrics.map(|listener| {
        let app = http::metrics_router(Arc::clone(&relay));
        tokio::spawn(async move {
            if axum::serve(listener, app).await.is_err() {
                tracing::error!("metrics listener failed");
            }
        })
    });

    tracing::info!(listen=%config.listen, tls=tls.is_some(), "relay listening");
    let result = http::serve(listener, tls, Arc::clone(&relay), handle).await;
    relay.stop();
    let _ = tokio::time::timeout(Duration::from_secs(4), relay.wait_drained()).await;
    signal.abort();
    reloader.abort();
    if let Some(metrics) = metrics {
        metrics.abort();
    }
    result.context("serve relay")
}
