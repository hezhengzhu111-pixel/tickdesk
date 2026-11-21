use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use ring::digest::{digest, SHA256};
use std::time::{SystemTime, UNIX_EPOCH};
use tickdesk_relay::auth::{Role, Roles};
use tokio::time::{Duration, Instant};

fn token(byte: u8) -> String {
    URL_SAFE_NO_PAD.encode([byte; 32])
}

fn hash(token: &str) -> String {
    digest(&SHA256, token.as_bytes())
        .as_ref()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

#[test]
fn digests_are_global_and_rotation_has_a_hard_deadline() {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs();
    let start = Instant::now();
    let app = token(1);
    let local = token(2);
    let previous = token(3);
    let app_hash = hash(&app);
    let local_hash = hash(&local);
    let previous_hash = hash(&previous);
    let config = format!(
        "[pairs.test.app]\nsha256='{app_hash}'\n[pairs.test.app.previous]\nsha256='{previous_hash}'\nexpires_at_unix={}\n[pairs.test.local]\nsha256='{local_hash}'\n",
        now + 30
    );
    let unix_now = Duration::from_secs(now) + Duration::from_millis(250);
    let roles = Roles::parse(&config, unix_now, start).unwrap();
    assert_eq!(roles.authenticate(&app, start).unwrap().role, Role::App);
    assert_eq!(roles.authenticate(&local, start).unwrap().pair_id, "test");
    let old = roles.authenticate(&previous, start).unwrap();
    assert!(roles.permits(&old, start + Duration::from_secs(29)));
    assert!(!roles.permits(&old, start + Duration::from_millis(29_750)));
    assert!(roles.authenticate(&format!("{app}="), start).is_none());
    assert!(roles.authenticate(&app_hash, start).is_none());
    assert!(roles.authenticate(&"A".repeat(42), start).is_none());
    assert!(Roles::parse(&config.replace(&local_hash, &app_hash), unix_now, start).is_err());
    assert!(Roles::parse(
        &config.replace(&(now + 30).to_string(), &(now + 601).to_string()),
        unix_now,
        start
    )
    .is_err());
    let invalid = config.replace(&local_hash, "DO_NOT_ECHO");
    assert!(!Roles::parse(&invalid, unix_now, start)
        .err()
        .unwrap()
        .to_string()
        .contains("DO_NOT_ECHO"));
    assert!(Roles::parse("[pairs]", unix_now, start).is_ok());
}
