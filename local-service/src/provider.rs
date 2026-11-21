//! Bounded OpenAI-compatible transport. No vendor error body, key, or input is logged.
use anyhow::{ensure, Context};
use reqwest::{header::HeaderValue, redirect::Policy, Client, Url};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{fmt, net::SocketAddr, path::PathBuf, time::Duration};

// Match the Python assistant bridge response limit for chat.
const CHAT_BRIDGE_RESULT_LIMIT: usize = 24 * 1024 * 1024;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AssistantConfig {
    pub address: SocketAddr,
    pub token_file: PathBuf,
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AiProviderConfig {
    /// Full OpenAI-compatible chat completions URL, including the path.
    pub endpoint: String,
    pub model: String,
    pub key_file: Option<PathBuf>,
    #[serde(default = "default_timeout")]
    pub timeout_ms: u64,
    #[serde(default = "default_response_limit")]
    pub max_response_bytes: usize,
}

fn default_timeout() -> u64 {
    30_000
}
fn default_response_limit() -> usize {
    256 * 1024
}

impl fmt::Debug for AiProviderConfig {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("AiProviderConfig")
            .field("configured", &true)
            .finish()
    }
}

impl AiProviderConfig {
    pub fn validate(&self) -> anyhow::Result<()> {
        let url = Url::parse(&self.endpoint).context("invalid AI endpoint URL")?;
        let loopback = url.host_str().is_some_and(|host| {
            host == "localhost"
                || host
                    .trim_matches(['[', ']'])
                    .parse::<std::net::IpAddr>()
                    .is_ok_and(|ip| ip.is_loopback())
        });
        ensure!(
            url.scheme() == "https" || (url.scheme() == "http" && loopback),
            "AI endpoint must use HTTPS; HTTP is limited to loopback"
        );
        ensure!(
            url.username().is_empty()
                && url.password().is_none()
                && url.query().is_none()
                && url.fragment().is_none(),
            "AI URL must not contain credentials, query, or fragment"
        );
        ensure!(
            !self.model.trim().is_empty() && self.model.len() <= 200,
            "AI model is invalid"
        );
        ensure!(
            (100..=120_000).contains(&self.timeout_ms),
            "AI timeout must be 100..120000 milliseconds"
        );
        ensure!(
            (1024..=1024 * 1024).contains(&self.max_response_bytes),
            "AI response limit must be 1024..1048576 bytes"
        );
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProviderError {
    NotConfigured,
    Timeout,
    Unavailable,
    InvalidResponse,
    TooLarge,
    Interrupted,
    UnverifiedSource,
    StaleContext,
    ConsentRequired,
    BudgetExhausted,
    BudgetNotConfigured,
    BudgetUnavailable,
    RateLimited,
    ProviderBalance,
    Replay,
}

impl ProviderError {
    pub fn code(self) -> &'static str {
        match self {
            Self::NotConfigured => "AI_NOT_CONFIGURED",
            Self::Timeout => "AI_TIMEOUT",
            Self::Unavailable => "AI_UNAVAILABLE",
            Self::InvalidResponse => "AI_INVALID_RESPONSE",
            Self::TooLarge => "AI_RESPONSE_TOO_LARGE",
            Self::Interrupted => "CHAT_INTERRUPTED",
            Self::UnverifiedSource => "AI_UNVERIFIED_SOURCE",
            Self::StaleContext => "AI_STALE_CONTEXT",
            Self::ConsentRequired => "VISION_CONSENT_REQUIRED",
            Self::BudgetExhausted => "AI_BUDGET_EXHAUSTED",
            Self::BudgetNotConfigured => "AI_BUDGET_NOT_CONFIGURED",
            Self::BudgetUnavailable => "AI_BUDGET_UNAVAILABLE",
            Self::RateLimited => "AI_RATE_LIMITED",
            Self::ProviderBalance => "AI_PROVIDER_BALANCE",
            Self::Replay => "AI_REPLAY",
        }
    }
}
impl fmt::Display for ProviderError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.code())
    }
}
impl std::error::Error for ProviderError {}

#[derive(Clone)]
pub struct AiProvider {
    client: Client,
    endpoint: Url,
    model: String,
    authorization: Option<HeaderValue>,
    limit: usize,
    bridge: bool,
}

impl AiProvider {
    pub fn new(config: &AiProviderConfig) -> anyhow::Result<Self> {
        config.validate()?;
        let authorization = config
            .key_file
            .as_ref()
            .map(|path| crate::transport::bearer_header(path, "AI key"))
            .transpose()?;
        let _ = rustls::crypto::ring::default_provider().install_default();
        let client = Client::builder()
            .redirect(Policy::none())
            .no_proxy()
            .timeout(Duration::from_millis(config.timeout_ms))
            .connect_timeout(Duration::from_secs(5))
            .build()
            .context("create AI transport")?;
        Ok(Self {
            client,
            endpoint: Url::parse(&config.endpoint)?,
            model: config.model.clone(),
            authorization,
            limit: config.max_response_bytes,
            bridge: false,
        })
    }

    pub fn bridge(config: &AssistantConfig) -> anyhow::Result<Self> {
        let _ = rustls::crypto::ring::default_provider().install_default();
        ensure!(
            config.address.ip().is_loopback(),
            "assistant requires loopback"
        );
        let authorization = crate::transport::bearer_header(&config.token_file, "assistant token")?;
        ensure!(
            authorization.as_bytes().len() >= 50,
            "assistant token is too short"
        );
        Ok(Self {
            client: Client::builder()
                .redirect(Policy::none())
                .no_proxy()
                .connect_timeout(Duration::from_secs(3))
                .timeout(Duration::from_secs(120))
                .build()?,
            endpoint: Url::parse(&format!("http://{}/api/tickdesk/ai/v1/", config.address))?,
            model: "tickdesk-python-v1".to_owned(),
            authorization: Some(authorization),
            limit: 256 * 1024,
            bridge: true,
        })
    }

    pub fn is_bridge(&self) -> bool {
        self.bridge
    }

    pub fn bridge_authorization(&self) -> Option<HeaderValue> {
        self.bridge.then(|| self.authorization.clone()).flatten()
    }

    pub(crate) async fn diagnostic_status(&self) -> Result<Value, ProviderError> {
        let authorization = self
            .bridge_authorization()
            .ok_or(ProviderError::NotConfigured)?;
        let endpoint = self
            .endpoint
            .join("status")
            .map_err(|_| ProviderError::InvalidResponse)?;
        let response = self
            .client
            .get(endpoint)
            .header(reqwest::header::AUTHORIZATION, authorization)
            .timeout(Duration::from_secs(3))
            .send()
            .await
            .map_err(map_transport)?;
        if !response.status().is_success() {
            return Err(ProviderError::Unavailable);
        }
        let bytes = crate::transport::read_limited(response, 16 * 1024)
            .await
            .map_err(body_error)?;
        crate::contract::strict_json(&bytes).map_err(|_| ProviderError::InvalidResponse)
    }

    pub async fn dispatch(
        &self,
        kind: &'static str,
        job_id: &str,
        body: Value,
    ) -> Result<Value, ProviderError> {
        let authorization = self
            .bridge_authorization()
            .ok_or(ProviderError::NotConfigured)?;
        let endpoint = self
            .endpoint
            .join(kind)
            .map_err(|_| ProviderError::InvalidResponse)?;
        let response = self
            .client
            .post(endpoint)
            .header(reqwest::header::AUTHORIZATION, authorization)
            .header("X-TickDesk-Quant-On-Error", "1")
            .json(&body)
            .send()
            .await
            .map_err(map_transport)?;
        let status = response.status().as_u16();
        let limit = if kind == "chat" {
            CHAT_BRIDGE_RESULT_LIMIT
        } else {
            self.limit
        };
        let bytes = crate::transport::read_limited(response, limit)
            .await
            .map_err(body_error)?;
        let response = crate::contract::strict_json_bounded(&bytes, limit)
            .map_err(|_| ProviderError::InvalidResponse)?;
        if status != 200 {
            return Err(match response["detail"].as_str() {
                Some("AI_NOT_CONFIGURED") => ProviderError::NotConfigured,
                Some("AI_TIMEOUT") => ProviderError::Timeout,
                Some("AI_RESPONSE_TOO_LARGE") => ProviderError::TooLarge,
                Some("AI_UNVERIFIED_SOURCE") => ProviderError::UnverifiedSource,
                Some("AI_STALE_CONTEXT") => ProviderError::StaleContext,
                Some("VISION_CONSENT_REQUIRED") => ProviderError::ConsentRequired,
                Some("AI_BUDGET_EXHAUSTED") => ProviderError::BudgetExhausted,
                Some("AI_BUDGET_NOT_CONFIGURED") => ProviderError::BudgetNotConfigured,
                Some("AI_BUDGET_UNAVAILABLE") => ProviderError::BudgetUnavailable,
                Some("AI_RATE_LIMITED") => ProviderError::RateLimited,
                Some("AI_PROVIDER_BALANCE") => ProviderError::ProviderBalance,
                Some("AI_REPLAY") => ProviderError::Replay,
                Some("AI_INVALID_RESPONSE" | "VISION_INVALID_RESPONSE") => {
                    ProviderError::InvalidResponse
                }
                _ => ProviderError::Unavailable,
            });
        }
        if response["version"] != 1
            || response["jobId"] != job_id
            || response["model"]
                .as_str()
                .is_none_or(|model| model.is_empty() || model.len() > 800)
            || response.get("result").is_none()
        {
            return Err(ProviderError::InvalidResponse);
        }
        Ok(response)
    }

    pub fn model(&self) -> &str {
        &self.model
    }

    pub async fn complete(&self, messages: Value) -> Result<String, ProviderError> {
        let body = json!({"model":self.model,"messages":messages,"temperature":0,"max_tokens":4096,"stream":false});
        // A vision request may contain a bounded original encoded as a data URL.
        if serde_json::to_vec(&body)
            .map_err(|_| ProviderError::InvalidResponse)?
            .len()
            > 30 * 1024 * 1024
        {
            return Err(ProviderError::TooLarge);
        }
        let mut request = self.client.post(self.endpoint.clone()).json(&body);
        if let Some(header) = &self.authorization {
            request = request.header(reqwest::header::AUTHORIZATION, header);
        }
        let response = request.send().await.map_err(map_transport)?;
        if !response.status().is_success() {
            return Err(ProviderError::Unavailable);
        }
        let bytes = crate::transport::read_limited(response, self.limit)
            .await
            .map_err(body_error)?;
        let output: Value =
            serde_json::from_slice(&bytes).map_err(|_| ProviderError::InvalidResponse)?;
        let choice = &output["choices"][0];
        if choice["finish_reason"] != "stop"
            || choice["message"]["tool_calls"]
                .as_array()
                .is_some_and(|calls| !calls.is_empty())
        {
            return Err(ProviderError::InvalidResponse);
        }
        choice["message"]["content"]
            .as_str()
            .filter(|s| !s.trim().is_empty())
            .map(str::to_owned)
            .ok_or(ProviderError::InvalidResponse)
    }
}

fn body_error(error: crate::transport::BodyError) -> ProviderError {
    match error {
        crate::transport::BodyError::TooLarge => ProviderError::TooLarge,
        crate::transport::BodyError::Transport(error) => map_transport(error),
    }
}

fn map_transport(error: reqwest::Error) -> ProviderError {
    if error.is_timeout() {
        ProviderError::Timeout
    } else {
        ProviderError::Unavailable
    }
}
