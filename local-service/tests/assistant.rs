use tickdesk_local::config::Config;

#[test]
fn assistant_configuration_rejects_remote_addresses_and_double_execution() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("local.toml");
    let base = "listen='127.0.0.1:18765'\ndata_dir='test-data'\n";
    let assistant = "[assistant]\naddress='127.0.0.1:3018'\ntoken_file='bridge.token'\n";
    std::fs::write(&path, format!("{base}{assistant}")).unwrap();
    assert!(Config::load(&path).is_ok());
    std::fs::write(
        &path,
        format!("{base}{}", assistant.replace("127.0.0.1", "192.0.2.1")),
    )
    .unwrap();
    assert!(Config::load(&path).is_err());
    std::fs::write(
        &path,
        format!("{base}{assistant}[chat]\nendpoint='https://example.com/chat'\nmodel='fixture'\n"),
    )
    .unwrap();
    assert!(Config::load(&path).is_err());
}

#[test]
fn schema_ten_provider_health_is_preserved_when_mcp_is_added() {
    use tickdesk_local::storage::Storage;
    let dir = tempfile::tempdir().unwrap();
    let mut store = Storage::open(dir.path()).unwrap();
    let identity = store.identity.clone();
    store
        .record_ai_health("deepseek", "online", None, 123)
        .unwrap();
    store
        .record_ai_health("vision", "offline", Some("AI_TIMEOUT"), 124)
        .unwrap();
    drop(store);
    let sql = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    sql.execute_batch(
        r#"ALTER TABLE provider_health RENAME TO saved_health;
        CREATE TABLE provider_health (name TEXT PRIMARY KEY CHECK(name IN ('deepseek','vision')),
          status TEXT NOT NULL CHECK(status IN ('unknown','online','degraded','offline')),
          checked_at INTEGER NOT NULL,error_code TEXT);
        INSERT INTO provider_health SELECT * FROM saved_health; DROP TABLE saved_health;
        INSERT INTO objects VALUES ('migration_test','preserved','{"value":17}');
        PRAGMA user_version=10;"#,
    )
    .unwrap();
    drop(sql);
    let mut store = Storage::open(dir.path()).unwrap();
    assert_eq!(store.identity, identity);
    store.record_ai_health("mcp", "online", None, 456).unwrap();
    let sql = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    assert_eq!(
        sql.query_row(
            "SELECT checked_at FROM provider_health WHERE name='deepseek'",
            [],
            |r| r.get::<_, u64>(0)
        )
        .unwrap(),
        123
    );
    assert_eq!(
        sql.query_row("PRAGMA user_version", [], |r| r.get::<_, u64>(0))
            .unwrap(),
        11
    );
    assert_eq!(
        sql.query_row(
            "SELECT error_code FROM provider_health WHERE name='vision'",
            [],
            |r| r.get::<_, String>(0)
        )
        .unwrap(),
        "AI_TIMEOUT"
    );
    assert_eq!(
        sql.query_row(
            "SELECT body FROM objects WHERE collection='migration_test'",
            [],
            |r| r.get::<_, String>(0)
        )
        .unwrap(),
        r#"{"value":17}"#
    );
}
