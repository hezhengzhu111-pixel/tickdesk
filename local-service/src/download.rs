use crate::{
    commands::{failure, now_ms, Outcome},
    contract::{digest, sha256},
    domain::Domain,
    file_service::{file_row, original_directory, original_path, regular_file, FILE_COLUMNS},
    storage::Storage,
};
use anyhow::ensure;
use base64::{engine::general_purpose::STANDARD, Engine};
use rusqlite::{params, OptionalExtension, Transaction, TransactionBehavior};
use serde_json::json;
use std::{fs, io::Read, path::Path};
use uuid::Uuid;

const CHUNK_SIZE: usize = 32 * 1024;
const WINDOW: usize = 4;
const DOWNLOAD_TTL: u64 = 24 * 60 * 60 * 1000;

pub(crate) fn download_command(domain: &Domain<'_, '_>) -> anyhow::Result<Outcome> {
    let tx = domain.tx;
    expire_downloads(tx, domain.now)?;
    if domain.data["cmd"] == "cancel_transfer" {
        let id = domain.data["transferId"].as_str().expect("validated id");
        let state: Option<String> = tx
            .query_row(
                "SELECT state FROM file_downloads WHERE download_id=?1 AND pair_id=?2 AND epoch=?3",
                params![id, domain.pair, domain.epoch],
                |row| row.get(0),
            )
            .optional()?;
        let status = match state.as_deref() {
            None => return Ok(failure("FILE_NOT_FOUND")),
            Some("complete") => "already_complete",
            Some(_) => {
                end_download(tx, id, "cancelled")?;
                "cancelled"
            }
        };
        return Ok(Ok(json!({"transferId": id, "status": status})));
    }

    let file_id = domain.data["fileId"].as_str().expect("validated file id");
    let file = tx
        .query_row(
            &format!("SELECT {FILE_COLUMNS} FROM files WHERE file_id=?1 AND pair_id=?2"),
            params![file_id, domain.pair],
            file_row,
        )
        .optional()?;
    let Some(file) = file else {
        return Ok(failure("FILE_NOT_FOUND"));
    };
    match file["status"].as_str() {
        Some("available") => {}
        Some("deleted") => return Ok(failure("FILE_DELETED")),
        Some("deleting") => return Ok(failure("FILE_BUSY")),
        _ => return Ok(failure("FILE_NOT_FOUND")),
    }
    let size = file["sizeBytes"].as_u64().expect("file size");
    let total = size.div_ceil(CHUNK_SIZE as u64);
    if !(1..=640).contains(&total) {
        return Ok(failure("FILE_NOT_FOUND"));
    }
    let id = Uuid::new_v4().to_string();
    let expires_at = domain.now + DOWNLOAD_TTL;
    tx.execute(
        "INSERT INTO file_downloads (
            download_id,file_id,pair_id,epoch,file_version,total_chunks,expires_at,state
        ) VALUES (?1,?2,?3,?4,?5,?6,?7,'sending')",
        params![
            id,
            file_id,
            domain.pair,
            domain.epoch,
            file["version"].as_u64(),
            total,
            expires_at
        ],
    )?;
    Ok(Ok(json!({
        "downloadId": id, "file": file, "chunkSize": CHUNK_SIZE, "totalChunks": total,
        "expiresAt": expires_at
    })))
}

fn scrub_pending(tx: &Transaction<'_>) -> anyhow::Result<()> {
    // Keep the receipt tuple and chunk identity, but discard the image body once no retry is possible.
    tx.execute(
        "UPDATE transport_outbox SET state='quarantined',frame='{}'
         WHERE state IN ('pending','quarantined')
           AND frame<>'{}'
           AND delivery_id IN (
             SELECT c.delivery_id FROM file_download_chunks c
             JOIN file_downloads d ON d.download_id=c.download_id WHERE d.state<>'sending'
           )",
        [],
    )?;
    Ok(())
}

pub(crate) fn expire_downloads(tx: &Transaction<'_>, now: u64) -> anyhow::Result<()> {
    if tx.execute(
        "UPDATE file_downloads SET state='expired' WHERE state='sending' AND expires_at<=?1",
        [now],
    )? != 0
    {
        scrub_pending(tx)?;
    }
    Ok(())
}

pub(crate) fn cancel_all_downloads(tx: &Transaction<'_>) -> anyhow::Result<()> {
    tx.execute(
        "UPDATE file_downloads SET state='cancelled' WHERE state='sending'",
        [],
    )?;
    scrub_pending(tx)
}

fn end_download(tx: &Transaction<'_>, id: &str, state: &str) -> anyhow::Result<()> {
    if tx.execute(
        "UPDATE file_downloads SET state=?2 WHERE download_id=?1 AND state='sending'",
        params![id, state],
    )? != 0
    {
        scrub_pending(tx)?;
    }
    Ok(())
}

pub(crate) fn quarantine_download_delivery(
    tx: &Transaction<'_>,
    delivery_id: &str,
) -> anyhow::Result<()> {
    let id: Option<String> = tx
        .query_row(
            "SELECT c.download_id FROM file_download_chunks c JOIN transport_outbox o USING(delivery_id)
             WHERE c.delivery_id=?1 AND o.state='pending'",
            [delivery_id],
            |row| row.get(0),
        )
        .optional()?;
    if let Some(id) = id {
        end_download(tx, &id, "failed")?;
    }
    Ok(())
}

pub(crate) fn receive_download_receipt(
    tx: &Transaction<'_>,
    delivery_id: &str,
) -> anyhow::Result<()> {
    let id: Option<String> = tx
        .query_row(
            "SELECT download_id FROM file_download_chunks WHERE delivery_id=?1",
            [delivery_id],
            |row| row.get(0),
        )
        .optional()?;
    let Some(id) = id else {
        return Ok(());
    };
    tx.execute(
        "UPDATE transport_outbox SET frame='{}' WHERE delivery_id=?1 AND state='delivered'",
        [delivery_id],
    )?;
    tx.execute(
        "UPDATE file_downloads SET state='complete'
         WHERE download_id=?1 AND state='sending'
           AND total_chunks=(
             SELECT count(*) FROM file_download_chunks c JOIN transport_outbox o USING(delivery_id)
             WHERE c.download_id=?1 AND o.state='delivered'
           )",
        [&id],
    )?;
    Ok(())
}

struct Download {
    id: String,
    file_id: String,
    pair: String,
    recipient: String,
    size: usize,
    hash: String,
    total: usize,
    next: usize,
}

impl Storage {
    pub fn pump_downloads(&mut self, now: u64) -> anyhow::Result<()> {
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        expire_downloads(&tx, now)?;
        let pending: usize = tx.query_row(
            "SELECT count(*) FROM transport_outbox o JOIN file_download_chunks c USING(delivery_id)
             WHERE o.state='pending'",
            [],
            |row| row.get(0),
        )?;
        tx.commit()?;

        let mut slots = WINDOW.saturating_sub(pending);
        // Bound disk reads even if the queue contains missing or corrupt originals.
        for _ in 0..WINDOW {
            if slots == 0 {
                break;
            }
            let Some(download) = self.next_download(now)? else {
                break;
            };
            let bytes = verified_original(&self.directory, &download);
            let tx = self
                .connection
                .transaction_with_behavior(TransactionBehavior::Immediate)?;
            expire_downloads(&tx, now.max(now_ms()))?;
            if let Ok(bytes) = bytes {
                let count =
                    enqueue_chunks(&tx, &self.identity.incarnation, &download, &bytes, slots)?;
                slots -= count;
            } else {
                end_download(&tx, &download.id, "failed")?;
            }
            tx.commit()?;
        }
        Ok(())
    }

    fn next_download(&self, now: u64) -> anyhow::Result<Option<Download>> {
        Ok(self
            .connection
            .query_row(
                "SELECT d.download_id,d.file_id,d.pair_id,p.incarnation,f.size_bytes,f.sha256,
             d.total_chunks,(SELECT count(*) FROM file_download_chunks c
             WHERE c.download_id=d.download_id) AS next_chunk
             FROM file_downloads d JOIN files f ON d.file_id=f.file_id AND d.file_version=f.version
             JOIN peer p ON p.pair_id=d.pair_id
             WHERE d.epoch=?1 AND d.state='sending' AND d.expires_at>?2 AND f.state='available'
               AND d.total_chunks>next_chunk
             ORDER BY d.expires_at,d.download_id LIMIT 1",
                params![self.identity.server_epoch, now],
                |row| {
                    Ok(Download {
                        id: row.get(0)?,
                        file_id: row.get(1)?,
                        pair: row.get(2)?,
                        recipient: row.get(3)?,
                        size: row.get(4)?,
                        hash: row.get(5)?,
                        total: row.get(6)?,
                        next: row.get(7)?,
                    })
                },
            )
            .optional()?)
    }
}

fn verified_original(directory: &Path, download: &Download) -> anyhow::Result<Vec<u8>> {
    ensure!(
        download.size > 0 && download.size.div_ceil(CHUNK_SIZE) == download.total,
        "invalid original size"
    );
    let path = original_path(&original_directory(directory)?, &download.file_id)?;
    let metadata = fs::symlink_metadata(&path)?;
    ensure!(
        regular_file(&metadata) && metadata.len() == download.size as u64,
        "invalid original"
    );
    let file = fs::File::open(path)?;
    ensure!(file.metadata()?.is_file(), "invalid original");
    let mut bytes = Vec::with_capacity(download.size);
    file.take(download.size as u64 + 1)
        .read_to_end(&mut bytes)?;
    ensure!(
        bytes.len() == download.size && sha256(&bytes) == download.hash,
        "invalid original"
    );
    Ok(bytes)
}

fn enqueue_chunks(
    tx: &Transaction<'_>,
    sender: &str,
    download: &Download,
    bytes: &[u8],
    slots: usize,
) -> anyhow::Result<usize> {
    let active: bool = tx.query_row(
        "SELECT EXISTS(SELECT 1 FROM file_downloads d
         JOIN files f ON f.file_id=d.file_id AND f.version=d.file_version
         JOIN peer p ON p.pair_id=d.pair_id AND p.incarnation=?2
         WHERE d.download_id=?1 AND d.state='sending' AND f.state='available')",
        params![download.id, download.recipient],
        |row| row.get(0),
    )?;
    if !active {
        return Ok(0);
    }
    let mut count = 0;
    for (index, chunk) in bytes
        .chunks(CHUNK_SIZE)
        .enumerate()
        .skip(download.next)
        .take(slots)
    {
        let id = Uuid::new_v4().to_string();
        let payload = json!({
            "transferId": download.id, "fileId": download.file_id, "index": index,
            "totalChunks": download.total, "chunkHash": sha256(chunk), "bytes": STANDARD.encode(chunk)
        });
        let hash = digest(&payload)?;
        let frame = json!({
            "v": 2, "type": "deliver", "category": "file_chunk", "deliveryId": id,
            "fromIncarnation": sender, "toIncarnation": download.recipient, "payloadHash": hash,
            "payload": payload
        });
        tx.execute(
            "INSERT INTO transport_outbox(delivery_id,pair_id,sender,recipient,payload_hash,event_id,frame)
             VALUES (?1,?2,?3,?4,?5,?6,?7)",
            params![id, download.pair, sender, download.recipient, hash,
                format!("file_chunk:{}:{index}", download.id), frame.to_string()],
        )?;
        tx.execute(
            "INSERT INTO file_download_chunks(download_id,chunk_index,delivery_id) VALUES (?1,?2,?3)",
            params![download.id, index, id],
        )?;
        count += 1;
    }
    Ok(count)
}
