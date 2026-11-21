use crate::{commands::now_ms, storage::Storage};
use anyhow::{ensure, Result};
use reqwest::{redirect::Policy, Client, StatusCode};
use rusqlite::params;
use serde::{de::DeserializeOwned, Deserialize};
use serde_json::Value;
use std::{
    collections::BTreeSet,
    net::SocketAddr,
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{sync::watch, time::MissedTickBehavior};

const SAMPLE_INTERVAL: Duration = Duration::from_secs(15);
const REQUEST_TIMEOUT: Duration = Duration::from_secs(3);
const BODY_LIMIT: usize = 512 * 1024;
const CATALOG_LIMIT: usize = 2048;
const ROUTE_LIMIT: usize = 128;

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct KernelConfig {
    pub address: SocketAddr,
    pub token_file: Option<std::path::PathBuf>,
}

impl KernelConfig {
    pub fn validate(&self) -> Result<()> {
        ensure!(
            self.address.ip().is_loopback() && self.address.port() != 0,
            "kernel address must use a loopback IP and nonzero port"
        );
        Ok(())
    }

    pub(crate) fn headers(&self) -> Result<reqwest::header::HeaderMap> {
        let mut headers = reqwest::header::HeaderMap::new();
        if let Some(path) = &self.token_file {
            headers.insert(
                reqwest::header::AUTHORIZATION,
                crate::transport::bearer_header(path, "kernel token")?,
            );
        }
        Ok(headers)
    }
}

#[derive(Deserialize)]
struct Health {
    status: String,
    version: String,
}

#[derive(Deserialize)]
struct Matrix {
    capabilities: Vec<Capability>,
}

#[derive(Deserialize)]
struct Capability {
    id: String,
    usable: bool,
    effective: String,
}

#[derive(Deserialize)]
struct Catalog {
    tickdesk_admission_version: u32,
    strategies: Vec<Strategy>,
    load_errors: Vec<Value>,
}

#[derive(Deserialize)]
struct Strategy {
    id: String,
    research_only: bool,
}

#[derive(Debug, PartialEq, Eq)]
enum Rejection {
    Unreachable,
    Authorization,
    InvalidResponse,
}

type ProbeResult<T> = std::result::Result<T, Rejection>;

struct Snapshot {
    checked_at: u64,
    status: &'static str,
    version: Option<String>,
    usable_capabilities: Vec<String>,
    strategy_ids: Vec<String>,
    strategy_load_errors: usize,
}

impl Snapshot {
    fn persist(&self, storage: &Storage) -> Result<()> {
        storage.connection.execute(
            "INSERT INTO kernel_probe(singleton,checked_at,status,version,usable_capabilities,strategy_ids,strategy_load_errors)
             VALUES (1,?1,?2,?3,?4,?5,?6)
             ON CONFLICT(singleton) DO UPDATE SET checked_at=excluded.checked_at,status=excluded.status,
             version=excluded.version,usable_capabilities=excluded.usable_capabilities,
             strategy_ids=excluded.strategy_ids,strategy_load_errors=excluded.strategy_load_errors",
            params![
                self.checked_at, self.status, self.version,
                serde_json::to_string(&self.usable_capabilities)?,
                serde_json::to_string(&self.strategy_ids)?, self.strategy_load_errors,
            ],
        )?;
        Ok(())
    }
}

struct Probe {
    address: SocketAddr,
    client: Client,
}

impl Probe {
    fn new(config: KernelConfig) -> Result<Self> {
        config.validate()?;
        let _ = rustls::crypto::ring::default_provider().install_default();
        Ok(Self {
            address: config.address,
            client: Client::builder()
                .default_headers(config.headers()?)
                .no_proxy()
                .redirect(Policy::none())
                .timeout(REQUEST_TIMEOUT)
                .build()?,
        })
    }

    async fn get<T: DeserializeOwned>(&self, path: &'static str) -> ProbeResult<T> {
        let response = self
            .client
            .get(format!("http://{}{path}", self.address))
            .send()
            .await
            .map_err(|_| Rejection::Unreachable)?;
        let status = response.status();
        if status == StatusCode::UNAUTHORIZED || status == StatusCode::FORBIDDEN {
            return Err(Rejection::Authorization);
        }
        if status != StatusCode::OK {
            return Err(Rejection::InvalidResponse);
        }
        let body = crate::transport::read_limited(response, BODY_LIMIT)
            .await
            .map_err(|error| match error {
                crate::transport::BodyError::TooLarge => Rejection::InvalidResponse,
                crate::transport::BodyError::Transport(_) => Rejection::Unreachable,
            })?;
        serde_json::from_slice(&body).map_err(|_| Rejection::InvalidResponse)
    }

    async fn sample(&self) -> Snapshot {
        let (health, matrix, catalog) = tokio::join!(
            self.get::<Health>("/health"),
            self.get::<Matrix>("/api/settings/capability-matrix"),
            self.get::<Catalog>(
                "/api/strategies?asset_type=stock&timeframe=1d&include_research=false&tickdesk_admission=true"
            )
        );
        let health = health.and_then(|health| {
            if health.status == "ok" && valid_label(&health.version) {
                Ok(health.version)
            } else {
                Err(Rejection::InvalidResponse)
            }
        });
        let matrix = matrix.and_then(usable_capabilities);
        let catalog = catalog.and_then(strategy_catalog);
        let status = match (&health, &matrix, &catalog) {
            (Err(Rejection::Unreachable), _, _) => "offline",
            (Err(Rejection::Authorization), _, _)
            | (Ok(_), Err(Rejection::Authorization), _)
            | (Ok(_), _, Err(Rejection::Authorization)) => "auth_required",
            (Ok(_), Ok(capabilities), Ok((strategies, 0)))
                if !strategies.is_empty() && capabilities.iter().any(|id| id == "daily") =>
            {
                "available"
            }
            _ => "degraded",
        };
        // An invalid catalog is not evidence that the valid catalog is empty.
        let (strategy_ids, strategy_load_errors) = catalog.unwrap_or_else(|_| (Vec::new(), 1));
        Snapshot {
            checked_at: now_ms(),
            status,
            version: health.ok(),
            usable_capabilities: matrix.unwrap_or_default(),
            strategy_ids,
            strategy_load_errors,
        }
    }
}

fn valid_label(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_.+".contains(&b))
}

fn usable_capabilities(matrix: Matrix) -> ProbeResult<Vec<String>> {
    if matrix.capabilities.len() > ROUTE_LIMIT {
        return Err(Rejection::InvalidResponse);
    }
    let mut seen = BTreeSet::new();
    let mut usable = Vec::new();
    for cap in matrix.capabilities {
        if !valid_label(&cap.id) || !valid_label(&cap.effective) || !seen.insert(cap.id.clone()) {
            return Err(Rejection::InvalidResponse);
        }
        if cap.usable {
            usable.push(cap.id);
        }
    }
    usable.sort();
    Ok(usable)
}

fn strategy_catalog(catalog: Catalog) -> ProbeResult<(Vec<String>, usize)> {
    if catalog.tickdesk_admission_version != 1
        || catalog.strategies.len() > CATALOG_LIMIT
        || catalog.load_errors.len() > CATALOG_LIMIT
    {
        return Err(Rejection::InvalidResponse);
    }
    let mut ids = BTreeSet::new();
    for strategy in catalog.strategies {
        if strategy.research_only || !valid_label(&strategy.id) || !ids.insert(strategy.id) {
            return Err(Rejection::InvalidResponse);
        }
    }
    Ok((ids.into_iter().collect(), catalog.load_errors.len()))
}

pub async fn run(
    config: KernelConfig,
    storage: Arc<Mutex<Storage>>,
    mut shutdown: watch::Receiver<bool>,
) -> Result<()> {
    let probe = Probe::new(config)?;
    let mut ticks = tokio::time::interval(SAMPLE_INTERVAL);
    ticks.set_missed_tick_behavior(MissedTickBehavior::Skip);
    while !*shutdown.borrow() {
        tokio::select! {
            biased;
            _ = shutdown.changed() => break,
            _ = ticks.tick() => {}
        }
        let snapshot = tokio::select! {
            biased;
            _ = shutdown.changed() => break,
            snapshot = probe.sample() => snapshot,
        };
        let storage = Arc::clone(&storage);
        tokio::task::spawn_blocking(move || {
            let storage = storage
                .lock()
                .map_err(|_| anyhow::anyhow!("storage lock poisoned"))?;
            snapshot.persist(&storage)
        })
        .await??;
    }
    Ok(())
}

#[cfg(test)]
mod tests;
