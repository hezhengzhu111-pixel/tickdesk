use anyhow::{ensure, Context};
use serde::Deserialize;
use std::{
    net::SocketAddr,
    path::{Path, PathBuf},
};
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub listen: SocketAddr,
    pub data_dir: PathBuf,
    pub relay: Option<crate::relay::PairingConfig>,
    pub kernel: Option<crate::kernel::KernelConfig>,
    pub chat: Option<crate::provider::AiProviderConfig>,
    pub vision: Option<crate::provider::AiProviderConfig>,
    pub assistant: Option<crate::provider::AssistantConfig>,
    pub budget_file: Option<PathBuf>,
}
impl Config {
    pub fn load(path: &Path) -> anyhow::Result<Self> {
        let path = if path.is_absolute() {
            path.to_owned()
        } else {
            std::env::current_dir()?.join(path)
        };
        let mut config: Self =
            toml::from_str(&std::fs::read_to_string(&path).context("read local configuration")?)
                .context("parse local configuration")?;
        ensure!(
            config.listen.ip().is_loopback(),
            "local management listener must use loopback"
        );
        ensure!(
            !config.data_dir.as_os_str().is_empty(),
            "data_dir must not be empty"
        );
        if config.data_dir.is_relative() {
            config.data_dir = path
                .parent()
                .context("configuration parent")?
                .join(&config.data_dir);
        }
        if let Some(kernel) = &mut config.kernel {
            if let Some(file) = kernel.token_file.as_mut() {
                if file.is_relative() {
                    *file = path.parent().context("configuration parent")?.join(&*file);
                }
            }
            kernel.validate()?;
        }
        if let Some(file) = config.budget_file.as_mut() {
            if file.is_relative() {
                *file = path.parent().context("configuration parent")?.join(&*file);
            }
        }
        if let Some(assistant) = &mut config.assistant {
            ensure!(
                assistant.address.ip().is_loopback() && assistant.address.port() != 0,
                "assistant address must be a literal loopback socket"
            );
            ensure!(config.chat.is_none() && config.vision.is_none(),
                "[assistant] replaces [chat] and [vision]; move model configuration to the Python backend");
            if assistant.token_file.is_relative() {
                assistant.token_file = path
                    .parent()
                    .context("configuration parent")?
                    .join(&assistant.token_file);
            }
        }
        for provider in [&mut config.chat, &mut config.vision].into_iter().flatten() {
            if let Some(key_file) = provider.key_file.as_mut() {
                if key_file.is_relative() {
                    *key_file = path
                        .parent()
                        .context("configuration parent")?
                        .join(&*key_file);
                }
            }
            provider.validate()?;
        }
        Ok(config)
    }
}
