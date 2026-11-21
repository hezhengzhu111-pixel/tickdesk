use anyhow::{ensure, Context};
use rusqlite::{params, Connection};
use std::{
    fs::{File, OpenOptions},
    path::{Path, PathBuf},
};
use uuid::Uuid;
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Identity {
    pub incarnation: String,
    pub server_epoch: String,
}
pub struct Storage {
    pub identity: Identity,
    pub(crate) directory: PathBuf,
    pub(crate) connection: Connection,
    _lock: File,
}
impl Storage {
    pub fn open(directory: &Path) -> anyhow::Result<Self> {
        std::fs::create_dir_all(directory).context("create local data directory")?;
        let lock = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(directory.join("service.lock"))?;
        fs2::FileExt::try_lock_exclusive(&lock).context("data directory already in use")?;
        let mut connection = Connection::open(directory.join("tickdesk.sqlite3"))?;
        let version: i32 = connection.query_row("PRAGMA user_version", [], |r| r.get(0))?;
        ensure!(
            (0..=11).contains(&version),
            "unsupported database schema version {version}"
        );
        if version == 0 {
            let objects: i64 = connection.query_row(
                "SELECT COUNT(*) FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'",
                [],
                |row| row.get(0),
            )?;
            ensure!(
                objects == 0,
                "unrecognized database; explicit migration or recovery is required"
            );
        }
        let existing_identity = if version >= 1 {
            Some(read_identity(&connection)?)
        } else {
            None
        };
        connection.busy_timeout(std::time::Duration::from_secs(5))?;
        let journal: String = connection.query_row("PRAGMA journal_mode=WAL", [], |r| r.get(0))?;
        ensure!(journal == "wal", "SQLite WAL is required");
        connection.pragma_update(None, "synchronous", "FULL")?;
        connection.pragma_update(None, "foreign_keys", "ON")?;
        if version == 0 {
            let transaction = connection.transaction()?;
            transaction.execute_batch(
                "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);",
            )?;
            for key in ["incarnation", "server_epoch"] {
                transaction.execute(
                    "INSERT INTO metadata (key,value) VALUES (?1,?2)",
                    params![key, Uuid::new_v4().to_string()],
                )?;
            }
            transaction.pragma_update(None, "user_version", 1)?;
            transaction.commit()?;
        }
        if version < 2 {
            let transaction = connection.transaction()?;
            transaction.execute_batch(include_str!("storage_v2.sql"))?;
            transaction.pragma_update(None, "user_version", 2)?;
            transaction.commit()?;
        }
        if version < 3 {
            let transaction = connection.transaction()?;
            transaction.execute_batch(include_str!("storage_v3.sql"))?;
            transaction.pragma_update(None, "user_version", 3)?;
            transaction.commit()?;
        }
        if version < 4 {
            let transaction = connection.transaction()?;
            transaction.execute_batch(include_str!("storage_recovery.sql"))?;
            transaction.pragma_update(None, "user_version", 4)?;
            transaction.commit()?;
        }
        if version < 5 {
            let transaction = connection.transaction()?;
            transaction.execute_batch(include_str!("storage_files.sql"))?;
            transaction.pragma_update(None, "user_version", 5)?;
            transaction.commit()?;
        }
        if version < 6 {
            let transaction = connection.transaction()?;
            transaction.execute_batch(include_str!("storage_downloads.sql"))?;
            transaction.pragma_update(None, "user_version", 6)?;
            transaction.commit()?;
        }
        if version < 7 {
            let transaction = connection.transaction()?;
            transaction.execute_batch(include_str!("storage_deletion.sql"))?;
            transaction.pragma_update(None, "user_version", 7)?;
            transaction.commit()?;
        }
        if version < 8 {
            let transaction = connection.transaction()?;
            transaction.execute_batch(include_str!("storage_ops.sql"))?;
            transaction.pragma_update(None, "user_version", 8)?;
            transaction.commit()?;
        }
        if version < 9 {
            let transaction = connection.transaction()?;
            transaction.execute_batch(include_str!("storage_chat.sql"))?;
            transaction.execute_batch(include_str!("storage_vision.sql"))?;
            transaction.pragma_update(None, "user_version", 9)?;
            transaction.commit()?;
        }
        if version < 10 {
            let transaction = connection.transaction()?;
            transaction.execute_batch(include_str!("storage_budget.sql"))?;
            transaction.pragma_update(None, "user_version", 10)?;
            transaction.commit()?;
        }
        if version < 11 {
            let transaction = connection.transaction()?;
            transaction.execute_batch(include_str!("storage_provider_health.sql"))?;
            transaction.pragma_update(None, "user_version", 11)?;
            transaction.commit()?;
        }
        let identity = match existing_identity {
            Some(identity) => identity,
            None => read_identity(&connection)?,
        };
        std::fs::create_dir_all(directory.join("files"))?;
        std::fs::create_dir_all(directory.join("tmp"))?;
        let mut storage = Self {
            identity,
            directory: directory.to_owned(),
            connection,
            _lock: lock,
        };
        storage.inspect_original_files()?;
        Ok(storage)
    }
}

fn read_identity(connection: &Connection) -> anyhow::Result<Identity> {
    let get = |key: &str| -> anyhow::Result<String> {
        let value: String = connection
            .query_row("SELECT value FROM metadata WHERE key=?1", [key], |r| {
                r.get(0)
            })
            .context(
                "identity missing; explicit recovery is required, do not reset automatically",
            )?;
        Uuid::parse_str(&value).context("invalid persisted identity")?;
        Ok(value)
    };
    Ok(Identity {
        incarnation: get("incarnation")?,
        server_epoch: get("server_epoch")?,
    })
}
