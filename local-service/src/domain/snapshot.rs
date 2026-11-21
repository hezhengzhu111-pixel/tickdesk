use super::*;

impl Domain<'_, '_> {
    pub(super) fn sync(&self, events: &mut Vec<Value>) -> anyhow::Result<Outcome> {
        let snapshot = self.data["snapshotId"].as_str();
        let cursor = self.data["pageCursor"].as_str();
        let page = match (snapshot, cursor) {
            (None, None) => {
                let Some(page) = self.create_snapshot()? else {
                    return Ok(failure("MESSAGE_TOO_LARGE"));
                };
                page
            }
            (Some(snapshot), Some(cursor)) => {
                let row: Option<(String,u64)> = self.tx.query_row(
                    "SELECT body,expires_at FROM snapshot_pages WHERE pair_id=?1 AND epoch=?2 AND snapshot_id=?3 AND cursor=?4",
                    params![self.pair,self.epoch,snapshot,cursor], |r|Ok((r.get(0)?,r.get(1)?))).optional()?;
                let Some((body, expires)) = row else {
                    return Ok(failure("INVALID_PARAMS"));
                };
                if expires <= self.now {
                    return Ok(failure("SNAPSHOT_EXPIRED"));
                }
                serde_json::from_str(&body)?
            }
            _ => return Ok(failure("INVALID_PARAMS")),
        };
        let p = &page["data"];
        let result = json!({"requestId":self.data["clientMsgId"],"serverEpoch":self.epoch,"snapshotId":p["snapshotId"],
            "snapshotSeq":p["snapshotSeq"],"pageIndex":p["pageIndex"],"pageEventId":page["id"],
            "nextPageCursor":p["nextPageCursor"],"hasMore":p["hasMore"]});
        events.push(page);
        Ok(Ok(result))
    }

    fn create_snapshot(&self) -> anyhow::Result<Option<Value>> {
        if !self.recovering {
            crate::valuation::refresh(self.tx, self.now)?;
        }
        let snapshot = id();
        let until = self.now + TTL;
        let seq = self.seq()?;
        let mut manifest = json!({});
        let mut ordered = Vec::new();
        for name in COLLECTIONS {
            let values = self.list(name)?;
            manifest[name] = manifest_entry(&values)?;
            for value in values {
                ordered.push((name, value));
            }
        }
        let meta = json!({"analysis":self.analysis()?,"settings":self.settings()?,
            "visionNotice":crate::vision::notice(self.tx)?,
            "valuation":crate::valuation::summary(self.tx, self.now)?,
            "positionRevision":self.position_revision()?});
        manifest["meta"] = manifest_entry(std::slice::from_ref(&meta))?;
        let mut first = None;
        let mut cursor: Option<String> = None;
        let mut offset = 0;
        let mut index = 0;
        loop {
            let event_id = id();
            let next = id();
            let mut page = json!({"v":2,"kind":"state_snapshot","id":event_id,
                "dedupeKey":format!("snapshot:{}:{snapshot}:{index}",self.epoch),
                "ts":self.now,"serverEpoch":self.epoch,"snapshotSeq":seq,
                "data":{"requestId":self.data["clientMsgId"],"serverEpoch":self.epoch,"snapshotId":snapshot,
                    "snapshotSeq":seq,"recoveryMode":self.recovering,"generatedAt":self.now,"expiresAt":until,
                    "pageIndex":index,"nextPageCursor":next,"hasMore":true,
                    "manifest":if index==0 {manifest.clone()} else {Value::Null},"meta":if index==0 {meta.clone()} else {Value::Null}}});
            for key in COLLECTIONS {
                page["data"][key] = json!([]);
            }
            if canonical(&page)?.len() > BUSINESS_LIMIT {
                return Ok(None);
            }
            let mut count = 0;
            while count < 20 && offset < ordered.len() {
                let (name, value) = &ordered[offset];
                page["data"][name]
                    .as_array_mut()
                    .unwrap()
                    .push(value.clone());
                if canonical(&page)?.len() > BUSINESS_LIMIT {
                    page["data"][name].as_array_mut().unwrap().pop();
                    if count == 0 && index != 0 {
                        return Ok(None);
                    }
                    break;
                }
                offset += 1;
                count += 1;
            }
            let has_more = offset < ordered.len();
            if !has_more {
                page["data"]["hasMore"] = Value::Bool(false);
                page["data"]["nextPageCursor"] = Value::Null;
            }
            self.tx.execute(
                "INSERT INTO snapshot_pages VALUES (?1,?2,?3,?4,?5,?6,?7)",
                params![
                    event_id,
                    self.pair,
                    self.epoch,
                    snapshot,
                    cursor,
                    until,
                    serde_json::to_string(&page)?
                ],
            )?;
            if first.is_none() {
                first = Some(page);
            }
            if !has_more {
                return Ok(first);
            }
            cursor = Some(next);
            index += 1;
        }
    }
}

fn manifest_entry(values: &[Value]) -> anyhow::Result<Value> {
    Ok(json!({"count":values.len(),"sha256":crate::contract::digest(&json!(values))?}))
}
