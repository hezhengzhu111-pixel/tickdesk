use axum::{
    body::Body,
    http::{Request, StatusCode},
};
use tickdesk_local::{config::Config, http::router, storage::Storage};
use tower::ServiceExt;

#[test]
fn rejected_version_one_database_keeps_its_original_journal_mode() {
    let temp = tempfile::tempdir().unwrap();
    let database = temp.path().join("tickdesk.sqlite3");
    let connection = rusqlite::Connection::open(&database).unwrap();
    connection
        .execute_batch("CREATE TABLE unrelated (value TEXT); PRAGMA user_version=1;")
        .unwrap();
    drop(connection);
    assert!(Storage::open(temp.path()).is_err());
    let connection = rusqlite::Connection::open(database).unwrap();
    let mode: String = connection
        .query_row("PRAGMA journal_mode", [], |r| r.get(0))
        .unwrap();
    assert_eq!(mode, "delete");
}

#[test]
fn foreign_database_is_not_silently_given_a_new_identity() {
    let temp = tempfile::tempdir().unwrap();
    let database = temp.path().join("tickdesk.sqlite3");
    let connection = rusqlite::Connection::open(&database).unwrap();
    connection
        .execute_batch("CREATE TABLE previous_application (value TEXT);")
        .unwrap();
    drop(connection);
    assert!(Storage::open(temp.path()).is_err());
    let connection = rusqlite::Connection::open(database).unwrap();
    let version: i32 = connection
        .query_row("PRAGMA user_version", [], |r| r.get(0))
        .unwrap();
    assert_eq!(version, 0);
}

#[test]
fn identity_survives_restart_and_second_writer_is_rejected() {
    let temp = tempfile::tempdir().unwrap();
    let first = Storage::open(temp.path()).unwrap();
    let identity = first.identity.clone();
    assert!(!identity.incarnation.is_empty());
    assert!(Storage::open(temp.path()).is_err());
    drop(first);
    let second = Storage::open(temp.path()).unwrap();
    assert_eq!(second.identity, identity);
}

#[test]
fn config_is_strict_and_paths_are_relative_to_config() {
    let temp = tempfile::tempdir().unwrap();
    let file = temp.path().join("local.toml");
    std::fs::write(&file, "listen='127.0.0.1:18765'\ndata_dir='state'\n").unwrap();
    assert_eq!(
        Config::load(&file).unwrap().data_dir,
        temp.path().join("state")
    );
    std::fs::write(&file, "listen='0.0.0.0:18765'\ndata_dir='state'\n").unwrap();
    assert!(Config::load(&file).is_err());
    std::fs::write(
        &file,
        "listen='127.0.0.1:18765'\ndata_dir='state'\ntypo=true\n",
    )
    .unwrap();
    assert!(Config::load(&file).is_err());
}

#[tokio::test]
async fn live_does_not_claim_business_readiness() {
    for (path, expected) in [
        ("/health/live", StatusCode::OK),
        ("/health/ready", StatusCode::SERVICE_UNAVAILABLE),
    ] {
        let response = router()
            .oneshot(Request::builder().uri(path).body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(response.status(), expected);
    }
}
