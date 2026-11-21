use crate::{
    commands::{failure, now_ms, Outcome},
    contract::{canonical, parse_frame, sha256},
    domain::Domain,
    storage::Storage,
};
use anyhow::{ensure, Context};
use base64::{engine::general_purpose::STANDARD, Engine};
use image::{ImageFormat, ImageReader, Limits};
use rusqlite::{params, Connection, OptionalExtension, Row, TransactionBehavior};
use serde_json::{json, Value};
use std::{
    fs::{self, File, Metadata},
    io::{Cursor, Read, Write},
    path::{Path, PathBuf},
};
use uuid::Uuid;

const CHUNK_SIZE: u64 = 32_768;
const UPLOAD_TTL: u64 = 86_400_000;
const LIST_TTL: u64 = 600_000;
const MAX_STAGING_BYTES: u64 = 256 * 1024 * 1024;
const MAX_STAGING_UPLOADS: u64 = 64;
const MAX_PIXELS: u64 = 40_000_000;
const ASSEMBLY_PREFIX: &str = ".upload-";

struct Upload {
    file_id: String,
    size: u64,
    mime: String,
    hash: String,
    total: u64,
    expires: u64,
    state: String,
}

fn string<'a>(data: &'a Value, key: &str) -> &'a str {
    data[key].as_str().expect("validated file command")
}

fn upload(db: &Connection, pair: &str, epoch: &str, id: &str) -> anyhow::Result<Option<Upload>> {
    Ok(db
        .query_row(
            "SELECT file_id,size_bytes,mime,sha256,total_chunks,expires_at,state FROM file_uploads
         WHERE upload_id=?1 AND pair_id=?2 AND epoch=?3",
            params![id, pair, epoch],
            |row| {
                Ok(Upload {
                    file_id: row.get(0)?,
                    size: row.get(1)?,
                    mime: row.get(2)?,
                    hash: row.get(3)?,
                    total: row.get(4)?,
                    expires: row.get(5)?,
                    state: row.get(6)?,
                })
            },
        )
        .optional()?)
}

fn upload_body(db: &Connection, pair: &str, epoch: &str, id: &str) -> anyhow::Result<Outcome> {
    let Some(upload) = upload(db, pair, epoch, id)? else {
        return Ok(failure("FILE_NOT_FOUND"));
    };
    let received = if upload.state == "complete" {
        (0..upload.total).collect()
    } else {
        let mut query = db.prepare(
            "SELECT chunk_index FROM file_upload_chunks WHERE upload_id=?1 ORDER BY chunk_index",
        )?;
        let indexes = query.query_map([id], |row| row.get::<_, u64>(0))?;
        indexes.collect::<Result<Vec<_>, _>>()?
    };
    Ok(Ok(json!({"upload":{
        "uploadId":id,"fileId":upload.file_id,"sizeBytes":upload.size,"chunkSize":CHUNK_SIZE,
        "totalChunks":upload.total,"receivedChunks":received,"expiresAt":upload.expires,"status":upload.state
    }})))
}

fn expire_uploads(db: &Connection, now: u64) -> anyhow::Result<()> {
    db.execute(
        "DELETE FROM file_upload_chunks WHERE upload_id IN
            (SELECT upload_id FROM file_uploads WHERE state='receiving' AND expires_at<=?1)",
        [now],
    )?;
    db.execute(
        "UPDATE file_uploads SET state='expired' WHERE state='receiving' AND expires_at<=?1",
        [now],
    )?;
    Ok(())
}

pub(crate) fn file_command(
    domain: &Domain<'_, '_>,
    prepared: Option<&Value>,
) -> anyhow::Result<Outcome> {
    let tx = domain.tx;
    let data = domain.data;
    let cmd = string(data, "cmd");
    if cmd != "list_files" {
        expire_uploads(tx, domain.now)?;
    }
    match cmd {
        "begin_upload" => {
            let id = Uuid::new_v4().to_string();
            let file_id = Uuid::new_v4().to_string();
            let size = data["sizeBytes"].as_u64().expect("validated size");
            let (count, reserved): (u64, u64) = tx.query_row(
                "SELECT count(*),coalesce(sum(size_bytes),0) FROM file_uploads WHERE state='receiving'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )?;
            if count >= MAX_STAGING_UPLOADS || reserved + size > MAX_STAGING_BYTES {
                return Ok(failure("DISK_FULL"));
            }
            tx.execute(
                "INSERT INTO file_uploads VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,'receiving')",
                params![
                    id,
                    file_id,
                    domain.pair,
                    domain.epoch,
                    string(data, "name"),
                    string(data, "mime"),
                    size,
                    string(data, "sha256"),
                    size.div_ceil(CHUNK_SIZE),
                    domain.now + UPLOAD_TTL
                ],
            )?;
            upload_body(tx, domain.pair, domain.epoch, &id)
        }
        "get_upload" => upload_body(tx, domain.pair, domain.epoch, string(data, "uploadId")),
        "finalize_upload" => finalize(domain, prepared),
        "list_files" => list(domain),
        "cancel_transfer" if data["kind"] == "upload" => {
            let id = string(data, "transferId");
            let Some(record) = upload(tx, domain.pair, domain.epoch, id)? else {
                return Ok(failure("FILE_NOT_FOUND"));
            };
            let status = if record.state == "complete" {
                "already_complete"
            } else {
                tx.execute(
                    "UPDATE file_uploads SET state='cancelled' WHERE upload_id=?1 AND state IN ('receiving','expired')",
                    [id],
                )?;
                tx.execute("DELETE FROM file_upload_chunks WHERE upload_id=?1", [id])?;
                "cancelled"
            };
            Ok(Ok(json!({"transferId":id,"status":status})))
        }
        _ => Ok(failure("DATA_UNAVAILABLE")),
    }
}

pub(crate) fn file_row(row: &Row<'_>) -> rusqlite::Result<Value> {
    Ok(json!({
        "fileId":row.get::<_, String>(0)?, "version":row.get::<_, u64>(1)?,
        "name":row.get::<_, String>(2)?, "mime":row.get::<_, String>(3)?,
        "sizeBytes":row.get::<_, u64>(4)?, "sha256":row.get::<_, String>(5)?,
        "status":row.get::<_, String>(6)?, "createdAt":row.get::<_, u64>(7)?,
        "deletedAt":row.get::<_, Option<u64>>(8)?,
    }))
}

pub(crate) const FILE_COLUMNS: &str =
    "file_id,version,name,mime,size_bytes,sha256,state,created_at,deleted_at";

fn finalize(domain: &Domain<'_, '_>, prepared: Option<&Value>) -> anyhow::Result<Outcome> {
    let id = string(domain.data, "uploadId");
    let tx = domain.tx;
    let Some(upload) = upload(tx, domain.pair, domain.epoch, id)? else {
        return Ok(failure("FILE_NOT_FOUND"));
    };
    match upload.state.as_str() {
        "expired" | "cancelled" => return Ok(failure("UPLOAD_EXPIRED")),
        "receiving" => {
            match prepared.and_then(|prepared| prepared["error"].as_str()) {
                Some("UPLOAD_INCOMPLETE") => return Ok(failure("UPLOAD_INCOMPLETE")),
                Some("FILE_HASH_MISMATCH") => return Ok(failure("FILE_HASH_MISMATCH")),
                Some("FILE_TYPE_UNSUPPORTED") => return Ok(failure("FILE_TYPE_UNSUPPORTED")),
                Some("DATA_UNAVAILABLE") => return Ok(failure("DATA_UNAVAILABLE")),
                _ => {}
            }
            ensure!(
                prepared.is_some_and(|prepared| prepared["fileId"] == upload.file_id
                    && prepared["sha256"] == upload.hash),
                "file was not archived before publishing metadata"
            );
            ensure!(tx.execute(
                "UPDATE file_uploads SET state='complete' WHERE upload_id=?1 AND pair_id=?2 AND epoch=?3
                 AND state='receiving' AND expires_at>?4",
                params![id, domain.pair, domain.epoch, domain.now],
            )? == 1, "upload was superseded");
            tx.execute(
                "INSERT INTO files SELECT file_id,pair_id,1,name,mime,size_bytes,sha256,'available',?2,NULL
                 FROM file_uploads WHERE upload_id=?1",
                params![id, domain.now],
            )?;
            tx.execute("DELETE FROM file_upload_chunks WHERE upload_id=?1", [id])?;
        }
        "complete" => {}
        _ => anyhow::bail!("invalid upload state"),
    }
    let file = tx.query_row(
        &format!("SELECT {FILE_COLUMNS} FROM files WHERE file_id=?1 AND pair_id=?2"),
        params![upload.file_id, domain.pair],
        file_row,
    )?;
    if file["status"] != "available" {
        return Ok(failure("FILE_NOT_FOUND"));
    }
    Ok(Ok(json!({"file":file})))
}

fn list(domain: &Domain<'_, '_>) -> anyhow::Result<Outcome> {
    let tx = domain.tx;
    let requested_limit = domain.data["limit"].as_u64();
    let (limit, cutoff, expires, after_at, after_id) = if let Some(cursor) =
        domain.data["cursor"].as_str()
    {
        let row = tx.query_row(
            "SELECT page_limit,file_cutoff,expires_at,last_created_at,last_file_id FROM file_list_cursors
             WHERE cursor=?1 AND pair_id=?2 AND epoch=?3",
            params![cursor, domain.pair, domain.epoch],
            |row| Ok((row.get::<_, u64>(0)?,row.get::<_, i64>(1)?,row.get::<_, u64>(2)?,
                row.get::<_, u64>(3)?,row.get::<_, String>(4)?)),
        ).optional()?;
        let Some(row) = row else {
            return Ok(failure("INVALID_PARAMS"));
        };
        if row.2 <= domain.now {
            return Ok(failure("SNAPSHOT_EXPIRED"));
        }
        if requested_limit.is_some_and(|limit| limit != row.0) {
            return Ok(failure("INVALID_PARAMS"));
        }
        (row.0, row.1, row.2, Some(row.3), Some(row.4))
    } else {
        let cutoff: i64 = tx.query_row(
            "SELECT coalesce(max(revision),0) FROM file_versions",
            [],
            |row| row.get(0),
        )?;
        (
            requested_limit.unwrap_or(20),
            cutoff,
            domain.now + LIST_TTL,
            None,
            None,
        )
    };
    let mut query = tx.prepare(&format!(
        "SELECT {FILE_COLUMNS} FROM file_versions page WHERE pair_id=?1 AND revision<=?2
         AND NOT EXISTS(SELECT 1 FROM file_versions newer
             WHERE newer.file_id=page.file_id AND newer.revision>page.revision AND newer.revision<=?2)
         AND (?3 IS NULL OR created_at<?3 OR (created_at=?3 AND file_id>?4))
         ORDER BY created_at DESC,file_id LIMIT ?5"
    ))?;
    let mut items = query
        .query_map(
            params![domain.pair, cutoff, after_at, after_id, limit + 1],
            file_row,
        )?
        .collect::<Result<Vec<_>, _>>()?;
    let next = if items.len() > limit as usize {
        items.pop();
        let last = items.last().context("invalid file page")?;
        let cursor = Uuid::new_v4().to_string();
        tx.execute(
            "INSERT INTO file_list_cursors VALUES (?1,?2,?3,?4,?5,?6,?7,?8)",
            params![
                cursor,
                domain.pair,
                domain.epoch,
                expires,
                limit,
                cutoff,
                last["createdAt"].as_u64(),
                last["fileId"].as_str()
            ],
        )?;
        Some(cursor)
    } else {
        None
    };
    Ok(Ok(json!({"items":items,"nextCursor":next})))
}

impl Storage {
    pub(crate) fn receive_file_chunk(&mut self, frame: &Value) -> anyhow::Result<Value> {
        ensure!(
            frame["type"] == "deliver" && frame["category"] == "file_chunk",
            "INVALID_FRAME"
        );
        parse_frame(&canonical(frame)?).map_err(|_| anyhow::anyhow!("INVALID_FRAME"))?;
        let id = string(frame, "deliveryId");
        let hash = string(frame, "payloadHash");
        let payload = &frame["payload"];
        let transfer = string(payload, "transferId");
        let bytes = STANDARD.decode(string(payload, "bytes"))?;
        let now = now_ms();
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let (pair, peer): (String, String) = tx
            .query_row(
                "SELECT pair_id,incarnation FROM peer WHERE singleton=1",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?
            .context("INCARNATION_MISMATCH")?;
        ensure!(
            frame["fromIncarnation"] == peer && frame["toIncarnation"] == self.identity.incarnation,
            "INCARNATION_MISMATCH"
        );
        let business_id: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM transport_inbox WHERE pair_id=?1 AND peer=?2 AND delivery_id=?3)",
            params![pair,peer,id], |row|row.get(0),
        )?;
        ensure!(!business_id, "DELIVERY_ID_REUSED");
        let prior: Option<(String, String)> = tx
            .query_row(
                "SELECT r.payload_hash,u.epoch FROM file_chunk_receipts r
             JOIN file_uploads u ON u.upload_id=r.upload_id
             WHERE r.pair_id=?1 AND r.peer=?2 AND r.delivery_id=?3",
                params![pair, peer, id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        if let Some((prior_hash, epoch)) = prior {
            ensure!(prior_hash == hash, "DELIVERY_ID_REUSED");
            ensure!(epoch == self.identity.server_epoch, "UPLOAD_NOT_FOUND");
        } else {
            expire_uploads(&tx, now)?;
            let record = upload(&tx, &pair, &self.identity.server_epoch, transfer)?
                .filter(|record| record.state == "receiving")
                .context("UPLOAD_NOT_FOUND")?;
            let index = payload["index"].as_u64().context("INVALID_FRAME")?;
            ensure!(
                payload["fileId"] == record.file_id
                    && payload["totalChunks"] == record.total
                    && index < record.total
                    && bytes.len() as u64 == (record.size - index * CHUNK_SIZE).min(CHUNK_SIZE),
                "CHUNK_CONFLICT"
            );
            let chunk_hash = string(payload, "chunkHash");
            let stored: Option<String> = tx.query_row(
                "SELECT chunk_hash FROM file_upload_chunks WHERE upload_id=?1 AND chunk_index=?2",
                params![transfer,index], |row| row.get(0),
            ).optional()?;
            if let Some(stored) = stored {
                ensure!(stored == chunk_hash, "CHUNK_CONFLICT");
            } else {
                tx.execute(
                    "INSERT INTO file_upload_chunks VALUES (?1,?2,?3,?4)",
                    params![transfer, index, chunk_hash, bytes],
                )?;
            }
            tx.execute(
                "INSERT INTO file_chunk_receipts VALUES (?1,?2,?3,?4,?5)",
                params![pair, peer, id, hash, transfer],
            )?;
            tx.execute(
                "UPDATE file_uploads SET expires_at=max(expires_at,?2) WHERE upload_id=?1",
                params![transfer, now + UPLOAD_TTL],
            )?;
        }
        tx.commit()?;
        Ok(
            json!({"v":2,"type":"receipt","deliveryId":id,"payloadHash":hash,
            "fromIncarnation":peer,"toIncarnation":self.identity.incarnation}),
        )
    }

    pub(crate) fn prepare_file_command(
        &self,
        data: &Value,
        now: u64,
    ) -> anyhow::Result<Option<Value>> {
        if data["cmd"] != "finalize_upload" {
            return Ok(None);
        }
        let Some(pair) = self
            .connection
            .query_row("SELECT pair_id FROM peer WHERE singleton=1", [], |row| {
                row.get::<_, String>(0)
            })
            .optional()?
        else {
            return Ok(None);
        };
        let id = string(data, "uploadId");
        let Some(upload) = upload(&self.connection, &pair, &self.identity.server_epoch, id)?
            .filter(|record| record.state == "receiving" && record.expires > now)
        else {
            return Ok(None);
        };

        let result = self.assemble(id, &upload)?;
        Ok(Some(match result {
            Ok(()) => json!({"fileId":upload.file_id,"sha256":upload.hash}),
            Err(code) => json!({"error":code}),
        }))
    }

    fn assemble(&self, id: &str, upload: &Upload) -> anyhow::Result<Result<(), &'static str>> {
        let mut bytes = Vec::with_capacity(upload.size as usize);
        {
            let mut query = self.connection.prepare(
                "SELECT chunk_index,bytes FROM file_upload_chunks WHERE upload_id=?1 ORDER BY chunk_index",
            )?;
            let mut rows = query.query([id])?;
            for expected in 0..upload.total {
                let Some(row) = rows.next()? else {
                    return Ok(Err("UPLOAD_INCOMPLETE"));
                };
                if row.get::<_, u64>(0)? != expected {
                    return Ok(Err("UPLOAD_INCOMPLETE"));
                }
                let chunk = row.get::<_, Vec<u8>>(1)?;
                if chunk.len() as u64 != (upload.size - expected * CHUNK_SIZE).min(CHUNK_SIZE) {
                    return Ok(Err("FILE_HASH_MISMATCH"));
                }
                bytes.extend_from_slice(&chunk);
            }
        }
        if bytes.len() as u64 != upload.size || sha256(&bytes) != upload.hash {
            return Ok(Err("FILE_HASH_MISMATCH"));
        }
        if !valid_image(&bytes, &upload.mime) {
            return Ok(Err("FILE_TYPE_UNSUPPORTED"));
        }
        let directory = original_directory(&self.directory)?;
        let destination = original_path(&directory, &upload.file_id)?;
        match fs::symlink_metadata(&destination) {
            Ok(metadata) => {
                if !regular_file(&metadata) || metadata.len() != upload.size {
                    return Ok(Err("DATA_UNAVAILABLE"));
                }
                let mut existing = Vec::new();
                File::open(&destination)?
                    .take(upload.size + 1)
                    .read_to_end(&mut existing)?;
                if existing != bytes {
                    return Ok(Err("DATA_UNAVAILABLE"));
                }
                sync_directory(&directory)?;
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                let mut staged = tempfile::Builder::new()
                    .prefix(ASSEMBLY_PREFIX)
                    .tempfile_in(&directory)?;
                staged.write_all(&bytes)?;
                staged.as_file().sync_all()?;
                publish(staged, &destination)?;
                sync_directory(&directory)?;
            }
            Err(error) => return Err(error.into()),
        }
        Ok(Ok(()))
    }

    pub(crate) fn inspect_original_files(&mut self) -> anyhow::Result<()> {
        let directory = original_directory(&self.directory)?;
        sync_directory(&self.directory)?;
        for item in fs::read_dir(&directory)? {
            let item = item?;
            if item
                .file_name()
                .to_str()
                .is_some_and(|name| name.starts_with(ASSEMBLY_PREFIX))
                && regular_file(&fs::symlink_metadata(item.path())?)
            {
                fs::remove_file(item.path())?;
            }
        }
        let missing = {
            let mut query = self
                .connection
                .prepare("SELECT file_id,size_bytes FROM files WHERE state='available'")?;
            let mut rows = query.query([])?;
            let mut missing = Vec::new();
            while let Some(row) = rows.next()? {
                let id: String = row.get(0)?;
                let size: u64 = row.get(1)?;
                let path = original_path(&directory, &id)?;
                let present = match fs::symlink_metadata(path) {
                    Ok(metadata) => regular_file(&metadata) && metadata.len() == size,
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => false,
                    Err(error) => return Err(error.into()),
                };
                if !present {
                    missing.push(id);
                }
            }
            missing
        };
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        expire_uploads(&tx, now_ms())?;
        for id in missing {
            tx.execute("UPDATE files SET state='missing',version=version+1 WHERE file_id=?1 AND state='available'", [id])?;
        }
        tx.commit()?;
        self.cleanup_abandoned_uploads(now_ms())?;
        Ok(())
    }

    pub(crate) fn cleanup_abandoned_uploads(&mut self, now: u64) -> anyhow::Result<()> {
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        expire_uploads(&tx, now)?;
        tx.commit()?;
        // Terminal upload rows are durable cleanup intentions. Keep them so an
        // unlink or process failure can be retried. Never remove a referenced
        // original or a published archive belonging to a still-active upload.
        let ids = {
            let mut query = self.connection.prepare(
                "SELECT u.file_id FROM file_uploads u
                WHERE u.state IN ('cancelled','expired')
                AND NOT EXISTS (SELECT 1 FROM files f WHERE f.file_id=u.file_id)",
            )?;
            let records = query
                .query_map([], |row| row.get::<_, String>(0))?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            records
        };
        let directory = original_directory(&self.directory)?;
        for id in ids {
            let path = original_path(&directory, &id)?;
            match fs::symlink_metadata(&path) {
                Ok(metadata) => {
                    ensure!(regular_file(&metadata), "unexpected abandoned archive type");
                    fs::remove_file(&path)?;
                    sync_directory(&directory)?;
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                Err(error) => return Err(error.into()),
            }
        }
        Ok(())
    }
}

fn valid_image(bytes: &[u8], mime: &str) -> bool {
    let format = match mime {
        "image/png" => ImageFormat::Png,
        "image/jpeg" => ImageFormat::Jpeg,
        _ => return false,
    };
    if image::guess_format(bytes).ok() != Some(format) {
        return false;
    }
    let mut limits = Limits::default();
    limits.max_image_width = Some(MAX_PIXELS as u32);
    limits.max_image_height = Some(MAX_PIXELS as u32);
    limits.max_alloc = Some(350 * 1024 * 1024);
    let mut reader = ImageReader::with_format(Cursor::new(bytes), format);
    reader.limits(limits.clone());
    let Ok((width, height)) = reader.into_dimensions() else {
        return false;
    };
    if u64::from(width) * u64::from(height) > MAX_PIXELS || width == 0 || height == 0 {
        return false;
    }
    let mut reader = ImageReader::with_format(Cursor::new(bytes), format);
    reader.limits(limits);
    reader.decode().is_ok()
}

pub(crate) fn is_link(metadata: &Metadata) -> bool {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        metadata.file_attributes() & 0x400 != 0
    }
    #[cfg(not(windows))]
    {
        metadata.file_type().is_symlink()
    }
}

pub(crate) fn regular_file(metadata: &Metadata) -> bool {
    metadata.is_file() && !is_link(metadata)
}

pub(crate) fn original_directory(data_dir: &Path) -> anyhow::Result<PathBuf> {
    let root = fs::symlink_metadata(data_dir)?;
    ensure!(root.is_dir() && !is_link(&root), "unsafe data directory");
    let directory = data_dir.join("files");
    let metadata = fs::symlink_metadata(&directory)?;
    ensure!(
        metadata.is_dir() && !is_link(&metadata),
        "unsafe original directory"
    );
    Ok(fs::canonicalize(directory)?)
}

pub(crate) fn original_path(directory: &Path, id: &str) -> anyhow::Result<PathBuf> {
    ensure!(
        Uuid::parse_str(id).is_ok_and(|uuid| uuid.to_string() == id),
        "invalid original file id"
    );
    Ok(directory.join(id))
}

#[cfg(unix)]
pub(crate) fn sync_directory(directory: &Path) -> std::io::Result<()> {
    File::open(directory)?.sync_all()
}

#[cfg(not(unix))]
pub(crate) fn sync_directory(_directory: &Path) -> std::io::Result<()> {
    Ok(())
}

#[cfg(not(windows))]
fn publish(staged: tempfile::NamedTempFile, destination: &Path) -> std::io::Result<()> {
    staged
        .persist_noclobber(destination)
        .map(|_| ())
        .map_err(|error| error.error)
}

#[cfg(windows)]
fn publish(staged: tempfile::NamedTempFile, destination: &Path) -> std::io::Result<()> {
    move_write_through(&staged.into_temp_path(), destination)
}

#[cfg(windows)]
pub(crate) fn move_write_through(source: &Path, destination: &Path) -> std::io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    // Write-through rename, without REPLACE_EXISTING. Source and destination share a directory.
    const MOVEFILE_WRITE_THROUGH: u32 = 0x8;
    #[link(name = "kernel32")]
    extern "system" {
        fn MoveFileExW(existing: *const u16, new_name: *const u16, flags: u32) -> i32;
    }
    let source: Vec<_> = source.as_os_str().encode_wide().chain([0]).collect();
    let target: Vec<_> = destination.as_os_str().encode_wide().chain([0]).collect();
    // Both pointers refer to NUL-terminated UTF-16 buffers for the duration of the call.
    if unsafe { MoveFileExW(source.as_ptr(), target.as_ptr(), MOVEFILE_WRITE_THROUGH) } == 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(())
}
