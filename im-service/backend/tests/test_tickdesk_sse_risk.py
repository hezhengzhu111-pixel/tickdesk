import json
import shutil
from datetime import datetime, timedelta
from hashlib import sha256

import httpx
import polars as pl
import pytest

from app.quant_evidence import load_input_snapshot, read_input_evidence

DAY = "2026-11-17"
RECEIVED = datetime.fromisoformat(f"{DAY}T14:45:00+08:00")
ROOT = "https://query.sse.com.cn/commonSoaQuery.do"
ROSTER = "https://www.sse.com.cn/assortment/stock/list/share/"
MAIN_ID = "PL_SSGSXX_FXJSBGPLB"
STAR_ID = "SSE_PL_SSGSXX_KCBFXTS_L"


def _board(query, rows, *, total=None):
    return {"sqlId": query, "actionErrors": [], "fieldErrors": {}, "isPagination": "false", "result": rows,
            "pageHelp": {"pageNo": 1, "pageCount": int(bool(rows)), "data": rows,
                         "total": len(rows) if total is None else total}}


def _snapshot(root, *, main_total=None, board_conflict=False):
    archive = root / "tickdesk-market/sse-risk-snapshots" / DAY
    archive.mkdir(parents=True)
    roster = archive / "active-roster.parquet"
    local_listing_ms = int((RECEIVED - timedelta(hours=4)).timestamp() * 1000)
    pl.DataFrame({
        "symbol": ["600000.SH", "600053.SH", "688201.SH"], "exchange": ["SH"] * 3,
        "name": ["浦发银行", "*ST九鼎", "ST信安"], "board": ["沪主板", "沪主板", "科创板"],
        "status": ["listed"] * 3, "source": ["上海证券交易所"] * 3, "source_ref": [ROSTER] * 3,
        "listing_date": [datetime(2020, 1, 1).date()] * 3, "as_of": [local_listing_ms] * 3,
    }).write_parquet(roster)
    board_sources = {
        "main-risk": (f"{ROOT}?sqlId={MAIN_ID}&domesticIndicator=S&productType=0", _board(
            MAIN_ID, [{"INSTRUMENT_ID": "600053", "INSTRUMENT_SHORT": "*ST九鼎" if not board_conflict else "*ST其他"}],
            total=main_total,
        )),
        "main-delisting": (f"{ROOT}?sqlId={MAIN_ID}&domesticIndicator=P&productType=0", _board(MAIN_ID, [])),
        "star-risk": (f"{ROOT}?sqlId={STAR_ID}&type=0", _board(STAR_ID, [{"secCode": "688201", "secNameCn": "ST信安"}])),
        "star-delisting": (f"{ROOT}?sqlId={STAR_ID}&type=1", _board(STAR_ID, [])),
    }
    for name, (_, body) in board_sources.items():
        (archive / f"{name}.json").write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    sources = {}
    for filename, url in [(roster.name, ROSTER), *((f"{name}.json", item[0]) for name, item in board_sources.items())]:
        body = (archive / filename).read_bytes()
        instant = RECEIVED if filename != roster.name else RECEIVED - timedelta(hours=4)
        sources[filename] = {"url": url, "receivedAt": instant.isoformat(),
                             "bytes": len(body), "sha256": sha256(body).hexdigest()}
    manifest = archive / "capture-evidence.json"
    manifest.write_text(json.dumps({"version": 1, "exchangeDate": DAY, "availableAt": RECEIVED.isoformat(),
                                   "sources": sources}), encoding="utf-8")
    return manifest


def _read(root, cutoff, *, snapshot=None):
    return read_input_evidence(root, cutoff, ["600000.SH", "600053.SH", "688201.SH", "600001.SH"], {"is_st"},
                               snapshot=snapshot)


def test_default_quant_snapshot_reads_verified_shanghai_board_only_at_its_original_time(tmp_path):
    _snapshot(tmp_path)
    inputs = _read(tmp_path, RECEIVED)
    assert inputs.fact("600000.SH", "is_st").value is False
    assert inputs.fact("600053.SH", "is_st").value is True
    assert inputs.fact("688201.SH", "is_st").value is True
    assert inputs.fact("600001.SH", "is_st").value is None
    assert inputs.fact("600000.SH", "is_st").available_at == RECEIVED.isoformat()
    assert _read(tmp_path, RECEIVED - timedelta(milliseconds=1)).fact("600000.SH", "is_st").value is None
    assert _read(tmp_path, RECEIVED + timedelta(days=1)).fact("600000.SH", "is_st").value is None


@pytest.mark.parametrize("problem", ["incomplete_report", "different_official_name", "mutated_raw", "mutated_manifest"])
def test_partial_conflicting_or_mutated_shanghai_snapshot_cannot_confirm_absence(tmp_path, problem):
    manifest = _snapshot(tmp_path, main_total=2 if problem == "incomplete_report" else None,
                         board_conflict=problem == "different_official_name")
    snapshot = load_input_snapshot(tmp_path, {"is_st"})
    if problem == "mutated_raw":
        (manifest.parent / "main-risk.json").write_bytes(b"{}")
    if problem == "mutated_manifest":
        manifest.write_bytes(manifest.read_bytes() + b"\n")
    fact = _read(tmp_path, RECEIVED, snapshot=snapshot).fact("600000.SH", "is_st")
    assert fact.value is None and fact.reason != "history_not_provided"


@pytest.mark.parametrize("conflicting", [False, True])
def test_sse_capture_publishes_once_after_complete_exchange_validation(tmp_path, monkeypatch, conflicting):
    from app.services import tickdesk_market_data, tickdesk_quote_feed
    from app.services.tickdesk_sse_risk import SSE_ST_ARCHIVE, capture_sse_st_snapshot

    source = _snapshot(tmp_path / "original", board_conflict=conflicting)
    root = tmp_path / "target"
    listing = root / "tickdesk-market/listing-observations" / f"{DAY}.parquet"
    listing.parent.mkdir(parents=True)
    shutil.copyfile(source.parent / "active-roster.parquet", listing)
    monkeypatch.setattr(tickdesk_market_data, "read_calendar_snapshot", lambda *_: (set(), {"source": "fuyao"}))
    monkeypatch.setattr(tickdesk_quote_feed, "quote_session", lambda *_: "open")
    archived = json.loads(source.read_bytes())["sources"]
    risk_responses = {entry["url"]: (source.parent / name).read_bytes()
                      for name, entry in archived.items() if name.endswith(".json")}
    queries = []

    def serve(request):
        url = str(request.url)
        queries.append(url)
        return httpx.Response(200, content=risk_responses[url])

    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        if conflicting:
            with pytest.raises(ValueError, match="disagree with the official"):
                capture_sse_st_snapshot(root, client, clock=lambda: RECEIVED)
            assert not (root / SSE_ST_ARCHIVE / DAY).exists()
        else:
            proof = capture_sse_st_snapshot(root, client, clock=lambda: RECEIVED)
            repeated = capture_sse_st_snapshot(root, client, clock=lambda: RECEIVED)
            assert proof["checkedAStocks"] == 3 and proof["riskAStocks"] == 2
            assert repeated == {"status": "already_recorded", "manifestSha256": proof["manifestSha256"]}
            assert _read(root, RECEIVED).fact("600000.SH", "is_st").value is False
    assert queries == list(risk_responses)
