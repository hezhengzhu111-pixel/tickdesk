import json
from datetime import date, datetime, timedelta
from hashlib import sha256

import polars as pl

from app.quant_evidence import load_input_snapshot, read_input_evidence
from app.services.tickdesk_regime_archive import INDEX_ARCHIVE, import_calendar

CLOSE = datetime.fromisoformat("2026-11-17T15:00:00+08:00")
SYMBOL = "600000.SH"
BOUND = "listing_sessions_lower_bound"
FIELDS = {"listing_sessions", BOUND}


def _inputs(root, *, calendar_at=CLOSE - timedelta(hours=1)):
    roster = root / "tickdesk-market/listing-observations/2026-11-17.parquet"
    roster.parent.mkdir(parents=True)
    pl.DataFrame({
        "symbol": [SYMBOL], "exchange": ["SH"], "status": ["listed"], "listing_date": [date(1999, 11, 10)],
        "source": ["上海证券交易所"], "source_ref": ["https://www.sse.com.cn/assortment/stock/list/share/"],
        "as_of": [int((CLOSE - timedelta(hours=14)).timestamp() * 1000)],
    }).write_parquet(roster)
    body = json.dumps({"source": "fuyao", "observedAt": int(calendar_at.timestamp() * 1000),
                       "through": "2026-11-17", "sessions": ["2026-11-13", "2026-11-16", "2026-11-17"]}).encode()
    import_calendar(root / INDEX_ARCHIVE, body, sha256(body).hexdigest())
    return roster


def test_bounded_official_listing_history_preserves_unknown_total(tmp_path):
    _inputs(tmp_path)
    result = read_input_evidence(tmp_path, CLOSE, [SYMBOL, "600001.SH"], FIELDS)
    assert result.fact(SYMBOL, "listing_sessions").value is None
    lower = result.fact(SYMBOL, BOUND)
    assert lower.value == 3 and lower.reason is None and len(lower.revision_ids) == 2
    assert datetime.fromisoformat(lower.available_at) == CLOSE - timedelta(hours=1)
    assert result.fact("600001.SH", BOUND).value is None
    assert read_input_evidence(tmp_path, CLOSE + timedelta(days=1), [SYMBOL], FIELDS).fact(SYMBOL, BOUND).value is None


def test_calendar_after_the_decision_cannot_supply_a_historical_lower_bound(tmp_path):
    _inputs(tmp_path, calendar_at=CLOSE + timedelta(milliseconds=1))
    assert read_input_evidence(tmp_path, CLOSE, [SYMBOL], FIELDS).fact(SYMBOL, BOUND).value is None


def test_frozen_roster_does_not_follow_a_changed_file(tmp_path):
    roster = _inputs(tmp_path)
    snapshot = load_input_snapshot(tmp_path, FIELDS)
    roster.write_bytes(roster.read_bytes() + b"changed")
    result = read_input_evidence(tmp_path, CLOSE, [SYMBOL], FIELDS, snapshot=snapshot)
    assert result.fact(SYMBOL, BOUND).value is None
