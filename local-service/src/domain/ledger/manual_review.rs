//! Human review notes for the reproducible trade-review model.
//!
//! These notes never rewrite an execution, tune a strategy or create a
//! notification. They are a separate, versioned audit trail: a new note can
//! supersede an older one, and withdrawal is another version, not a delete.
use super::{failure, int, str_at, Domain, Outcome};
use serde_json::json;
use std::collections::BTreeSet;

const COLLECTION: &str = "trade_review_notes";
const HISTORY: &str = "trade_review_note_history";
const MAX_PAGE: usize = 50;

impl Domain<'_, '_> {
    pub(crate) fn manual_trade_review(&self) -> anyhow::Result<Outcome> {
        match self.data["cmd"].as_str() {
            Some("record_trade_review_note") => self.record_review_note(),
            Some("withdraw_trade_review_note") => self.withdraw_review_note(),
            _ => self.list_review_notes(),
        }
    }

    fn record_review_note(&self) -> anyhow::Result<Outcome> {
        let note_id = str_at(self.data, "noteId");
        let subject_type = str_at(self.data, "subjectType");
        let subject_id = str_at(self.data, "subjectId");
        let verdict = str_at(self.data, "verdict");
        let note = str_at(self.data, "note").trim();
        let reviewer = str_at(self.data, "reviewer").trim();
        let at = int(self.data, "at");
        if note.is_empty() || reviewer.is_empty() || at > self.now {
            return Ok(failure("INVALID_PARAMS"));
        }
        if subject_type == "execution" && self.get("executions", subject_id)?.is_none() {
            return Ok(failure("INVALID_PARAMS"));
        }
        let mut evidence = BTreeSet::new();
        for id in self.data["evidenceExecutionIds"]
            .as_array()
            .expect("validated evidence array")
        {
            let id = id.as_str().expect("validated ID");
            if !evidence.insert(id.to_owned()) || self.get("executions", id)?.is_none() {
                return Ok(failure("INVALID_PARAMS"));
            }
        }
        if self.get(COLLECTION, note_id)?.is_some() {
            return Ok(Err((
                "TRADE_REVIEW_NOTE_CONFLICT",
                json!({"noteId":note_id}),
            )));
        }
        let supersedes = self.data["supersedesNoteId"].as_str();
        let mut version = 1_u64;
        if let Some(previous) = supersedes {
            let Some(mut old) = self.get(COLLECTION, previous)? else {
                return Ok(failure("TRADE_REVIEW_NOTE_NOT_FOUND"));
            };
            if old["subjectId"] != subject_id
                || old["subjectType"] != subject_type
                || old["status"] != "active"
            {
                return Ok(Err((
                    "TRADE_REVIEW_NOTE_CONFLICT",
                    json!({"noteId":previous}),
                )));
            }
            version = old["version"].as_u64().unwrap_or(1) + 1;
            old["status"] = json!("superseded");
            old["updatedAt"] = json!(at);
            self.put(
                HISTORY,
                &format!("{previous}:{}", old["version"].as_u64().unwrap_or(1)),
                &old,
            )?;
            self.put(COLLECTION, previous, &old)?;
        }
        let duplicate = self.list(COLLECTION)?.into_iter().any(|item| {
            item["subjectType"] == subject_type
                && item["subjectId"] == subject_id
                && item["verdict"] == verdict
                && item["note"] == note
                && item["reviewer"] == reviewer
                && item["status"] == "active"
                && item["evidenceExecutionIds"]
                    == json!(evidence.iter().cloned().collect::<Vec<_>>())
        });
        if duplicate {
            return Ok(failure("TRADE_REVIEW_NOTE_DUPLICATE"));
        }
        if version >= 9_007_199_254_740_991 {
            return Ok(failure("TRADE_REVIEW_NOTE_CONFLICT"));
        }
        let value = json!({
            "noteId":note_id,"subjectType":subject_type,"subjectId":subject_id,
            "verdict":verdict,"note":note,"reviewer":reviewer,
            "evidenceExecutionIds":evidence.iter().cloned().collect::<Vec<_>>(),
            "status":"active","version":version,"supersedesNoteId":supersedes,
            "createdAt":at,"updatedAt":at,
        });
        self.put(COLLECTION, note_id, &value)?;
        Ok(Ok(json!({"note":value})))
    }

    fn withdraw_review_note(&self) -> anyhow::Result<Outcome> {
        let note_id = str_at(self.data, "noteId");
        let base = int(self.data, "baseVersion");
        let reason = str_at(self.data, "reason").trim();
        let at = int(self.data, "at");
        if reason.is_empty() || at > self.now {
            return Ok(failure("INVALID_PARAMS"));
        }
        let Some(mut note) = self.get(COLLECTION, note_id)? else {
            return Ok(failure("TRADE_REVIEW_NOTE_NOT_FOUND"));
        };
        if note["status"] != "active" || note["version"].as_u64() != Some(base) {
            return Ok(Err((
                "TRADE_REVIEW_NOTE_CONFLICT",
                json!({"noteId":note_id,"currentVersion":note["version"]}),
            )));
        }
        self.put(
            HISTORY,
            &format!("{note_id}:{base}"),
            &json!({"note":note,"reason":reason,"at":at,"action":"withdraw"}),
        )?;
        note["status"] = json!("withdrawn");
        note["version"] = json!(base + 1);
        note["updatedAt"] = json!(at);
        self.put(COLLECTION, note_id, &note)?;
        Ok(Ok(json!({"note":note})))
    }

    fn list_review_notes(&self) -> anyhow::Result<Outcome> {
        // `subjectId` is an optional filter; the full manual-review list is a
        // valid query and must not panic when the field is absent.
        let subject = self.data["subjectId"].as_str().unwrap_or("");
        let limit = self.data["limit"]
            .as_u64()
            .unwrap_or(20)
            .clamp(1, MAX_PAGE as u64) as usize;
        let mut notes = self
            .list(COLLECTION)?
            .into_iter()
            .filter(|note| subject.is_empty() || note["subjectId"] == subject)
            .collect::<Vec<_>>();
        notes.sort_by(|a, b| {
            b["createdAt"]
                .as_u64()
                .cmp(&a["createdAt"].as_u64())
                .then_with(|| b["noteId"].as_str().cmp(&a["noteId"].as_str()))
        });
        let manual_count = notes
            .iter()
            .filter(|note| note["status"] == "active")
            .count() as u64;
        let digest = crate::contract::digest(&json!(notes))?;
        let offset = match self.data["cursor"].as_str() {
            None => 0,
            Some(cursor) => {
                let parts = cursor.split(':').collect::<Vec<_>>();
                if parts.len() != 3 || parts[0] != "v1" || parts[2] != digest {
                    return Ok(failure("TRADE_REVIEW_CURSOR_EXPIRED"));
                }
                parts[1]
                    .parse::<usize>()
                    .map_err(|_| anyhow::anyhow!("cursor offset"))?
            }
        };
        if offset > notes.len() {
            return Ok(failure("TRADE_REVIEW_CURSOR_EXPIRED"));
        }
        let end = (offset + limit).min(notes.len());
        let next = (end < notes.len()).then(|| format!("v1:{end}:{digest}"));
        Ok(Ok(json!({
            "items":notes[offset..end],
            "nextCursor":next,
            "hasMore":next.is_some(),
            "manualCount":manual_count,
        })))
    }
}
