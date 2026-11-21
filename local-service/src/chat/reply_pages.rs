use super::*;

const PAGE_CHARS: usize = 2048;
const RESULT_LIMIT: usize = contract::BUSINESS_LIMIT - 2048;
const ACCESSIBLE: &str =
    "FROM chat_jobs j JOIN peer p ON p.pair_id=j.pair_id AND p.fence=j.peer_fence
    WHERE j.pair_id=?1 AND j.epoch=?2 AND j.request_id=?3 AND j.state='completed'
      AND j.epoch=(SELECT value FROM metadata WHERE key='server_epoch')
      AND NOT EXISTS (SELECT 1 FROM recovery)
      AND j.raw_output IS NOT NULL
      AND CASE WHEN json_valid(j.reply) THEN
        json_extract(j.reply,'$.data.status')='completed'
        AND json_extract(j.reply,'$.data.requestId')=j.request_id
      ELSE 0 END";

pub(crate) fn can_read(
    tx: &rusqlite::Transaction<'_>,
    pair: &str,
    epoch: &str,
    request_id: &str,
) -> anyhow::Result<bool> {
    Ok(tx.query_row(
        &format!("SELECT EXISTS(SELECT 1 {ACCESSIBLE})"),
        params![pair, epoch, request_id],
        |row| row.get(0),
    )?)
}

pub(crate) fn command(domain: &Domain<'_, '_>) -> anyhow::Result<Outcome> {
    let request_id = str_at(domain.data, "requestId");
    let offset = domain.data["offset"].as_u64().unwrap_or(0);
    let expected_hash = domain.data["sha256"].as_str();
    if offset > 0 && expected_hash.is_none() {
        return Ok(failure("INVALID_PARAMS"));
    }
    let sql = format!(
        "SELECT j.job_id,j.request_id,j.deadline,j.text,j.evidence,j.raw_output,
                j.prompt_version='tickdesk-assistant-v1' {ACCESSIBLE}"
    );
    let saved = domain
        .tx
        .query_row(
            &sql,
            params![domain.pair, domain.epoch, request_id],
            |row| {
                Ok((
                    job_from_row(row)?,
                    row.get::<_, String>(5)?,
                    row.get::<_, bool>(6)?,
                ))
            },
        )
        .optional()?;
    let Some(((job_id, request_id, deadline, text, evidence), raw, bridge)) = saved else {
        return Ok(failure("AI_REPLY_UNAVAILABLE"));
    };
    let Ok(evidence) = serde_json::from_str(&evidence) else {
        return Ok(failure("AI_REPLY_UNAVAILABLE"));
    };
    let job = ChatJob {
        job_id,
        request_id,
        deadline,
        text,
        evidence,
    };
    // The accepted final reply may have been excerpted for its one-frame
    // transport. Rebuild only the published text and checked citations from
    // the same sealed inputs. Never return the retained provider artifact.
    let Ok(full) = build_reply(&job, &raw, bridge) else {
        return Ok(failure("AI_REPLY_UNAVAILABLE"));
    };
    let text = str_at(&full, "text");
    let sha256 = contract::sha256(text.as_bytes());
    let total = text.chars().count();
    if offset >= total as u64 || expected_hash.is_some_and(|expected| expected != sha256) {
        return Ok(failure("INVALID_PARAMS"));
    }
    let offset = offset as usize;
    let end = total.min(offset + PAGE_CHARS);
    let page: String = text.chars().skip(offset).take(end - offset).collect();
    let sources = if offset == 0 {
        full["sources"].clone()
    } else {
        json!([])
    };
    let result = json!({
        "requestId":job.request_id, "offset":offset, "nextOffset":(end < total).then_some(end),
        "totalChars":total, "sha256":sha256, "text":page, "sources":sources,
    });
    if contract::canonical(&result)?.len() > RESULT_LIMIT {
        return Ok(failure("MESSAGE_TOO_LARGE"));
    }
    Ok(Ok(result))
}
