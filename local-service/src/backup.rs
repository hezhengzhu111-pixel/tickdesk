use crate::{
    commands::{now_ms, snapshot_seq},
    file_service::{original_directory, original_path, regular_file, sync_directory},
    storage::Storage,
};
use anyhow::{ensure, Context};
use ring::digest::{Context as Digest, SHA256};
use rusqlite::{backup::StepResult, Connection, OpenFlags};
use serde::{Deserialize, Serialize};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    time::Duration,
};

const DB: &str = "tickdesk.sqlite3";
const MANIFEST: &str = "manifest.json";
const FORMAT: u32 = 1;

#[derive(Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct Original {
    file_id: String,
    size_bytes: u64,
    sha256: String,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct Manifest {
    format: u32,
    completed_at: u64,
    snapshot_at: u64,
    schema_version: u32,
    server_epoch: String,
    snapshot_seq: u64,
    database_sha256: String,
    originals: Vec<Original>,
}

impl Storage {
    pub fn backup_to(&self, destination: &Path) -> anyhow::Result<()> {
        ensure!(
            !self.recovery_active()?,
            "finish recovery before creating a normal backup"
        );
        let snapshot_at = now_ms();
        let staged = stage(destination)?;
        let source = original_directory(&self.directory)?;
        let images = staged.path().join("files");
        fs::create_dir(&images)?;
        let database = staged.path().join(DB);
        {
            let mut target = Connection::open(&database)?;
            let backup = rusqlite::backup::Backup::new(&self.connection, &mut target)?;
            loop {
                match backup.step(128)? {
                    StepResult::Done => break,
                    StepResult::More => {}
                    _ => anyhow::bail!("could not take a consistent SQLite backup"),
                }
            }
            drop(backup);
            // A published archive must stay a single database even when opened for audit.
            let journal: String =
                target.query_row("PRAGMA journal_mode=DELETE", [], |row| row.get(0))?;
            ensure!(journal == "delete", "backup database could not be sealed");
        }
        OpenOptions::new().write(true).open(&database)?.sync_all()?;
        let list = originals(&self.connection)?;
        for entry in &list {
            let file = original_path(&source, &entry.file_id)?;
            let target = original_path(&images, &entry.file_id)?;
            let hash = copy_verified(&file, Some(&target), Some(entry.size_bytes))?;
            ensure!(
                hash == entry.sha256,
                "original hash does not match its manifest"
            );
        }
        sync_directory(&images)?;
        let manifest = Manifest {
            format: FORMAT,
            completed_at: now_ms(),
            snapshot_at,
            schema_version: self
                .connection
                .query_row("PRAGMA user_version", [], |r| r.get(0))?,
            server_epoch: self.identity.server_epoch.clone(),
            snapshot_seq: snapshot_seq(&self.connection)?,
            database_sha256: copy_verified(&database, None, None)?,
            originals: list,
        };
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(staged.path().join(MANIFEST))?;
        file.write_all(&serde_json::to_vec_pretty(&manifest)?)?;
        file.sync_all()?;
        drop(file);
        publish(staged, destination)
    }

    pub fn restore_from(source: &Path, destination: &Path) -> anyhow::Result<()> {
        let staged = stage(destination)?;
        let manifest_path = source.join(MANIFEST);
        ensure!(
            regular_file(&fs::symlink_metadata(&manifest_path)?),
            "invalid backup manifest path"
        );
        let manifest: Manifest =
            serde_json::from_reader(File::open(&manifest_path)?.take(16 * 1024 * 1024))?;
        ensure!(manifest.format == FORMAT, "unsupported backup format");
        let database = staged.path().join(DB);
        ensure!(
            copy_verified(&source.join(DB), Some(&database), None)? == manifest.database_sha256,
            "backup database hash mismatch"
        );
        {
            let db = Connection::open_with_flags(
                &database,
                OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
            )?;
            let check: String = db.query_row("PRAGMA integrity_check", [], |r| r.get(0))?;
            ensure!(check == "ok", "backup database integrity check failed");
            let epoch: String = db.query_row(
                "SELECT value FROM metadata WHERE key='server_epoch'",
                [],
                |r| r.get(0),
            )?;
            let schema: u32 = db.query_row("PRAGMA user_version", [], |r| r.get(0))?;
            ensure!(
                schema == manifest.schema_version
                    && epoch == manifest.server_epoch
                    && snapshot_seq(&db)? == manifest.snapshot_seq
                    && originals(&db)? == manifest.originals,
                "backup manifest metadata mismatch"
            );
        }
        if !manifest.originals.is_empty() {
            let src = original_directory(source)?;
            let quarantine = staged.path().join("restore-quarantine");
            fs::create_dir(&quarantine)?;
            for entry in &manifest.originals {
                let hash = copy_verified(
                    &original_path(&src, &entry.file_id)?,
                    Some(&original_path(&quarantine, &entry.file_id)?),
                    Some(entry.size_bytes),
                )?;
                ensure!(hash == entry.sha256, "backup original hash mismatch");
            }
            sync_directory(&quarantine)?;
        }
        {
            // Normal open notices that archived originals are intentionally outside
            // the serving directory. Restoration never makes them available.
            let mut db = Storage::open(staged.path())?;
            db.enter_recovery()?;
            db.connection.execute(
                "INSERT INTO metadata(key,value) VALUES ('restored_backup_cutoff',?1)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [manifest.snapshot_at.to_string()],
            )?;
            let (busy, _, _): (i64, i64, i64) =
                db.connection
                    .query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |r| {
                        Ok((r.get(0)?, r.get(1)?, r.get(2)?))
                    })?;
            ensure!(busy == 0, "could not checkpoint prepared restore");
        }
        publish(staged, destination)
    }
}

fn originals(db: &Connection) -> anyhow::Result<Vec<Original>> {
    let busy: bool = db.query_row(
        "SELECT EXISTS(SELECT 1 FROM files WHERE state='deleting')",
        [],
        |r| r.get(0),
    )?;
    ensure!(!busy, "an original deletion is pending");
    let mut stmt = db.prepare(
        "SELECT file_id,size_bytes,sha256 FROM files WHERE state='available' ORDER BY file_id",
    )?;
    let files = stmt.query_map([], |r| {
        Ok(Original {
            file_id: r.get(0)?,
            size_bytes: r.get(1)?,
            sha256: r.get(2)?,
        })
    })?;
    Ok(files.collect::<rusqlite::Result<_>>()?)
}

fn parent(destination: &Path) -> anyhow::Result<PathBuf> {
    let parent = destination.parent().context("destination needs a parent")?;
    let parent = if parent.as_os_str().is_empty() {
        Path::new(".")
    } else {
        parent
    };
    parent
        .canonicalize()
        .context("destination parent must already exist")
}

fn stage(destination: &Path) -> anyhow::Result<tempfile::TempDir> {
    ensure!(
        destination.file_name().is_some(),
        "destination must name a new directory"
    );
    ensure!(
        matches!(fs::symlink_metadata(destination),Err(e) if e.kind()==std::io::ErrorKind::NotFound),
        "destination already exists or is inaccessible"
    );
    Ok(tempfile::Builder::new()
        .prefix(".tickdesk-stage-")
        .tempdir_in(parent(destination)?)?)
}

fn publish(staged: tempfile::TempDir, destination: &Path) -> anyhow::Result<()> {
    ensure!(
        matches!(fs::symlink_metadata(destination),Err(e) if e.kind()==std::io::ErrorKind::NotFound),
        "destination already exists or is inaccessible"
    );
    sync_directory(staged.path())?;
    fs::rename(staged.path(), destination)?;
    sync_directory(&parent(destination)?)?;
    Ok(())
}

fn copy_verified(
    source: &Path,
    destination: Option<&Path>,
    expected: Option<u64>,
) -> anyhow::Result<String> {
    let metadata = fs::symlink_metadata(source)?;
    ensure!(
        regular_file(&metadata),
        "backup source is not a regular file"
    );
    if let Some(bytes) = expected {
        ensure!(metadata.len() == bytes, "backup source length mismatch");
    }
    let mut from = File::open(source)?;
    let mut to = destination
        .map(|path| OpenOptions::new().write(true).create_new(true).open(path))
        .transpose()?;
    let mut hash = Digest::new(&SHA256);
    let mut total = 0u64;
    let mut buffer = vec![0u8; 64 * 1024];
    loop {
        let count = from.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        total += count as u64;
        ensure!(total <= metadata.len(), "backup source changed during copy");
        hash.update(&buffer[..count]);
        if let Some(target) = &mut to {
            target.write_all(&buffer[..count])?;
        }
    }
    ensure!(total == metadata.len(), "backup source changed during copy");
    if let Some(out) = to {
        out.sync_all()?;
    }
    Ok(hash
        .finish()
        .as_ref()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect())
}

pub(crate) fn run_daily_backup(db: &Storage, destination: &Path) -> anyhow::Result<()> {
    if db.recovery_active()? {
        return Ok(());
    }
    let now = chrono::DateTime::from_timestamp_millis(now_ms() as i64)
        .context("invalid system clock")?
        .with_timezone(&chrono_tz::Asia::Shanghai);
    use chrono::Timelike;
    if now.hour() < 3 {
        return Ok(());
    }
    // Daily names are deterministic. An incomplete staging directory never counts as a backup.
    fs::create_dir_all(destination)?;
    let metadata = fs::symlink_metadata(destination)?;
    ensure!(
        metadata.is_dir() && !crate::file_service::is_link(&metadata),
        "unsafe daily backup directory"
    );
    let today = destination.join(now.date_naive().format("%Y-%m-%d").to_string());
    if !today.join(MANIFEST).is_file() {
        db.backup_to(&today)?;
        tracing::info!("daily backup complete");
    }
    prune_completed(destination, now.date_naive() - chrono::Days::new(13))
}

pub async fn daily_worker(
    db: std::sync::Arc<std::sync::Mutex<Storage>>,
    destination: PathBuf,
    mut shutdown: tokio::sync::watch::Receiver<bool>,
) {
    let mut tick = tokio::time::interval(Duration::from_secs(300));
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    loop {
        tokio::select! {
            _=shutdown.changed()=>return,
            _=tick.tick()=>{
                let db=db.clone();
                let destination=destination.clone();
                let result=tokio::task::spawn_blocking(move || {
                    let db=db.lock().map_err(|_|anyhow::anyhow!("storage lock failed"))?;
                    run_daily_backup(&db,&destination)
                }).await;
                if !matches!(result,Ok(Ok(()))) {
                    tracing::warn!("scheduled backup did not finish");
                }
            }
        }
    }
}

impl Storage {
    pub fn resolve_restored_original(&mut self, id: &str, decision: &str) -> anyhow::Result<()> {
        use rusqlite::{params, OptionalExtension, TransactionBehavior};
        let row: Option<(String, String, u64, String)> = self
            .connection
            .query_row(
                "SELECT r.session_id,f.state,f.size_bytes,f.sha256 FROM recovery r
             JOIN recovery_original_reviews v ON v.session_id=r.session_id
             JOIN files f ON f.file_id=v.file_id WHERE f.file_id=?1 AND v.decision IS NULL",
                [id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
            )
            .optional()?;
        let (session, state, length, sha) = row.context("no pending file review for this ID")?;
        ensure!(
            matches!(decision, "keep" | "missing" | "deleted"),
            "invalid original review decision"
        );
        ensure!(
            state != "deleting" || decision == "deleted",
            "the saved deletion intent permits only a tombstone"
        );
        let live = original_directory(&self.directory)?;
        let live_file = original_path(&live, id)?;
        let quarantine = self.directory.join("restore-quarantine");
        let archived = original_path(&quarantine, id)?;
        if let Some(metadata) = file_metadata(&quarantine)? {
            ensure!(
                metadata.is_dir() && !crate::file_service::is_link(&metadata),
                "unsafe quarantine directory"
            );
        }
        let present = |p: &Path| -> anyhow::Result<bool> {
            if let Some(metadata) = file_metadata(p)? {
                ensure!(regular_file(&metadata), "unsafe original path");
                Ok(true)
            } else {
                Ok(false)
            }
        };
        let has_live = present(&live_file)?;
        let has_archive = present(&archived)?;
        match decision {
            "keep" => {
                ensure!(
                    has_archive || has_live,
                    "verified original bytes are missing"
                );
                if has_archive {
                    ensure!(
                        copy_verified(&archived, None, Some(length))? == sha,
                        "original bytes failed integrity check"
                    );
                }
                if has_live {
                    ensure!(
                        copy_verified(&live_file, None, Some(length))? == sha,
                        "original bytes failed integrity check"
                    );
                }
                if has_live && has_archive {
                    fs::remove_file(&archived)?;
                } else if has_archive {
                    fs::rename(&archived, &live_file)?;
                }
            }
            "missing" => ensure!(
                !has_live && !has_archive,
                "original still has bytes; select keep or deleted"
            ),
            "deleted" => {
                if has_archive {
                    fs::remove_file(&archived)?;
                }
                if has_live {
                    fs::remove_file(&live_file)?;
                }
            }
            _ => unreachable!(),
        }
        sync_directory(&live)?;
        if has_archive {
            sync_directory(&quarantine)?;
        }
        let timestamp = now_ms();
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let next = match decision {
            "keep" => "available",
            "deleted" => "deleted",
            _ => "missing",
        };
        tx.execute(
            "UPDATE files SET state=?1,version=version+1,deleted_at=?2 WHERE file_id=?3",
            params![next, (decision == "deleted").then_some(timestamp), id],
        )?;
        tx.execute("UPDATE recovery_original_reviews SET decision=?1,decided_at=?2 WHERE session_id=?3 AND file_id=?4",
            params![decision,timestamp,session,id])?;
        tx.execute(
            "UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='snapshot_seq'",
            [],
        )?;
        tx.execute(
            "UPDATE recovery SET reviewed_seq=?1 WHERE session_id=?2",
            params![snapshot_seq(&tx)?, session],
        )?;
        tx.execute("DELETE FROM recovery_seals WHERE session_id=?1", [session])?;
        tx.commit()?;
        Ok(())
    }
}

fn file_metadata(path: &Path) -> anyhow::Result<Option<fs::Metadata>> {
    match fs::symlink_metadata(path) {
        Ok(meta) => Ok(Some(meta)),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(err) => Err(err.into()),
    }
}

fn prune_completed(root: &Path, oldest: chrono::NaiveDate) -> anyhow::Result<()> {
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        let name = entry.file_name();
        let Some(date) = name
            .to_str()
            .and_then(|s| chrono::NaiveDate::parse_from_str(s, "%Y-%m-%d").ok())
        else {
            continue;
        };
        if date >= oldest
            || !entry.file_type()?.is_dir()
            || name.to_str() != Some(date.format("%Y-%m-%d").to_string().as_str())
        {
            continue;
        }
        let path = entry.path();
        let manifest_path = path.join(MANIFEST);
        let Some(metadata) = file_metadata(&manifest_path)? else {
            continue;
        };
        if !regular_file(&metadata) || metadata.len() > 16 * 1024 * 1024 {
            continue;
        }
        let Ok(manifest) = serde_json::from_reader::<_, Manifest>(File::open(&manifest_path)?)
        else {
            continue;
        };
        let same_day = chrono::DateTime::from_timestamp_millis(manifest.snapshot_at as i64)
            .is_some_and(|ts| ts.with_timezone(&chrono_tz::Asia::Shanghai).date_naive() == date);
        if manifest.format != FORMAT || !same_day {
            continue;
        }
        let images = original_directory(&path)?;
        let expected: std::collections::BTreeSet<_> = manifest
            .originals
            .iter()
            .map(|file| file.file_id.clone())
            .collect();
        let actual = fs::read_dir(&images)?
            .map(|file| Ok(file?.file_name().to_string_lossy().into_owned()))
            .collect::<std::io::Result<std::collections::BTreeSet<_>>>()?;
        let entries = fs::read_dir(&path)?
            .map(|file| Ok(file?.file_name().to_string_lossy().into_owned()))
            .collect::<std::io::Result<std::collections::BTreeSet<_>>>()?;
        if actual != expected || entries != [MANIFEST, DB, "files"].map(str::to_owned).into() {
            continue;
        }
        // Remove only exact files owned by this verified backup directory.
        let files = manifest
            .originals
            .iter()
            .map(|entry| original_path(&images, &entry.file_id))
            .collect::<anyhow::Result<Vec<_>>>()?;
        if files
            .iter()
            .chain(std::iter::once(&path.join(DB)))
            .any(|file| !fs::symlink_metadata(file).is_ok_and(|meta| regular_file(&meta)))
        {
            continue;
        }
        for file in files {
            fs::remove_file(file)?;
        }
        fs::remove_dir(images)?;
        fs::remove_file(path.join(DB))?;
        fs::remove_file(manifest_path)?;
        fs::remove_dir(path)?;
    }
    Ok(())
}
