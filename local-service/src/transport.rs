//! Shared bounded I/O. Callers retain their own status, timeout and protocol policy.
use anyhow::ensure;
use reqwest::{header::HeaderValue, Response};
use std::{fmt, io::Read, path::Path};

const CREDENTIAL_LIMIT: u64 = 8192;

pub(crate) fn bearer_header(path: &Path, label: &str) -> anyhow::Result<HeaderValue> {
    let file =
        std::fs::File::open(path).map_err(|_| anyhow::anyhow!("cannot read {label} file"))?;
    let mut bytes = Vec::new();
    file.take(CREDENTIAL_LIMIT + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| anyhow::anyhow!("cannot read {label} file"))?;
    ensure!(
        bytes.len() as u64 <= CREDENTIAL_LIMIT,
        "{label} file too large"
    );
    let token = std::str::from_utf8(&bytes)
        .map_err(|_| anyhow::anyhow!("invalid {label} encoding"))?
        .trim();
    ensure!(
        !token.is_empty() && token.bytes().all(|b| b.is_ascii_graphic()),
        "invalid {label}"
    );
    let mut header = HeaderValue::from_str(&format!("Bearer {token}"))
        .map_err(|_| anyhow::anyhow!("invalid {label}"))?;
    header.set_sensitive(true);
    Ok(header)
}

#[derive(Debug)]
pub(crate) enum BodyError {
    TooLarge,
    Transport(reqwest::Error),
}

impl fmt::Display for BodyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::TooLarge => "response exceeds body limit",
            Self::Transport(_) => "response body unavailable",
        })
    }
}

impl std::error::Error for BodyError {}

pub(crate) async fn read_limited(
    mut response: Response,
    limit: usize,
) -> Result<Vec<u8>, BodyError> {
    if response
        .content_length()
        .is_some_and(|size| size > limit as u64)
    {
        return Err(BodyError::TooLarge);
    }
    let mut bytes = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(BodyError::Transport)? {
        if bytes.len().saturating_add(chunk.len()) > limit {
            return Err(BodyError::TooLarge);
        }
        bytes.extend_from_slice(&chunk);
    }
    Ok(bytes)
}
