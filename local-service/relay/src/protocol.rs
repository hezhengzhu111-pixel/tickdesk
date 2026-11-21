use crate::auth::{valid_id, Role};
use base64::{engine::general_purpose::STANDARD, Engine};
use ring::digest::{digest, SHA256};
use serde::{
    de::{self, MapAccess, SeqAccess, Visitor},
    Deserialize, Deserializer,
};
use serde_json::{json, Map, Number, Value};
use std::fmt;

pub(crate) const MAX_MESSAGE: usize = 128 * 1024;
const MAX_BUSINESS: usize = 64 * 1024;
const MAX_CHUNK: usize = 32 * 1024;
const MAX_INTEGER: u64 = (1 << 53) - 1;

pub(crate) struct Protocol {
    validator: jsonschema::Validator,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Lane {
    Control,
    Business,
    File,
}

#[derive(Clone)]
pub(crate) struct Hello {
    pub incarnation: String,
    pub epoch: Option<String>,
}

#[derive(Debug, PartialEq, Eq)]
pub(crate) struct Failure {
    code: &'static str,
    id: Option<String>,
}

impl Failure {
    pub fn new(code: &'static str, id: Option<&str>) -> Self {
        Self {
            code,
            id: id.filter(|id| valid_id(id)).map(str::to_owned),
        }
    }

    pub fn invalid() -> Self {
        Self::new("INVALID_FRAME", None)
    }

    pub fn code(&self) -> &'static str {
        self.code
    }

    pub fn frame(&self) -> Value {
        let (retryable, after) = match self.code {
            "BUSY" => (true, 1_000),
            "PEER_OFFLINE" => (true, 0),
            _ => (false, 0),
        };
        json!({"v":2,"type":"error","refDeliveryId":self.id,"code":self.code,
            "retryable":retryable,"retryAfterMs":after})
    }
}

pub(crate) struct Incoming {
    pub lane: Lane,
    pub id: Option<String>,
    pub incarnations: Option<(String, String)>,
}

impl Protocol {
    pub fn new() -> anyhow::Result<Self> {
        let relay = serde_json::from_str(include_str!("../contracts/v2/relay.schema.json"))?;
        let business: Value =
            serde_json::from_str(include_str!("../contracts/v2/business.schema.json"))?;
        let id = business["$id"]
            .as_str()
            .expect("embedded business schema id")
            .to_owned();
        let registry = jsonschema::Registry::new().add(id, business)?.prepare()?;
        let validator = jsonschema::options()
            .with_registry(&registry)
            .build(&relay)
            .map_err(|_| anyhow::anyhow!("invalid embedded relay schema"))?;
        Ok(Self { validator })
    }

    pub fn hello(&self, bytes: &[u8], role: Role) -> Result<Hello, Failure> {
        let frame = self.decode(bytes)?;
        let epoch = frame["serverEpoch"].as_str().map(str::to_owned);
        if frame["type"] != "hello" || (role == Role::Local && epoch.is_none()) {
            return Err(Failure::invalid());
        }
        Ok(Hello {
            incarnation: field(&frame, "incarnation"),
            epoch,
        })
    }

    pub fn incoming(&self, bytes: &[u8], role: Role) -> Result<Incoming, Failure> {
        let frame = self.decode(bytes)?;
        let id = frame["deliveryId"].as_str();
        let incarnations = |reverse: bool| {
            let from = field(&frame, "fromIncarnation");
            let to = field(&frame, "toIncarnation");
            Some(if reverse { (to, from) } else { (from, to) })
        };
        let (lane, id, incarnations) = match frame["type"].as_str() {
            Some("deliver") => {
                let payload = &frame["payload"];
                let failure = |code| Failure::new(code, id);
                let lane = if frame["category"] == "business" {
                    #[derive(Deserialize)]
                    struct RawPayload<'a> {
                        #[serde(borrow)]
                        payload: &'a serde_json::value::RawValue,
                    }
                    let raw: RawPayload<'_> =
                        serde_json::from_slice(bytes).map_err(|_| failure("INVALID_FRAME"))?;
                    if raw.payload.get().len() > MAX_BUSINESS
                        || (payload["kind"] == "command") != (role == Role::App)
                    {
                        return Err(failure("INVALID_FRAME"));
                    }
                    Lane::Business
                } else {
                    let raw = payload["bytes"].as_str().expect("validated chunk encoding");
                    let raw = STANDARD.decode(raw).map_err(|_| failure("INVALID_FRAME"))?;
                    let index = payload["index"].as_u64().expect("validated chunk index");
                    let total = payload["totalChunks"]
                        .as_u64()
                        .expect("validated chunk count");
                    if raw.is_empty()
                        || raw.len() > MAX_CHUNK
                        || index >= total
                        || (index + 1 < total && raw.len() != MAX_CHUNK)
                    {
                        return Err(failure("INVALID_FRAME"));
                    }
                    if sha256_hex(&raw) != payload["chunkHash"] {
                        return Err(failure("HASH_MISMATCH"));
                    }
                    Lane::File
                };
                // Strict parsing already preserved every member and enforced TD-C14N-1 numbers.
                if sha256_hex(&canonical(payload.clone())) != frame["payloadHash"] {
                    return Err(failure("HASH_MISMATCH"));
                }
                (lane, id, incarnations(false))
            }
            Some("receipt") => (Lane::Control, id, incarnations(true)),
            Some("error") => (Lane::Control, frame["refDeliveryId"].as_str(), None),
            _ => return Err(Failure::invalid()),
        };
        Ok(Incoming {
            lane,
            id: id.map(str::to_owned),
            incarnations,
        })
    }

    fn decode(&self, bytes: &[u8]) -> Result<Value, Failure> {
        let value = parse(bytes).map_err(|_| Failure::invalid())?;
        if !self.validator.is_valid(&value) {
            return Err(Failure::new("INVALID_FRAME", value["deliveryId"].as_str()));
        }
        Ok(value)
    }
}

fn field(frame: &Value, name: &str) -> String {
    frame[name]
        .as_str()
        .expect("validated relay field")
        .to_owned()
}

fn parse(bytes: &[u8]) -> serde_json::Result<Value> {
    serde_json::from_slice::<Strict>(bytes).map(|value| value.0)
}

fn canonical(mut value: Value) -> Vec<u8> {
    value.sort_all_objects();
    serde_json::to_vec(&value).expect("serialize valid JSON")
}

pub(crate) fn sha256_hex(bytes: &[u8]) -> String {
    let hash = digest(&SHA256, bytes);
    let mut result = String::with_capacity(64);
    const HEX: &[u8; 16] = b"0123456789abcdef";
    for &byte in hash.as_ref() {
        result.push(char::from(HEX[usize::from(byte >> 4)]));
        result.push(char::from(HEX[usize::from(byte & 15)]));
    }
    result
}

struct Strict(Value);

impl<'de> Deserialize<'de> for Strict {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct StrictVisitor;

        impl<'de> Visitor<'de> for StrictVisitor {
            type Value = Strict;

            fn expecting(&self, fmt: &mut fmt::Formatter<'_>) -> fmt::Result {
                fmt.write_str("a TD-C14N-1 JSON value")
            }

            fn visit_bool<E: de::Error>(self, value: bool) -> Result<Strict, E> {
                Ok(Strict(Value::Bool(value)))
            }

            fn visit_unit<E: de::Error>(self) -> Result<Strict, E> {
                Ok(Strict(Value::Null))
            }

            fn visit_str<E: de::Error>(self, value: &str) -> Result<Strict, E> {
                Ok(Strict(Value::String(value.to_owned())))
            }

            fn visit_i64<E: de::Error>(self, value: i64) -> Result<Strict, E> {
                if value.unsigned_abs() > MAX_INTEGER {
                    return Err(E::custom("number outside safe integer range"));
                }
                Ok(Strict(Value::Number(Number::from(value))))
            }

            fn visit_u64<E: de::Error>(self, value: u64) -> Result<Strict, E> {
                if value > MAX_INTEGER {
                    return Err(E::custom("number outside safe integer range"));
                }
                Ok(Strict(Value::Number(Number::from(value))))
            }

            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Strict, A::Error> {
                let mut items = Vec::new();
                while let Some(Strict(item)) = seq.next_element()? {
                    items.push(item);
                }
                Ok(Strict(Value::Array(items)))
            }

            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Strict, A::Error> {
                let mut fields = Map::new();
                while let Some(key) = map.next_key::<String>()? {
                    if !key.is_ascii() || fields.contains_key(&key) {
                        return Err(de::Error::custom("non-ASCII or duplicate object key"));
                    }
                    let Strict(value) = map.next_value()?;
                    fields.insert(key, value);
                }
                Ok(Strict(Value::Object(fields)))
            }
        }

        d.deserialize_any(StrictVisitor)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strict_json_checks_original_tokens_and_matches_canonical_vectors() {
        for input in [
            "-0",
            "0.0",
            "1e0",
            "9007199254740992",
            r#"{"x":0,"\u0078":1}"#,
            r#"{"β":0}"#,
            r#""\ud800""#,
        ] {
            assert!(parse(input.as_bytes()).is_err(), "{input}");
        }
        let vectors: Value =
            serde_json::from_str(include_str!("../contracts/v2/hash-vectors.json")).unwrap();
        let vector = &vectors["business"];
        let bytes = canonical(vector["input"].clone());
        assert_eq!(bytes, vector["canonicalUtf8"].as_str().unwrap().as_bytes());
        assert_eq!(sha256_hex(&bytes), vector["sha256"]);
    }

    #[test]
    fn embedded_relay_schema_and_runtime_rules_accept_valid_fixtures() {
        let proto = Protocol::new().unwrap();
        let fixtures: Value =
            serde_json::from_str(include_str!("../contracts/v2/relay-fixtures.json")).unwrap();
        for case in fixtures["cases"].as_array().unwrap() {
            let frame = &case["payload"];
            let bytes = serde_json::to_vec(frame).unwrap();
            let accepted = match frame["type"].as_str() {
                Some("hello") => proto.hello(&bytes, Role::App).is_ok(),
                Some("welcome" | "presence") => proto.decode(&bytes).is_ok(),
                _ => proto.incoming(&bytes, Role::App).is_ok(),
            };
            assert_eq!(Some(accepted), case["valid"].as_bool(), "{}", case["name"]);
        }
    }

    #[test]
    fn non_tail_chunks_must_be_full_size_even_with_correct_hashes() {
        let proto = Protocol::new().unwrap();
        let fixtures: Value =
            serde_json::from_str(include_str!("../contracts/v2/relay-fixtures.json")).unwrap();
        let mut frame = fixtures["cases"]
            .as_array()
            .unwrap()
            .iter()
            .find(|case| case["name"] == "file_chunk")
            .unwrap()["payload"]
            .clone();
        frame["payload"]["totalChunks"] = json!(2);
        frame["payloadHash"] = json!(sha256_hex(&canonical(frame["payload"].clone())));
        let result = proto.incoming(&serde_json::to_vec(&frame).unwrap(), Role::App);
        assert_eq!(result.err().unwrap().code(), "INVALID_FRAME");
    }
}
