use serde_json::{json, Value};
use tickdesk_local::contract::{
    canonical, digest, parse_business, parse_frame, strict_json, validate_definition,
};

#[test]
fn strict_wire_json_and_canonical_vector() {
    let vector: Value =
        serde_json::from_str(include_str!("../contracts/v2/hash-vectors.json")).unwrap();
    let business = &vector["business"];
    assert_eq!(
        canonical(&business["input"]).unwrap(),
        business["canonicalUtf8"].as_str().unwrap().as_bytes()
    );
    assert_eq!(digest(&business["input"]).unwrap(), business["sha256"]);
    for raw in [
        r#"{"a":1,"\u0061":2}"#,
        r#"{"a":[{"x":1,"x":2}]}"#,
        r#"{"ä":0}"#,
        r#"{"a":-0}"#,
        r#"{"a":1.0}"#,
        r#"{"a":1e0}"#,
        r#"{"a":9007199254740992}"#,
        r#"{"a":-9007199254740992}"#,
        r#"{"a":"\ud800"}"#,
    ] {
        assert!(strict_json(raw.as_bytes()).is_err(), "{raw}");
    }
    assert!(strict_json(b"{\"a\":\"\xff\"}").is_err());
    assert_eq!(
        strict_json(br#"[-9007199254740991,9007199254740991,0,"-0"]"#).unwrap(),
        json!([-9007199254740991_i64, 9007199254740991_i64, 0, "-0"])
    );
}

#[test]
fn frozen_fixtures_and_request_hashes() {
    let business: Value =
        serde_json::from_str(include_str!("../contracts/v2/fixtures.json")).unwrap();
    let relay: Value =
        serde_json::from_str(include_str!("../contracts/v2/relay-fixtures.json")).unwrap();
    for case in business["cases"].as_array().unwrap() {
        let result = parse_business(&serde_json::to_vec(&case["payload"]).unwrap());
        assert_eq!(
            result.is_ok(),
            case["valid"],
            "{}: {:?}",
            case["name"],
            result.err()
        );
    }
    for case in relay["cases"].as_array().unwrap() {
        let result = parse_frame(&serde_json::to_vec(&case["payload"]).unwrap());
        assert_eq!(
            result.is_ok(),
            case["valid"],
            "{}: {:?}",
            case["name"],
            result.err()
        );
    }
    let catalog: Value = serde_json::from_str(tickdesk_local::contract::CATALOG).unwrap();
    let cases = business["cases"].as_array().unwrap();
    for entry in catalog["commands"].as_array().unwrap() {
        let name = entry["name"].as_str().unwrap();
        let find = |prefix: &str| {
            &cases
                .iter()
                .find(|case| case["name"] == format!("{prefix}_{name}"))
                .unwrap()["payload"]
        };
        let request = find("request");
        let ack = &find("ack")["data"];
        assert_eq!(
            ack["requestHash"],
            digest(&request["data"]).unwrap(),
            "{name}"
        );
        assert_eq!(ack["refId"], request["id"]);
    }
    let push = serde_json::from_str(include_str!("../contracts/v2/push.example.json")).unwrap();
    validate_definition("Push", &push).unwrap();
}

#[test]
fn frozen_recovery_evidence_bytes_and_digests() {
    use base64::{engine::general_purpose::STANDARD, Engine};
    let vectors: Value =
        serde_json::from_str(include_str!("../contracts/v2/evidence-vectors.json")).unwrap();
    for record in vectors["records"].as_array().unwrap() {
        let bytes: Vec<u8> = record["parts"]
            .as_array()
            .unwrap()
            .iter()
            .flat_map(|part| STANDARD.decode(part.as_str().unwrap()).unwrap())
            .collect();
        let value = strict_json(&bytes).unwrap();
        assert_eq!(value, record["payload"]);
        assert_eq!(canonical(&value).unwrap(), bytes);
        assert_eq!(digest(&value).unwrap(), record["payloadHash"]);
        validate_definition(
            &format!("Evidence_{}", record["sourceType"].as_str().unwrap()),
            &value,
        )
        .unwrap();
    }
}

#[test]
fn bundled_manifest_matches_each_shared_contract_file() {
    let manifest: Value =
        serde_json::from_str(include_str!("../contracts/v2/manifest.json")).unwrap();
    assert_eq!(manifest["protocolMajor"], 2);
    let directory = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("contracts/v2");
    for (name, expected) in manifest["files"].as_object().unwrap() {
        let bytes = std::fs::read(directory.join(name)).unwrap();
        assert_eq!(
            tickdesk_local::contract::sha256(&bytes),
            *expected,
            "{name}"
        );
    }
}
