use crate::{
    commands::{failure, Outcome},
    contract::digest,
    domain::Domain,
    file_service::{
        file_row, original_directory, original_path, regular_file, sync_directory, FILE_COLUMNS,
    },
    storage::Storage,
};
use anyhow::{ensure, Context};
use rusqlite::{params, OptionalExtension, TransactionBehavior};
use serde_json::{json, Value};
use std::{fs, io::ErrorKind, path::Path};

fn text<'a>(data: &'a Value, field: &str) -> &'a str {
    data[field].as_str().expect("validated deletion field")
}

fn rejected(code: &str) -> Option<Value> {
    Some(json!({"error":code}))
}

impl Storage {
    pub(crate) fn prepare_delete(
        &mut self,
        data: &Value,
        now: u64,
    ) -> anyhow::Result<Option<Value>> {
        let id = text(data, "fileId");
        let client = text(data, "clientMsgId");
        let base_version = data["baseVersion"]
            .as_u64()
            .context("invalid file version")?;
        let request_hash = digest(data)?;
        let epoch = &self.identity.server_epoch;
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let pair: String =
            tx.query_row("SELECT pair_id FROM peer WHERE singleton=1", [], |row| {
                row.get(0)
            })?;
        let file: Option<(u64, String)> = tx
            .query_row(
                "SELECT version,state FROM files WHERE file_id=?1 AND pair_id=?2",
                params![id, pair],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        let Some((version, state)) = file else {
            return Ok(rejected("FILE_NOT_FOUND"));
        };
        let deleting_version = match state.as_str() {
            "deleted" => return Ok(rejected("FILE_DELETED")),
            "deleting" => {
                let owns_deletion: bool = tx.query_row(
                    "SELECT EXISTS(SELECT 1 FROM file_deletions
                     WHERE file_id=?1 AND pair_id=?2 AND epoch=?3 AND client_msg_id=?4
                       AND request_hash=?5 AND deleting_version=?6)",
                    params![id, pair, epoch, client, request_hash, version],
                    |row| row.get(0),
                )?;
                if !owns_deletion {
                    let original_can_finish: bool = tx.query_row(
                        "SELECT EXISTS(SELECT 1 FROM file_deletions d
                         JOIN transport_inbox i ON i.pair_id=d.pair_id AND i.command_epoch=d.epoch
                            AND i.client_msg_id=d.client_msg_id AND i.request_hash=d.request_hash
                         JOIN peer p ON p.pair_id=i.pair_id AND p.incarnation=i.peer AND p.fence=i.fence
                         WHERE d.file_id=?1 AND d.epoch=?2 AND i.state='pending' AND i.received_epoch=?2)",
                        params![id, epoch], |row| row.get(0),
                    )?;
                    if original_can_finish {
                        return Ok(rejected("FILE_BUSY"));
                    }
                    if base_version != version {
                        return Ok(rejected("FILE_VERSION_CONFLICT"));
                    }
                    // Only a newly confirmed command referencing the current deleting version can
                    // resume an intent abandoned by a retired endpoint or recovery epoch.
                    ensure!(
                        tx.execute(
                            "UPDATE file_deletions SET epoch=?1,client_msg_id=?2,request_hash=?3
                         WHERE file_id=?4 AND pair_id=?5 AND deleting_version=?6",
                            params![epoch, client, request_hash, id, pair, version],
                        )? == 1,
                        "deletion intent missing during explicit recovery"
                    );
                }
                version
            }
            "available" | "missing" => {
                if base_version != version {
                    return Ok(rejected("FILE_VERSION_CONFLICT"));
                }
                crate::vision::expire(&tx, now)?;
                let leased: bool = tx.query_row(
                    "SELECT EXISTS(SELECT 1 FROM file_downloads
                     WHERE file_id=?1 AND state='sending' AND expires_at>?2)
                     OR EXISTS(SELECT 1 FROM vision_jobs WHERE file_id=?1
                     AND state IN ('queued','running') AND deadline>?2)",
                    params![id, now],
                    |row| row.get(0),
                )?;
                if leased {
                    return Ok(rejected("FILE_BUSY"));
                }
                ensure!(tx.execute(
                    "UPDATE files SET state='deleting',version=version+1 WHERE file_id=?1 AND pair_id=?2
                     AND version=?3 AND state IN ('available','missing')",
                    params![id,pair,version],
                )? == 1, "file changed while preparing deletion");
                tx.execute(
                    "INSERT INTO file_deletions VALUES (?1,?2,?3,?4,?5,?6)",
                    params![id, pair, epoch, client, request_hash, version + 1],
                )?;
                version + 1
            }
            _ => anyhow::bail!("unknown file state"),
        };
        tx.commit()?;
        let directory = original_directory(&self.directory)?;
        let original = original_path(&directory, id)?;
        remove_original(&directory, &original)?;
        Ok(Some(
            json!({"fileId":id,"deletingVersion":deleting_version}),
        ))
    }
}

fn remove_original(directory: &Path, path: &Path) -> anyhow::Result<()> {
    #[cfg(windows)]
    let staged = directory.join(format!(
        ".deleting-{}",
        path.file_name().unwrap().to_string_lossy()
    ));
    match fs::symlink_metadata(path) {
        Ok(metadata) => {
            ensure!(
                regular_file(&metadata),
                "unsafe original file during deletion"
            );
            #[cfg(windows)]
            crate::file_service::move_write_through(path, &staged)?;
            #[cfg(not(windows))]
            fs::remove_file(path)?;
        }
        Err(error) if error.kind() == ErrorKind::NotFound => {}
        Err(error) => return Err(error.into()),
    }
    #[cfg(windows)]
    match fs::symlink_metadata(&staged) {
        Ok(metadata) => {
            ensure!(regular_file(&metadata), "unsafe staged deletion");
            fs::remove_file(staged)?;
        }
        Err(error) if error.kind() == ErrorKind::NotFound => {}
        Err(error) => return Err(error.into()),
    }
    sync_directory(directory)?;
    Ok(())
}

pub(crate) fn delete_command(
    domain: &Domain<'_, '_>,
    prepared: Option<&Value>,
) -> anyhow::Result<Outcome> {
    let prepared = prepared.context("file deletion was not prepared")?;
    for code in [
        "FILE_NOT_FOUND",
        "FILE_DELETED",
        "FILE_BUSY",
        "FILE_VERSION_CONFLICT",
    ] {
        if prepared["error"] == code {
            return Ok(failure(code));
        }
    }
    let id = text(domain.data, "fileId");
    let client = text(domain.data, "clientMsgId");
    let request_hash = digest(domain.data)?;
    let version = prepared["deletingVersion"]
        .as_u64()
        .context("missing prepared deletion")?;
    ensure!(
        prepared["fileId"] == id,
        "prepared deletion named a different file"
    );
    let tx = domain.tx;
    ensure!(tx.execute(
        "UPDATE files SET state='deleted',deleted_at=?1,version=version+1
         WHERE file_id=?2 AND pair_id=?3 AND state='deleting' AND version=?4
         AND EXISTS(SELECT 1 FROM file_deletions WHERE file_id=?2 AND pair_id=?3 AND deleting_version=?4
           AND epoch=?5 AND client_msg_id=?6 AND request_hash=?7)",
        params![domain.now,id,domain.pair,version,domain.epoch,client,request_hash],
    )? == 1, "deletion intent changed before tombstone commit");
    tx.execute("DELETE FROM file_deletions WHERE file_id=?1", [id])?;
    // The file-state trigger also updates linked imports. Advance the snapshot
    // version so the App can wait for sync to observe those changes.
    domain.bump()?;
    let file = tx.query_row(
        &format!("SELECT {FILE_COLUMNS} FROM files WHERE file_id=?1 AND pair_id=?2"),
        params![id, domain.pair],
        file_row,
    )?;
    Ok(Ok(json!({"file":file})))
}
