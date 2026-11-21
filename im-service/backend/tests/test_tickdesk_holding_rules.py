from datetime import datetime

import pytest

from app.tickflow.repository import DataStore, KlineRepository

SYMBOL = "000001.SZ"
DAY = "2026-01-02"
SOURCE = "synthetic-holding-rule-fixture"


def _ms(day, clock):
    return int(datetime.fromisoformat(f"{day}T{clock}+08:00").timestamp() * 1000)


def holding_evidence_row(symbol=SYMBOL, day=DAY, *, available_clock="08:00:00", settlement="T+1"):
    calendar_at = _ms(DAY, "07:30:00")
    event_at = _ms(day, "07:30:00")
    return {"field": "holding_rules_v3", "symbol": symbol, "effective_date": day,
            "source": SOURCE, "available_at": f"{day}T{available_clock}+08:00", "value": [{
        "rule": {
            "ruleSnapshotId": f"synthetic-rule-{symbol[:6]}-{day}", "symbol": symbol,
            "tradeDate": day, "channel": "auction", "exchange": "SZSE", "instrumentType": "stock",
            "board": "main", "dayTrading": "NO", "settlement": settlement,
            "quantityUnit": 100, "maxOrderQty": 100000, "minPriceTick": "0.01",
            "limitRatio": "0.2", "adjustedPrevClose": "10", "referencePrice": "10",
            "upperLimit": "12", "lowerLimit": "8", "riskStatus": "normal",
            "tradingStatus": "trading", "ruleVersion": "synthetic-2026-01",
            "sources": [{"name": SOURCE, "ref": "fixture:effective-rules", "asOf": event_at}],
            "sourceTime": event_at, "dataStatus": "complete", "issueFields": [],
        },
        "calendar": {
            "revisionId": "synthetic-calendar-january", "availableAtMs": calendar_at,
            "source": {"name": SOURCE, "ref": "fixture:calendar", "asOf": calendar_at},
            "coverageStart": DAY, "coverageEnd": "2026-01-05", "sessions": [
                {"date": DAY, "firstExecutionAtMs": _ms(DAY, "09:15:00")},
                {"date": "2026-01-05", "firstExecutionAtMs": _ms("2026-01-05", "09:15:00")},
            ],
        },
        "companyActionsCheckedThrough": day,
    }]}


@pytest.fixture
def repo(tmp_path):
    store = DataStore.readonly(tmp_path)
    try:
        yield KlineRepository(store)
    finally:
        store.db.close()


def request_at(day, clock, symbol=SYMBOL):
    return {"requestedAt": _ms(day, clock), "objects": [{"symbol": symbol, "qty": 1000}]}


def test_holding_rule_batch_uses_archived_exact_availability_and_does_not_use_old_day(repo):
    from app.services.tickdesk_holding_rules import prepare_holding_rule_batch

    row = holding_evidence_row()
    repo.write_quant_reference_evidence([row])
    assert prepare_holding_rule_batch(repo, request_at(DAY, "07:59:59")) is None
    batch = prepare_holding_rule_batch(repo, request_at(DAY, "09:30:00"))
    expected_rule = {**row["value"][0], "ruleAvailableAtMs": _ms(DAY, "08:00:00")}
    assert batch == {"version": 3, "rules": [expected_rule]}
    assert batch["rules"][0]["rule"]["sourceTime"] == _ms(DAY, "07:30:00")
    assert prepare_holding_rule_batch(repo, request_at("2026-01-05", "09:30:00")) is None


def test_conflicting_current_rules_only_hide_affected_holding(repo):
    from app.services.tickdesk_holding_rules import prepare_holding_rule_batch

    other = "000002.SZ"
    original = holding_evidence_row()
    revision = holding_evidence_row()
    revision["value"][0]["rule"]["lowerLimit"] = "9"
    repo.write_quant_reference_evidence([original, revision, holding_evidence_row(other)])
    request = request_at(DAY, "09:30:00")
    request["objects"].extend([{"symbol": other, "qty": 100}, {"symbol": "000003.SZ"}])
    batch = prepare_holding_rule_batch(repo, request)
    assert [item["rule"]["symbol"] for item in batch["rules"]] == [other]
    assert batch["unavailable"] == [{"symbol": SYMBOL, "tradeDate": DAY,
                                    "availableAtMs": _ms(DAY, "08:00:00"), "reason": "conflict"}]


def test_explicit_withdrawal_is_visible_only_after_its_archive_clock(repo):
    from app.services.tickdesk_holding_rules import prepare_holding_rule_batch

    rule = holding_evidence_row()
    withdrawal = {**rule, "available_at": f"{DAY}T10:00:00+08:00", "value": None}
    repo.write_quant_reference_evidence([rule, withdrawal])
    before = prepare_holding_rule_batch(repo, request_at(DAY, "09:30:00"))
    assert len(before["rules"]) == 1
    after = prepare_holding_rule_batch(repo, request_at(DAY, "10:00:00"))
    assert after == {"version": 3, "rules": [], "unavailable": [
        {"symbol": SYMBOL, "tradeDate": DAY, "availableAtMs": _ms(DAY, "10:00:00"),
         "reason": "withdrawn"},
    ]}
    assert prepare_holding_rule_batch(repo, request_at("2026-01-05", "10:00:00")) is None


@pytest.mark.parametrize("reason", ["conflict", "withdrawn"])
@pytest.mark.parametrize("other_source", ["SZSE-official-holding-v1", SOURCE])
def test_current_uncertainty_limits_only_official_partial_sources_to_auction(repo, reason, other_source):
    from app.services.tickdesk_holding_rules import prepare_holding_rule_batch

    first = {**holding_evidence_row(), "source": "SZSE-official-holding-v1"}
    second = {**holding_evidence_row(), "source": other_source}
    if reason == "withdrawn":
        first["value"] = second["value"] = None
    else:
        first["value"][0]["rule"].update(dataStatus="missing", tradingStatus="unknown", issueFields=["tradingStatus"])
        second["value"][0]["rule"]["lowerLimit"] = "9"
    repo.write_quant_reference_evidence([first, second])
    actual, = prepare_holding_rule_batch(repo, request_at(DAY, "09:30:00"))["unavailable"]
    expected = {"symbol": SYMBOL, "tradeDate": DAY, "availableAtMs": _ms(DAY, "08:00:00"), "reason": reason}
    if other_source != SOURCE:
        expected["scope"] = "auctionStatus"
    assert actual == expected


def test_malformed_rule_witness_cannot_be_written_into_archive(repo):
    row = holding_evidence_row()
    row["value"][0]["calendar"]["sessions"][0]["firstExecutionAtMs"] = _ms("2026-01-05", "09:15:00")
    with pytest.raises(ValueError, match="session"):
        repo.write_quant_reference_evidence([row])
