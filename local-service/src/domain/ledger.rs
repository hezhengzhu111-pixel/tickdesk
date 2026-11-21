use super::*;
use std::collections::BTreeSet;

mod manual_review;
mod positions;
mod review;
mod rules;

const SAFE_QTY: u64 = 9_007_199_254_740_991;

impl Domain<'_, '_> {
    pub(super) fn adjust(&self) -> anyhow::Result<Outcome> {
        let data = self.data;
        let symbol = str_at(data, "symbol");
        let revision = self.position_revision()?;
        if data["basePositionRevision"] != revision {
            return Ok(Err((
                "POSITION_VERSION_CONFLICT",
                json!({"currentRevision":revision}),
            )));
        }
        let before = self.get("positions", symbol)?;
        let as_of = int(data, "asOf");
        let qty = int(data, "qty");
        if as_of > self.now
            || before.as_ref().is_some_and(|p| int(p, "asOf") > as_of)
            || (qty == 0 && data["confirmZero"] != true)
            || str_at(data, "reason").trim().is_empty()
        {
            return Ok(failure("INVALID_PARAMS"));
        }
        let available = data["availableQty"].as_u64();
        if available.is_some_and(|n| n > qty) {
            return Ok(failure("INVALID_SELLABLE_QTY"));
        }
        if let Some(lots) = data["pendingLots"].as_array() {
            let sum = lots
                .iter()
                .try_fold(0_u64, |sum, lot| sum.checked_add(int(lot, "qty")));
            if available.is_none_or(|n| sum != Some(qty - n)) {
                return Ok(failure("INVALID_SELLABLE_QTY"));
            }
        }
        let mut resolved = Vec::new();
        let mut ids = BTreeSet::new();
        for entry in data["resolvedExecutionIds"].as_array().unwrap() {
            let key = entry.as_str().unwrap();
            let Some(mut execution) = self.get("executions", key)? else {
                return Ok(failure("INVALID_PARAMS"));
            };
            if !ids.insert(key)
                || execution["symbol"] != symbol
                || execution["positionEffect"] != "needs_reconciliation"
                || int(&execution, "executedAt") > as_of
            {
                return Ok(failure("INVALID_PARAMS"));
            }
            execution["positionEffect"] = json!("reconciled");
            execution["reconciliationRequired"] = json!(false);
            resolved.push((key, execution));
        }
        for (key, execution) in resolved {
            self.put("executions", key, &execution)?;
        }
        let unresolved = self.unresolved(symbol)?;
        let mut preserved = None;
        let mut pending_lots = &data["pendingLots"];
        if data["cmd"] == "adjust_position"
            && qty > 0
            && ids.is_empty()
            && pending_lots.is_null()
            && !unresolved
        {
            if let Some(position) = before.as_ref().filter(|position| {
                position["qty"] == data["qty"] && position["availableQty"] == data["availableQty"]
            }) {
                preserved = self.reconciled_holding_to_keep(symbol, position)?;
                if preserved.is_some() {
                    pending_lots = &position["pendingLots"];
                }
            }
        }
        let rev = self.bump_position()?;
        let name = data["name"]
            .as_str()
            .or_else(|| before.as_ref().map(|pos| str_at(pos, "name")))
            .unwrap_or(symbol);
        let after = json!({"symbol":symbol,"name":name,"qty":qty,"availableQty":data["availableQty"],"cost":data["cost"],
            "pendingLots":pending_lots,"asOf":as_of,"positionRevision":rev,
            "reconciliationRequired":unresolved,"lastPrice":null,"priceAsOf":null,"priceStatus":"missing",
            "marketValue":null,"unrealizedPnl":null,"unrealizedPnlRatio":null});
        let key = id();
        let adjustment = json!({"adjustmentId":key,"symbol":symbol,"asOf":as_of,"before":before,"after":after,"reason":data["reason"],
            "resolvedExecutionIds":data["resolvedExecutionIds"],"createdAt":self.now});
        self.put("positions", symbol, &after)?;
        if qty == 0
            && before
                .as_ref()
                .is_some_and(|position| position["qty"].as_u64().is_some_and(|qty| qty > 0))
        {
            crate::quant_observations::close_holding_events(self, symbol, as_of)?;
        }
        if let Some(holding) = preserved {
            self.save_holding(symbol, holding, &after)?;
        } else {
            self.confirm_holding(symbol, &after)?;
        }
        self.put("adjustments", &key, &adjustment)?;
        Ok(Ok(
            json!({"adjustment":adjustment,"snapshotSeq":self.bump()?,"positionRevision":rev}),
        ))
    }

    fn unresolved(&self, symbol: &str) -> anyhow::Result<bool> {
        Ok(self.tx.query_row("SELECT EXISTS(SELECT 1 FROM objects WHERE collection='executions'
            AND json_extract(body,'$.symbol')=?1 AND json_extract(body,'$.reconciliationRequired')=1)",
            [symbol], |r|r.get(0))?)
    }

    pub(super) fn record_execution(&self) -> anyhow::Result<Outcome> {
        self.record(self.data, str_at(self.data, "symbol"))
    }

    pub(super) fn record(&self, data: &Value, symbol: &str) -> anyhow::Result<Outcome> {
        self.record_inner(data, symbol, None)
    }

    pub(super) fn record_recovered(
        &self,
        data: &Value,
        symbol: &str,
        new_id: &str,
    ) -> anyhow::Result<Outcome> {
        self.record_inner(data, symbol, Some(new_id))
    }

    fn record_inner(
        &self,
        data: &Value,
        symbol: &str,
        recovery_id: Option<&str>,
    ) -> anyhow::Result<Outcome> {
        let executed_at = int(data, "executedAt");
        let qty = int(data, "qty");
        let side = str_at(data, "side");
        let Some(date) = shanghai_date(executed_at) else {
            return Ok(failure("INVALID_PARAMS"));
        };
        if executed_at > self.now {
            return Ok(failure("INVALID_PARAMS"));
        }
        if let Some(task_id) = data["taskId"].as_str() {
            let Some(task) = self.get("tasks", task_id)? else {
                return Ok(failure("TASK_NOT_FOUND"));
            };
            if task["symbol"] != symbol
                || (task["direction"] != "risk" && task["direction"] != side)
            {
                return Ok(failure("INVALID_PARAMS"));
            }
        }
        let supersedes = data["supersedesExecutionId"].as_str();
        let mut superseded = if let Some(old) = supersedes {
            let Some(execution) = self.get("executions", old)? else {
                return Ok(failure("INVALID_PARAMS"));
            };
            if execution["positionEffect"] == "superseded" || execution["symbol"] != symbol {
                return Ok(failure("INVALID_PARAMS"));
            }
            Some(execution)
        } else {
            None
        };
        let similar = self.list("executions")?.into_iter().any(|item| {
            item["positionEffect"] != "superseded"
                && item["symbol"] == symbol
                && item["executionId"].as_str() != supersedes
                && item["side"] == side
                && item["qty"] == qty
                && item["executedAt"] == executed_at
                && decimal_key(str_at(&item, "price")) == decimal_key(str_at(data, "price"))
        });
        if similar && data["duplicateConfirmed"] != true {
            return Ok(failure("POSSIBLE_DUPLICATE"));
        }
        let trade_date = date.to_string();
        let rule = self.holding_rule(symbol, &trade_date, data["channel"].as_str())?;
        let settlement = rule.as_ref().and_then(|rule| rule.settlement(executed_at));
        // Whole-session suspension evidence available by the time of the
        // declaration can contradict an earlier actual fill. Do not use it
        // as point-in-time settlement evidence or apply it to another channel.
        let confirmed_auction_suspension = self
            .verified_auction_status_at(symbol, &trade_date, self.now)?
            .filter(|status| Self::official_suspension_ref(status).is_some());
        let channel_needs_review =
            confirmed_auction_suspension.is_some() && data["channel"].is_null();
        let suspension = confirmed_auction_suspension.filter(|_| data["channel"] == "auction");
        let conflict = if suspension.is_some() {
            Some("EXECUTION_DURING_SUSPENSION")
        } else {
            rule.as_ref()
                .and_then(|rule| rule.conflict(data, executed_at))
        };
        let mut position = self.get("positions", symbol)?;
        let mut applied = false;
        let mut sellable = None;
        let mut holding = None;
        let mut warnings = Vec::new();
        if let Some(code) = conflict {
            warnings.push(json!({"code":code,"message":"实际成交与当日规则证据存在差异。","fields":[],"symbol":symbol}));
        } else if channel_needs_review {
            warnings.push(json!({"code":"EXECUTION_CHANNEL_UNCONFIRMED","message":"当日竞价停牌，请核对实际成交渠道。","fields":["channel"],"symbol":symbol}));
        } else if settlement.is_none() {
            warnings.push(
                json!({"code":"RULE_DATA_MISSING","message":"未取得该标的当日完整规则快照。",
                "fields":[],"symbol":symbol}),
            );
        }
        if let Some(current) = position.as_mut() {
            if recovery_id.is_none()
                && supersedes.is_none()
                && conflict.is_none()
                && !channel_needs_review
                && !similar
                && executed_at >= int(current, "asOf")
                && !self.unresolved(symbol)?
            {
                holding = self.load_holding(symbol, current)?.and_then(|mut state| {
                    state
                        .recheck(&trade_date, executed_at, rule.as_ref())
                        .then_some(state)
                });
                if let Some(state) = holding.as_mut() {
                    if side == "sell" && int(current, "qty") >= qty && state.sell(qty) {
                        current["qty"] = json!(int(current, "qty") - qty);
                        applied = true;
                    } else if side == "buy" && qty + int(current, "qty") <= SAFE_QTY {
                        let effective = match settlement {
                            Some("T+0") => Some(trade_date.as_str()),
                            Some("T+1") => rule.as_ref().and_then(|r| r.next_session(&trade_date)),
                            _ => None,
                        };
                        let cost = weighted_cost(
                            str_at(current, "cost"),
                            int(current, "qty"),
                            str_at(data, "price"),
                            qty,
                        );
                        if let (Some(effective), Some(cost), Some(rule)) = (effective, cost, &rule)
                        {
                            if state.add_buy(
                                qty,
                                &trade_date,
                                effective,
                                str_at(&rule.rule, "ruleSnapshotId"),
                            ) {
                                current["qty"] = json!(int(current, "qty") + qty);
                                current["cost"] = json!(cost);
                                sellable = Some(effective);
                                applied = true;
                            }
                        }
                    }
                }
            }
        }
        if !applied {
            warnings.push(json!({"code":"POSITION_RECONCILIATION_REQUIRED","message":"成交已留存，持仓数量需按确认快照对账。",
                "fields":[],"symbol":symbol}));
        }
        if let Some(ref mut previous) = superseded {
            previous["positionEffect"] = json!("superseded");
            previous["reconciliationRequired"] = json!(false);
            self.put("executions", supersedes.unwrap(), previous)?;
        }
        if let Some(ref mut current) = position {
            if applied || current["reconciliationRequired"] == false {
                if let Some(state) = holding.filter(|_| applied) {
                    current["asOf"] = json!(executed_at);
                    state.project(current);
                    current["positionRevision"] = json!(self.bump_position()?);
                    self.save_holding_after_fill(symbol, state, current)?;
                } else {
                    current["positionRevision"] = json!(self.bump_position()?);
                }
                current["reconciliationRequired"] = json!(!applied);
                self.put("positions", symbol, current)?;
                if applied && side == "sell" && current["qty"] == 0 {
                    crate::quant_observations::close_holding_events(self, symbol, executed_at)?;
                }
            }
        }
        let key = recovery_id.map(str::to_owned).unwrap_or_else(id);
        let rule_snapshot = suspension
            .as_ref()
            .or_else(|| rule.as_ref().map(|r| &r.rule));
        let rule_status = if conflict.is_some() {
            "conflict"
        } else if settlement.is_some() {
            "verified"
        } else if rule_snapshot.is_some() {
            "partial"
        } else {
            "unverified"
        };
        let declared_settlement = data["settlement"].as_str();
        let usable_settlement = settlement.filter(|_| conflict.is_none());
        let execution = json!({"executionId":key,"taskId":data["taskId"],"symbol":symbol,"side":side,
            "price":data["price"],"qty":qty,"executedAt":executed_at,"channel":data["channel"],
            "priceBasis":data["priceBasis"].as_str().unwrap_or("unknown"),"orderId":data["orderId"],"orderQty":data["orderQty"],
            "positionEffect":if applied {"applied"} else {"needs_reconciliation"},"reconciliationRequired":!applied,
            "ruleStatus":rule_status,"ruleSnapshotId":rule_snapshot.map(|r| &r["ruleSnapshotId"]),"sellableDate":sellable,
            "realizedPnl":null,"warnings":warnings,"supersedesExecutionId":data["supersedesExecutionId"],
            "fees":data["fees"],"feeSource":if data["fees"].is_string() {"user"} else {"unknown"},
            "createdAt":self.now,"note":data["note"].as_str().unwrap_or(""),"tradeDate":trade_date,
            "settlement":declared_settlement.or(usable_settlement),"settlementSource":if usable_settlement.is_some() {"snapshot"}
                else if declared_settlement.is_some() {"user"} else {"unknown"},
            "verificationScope":if rule_snapshot.is_some() {"recorded_execution_and_available_rule_data"} else {"recorded_execution_only"},
            "ruleSnapshot":rule_snapshot});
        self.put("executions", &key, &execution)?;
        Ok(Ok(
            json!({"execution":execution,"position":position,"snapshotSeq":self.bump()?}),
        ))
    }
}

fn shanghai_date(at: u64) -> Option<chrono::NaiveDate> {
    chrono::DateTime::from_timestamp_millis(at.try_into().ok()?)
        .map(|ts| ts.with_timezone(&chrono_tz::Asia::Shanghai).date_naive())
}

fn decimal_key(s: &str) -> &str {
    if s.contains('.') {
        s.trim_end_matches('0').trim_end_matches('.')
    } else {
        s
    }
}

fn weighted_cost(cost: &str, previous: u64, price: &str, qty: u64) -> Option<String> {
    use num_bigint::BigUint;
    let parts = |s: &str| {
        let places = s.split_once('.').map_or(0, |(_, fraction)| fraction.len());
        (
            s.replace('.', "")
                .parse::<BigUint>()
                .expect("validated decimal"),
            places,
        )
    };
    let (cost, cost_places) = parts(cost);
    let (price, price_places) = parts(price);
    let mut places = cost_places.max(price_places);
    let ten = BigUint::from(10_u32);
    let mut scaled = cost * previous * ten.pow((places - cost_places) as u32)
        + price * qty * ten.pow((places - price_places) as u32);
    let divisor = previous + qty;
    while &scaled % divisor != BigUint::ZERO {
        if places >= 46 {
            return None;
        }
        scaled *= &ten;
        places += 1;
    }
    let digits = (scaled / divisor).to_string();
    let output = if places == 0 {
        digits
    } else {
        let padded = format!("{digits:0>width$}", width = places + 1);
        let (whole, fraction) = padded.split_at(padded.len() - places);
        decimal_key(&format!("{whole}.{fraction}")).to_owned()
    };
    (output.len() <= 48).then_some(output)
}
