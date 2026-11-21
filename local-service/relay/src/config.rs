use anyhow::{ensure, Context};
use serde::Deserialize;
use std::{
    net::SocketAddr,
    path::{Path, PathBuf},
};
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Tls {
    pub cert: PathBuf,
    pub key: PathBuf,
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub listen: SocketAddr,
    pub tls: Option<Tls>,
    pub roles_file: PathBuf,
    pub metrics_listen: Option<SocketAddr>,
}
impl Config {
    pub fn load(path: &Path) -> anyhow::Result<Self> {
        let path = if path.is_absolute() {
            path.to_owned()
        } else {
            std::env::current_dir()?.join(path)
        };
        let contents = std::fs::read_to_string(&path).context("read relay configuration")?;
        let mut config: Self = toml::from_str(&contents)
            .map_err(|_| anyhow::anyhow!("invalid relay configuration TOML"))?;
        ensure!(
            config.tls.is_some() || config.listen.ip().is_loopback(),
            "plaintext is only allowed on loopback"
        );
        ensure!(
            config
                .metrics_listen
                .is_none_or(|addr| addr.ip().is_loopback()),
            "metrics must bind to loopback"
        );
        ensure!(
            !config.roles_file.as_os_str().is_empty(),
            "roles_file must not be empty"
        );
        let parent = path.parent().context("configuration parent")?;
        if config.roles_file.is_relative() {
            config.roles_file = parent.join(&config.roles_file);
        }
        if let Some(tls) = &mut config.tls {
            ensure!(
                !tls.cert.as_os_str().is_empty() && !tls.key.as_os_str().is_empty(),
                "TLS paths must not be empty"
            );
            if tls.cert.is_relative() {
                tls.cert = parent.join(&tls.cert);
            }
            if tls.key.is_relative() {
                tls.key = parent.join(&tls.key);
            }
        }
        Ok(config)
    }
}
