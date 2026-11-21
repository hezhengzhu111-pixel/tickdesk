use super::*;

impl Domain<'_, '_> {
    pub(super) fn get_task(&self) -> anyhow::Result<Outcome> {
        let Some(task) = self.get("tasks", str_at(self.data, "taskId"))? else {
            return Ok(failure("TASK_NOT_FOUND"));
        };
        Ok(Ok(
            json!({"task":task,"serverEpoch":self.epoch,"recoveryMode":self.recovering,"snapshotSeq":self.seq()?}),
        ))
    }

    pub(super) fn get_task_actions(&self) -> anyhow::Result<Outcome> {
        let key = str_at(self.data, "taskId");
        if self.get("tasks", key)?.is_none() {
            return Ok(failure("TASK_NOT_FOUND"));
        }
        self.page(
            &format!("task:{key}"),
            "pageCursor",
            || {
                self.read_bodies(
                    "SELECT body FROM task_actions WHERE task_id=?1 ORDER BY at DESC,action_id",
                    [key],
                )
            },
            json!({"taskId":key}),
            "nextPageCursor",
            true,
        )
    }

    pub(super) fn change_task(&self, events: &mut Vec<Value>) -> anyhow::Result<Outcome> {
        let key = str_at(self.data, "taskId");
        let Some(mut task) = self.get("tasks", key)? else {
            return Ok(failure("TASK_NOT_FOUND"));
        };
        let err = |code, task: &Value| Ok(Err((code, json!({"currentTask":task}))));
        match str_at(&task, "status") {
            "expired" => return err("TASK_EXPIRED", &task),
            "acted" | "ignored" => return err("TASK_TERMINAL", &task),
            _ => {}
        }
        if self.now >= int(&task, "validUntil") {
            return err("TASK_EXPIRED", &task);
        }
        if self.data["baseActionVersion"] != task["actionVersion"] {
            return err("TASK_VERSION_CONFLICT", &task);
        }
        let name = str_at(self.data, "cmd");
        let mut execution = Value::Null;
        let mut position = Value::Null;
        let action = match name {
            "snooze" => {
                let level = str_at(&task, "level");
                if level == "info" || (level == "warn" && int(&task, "remindedCount") >= 2) {
                    return err("SNOOZE_NOT_APPLICABLE", &task);
                }
                let minutes = self.data["minutes"].as_u64().unwrap_or(5);
                let until = self.now + minutes * 60_000;
                let expiry = int(&task, "validUntil").max(until + 180_000);
                if expiry > int(&task, "createdAt") + 3_600_000 {
                    return err("SNOOZE_LIMIT", &task);
                }
                task["validUntil"] = json!(expiry);
                task["snoozeUntil"] = json!(until);
                task["nextReminderAt"] = json!(until);
                "snoozed"
            }
            "act" => {
                let direction = str_at(&task, "direction");
                if self.data["side"].is_null() {
                    if direction != "risk" {
                        return Ok(failure("INVALID_PARAMS"));
                    }
                } else {
                    if direction != "risk" && self.data["side"] != direction {
                        return Ok(failure("INVALID_PARAMS"));
                    }
                    match self.record(self.data, str_at(&task, "symbol"))? {
                        Ok(row) => {
                            execution = row["execution"].clone();
                            position = row["position"].clone();
                        }
                        Err(err) => return Ok(Err(err)),
                    }
                }
                "acted"
            }
            _ => "ignored",
        };
        if action != "snoozed" {
            task["status"] = json!(action);
            task["nextReminderAt"] = Value::Null;
            task["snoozeUntil"] = Value::Null;
        }
        task["currentReminder"] = Value::Null;
        task["version"] = json!(int(&task, "version") + 1);
        task["actionVersion"] = json!(int(&task, "actionVersion") + 1);
        let action_id = id();
        let log = json!({"actionId":action_id,"taskId":key,"type":action,"at":self.now,
            "clientMsgId":self.data["clientMsgId"],"reason":self.data["reason"],
            "taskVersion":task["version"],"actionVersion":task["actionVersion"]});
        self.tx.execute(
            "INSERT INTO task_actions VALUES (?1,?2,?3,?4)",
            params![action_id, key, self.now, serde_json::to_string(&log)?],
        )?;
        self.put("tasks", key, &task)?;
        crate::reminders::retire(self.tx, self.now)?;
        let seq = self.bump()?;
        events.push(json!({"v":2,"kind":"task_update","id":id(),"dedupeKey":format!("task:{key}:{}",task["version"]),
            "ts":self.now,"serverEpoch":self.epoch,"snapshotSeq":seq,"data":task}));
        Ok(Ok(if name == "act" {
            json!({"task":task,"execution":execution,"position":position,"snapshotSeq":seq})
        } else {
            json!({"task":task,"snapshotSeq":seq})
        }))
    }
}
