use super::{failure, int, rules::RuleEvidence, shanghai_date, str_at, Domain, Outcome};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

const COLLECTION: &str = "holding_partitions";
const MAX_PARTITIONS: usize = 32;

#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub(super) struct Holding {
    epoch: String,
    position_revision: u64,
    confirmed_at: u64,
    partitions: Vec<Partition>,
    #[serde(default)]
    confirmed_available_known: bool,
    #[serde(default)]
    derived_from_fills: bool,
    #[serde(default)]
    unknown_origin: bool,
    #[serde(default)]
    broker_reconciled: bool,
}

#[derive(Debug, Deserialize, Serialize)]
struct Partition {
    qty: u64,
    restrictions: Vec<Restriction>,
    origin: Option<Origin>,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct Origin {
    group: String,
    strategy_id: String,
    code_hash: String,
    parameters_hash: String,
    dependencies_hash: String,
}

#[derive(Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(
    tag = "kind",
    rename_all = "snake_case",
    rename_all_fields = "camelCase"
)]
enum Restriction {
    Unknown,
    Freeze {
        evidence_ref: String,
    },
    Settlement {
        acquired_on: String,
        earliest_session: String,
        rule_snapshot_id: String,
    },
}

impl Domain<'_, '_> {
    pub(crate) fn reconcile_holding(&self) -> anyhow::Result<Outcome> {
        let symbol = str_at(self.data, "symbol");
        let confirmed_at = int(self.data, "asOf");
        let Some(day) = shanghai_date(confirmed_at).map(|d| d.to_string()) else {
            return Ok(failure("INVALID_PARAMS"));
        };
        let partitions: Vec<Partition> = serde_json::from_value(self.data["partitions"].clone())?;
        let total = partitions
            .iter()
            .try_fold(0u64, |sum, p| sum.checked_add(p.qty));
        if total != self.data["qty"].as_u64() {
            return Ok(failure("INVALID_SELLABLE_QTY"));
        }
        for partition in &partitions {
            let mut settlement = false;
            for restriction in &partition.restrictions {
                if let Restriction::Settlement {
                    acquired_on,
                    earliest_session,
                    rule_snapshot_id,
                } = restriction
                {
                    if settlement || acquired_on > &day || earliest_session <= &day {
                        return Ok(failure("INVALID_PARAMS"));
                    }
                    settlement = true;
                    let evidence = self.holding_rule(symbol, acquired_on, Some("auction"))?;
                    if evidence.is_none_or(|rule| {
                        rule.rule["ruleSnapshotId"] != *rule_snapshot_id
                            || rule.settlement(confirmed_at) != Some("T+1")
                            || rule.next_session(acquired_on) != Some(earliest_session.as_str())
                    }) {
                        return Ok(failure("INVALID_PARAMS"));
                    }
                }
            }
        }
        let holding = Holding {
            epoch: self.epoch.to_owned(),
            position_revision: self.position_revision()?,
            confirmed_at,
            partitions,
            confirmed_available_known: true,
            derived_from_fills: false,
            unknown_origin: false,
            broker_reconciled: true,
        };
        let mut input = self.data.clone();
        holding.project(&mut input);
        let domain = Domain {
            data: &input,
            ..*self
        };
        let result = domain.adjust()?;
        if let Ok(result) = &result {
            let key = str_at(&result["adjustment"], "adjustmentId");
            self.put(
                "holding_adjustments",
                key,
                &json!({"epoch":self.epoch,"request":self.data}),
            )?;
            self.save_holding(symbol, holding, &result["adjustment"]["after"])?;
        }
        Ok(result)
    }

    pub(crate) fn get_holding_details(&self) -> anyhow::Result<Outcome> {
        let symbol = str_at(self.data, "symbol");
        let details = self.holding_details_for(symbol)?;
        Ok(Ok(
            json!({"version":3,"serverEpoch":self.epoch,"snapshotSeq":self.seq()?,"holding":details}),
        ))
    }

    pub(crate) fn holding_details_for(&self, symbol: &str) -> anyhow::Result<Value> {
        let mut details = Value::Null;
        if let Some(position) = self.get("positions", symbol)? {
            let mut holding = self.load_holding(symbol, &position)?;
            let mut available = None;
            let mut basis = "unknown";
            if let (Some(state), Some(day)) = (holding.as_mut(), shanghai_date(self.now)) {
                let day = day.to_string();
                let rule = self.holding_rule(symbol, &day, Some("auction"))?;
                let confirmed_snapshot = !state.derived_from_fills
                    && shanghai_date(state.confirmed_at).is_some_and(|d| d.to_string() == day);
                if state.confirmed_available_known
                    && int(&position, "asOf") <= self.now
                    && (confirmed_snapshot || rule.is_some())
                    && state.recheck(&day, self.now, rule.as_ref())
                {
                    available = Some(state.available());
                    basis = if confirmed_snapshot {
                        "confirmed_snapshot"
                    } else {
                        "local_inferred"
                    };
                }
            }
            details = json!({"symbol":symbol,"qty":position["qty"],"availableQty":available,"sellableBasis":basis,
                "positionRevision":position["positionRevision"],"positionAsOf":position["asOf"],
                "snapshotAsOf":holding.as_ref().map(|state|state.confirmed_at),
                "reconciliationRequired":position["reconciliationRequired"] == true || holding.is_none(),
                "originStatus":holding.as_ref().map_or("unknown", Holding::origin_status),
                "partitions":holding.as_ref().map(|state|&state.partitions)});
        }
        Ok(details)
    }

    pub(super) fn confirm_holding(&self, symbol: &str, position: &Value) -> anyhow::Result<()> {
        self.save_holding(symbol, Holding::confirmed(self.epoch, position), position)
    }

    pub(super) fn reconciled_holding_to_keep(
        &self,
        symbol: &str,
        position: &Value,
    ) -> anyhow::Result<Option<Holding>> {
        if self.recovering || position["reconciliationRequired"] != false {
            return Ok(None);
        }
        let Some(body) = self.get(COLLECTION, symbol)? else {
            return Ok(None);
        };
        let mut holding: Holding = serde_json::from_value(body)?;
        let total = holding
            .partitions
            .iter()
            .try_fold(0u64, |sum, p| sum.checked_add(p.qty));
        if holding.epoch != self.epoch
            || position["positionRevision"] != holding.position_revision
            || holding.confirmed_at > int(position, "asOf")
            || total != position["qty"].as_u64()
            || holding
                .confirmed_available_known
                .then(|| holding.available())
                != position["availableQty"].as_u64()
        {
            return Ok(None);
        }
        if !holding.broker_reconciled {
            // Identify a pre-upgrade snapshot from the existing v3 audit. The latest
            // adjustment at this basis time must itself be the broker reconcile.
            holding.broker_reconciled = self.tx.query_row(
                "WITH original AS (SELECT object_id FROM objects WHERE collection='adjustments' \
                    AND json_extract(body,'$.symbol')=?1 AND json_extract(body,'$.asOf')=?2 \
                    AND json_extract(body,'$.after.positionRevision')<=?3 \
                    ORDER BY json_extract(body,'$.after.positionRevision') DESC LIMIT 1) \
                 SELECT EXISTS(SELECT 1 FROM objects JOIN original USING(object_id) \
                    WHERE collection='holding_adjustments' AND json_extract(body,'$.epoch')=?4)",
                super::params![
                    symbol,
                    holding.confirmed_at,
                    holding.position_revision,
                    self.epoch
                ],
                |row| row.get(0),
            )?;
        }
        Ok(holding.broker_reconciled.then_some(holding))
    }

    pub(super) fn load_holding(
        &self,
        symbol: &str,
        position: &Value,
    ) -> anyhow::Result<Option<Holding>> {
        if position["reconciliationRequired"] == true || self.recovering {
            return Ok(None);
        }
        if let Some(body) = self.get(COLLECTION, symbol)? {
            let holding: Holding = serde_json::from_value(body)?;
            if holding.epoch != self.epoch {
                return Ok(None);
            }
            if position["positionRevision"] == holding.position_revision {
                return Ok(Some(holding));
            }
        }
        let restored: bool = self.tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM metadata WHERE key='restored_backup_cutoff')",
            [],
            |row| row.get(0),
        )?;
        if restored {
            return Ok(None);
        }
        let revision = int(position, "positionRevision");
        let confirmed: bool = self.tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM objects WHERE collection='adjustments' \
             AND json_extract(body,'$.symbol')=?1 AND json_extract(body,'$.after.positionRevision')=?2)",
            super::params![symbol, revision],
            |row| row.get(0),
        )?;
        let mut state = Holding::confirmed(self.epoch, position);
        state.derived_from_fills = !confirmed;
        Ok(Some(state))
    }

    pub(super) fn save_holding_after_fill(
        &self,
        symbol: &str,
        mut holding: Holding,
        position: &Value,
    ) -> anyhow::Result<()> {
        holding.derived_from_fills = true;
        self.save_holding(symbol, holding, position)
    }

    pub(super) fn save_holding(
        &self,
        symbol: &str,
        mut holding: Holding,
        position: &Value,
    ) -> anyhow::Result<()> {
        holding.position_revision = super::int(position, "positionRevision");
        self.put(COLLECTION, symbol, &serde_json::to_value(holding)?)
    }
}

impl Holding {
    fn confirmed(epoch: &str, position: &Value) -> Self {
        let qty = super::int(position, "qty");
        let available = position["availableQty"].as_u64().unwrap_or(0);
        let mut holding = Self {
            epoch: epoch.to_owned(),
            position_revision: super::int(position, "positionRevision"),
            confirmed_at: super::int(position, "asOf"),
            partitions: vec![],
            confirmed_available_known: position["availableQty"].is_u64(),
            derived_from_fills: false,
            unknown_origin: false,
            broker_reconciled: false,
        };
        holding.add(available, None);
        // Legacy snapshots may describe a remainder with a sellable date, but
        // cannot attest that the same shares carry no other restrictions.
        holding.add(qty.saturating_sub(available), Some(Restriction::Unknown));
        holding
    }

    pub(super) fn add_buy(
        &mut self,
        qty: u64,
        acquired_on: &str,
        earliest_session: &str,
        rule_snapshot_id: &str,
    ) -> bool {
        let restriction = (acquired_on != earliest_session).then(|| Restriction::Settlement {
            acquired_on: acquired_on.to_owned(),
            earliest_session: earliest_session.to_owned(),
            rule_snapshot_id: rule_snapshot_id.to_owned(),
        });
        self.add(qty, restriction)
    }

    fn add(&mut self, qty: u64, restriction: Option<Restriction>) -> bool {
        if qty == 0 {
            return true;
        }
        let restrictions: Vec<_> = restriction.into_iter().collect();
        if let Some(p) = self
            .partitions
            .iter_mut()
            .find(|p| p.restrictions == restrictions && p.origin.is_none())
        {
            p.qty += qty;
        } else if self.partitions.len() == MAX_PARTITIONS {
            return false;
        } else {
            self.partitions.push(Partition {
                qty,
                restrictions,
                origin: None,
            });
        }
        true
    }

    pub(super) fn recheck(
        &mut self,
        trade_date: &str,
        at: u64,
        rule: Option<&RuleEvidence>,
    ) -> bool {
        if at < self.confirmed_at {
            return false;
        }
        let same_date =
            shanghai_date(self.confirmed_at).is_some_and(|d| d.to_string() == trade_date);
        if same_date {
            return rule.is_none_or(|rule| rule.session_rechecked(trade_date, at));
        }
        let Some(rule) = rule else {
            return false;
        };
        if !rule.session_rechecked(trade_date, at)
            || !rule.actions_checked(trade_date)
            || self
                .partitions
                .iter()
                .any(|p| p.restrictions.contains(&Restriction::Unknown))
        {
            return false;
        }
        for partition in &mut self.partitions {
            partition
                .restrictions
                .retain(|restriction| match restriction {
                    Restriction::Settlement {
                        acquired_on,
                        earliest_session,
                        ..
                    } => {
                        trade_date < earliest_session.as_str()
                            || rule
                                .next_session(acquired_on)
                                .is_none_or(|next| trade_date < next)
                    }
                    Restriction::Unknown | Restriction::Freeze { .. } => true,
                });
        }
        true
    }

    pub(super) fn sell(&mut self, qty: u64) -> bool {
        let available = self.available();
        if !self.confirmed_available_known || qty > available {
            return false;
        }
        let mut available_partitions = self.partitions.iter().filter(|p| p.restrictions.is_empty());
        let first = available_partitions.next().and_then(|p| p.origin.clone());
        let ambiguous = qty < available && available_partitions.any(|p| p.origin != first);
        self.partitions.retain(|p| !p.restrictions.is_empty());
        if ambiguous {
            self.unknown_origin = true;
            for partition in &mut self.partitions {
                partition.origin = None;
            }
        }
        if qty < available {
            self.partitions.push(Partition {
                qty: available - qty,
                restrictions: vec![],
                origin: if ambiguous { None } else { first },
            });
        }
        if self.partitions.is_empty() {
            self.unknown_origin = false;
        }
        true
    }

    fn origin_status(&self) -> &'static str {
        if self.unknown_origin && !self.partitions.is_empty() {
            return "unknown";
        }
        let any = self.partitions.iter().any(|p| p.origin.is_some());
        let all = self.partitions.iter().all(|p| p.origin.is_some());
        match (any, all) {
            (true, true) => "assigned",
            (true, false) => "partial",
            _ => "unallocated",
        }
    }

    fn available(&self) -> u64 {
        self.partitions
            .iter()
            .filter(|p| p.restrictions.is_empty())
            .map(|p| p.qty)
            .sum()
    }

    pub(super) fn project(&self, position: &mut Value) {
        position["availableQty"] = json!(self.confirmed_available_known.then(|| self.available()));
        if !self.confirmed_available_known {
            position["pendingLots"] = Value::Null;
            return;
        }
        let mut lots = vec![];
        for partition in &self.partitions {
            match partition.restrictions.as_slice() {
                [] => {}
                [Restriction::Settlement {
                    earliest_session, ..
                }] => {
                    lots.push(json!({"qty":partition.qty,"sellableDate":earliest_session}));
                }
                _ => {
                    position["pendingLots"] = Value::Null;
                    return;
                }
            }
        }
        position["pendingLots"] = json!(lots);
    }
}
