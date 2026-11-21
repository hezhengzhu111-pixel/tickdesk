use anyhow::{ensure, Context};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use ring::digest::{digest, SHA256};
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, HashSet},
    path::Path,
    time::{SystemTime, UNIX_EPOCH},
};
use subtle::{Choice, ConditionallySelectable, ConstantTimeEq};
use tokio::time::{Duration, Instant};

pub(crate) const MAX_PAIRS: usize = 100;
const MAX_ROLE_FILE_BYTES: u64 = 128 * 1024;
const MAX_OVERLAP: Duration = Duration::from_secs(600);
type Digest = [u8; 32];

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    App,
    Local,
}

impl Role {
    pub(crate) fn opposite(self) -> Self {
        match self {
            Self::App => Self::Local,
            Self::Local => Self::App,
        }
    }
}

#[derive(Clone)]
pub struct Identity {
    pub pair_id: String,
    pub role: Role,
    digest: Digest,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RoleFile {
    pairs: BTreeMap<String, Pair>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Pair {
    app: Option<RoleConfig>,
    local: Option<RoleConfig>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RoleConfig {
    sha256: String,
    previous: Option<Previous>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Previous {
    sha256: String,
    expires_at_unix: u64,
}

struct Credential {
    identity: Identity,
    expiry: Option<Instant>,
}

pub struct Roles {
    credentials: Vec<Credential>,
    complete_pairs: usize,
}

impl Roles {
    pub fn load(path: &Path) -> anyhow::Result<Self> {
        Self::from_bytes(&Self::read(path)?)
    }

    pub(crate) fn read(path: &Path) -> anyhow::Result<Vec<u8>> {
        use std::io::Read;
        let mut bytes = Vec::new();
        std::fs::File::open(path)
            .context("open role digest file")?
            .take(MAX_ROLE_FILE_BYTES + 1)
            .read_to_end(&mut bytes)
            .context("read role digest file")?;
        ensure!(
            bytes.len() as u64 <= MAX_ROLE_FILE_BYTES,
            "role digest file too large"
        );
        Ok(bytes)
    }

    pub(crate) fn from_bytes(bytes: &[u8]) -> anyhow::Result<Self> {
        let now = Instant::now();
        let unix_now = SystemTime::now().duration_since(UNIX_EPOCH)?;
        let text = std::str::from_utf8(bytes).context("role digest file must be UTF-8")?;
        Self::parse(text, unix_now, now)
    }

    pub fn parse(text: &str, unix_now: Duration, now: Instant) -> anyhow::Result<Self> {
        // TOML's normal diagnostics include input excerpts, including accidental secrets.
        let file: RoleFile =
            toml::from_str(text).map_err(|_| anyhow::anyhow!("invalid role digest TOML"))?;
        ensure!(file.pairs.len() <= MAX_PAIRS, "too many configured pairs");
        let mut roles = Self {
            credentials: Vec::new(),
            complete_pairs: 0,
        };
        let mut used = HashSet::new();
        for (pair_id, pair) in file.pairs {
            ensure!(valid_id(&pair_id), "invalid configured pair id");
            ensure!(
                pair.app.is_some() || pair.local.is_some(),
                "configured pair has no roles"
            );
            roles.complete_pairs += usize::from(pair.app.is_some() && pair.local.is_some());
            for (role, config) in [(Role::App, pair.app), (Role::Local, pair.local)] {
                let Some(config) = config else { continue };
                let mut add = |text: &str, expiry| -> anyhow::Result<()> {
                    let digest = parse_digest(text)?;
                    ensure!(used.insert(digest), "duplicate role digest");
                    let identity = Identity {
                        pair_id: pair_id.clone(),
                        role,
                        digest,
                    };
                    roles.credentials.push(Credential { identity, expiry });
                    Ok(())
                };
                add(&config.sha256, None)?;
                if let Some(previous) = config.previous {
                    let remaining =
                        Duration::from_secs(previous.expires_at_unix).saturating_sub(unix_now);
                    ensure!(
                        remaining <= MAX_OVERLAP,
                        "rotation overlap exceeds 600 seconds"
                    );
                    add(&previous.sha256, Some(now + remaining))?;
                }
            }
        }
        Ok(roles)
    }

    pub fn authenticate(&self, token: &str, now: Instant) -> Option<Identity> {
        let mut decoded = [0; 33];
        if token.len() != 43 || URL_SAFE_NO_PAD.decode_slice(token, &mut decoded).ok()? != 32 {
            return None;
        }
        let key = digest(&SHA256, token.as_bytes());
        let mut matched = Choice::from(0);
        let mut found = 0u32;
        // Select across every configured digest before branching on the result.
        for (index, entry) in self.credentials.iter().enumerate() {
            let same = entry.identity.digest.as_slice().ct_eq(key.as_ref());
            found.conditional_assign(&(index as u32), same);
            matched |= same;
        }
        if !bool::from(matched) {
            return None;
        }
        let entry = &self.credentials[found as usize];
        entry.active(now).then(|| entry.identity.clone())
    }

    pub fn permits(&self, identity: &Identity, now: Instant) -> bool {
        self.credentials.iter().any(|entry| {
            let current = &entry.identity;
            current.role == identity.role
                && current.pair_id == identity.pair_id
                && bool::from(current.digest.ct_eq(&identity.digest))
                && entry.active(now)
        })
    }

    pub(crate) fn has_complete_pair(&self) -> bool {
        self.complete_pairs != 0
    }

    pub(crate) fn preserve_rotation_deadlines(&mut self, current: &Self) {
        for entry in &mut self.credentials {
            if let Some(expiry) = &mut entry.expiry {
                if let Some(previous) = current
                    .credentials
                    .iter()
                    .find(|previous| previous.identity.digest == entry.identity.digest)
                    .and_then(|previous| previous.expiry)
                {
                    *expiry = (*expiry).min(previous);
                }
            }
        }
    }
}

impl Credential {
    fn active(&self, now: Instant) -> bool {
        self.expiry.is_none_or(|expiry| now < expiry)
    }
}

fn parse_digest(text: &str) -> anyhow::Result<Digest> {
    ensure!(
        text.len() == 64
            && text
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
        "role digest must be 64 lowercase hex characters"
    );
    let mut result = [0; 32];
    for (byte, hex) in result.iter_mut().zip(text.as_bytes().chunks_exact(2)) {
        let nibble = |b| if b <= b'9' { b - b'0' } else { b - b'a' + 10 };
        *byte = (nibble(hex[0]) << 4) | nibble(hex[1]);
    }
    Ok(result)
}

pub(crate) fn valid_id(text: &str) -> bool {
    (1..=64).contains(&text.len())
        && text
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}
