//! Verified exchange restrictions do not depend on a production strategy hit.
use super::{event, store_new_task};
use crate::{
    contract::validate_definition,
    domain::{id, Domain},
};
use anyhow::Context;
use rusqlite::params;
use serde_json::{json, Value};
use std::{cmp::Ordering, collections::BTreeSet};

const HEADS: &str = "market_suspension_heads";
const EPISODES: &str = "market_suspensions";

fn head(d: &Domain<'_, '_>, symbol: &str) -> anyhow::Result<Option<Value>> {
    Ok(d.get(HEADS, symbol)?
        .filter(|head| head["epoch"] == d.epoch))
}

pub(super) fn trading_constraint(
    d: &Domain<'_, '_>,
    symbol: &str,
    date: &str,
    direction: &str,
) -> anyhow::Result<Option<&'static str>> {
    if !matches!(direction, "buy" | "sell") {
        return Ok(None);
    }
    if let Some(rule) = d.verified_auction_status_at(symbol, date, d.now)? {
        return Ok(
            (rule["tradingStatus"] == "suspended").then_some(if direction == "buy" {
                "HARD_RISK_VETO"
            } else {
                "TRADING_SUSPENDED"
            }),
        );
    }
    Ok(head(d, symbol)?
        .is_some_and(|row| row["active"] == true)
        .then_some("TRADING_STATUS_UNCONFIRMED"))
}

pub(super) fn retire_superseded(
    d: &Domain<'_, '_>,
    date: &str,
    symbols: &BTreeSet<String>,
    events: &mut Vec<Value>,
) -> anyhow::Result<()> {
    for symbol in symbols {
        let Some(head) = head(d, symbol)?.filter(|head| head["active"] == true) else {
            continue;
        };
        match d.verified_auction_status_at(symbol, date, d.now)? {
            None => expire_notice(d, &head, UNCONFIRMED, events)?,
            Some(rule) if rule["tradingStatus"] == "trading" => {
                close(d, symbol, head, &rule, RESUMED, events)?;
            }
            Some(_) => {}
        }
    }
    if !events.is_empty() {
        crate::reminders::retire(d.tx, d.now)?;
    }
    Ok(())
}

const UNCONFIRMED: &str = "当日停复牌证据待核实";
const RESUMED: &str = "已核验复牌";

pub(super) fn advance(
    d: &Domain<'_, '_>,
    scan: &str,
    date: &str,
    symbols: BTreeSet<String>,
    events: &mut Vec<Value>,
) -> anyhow::Result<Vec<Value>> {
    let mut published = Vec::new();
    let mut closed = false;
    for symbol in symbols {
        let head = head(d, &symbol)?;
        let Some(rule) = d.verified_auction_status_at(&symbol, date, d.now)? else {
            if let Some(head) = head.filter(|head| head["active"] == true) {
                expire_notice(d, &head, UNCONFIRMED, events)?;
                closed = true;
            }
            continue;
        };
        if rule["tradingStatus"] == "trading" {
            if let Some(head) = head.filter(|head| head["active"] == true) {
                close(d, &symbol, head, &rule, RESUMED, events)?;
                closed = true;
            }
            continue;
        }
        if let Some(head) = head {
            let active = head["active"] == true;
            let episode = d
                .get(
                    EPISODES,
                    head["episode"].as_str().context("suspension episode ID")?,
                )?
                .context("suspension episode")?;
            match official_episode_order(&episode["rule"], &rule) {
                Some(Ordering::Less) => continue,
                Some(Ordering::Equal) if !active => continue,
                Some(Ordering::Greater) if active => {
                    close(d, &symbol, head, &rule, "已核验新的停牌事件", events)?;
                    closed = true;
                }
                _ if active => {
                    refresh(d, head, episode, &rule, events)?;
                    continue;
                }
                _ => {}
            }
        }
        let Some(position) = d
            .get("positions", &symbol)?
            .filter(|position| position["qty"].as_u64().is_some_and(|qty| qty > 0))
        else {
            continue;
        };
        let task_id = id();
        let episode = id();
        let task = json!({
            "taskId":task_id, "scanId":scan, "sourceEventKey":episode, "riskEventKey":episode, "eventRevision":1,
            "symbol":symbol, "name":position["name"], "direction":"risk", "level":"critical", "status":"pending",
            "version":1, "actionVersion":1, "score":null, "confidence":"high", "degraded":false,
            "dataAsOf":rule["sourceTime"], "strategies":[], "reasons":reasons(&rule)?,
            "stopPrice":null, "targetPrice":null, "entryZone":null, "createdAt":d.now,
            "validUntil":d.now+1_800_000, "snoozeUntil":null, "nextReminderAt":null,
            "remindedCount":1, "lastRemindedAt":d.now, "levelReason":"交易规则证据确认停牌",
            "currentReminder":{"reminderId":id(),"scheduledAt":d.now,"displayUntil":d.now+60_000}
        });
        store_new_task(d, &task)?;
        d.put(
            HEADS,
            &symbol,
            &json!({"epoch":d.epoch,"active":true,"episode":episode,"taskId":task_id}),
        )?;
        d.put(
            EPISODES,
            &episode,
            &json!({"rule":rule,"openedAt":d.now,"closedAt":null}),
        )?;
        published.push(task["taskId"].clone());
        events.push(event(d, "task_update", task.clone())?);
        events.push(event(d, "reminder", task)?);
    }
    if closed {
        crate::reminders::retire(d.tx, d.now)?;
    }
    Ok(published)
}

fn reasons(rule: &Value) -> anyhow::Result<Value> {
    let official = Domain::official_suspension_ref(rule);
    let sources = rule["sources"]
        .as_array()
        .context("verified rule sources")?
        .iter()
        .filter(|source| official.is_none_or(|reference| source["ref"] == reference))
        .collect::<Vec<_>>();
    let evidence = sources.iter().map(|source| json!({
            "type":"trading_suspension","ruleId":rule["ruleSnapshotId"],"ruleVersion":rule["ruleVersion"],
            "source":source["name"],"sourceTime":source["asOf"],"sourceRef":source["ref"],
        })).collect::<Vec<_>>();
    Ok(json!({
        "quant":"当前交易日的交易规则证据确认停牌。保留持仓，待核实复牌及可卖状态。",
        "ai":null,"verificationStatus":"verified","sources":sources, "riskEvidence":evidence,
        "risks":["停牌期间该证券的竞价交易受限。"],
    }))
}

fn active_task(d: &Domain<'_, '_>, head: &Value) -> anyhow::Result<Option<Value>> {
    let id = head["taskId"].as_str().context("suspension task ID")?;
    Ok(d.get("tasks", id)?.filter(|task| {
        matches!(task["status"].as_str(), Some("pending" | "reminded"))
            && task["validUntil"]
                .as_u64()
                .is_some_and(|until| until > d.now)
    }))
}

fn official_episode_order(previous: &Value, current: &Value) -> Option<Ordering> {
    Some(Domain::official_suspension_ref(current)?.cmp(Domain::official_suspension_ref(previous)?))
}

fn refresh(
    d: &Domain<'_, '_>,
    head: Value,
    mut episode: Value,
    rule: &Value,
    events: &mut Vec<Value>,
) -> anyhow::Result<()> {
    let key = head["episode"].as_str().context("suspension episode ID")?;
    if episode["rule"] == *rule {
        return Ok(());
    }
    episode["rule"] = rule.clone();
    d.put(EPISODES, key, &episode)?;
    if let Some(mut task) = active_task(d, &head)? {
        task["reasons"] = reasons(rule)?;
        task["dataAsOf"] = rule["sourceTime"].clone();
        task["version"] = json!(task["version"].as_u64().context("task version")? + 1);
        save_task(d, task, events)?;
    }
    Ok(())
}

fn close(
    d: &Domain<'_, '_>,
    symbol: &str,
    mut head: Value,
    rule: &Value,
    reason: &str,
    events: &mut Vec<Value>,
) -> anyhow::Result<()> {
    let key = head["episode"].as_str().context("suspension episode ID")?;
    let mut episode = d.get(EPISODES, key)?.context("suspension episode")?;
    episode["closedAt"] = json!(d.now);
    episode["closureRule"] = rule.clone();
    d.put(EPISODES, key, &episode)?;
    head["active"] = json!(false);
    d.put(HEADS, symbol, &head)?;
    expire_notice(d, &head, reason, events)
}

fn expire_notice(
    d: &Domain<'_, '_>,
    head: &Value,
    reason: &str,
    events: &mut Vec<Value>,
) -> anyhow::Result<()> {
    if let Some(mut task) = active_task(d, head)? {
        task["status"] = json!("expired");
        task["snoozeUntil"] = Value::Null;
        task["nextReminderAt"] = Value::Null;
        task["currentReminder"] = Value::Null;
        task["version"] = json!(task["version"].as_u64().context("task version")? + 1);
        task["actionVersion"] = json!(
            task["actionVersion"]
                .as_u64()
                .context("task action version")?
                + 1
        );
        let action_id = id();
        let task_id = task["taskId"].as_str().context("suspension task ID")?;
        let action = json!({"actionId":action_id,"taskId":task_id,"type":"expired","at":d.now,
            "clientMsgId":null,"reason":reason,"taskVersion":task["version"],
            "actionVersion":task["actionVersion"]});
        d.tx.execute(
            "INSERT INTO task_actions VALUES (?1,?2,?3,?4)",
            params![action_id, task_id, d.now, action.to_string()],
        )?;
        save_task(d, task, events)?;
    }
    Ok(())
}

fn save_task(d: &Domain<'_, '_>, task: Value, events: &mut Vec<Value>) -> anyhow::Result<()> {
    validate_definition("Task", &task)?;
    d.put(
        "tasks",
        task["taskId"].as_str().context("suspension task ID")?,
        &task,
    )?;
    d.bump()?;
    events.push(event(d, "task_update", task)?);
    Ok(())
}
