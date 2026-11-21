use anyhow::{anyhow, ensure, Context};
use base64::{engine::general_purpose::STANDARD, Engine};
use serde::{
    de::{self, MapAccess, SeqAccess, Visitor},
    Deserialize, Deserializer,
};
use serde_json::{json, value::RawValue, Map, Value};
use std::{collections::BTreeMap, fmt, sync::OnceLock};

pub const PROTOCOL_MAJOR: u32 = 2;
pub const CONTRACT_VERSION: &str = "2.0.0";
pub const CATALOG: &str = include_str!("../contracts/v2/commands.json");
const BUSINESS: &str = include_str!("../contracts/v2/business.schema.json");
const RELAY: &str = include_str!("../contracts/v2/relay.schema.json");
pub const BUSINESS_LIMIT: usize = 64 * 1024;
pub const FRAME_LIMIT: usize = 128 * 1024;
const SAFE_INTEGER: i64 = 9_007_199_254_740_991;

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CommandPolicy {
    pub name: String,
    pub mode: String,
    pub allowed_during_recovery: bool,
    pub only_during_recovery: bool,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Catalog {
    contract_version: String,
    commands: Vec<CommandPolicy>,
}

struct Validators {
    catalog: Catalog,
    business: jsonschema::Validator,
    relay: jsonschema::Validator,
}

fn validators() -> anyhow::Result<&'static Validators> {
    static VALIDATORS: OnceLock<Result<Validators, String>> = OnceLock::new();
    VALIDATORS
        .get_or_init(|| {
            fn compile() -> anyhow::Result<Validators> {
                let catalog: Catalog = serde_json::from_str(CATALOG)?;
                let business: Value = serde_json::from_str(BUSINESS)?;
                let relay: Value = serde_json::from_str(RELAY)?;
                let business_uri = business["$id"].as_str().context("business schema ID")?;
                let relay = jsonschema::draft202012::options()
                    .should_validate_formats(true)
                    .with_resource(
                        business_uri,
                        jsonschema::Resource::from_contents(business.clone())?,
                    )
                    .build(&relay)
                    .map_err(|err| anyhow!("invalid relay schema: {err}"))?;
                Ok(Validators {
                    catalog,
                    business: jsonschema::draft202012::options()
                        .should_validate_formats(true)
                        .build(&business)
                        .map_err(|err| anyhow!("invalid business schema: {err}"))?,
                    relay,
                })
            }
            compile().map_err(|error| error.to_string())
        })
        .as_ref()
        .map_err(|err| anyhow!("{err}"))
}

pub fn verify_catalog() -> anyhow::Result<()> {
    let catalog = &validators()?.catalog;
    ensure!(
        catalog.contract_version == CONTRACT_VERSION,
        "unsupported contract snapshot"
    );
    let supported = crate::commands::SUPPORTED_COMMANDS;
    ensure!(
        catalog.commands.len() == supported.len()
            && supported.iter().all(|name| {
                catalog
                    .commands
                    .iter()
                    .filter(|entry| entry.name == *name)
                    .count()
                    == 1
            }),
        "incomplete command catalog"
    );
    Ok(())
}

pub fn policy(command: &str) -> anyhow::Result<&'static CommandPolicy> {
    validators()?
        .catalog
        .commands
        .iter()
        .find(|entry| entry.name == command)
        .context("unknown business command")
}

struct Strict(Value);

impl<'de> Deserialize<'de> for Strict {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct StrictVisitor;
        impl<'de> Visitor<'de> for StrictVisitor {
            type Value = Strict;

            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("TD-C14N-1 JSON")
            }

            fn visit_map<M: MapAccess<'de>>(self, mut entries: M) -> Result<Strict, M::Error> {
                let mut values = Map::new();
                while let Some(key) = entries.next_key::<String>()? {
                    if !key.is_ascii() || values.contains_key(&key) {
                        return Err(de::Error::custom("duplicate or non-ASCII object key"));
                    }
                    values.insert(key, entries.next_value::<Strict>()?.0);
                }
                Ok(Strict(Value::Object(values)))
            }

            fn visit_seq<S: SeqAccess<'de>>(self, mut entries: S) -> Result<Strict, S::Error> {
                let mut values = Vec::new();
                while let Some(item) = entries.next_element::<Strict>()? {
                    values.push(item.0);
                }
                Ok(Strict(Value::Array(values)))
            }

            fn visit_i64<E: de::Error>(self, n: i64) -> Result<Strict, E> {
                if !(-SAFE_INTEGER..=SAFE_INTEGER).contains(&n) {
                    return Err(E::custom("integer outside safe range"));
                }
                Ok(Strict(Value::from(n)))
            }

            fn visit_u64<E: de::Error>(self, n: u64) -> Result<Strict, E> {
                if n > SAFE_INTEGER as u64 {
                    return Err(E::custom("integer outside safe range"));
                }
                Ok(Strict(Value::from(n)))
            }

            fn visit_str<E: de::Error>(self, s: &str) -> Result<Strict, E> {
                Ok(Strict(Value::from(s)))
            }

            fn visit_bool<E: de::Error>(self, b: bool) -> Result<Strict, E> {
                Ok(Strict(Value::from(b)))
            }

            fn visit_unit<E: de::Error>(self) -> Result<Strict, E> {
                Ok(Strict(Value::Null))
            }
        }
        // serde_json presents decimal/exponent tokens and negative zero to visit_f64,
        // which the visitor rejects. The deserializer also checks Unicode and depth.
        d.deserialize_any(StrictVisitor)
    }
}

pub fn strict_json(bytes: &[u8]) -> anyhow::Result<Value> {
    strict_json_bounded(bytes, FRAME_LIMIT)
}

pub(crate) fn strict_json_bounded(bytes: &[u8], max_bytes: usize) -> anyhow::Result<Value> {
    ensure!(bytes.len() <= max_bytes, "message too large");
    Ok(serde_json::from_slice::<Strict>(bytes)?.0)
}

pub fn canonical(value: &Value) -> anyhow::Result<Vec<u8>> {
    fn encode(value: &Value, out: &mut Vec<u8>) -> anyhow::Result<()> {
        match value {
            Value::Object(entries) => {
                out.push(b'{');
                for (i, (key, item)) in entries
                    .iter()
                    .collect::<BTreeMap<_, _>>()
                    .into_iter()
                    .enumerate()
                {
                    ensure!(key.is_ascii(), "non-ASCII object key");
                    if i != 0 {
                        out.push(b',');
                    }
                    serde_json::to_writer(&mut *out, key)?;
                    out.push(b':');
                    encode(item, out)?;
                }
                out.push(b'}');
            }
            Value::Array(entries) => {
                out.push(b'[');
                for (i, item) in entries.iter().enumerate() {
                    if i != 0 {
                        out.push(b',');
                    }
                    encode(item, out)?;
                }
                out.push(b']');
            }
            Value::Number(n) => {
                let i = n.as_i64().context("only safe integers are allowed")?;
                ensure!(
                    (-SAFE_INTEGER..=SAFE_INTEGER).contains(&i),
                    "integer outside safe range"
                );
                serde_json::to_writer(out, &i)?;
            }
            other => serde_json::to_writer(out, other)?,
        }
        Ok(())
    }

    let mut out = Vec::new();
    encode(value, &mut out)?;
    Ok(out)
}

pub fn sha256(bytes: &[u8]) -> String {
    use std::fmt::Write;
    let mut hex = String::with_capacity(64);
    for byte in ring::digest::digest(&ring::digest::SHA256, bytes).as_ref() {
        write!(hex, "{byte:02x}").expect("writing to String cannot fail");
    }
    hex
}

pub fn digest(value: &Value) -> anyhow::Result<String> {
    Ok(sha256(&canonical(value)?))
}

pub fn parse_business(bytes: &[u8]) -> anyhow::Result<Value> {
    ensure!(bytes.len() <= BUSINESS_LIMIT, "business payload too large");
    let value = strict_json(bytes)?;
    validate_business(&value)?;
    Ok(value)
}

pub fn validate_business(value: &Value) -> anyhow::Result<()> {
    ensure!(
        validators()?.business.is_valid(value),
        "invalid v2 business envelope"
    );
    if value["kind"] == "command" {
        ensure!(
            value["serverEpoch"] == value["data"]["serverEpoch"],
            "epoch fields differ"
        );
    }
    Ok(())
}

struct DefinitionValidators {
    definitions: Map<String, Value>,
    compiled: BTreeMap<String, OnceLock<Result<jsonschema::Validator, String>>>,
}

fn definition_validator(name: &str) -> anyhow::Result<&'static jsonschema::Validator> {
    static DEFINITIONS: OnceLock<Result<DefinitionValidators, String>> = OnceLock::new();
    let definitions = DEFINITIONS
        .get_or_init(|| {
            fn parse() -> anyhow::Result<DefinitionValidators> {
                let mut business: Value = serde_json::from_str(BUSINESS)?;
                let Value::Object(definitions) = business["$defs"].take() else {
                    anyhow::bail!("invalid schema definitions");
                };
                let compiled = definitions
                    .keys()
                    .map(|name| (name.clone(), OnceLock::new()))
                    .collect();
                Ok(DefinitionValidators {
                    definitions,
                    compiled,
                })
            }
            parse().map_err(|error| error.to_string())
        })
        .as_ref()
        .map_err(|error| anyhow!("{error}"))?;
    // Only bundled names occupy cache entries; arbitrary input cannot grow it.
    let compiled = definitions
        .compiled
        .get(name)
        .context("unknown schema definition")?;
    compiled
        .get_or_init(|| {
            let schema = json!({
                "$defs": definitions.definitions,
                "$ref": format!("#/$defs/{name}"),
            });
            jsonschema::draft202012::options()
                .should_validate_formats(true)
                .build(&schema)
                .map_err(|_| format!("invalid schema definition {name}"))
        })
        .as_ref()
        .map_err(|error| anyhow!("{error}"))
}

pub fn validate_definition(name: &str, value: &Value) -> anyhow::Result<()> {
    let validator = definition_validator(name)?;
    ensure!(validator.is_valid(value), "invalid v2 {name}");
    Ok(())
}

pub fn parse_frame(bytes: &[u8]) -> anyhow::Result<Value> {
    let value = strict_json(bytes)?;
    ensure!(
        validators()?.relay.is_valid(&value),
        "invalid v2 relay frame"
    );
    match value["type"].as_str() {
        Some("presence") => {
            let peer = &value["peer"];
            ensure!(
                peer["online"] == peer["incarnation"].is_string(),
                "presence identity mismatch"
            );
            ensure!(
                peer["online"] == true || peer["serverEpoch"].is_null(),
                "offline peer has epoch"
            );
        }
        Some("deliver") => {
            let payload = &value["payload"];
            if value["category"] == "business" {
                #[derive(Deserialize)]
                struct Delivery<'a> {
                    #[serde(borrow)]
                    payload: &'a RawValue,
                }
                let raw: Delivery<'_> = serde_json::from_slice(bytes)?;
                ensure!(
                    raw.payload.get().len() <= BUSINESS_LIMIT,
                    "business payload too large"
                );
                validate_business(payload)?;
            } else {
                let bytes = STANDARD.decode(payload["bytes"].as_str().context("chunk bytes")?)?;
                let index = payload["index"].as_u64().context("chunk index")?;
                let total = payload["totalChunks"].as_u64().context("chunk count")?;
                ensure!(
                    (1..=32768).contains(&bytes.len()) && index < total,
                    "invalid chunk dimensions"
                );
                ensure!(
                    index + 1 == total || bytes.len() == 32768,
                    "invalid non-final chunk length"
                );
                ensure!(
                    payload["chunkHash"] == sha256(&bytes),
                    "chunk hash mismatch"
                );
            }
            ensure!(
                value["payloadHash"] == digest(payload)?,
                "payload hash mismatch"
            );
        }
        _ => {}
    }
    Ok(value)
}
