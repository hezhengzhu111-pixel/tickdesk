import json
from datetime import date
from pathlib import Path

import polars as pl

from app.services import tickdesk_daily_attribution as attribution


def session(path: Path, day: str, symbols):
    part = path / "kline_daily" / f"date={day}" / "part.parquet"
    part.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": list(symbols), "date": [date.fromisoformat(day)] * len(symbols)}).write_parquet(part)


def coverage(day_counts, symbol_expected):
    return {
        "version": 1,
        "source": "fuyao",
        "start": sorted(day_counts)[0],
        "through": sorted(day_counts)[-1],
        "expectedThrough": sorted(day_counts)[-1],
        "requestedSymbols": len(symbol_expected),
        "observedSymbols": len(symbol_expected),
        "rows": sum(day_counts.values()),
        "conflictingRows": 0,
        "dates": {day: {"observed": count, "expected": count} for day, count in day_counts.items()},
        "symbols": {symbol: {"observed": expected, "expected": expected}
                    for symbol, expected in symbol_expected.items()},
    }


def write_archive(root: Path, day: str, *, sse=(), szse=()):
    directory = root / attribution.ARCHIVE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "day": day, "capturedAt": 1, "sse": list(sse), "szse": list(szse),
               "sources": {"sse": attribution.SSE_SOURCE, "szse": attribution.SZSE_SOURCE}}
    (directory / f"{day}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_attributes_missing_bars_to_ongoing_suspensions(tmp_path):
    session(tmp_path, "2026-11-13", ["600185.SH", "000635.SZ", "000001.SZ"])
    session(tmp_path, "2026-11-16", ["000001.SZ"])
    state = coverage({"2026-11-13": 3, "2026-11-16": 1},
                     {"600185.SH": 2, "000635.SZ": 2, "000001.SZ": 2})
    write_archive(tmp_path, "2026-11-13", sse=[{
        "productCode": "600185", "controlType": "TR", "type": "LXTP",
        "startStopDate": "20261112", "endStopDate": "", "stopTime": ""}])
    write_archive(tmp_path, "2026-11-16",
                  sse=[{"productCode": "600185", "controlType": "TR", "type": "LXTP",
                        "startStopDate": "20261112", "endStopDate": "", "stopTime": ""}],
                  szse=[{"zqdm": "000635", "tpsj": "停牌", "tpkssj": "2026-11-13 开市",
                         "fpkssj": "", "tpyy": "重大事项"}])
    result = attribution.attribute(tmp_path, state)
    assert result["status"] == "all_attributed"
    assert result["missingCells"] == 2 and result["attributedCells"] == 2
    assert result["unexplainedCells"] == 0
    assert result["attributedByDay"] == {"2026-11-16": 2}
    assert result["sources"]["sseDays"] == 2 and result["sources"]["szseDays"] == 1


def test_intraday_halt_does_not_explain_missing_daily_bar(tmp_path):
    session(tmp_path, "2026-11-13", ["600185.SH"])
    session(tmp_path, "2026-11-16", [])
    state = coverage({"2026-11-13": 1, "2026-11-16": 0}, {"600185.SH": 2})
    write_archive(tmp_path, "2026-11-16", sse=[{
        "productCode": "600185", "controlType": "TR", "type": "LSTP",
        "startStopDate": "20261116", "endStopDate": "20261116", "stopTime": "10:30"}])
    result = attribution.attribute(tmp_path, state)
    assert result["status"] == "partial"
    assert result["missingCells"] == 1 and result["attributedCells"] == 0
    assert result["unexplained"] == [{"day": "2026-11-16", "symbol": "600185.SH"}]


def test_szse_one_day_suspension_resumes_at_open(tmp_path):
    session(tmp_path, "2026-11-13", [])
    session(tmp_path, "2026-11-16", ["002822.SZ"])
    state = coverage({"2026-11-13": 0, "2026-11-16": 1}, {"002822.SZ": 2})
    write_archive(tmp_path, "2026-11-13", szse=[{
        "zqdm": "002822", "tpsj": "1天", "tpkssj": "2026-11-13 开市",
        "fpkssj": "2026-11-16 开市"}])
    result = attribution.attribute(tmp_path, state)
    assert result["status"] == "all_attributed"
    assert result["attributedByDay"] == {"2026-11-13": 1}


def test_enrich_is_stable_and_persists_signature(tmp_path):
    session(tmp_path, "2026-11-16", [])
    state = coverage({"2026-11-16": 0}, {"600185.SH": 1})
    state_path = tmp_path / attribution.STATE_PATH
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    write_archive(tmp_path, "2026-11-16", sse=[{
        "productCode": "600185", "controlType": "TR", "type": "LXTP",
        "startStopDate": "20261116", "endStopDate": "", "stopTime": ""}])
    enriched = attribution.attach(tmp_path, state)
    assert enriched["attribution"]["status"] == "all_attributed"
    assert enriched["attribution"]["archiveSignature"]
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["attribution"]["status"] == "all_attributed"
    assert attribution.enrich(tmp_path, enriched) is enriched


def test_missing_archive_leaves_coverage_unchanged(tmp_path):
    state = coverage({"2026-11-16": 0}, {"600185.SH": 1})
    assert attribution.enrich(tmp_path, state) is state

def test_ensure_archive_fetches_paginated_reports_and_skips_existing(tmp_path, monkeypatch):
    import asyncio

    calls = []

    async def fake_request(client, url, params, referer):
        calls.append((url, dict(params)))
        if url == attribution.SSE_URL:
            page = params["pageHelp.pageNo"]
            rows = [{"productCode": f"60000{page}", "controlType": "TR", "type": "LXTP",
                     "startStopDate": "20261117", "endStopDate": "", "stopTime": ""}]
            return {"result": rows, "pageHelp": {"total": 2}}
        page = params["PAGENO"]
        return [{"metadata": {"recordcount": 2, "pagesize": 10, "pagecount": 2, "pageno": page},
                 "data": [{"zqdm": f"00000{page}", "tpsj": "停牌", "tpkssj": "2026-11-17 开市",
                           "fpkssj": ""}]}]

    monkeypatch.setattr(attribution, "_request", fake_request)
    fetched = asyncio.run(attribution.ensure_archive(tmp_path, ["2026-11-17"]))
    assert fetched == ["2026-11-17"]
    payload = json.loads((tmp_path / attribution.ARCHIVE_DIR / "2026-11-17.json").read_text(encoding="utf-8"))
    assert len(payload["sse"]) == 2 and len(payload["szse"]) == 2
    assert payload["sources"]["sse"]["name"] == "上海证券交易所停复牌信息"
    sse_calls = [call for call in calls if call[0] == attribution.SSE_URL]
    szse_calls = [call for call in calls if call[0] == attribution.SZSE_URL]
    assert [call[1]["pageHelp.pageNo"] for call in sse_calls] == [1, 2]
    assert [call[1]["PAGENO"] for call in szse_calls] == [1, 2]
    calls.clear()
    assert asyncio.run(attribution.ensure_archive(tmp_path, ["2026-11-17"])) == []
    assert calls == []


def test_ensure_archive_keeps_missing_file_when_official_source_fails(tmp_path, monkeypatch):
    import asyncio

    async def broken(client, url, params, referer):
        raise ValueError("incomplete suspension list")

    monkeypatch.setattr(attribution, "_request", broken)
    assert asyncio.run(attribution.ensure_archive(tmp_path, ["2026-11-17"])) == []
    assert not (tmp_path / attribution.ARCHIVE_DIR / "2026-11-17.json").exists()
