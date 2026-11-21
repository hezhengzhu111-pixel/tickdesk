import json
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
from zipfile import ZipFile

import httpx
import pytest
from test_quant_data_quality import repo

from app.services import tickdesk_szse_risk as risk
from app.services.tickdesk_szse_risk import read_szse_st_snapshot

API = "https://www.szse.cn/api/report/ShowReport"
START = "2026-11-16T14:40:00+08:00"
RECEIVED = "2026-11-16T14:40:00.000123+08:00"
AVAILABLE = datetime.fromisoformat("2026-11-16T14:40:00.001+08:00")


def write_xlsx(path, rows):
    cells = []
    for i, row in enumerate(rows, 1):
        cell = "".join(f'<c r="{column}{i}" t="inlineStr"><is><t>{value}</t></is></c>'
                       for column, value in zip("AB", row, strict=True))
        cells.append(f'<row r="{i}">{cell}</row>')
    with ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr("_rels/.rels",
                         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                         'relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr("xl/workbook.xml",
                         '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                         'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                         '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels",
                         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                         'relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr("xl/worksheets/sheet1.xml",
                         '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                         + "<sheetData>" + "".join(cells) + "</sheetData></worksheet>")


def write_manifest(root, report):
    (root / "capture-evidence.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")


def snapshot(root):
    count = {"tab1": {"rows": 1, "pages": 1, "pageSize": 20}, "tab2": {"rows": 0, "pages": 0, "pageSize": 20}}
    listing = [["A股代码", "板块"], ["300502", "创业板"], ["300027", "创业板"]]
    write_xlsx(root / "active-roster.xlsx", listing)
    write_xlsx(root / "risk-board.xlsx", [["证券代码", "证券简称"], ["300027", "许运营"]])
    pages = [
        {"metadata": {"catalogid": "fxjsb", "name": name, "tabkey": key,
                      "pagesize": 20, "pageno": 1, "pagecount": count[key]["pages"],
                      "recordcount": count[key]["rows"], "cols": {"zqdm": "证券代码", "gsjc": "证券简称"},
                      "conditions": []},
         "data": [{"zqdm": "300027", "gsjc": "<u>许运营</u>"}] if key == "tab1" else []}
        for key, name in (("tab1", "风险警示股票"), ("tab2", "退市整理股票"))
    ]
    checked = f"{API}/data?SHOWTYPE=JSON&CATALOGID=fxjsb&TABKEY=tab1&PAGENO=1"
    source_names = {
        "active-roster.xlsx": f"{API}?SHOWTYPE=xlsx&CATALOGID=1110&TABKEY=tab1",
        "risk-board.xlsx": f"{API}?SHOWTYPE=xlsx&CATALOGID=fxjsb&TABKEY=tab1",
        "sweep1-page-1.json": checked, "sweep2-page-1.json": checked,
    }
    for prefix in ("sweep1", "sweep2"):
        (root / f"{prefix}-page-1.json").write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
    sources = {}
    for name, url in source_names.items():
        body = (root / name).read_bytes()
        sources[name] = {"url": url, "receivedAt": RECEIVED, "bytes": len(body), "sha256": sha256(body).hexdigest()}
    report = {"exchangeDate": "2026-11-16", "collectionStartedAt": START,
              "availableAt": AVAILABLE.isoformat(), "availableAtMs": 1794811200001, "pagesRead": 1,
              "coverage": count, "workbookRows": 1, "activeSecurityCount": 2,
              "workbookAndTwoJsonSweepsMatch": True, "sources": sources}
    write_manifest(root, report)
    return report, pages


def test_dossier_binds_status_to_roster_first_reception_and_exact_shanghai_day(tmp_path):
    snapshot(tmp_path)
    available = read_szse_st_snapshot(tmp_path, "300502.SZ", AVAILABLE)
    assert available.value is False and available.reason is None
    assert available.available_at == AVAILABLE.isoformat()
    assert len(available.revision_ids) == 1 and len(available.revision_ids[0]) == 64
    assert read_szse_st_snapshot(tmp_path, "300027.SZ", AVAILABLE).value is True
    for symbol, cutoff, reason in (
        ("300502.SZ", AVAILABLE - timedelta(milliseconds=1), "snapshot_not_yet_available"),
        ("300502.SZ", AVAILABLE + timedelta(days=1), "snapshot_day_mismatch"),
        ("300502.SZ", AVAILABLE.replace(tzinfo=None), "invalid_cutoff"),
        ("300502.SH", AVAILABLE, "unsupported_symbol"),
        ("300000.SZ", AVAILABLE, "security_not_listed"),
    ):
        unknown = read_szse_st_snapshot(tmp_path, symbol, cutoff)
        assert unknown.value is None and unknown.reason == reason


def test_default_quant_inputs_freeze_the_official_same_day_risk_board(tmp_path):
    r = repo(tmp_path)
    fields = {"is_st"}
    symbols = ["300502.SZ", "300027.SZ", "300502.SH"]
    before = r.get_quant_evidence_snapshot(fields)
    day = AVAILABLE.date().isoformat()
    directory = tmp_path / "tickdesk-market" / "szse-risk-snapshots" / day
    directory.mkdir(parents=True)
    manifest, _ = snapshot(directory)
    read = r.get_quant_input_evidence
    already_frozen = read(AVAILABLE, symbols, fields, snapshot=before)
    assert already_frozen.fact("300502.SZ", "is_st").value is None
    assert read(AVAILABLE - timedelta(milliseconds=1), symbols, fields).fact("300502.SZ", "is_st").value is None
    actual = read(AVAILABLE, symbols, fields)
    assert actual.fact("300502.SZ", "is_st").value is False
    assert actual.fact("300027.SZ", "is_st").value is True
    assert actual.fact("300502.SH", "is_st").value is None
    assert read(AVAILABLE + timedelta(days=1), symbols, fields).fact("300502.SZ", "is_st").value is None

    frozen = r.get_quant_evidence_snapshot(fields)
    write_manifest(directory, {**manifest, "modified": True})
    assert read(AVAILABLE, symbols, fields, snapshot=frozen).fact("300502.SZ", "is_st").value is None
    write_manifest(directory, manifest)
    r.write_quant_reference_evidence([dict(symbol="300502.SZ", field="is_st", value=True,
                                           source="independent", effective_date=day, available_at=START)])
    conflict = read(AVAILABLE, symbols, fields).fact("300502.SZ", "is_st")
    assert conflict.value is None and conflict.reason == "conflicting_evidence"
    assert len(conflict.revision_ids) == 2


def test_rejects_dossier_below_maximum_receipt_and_conflicting_raw_sweep(tmp_path):
    original, pages = snapshot(tmp_path)
    inconsistent = deepcopy(original)
    inconsistent["sources"]["risk-board.xlsx"]["receivedAt"] = (AVAILABLE + timedelta(seconds=1)).isoformat()
    write_manifest(tmp_path, inconsistent)
    assert read_szse_st_snapshot(tmp_path, "300502.SZ", AVAILABLE).reason == "invalid_snapshot"
    pages[0]["data"][0]["gsjc"] = "<u>冲突行</u>"
    changed = json.dumps(pages, ensure_ascii=False).encode()
    (tmp_path / "sweep2-page-1.json").write_bytes(changed)
    write_manifest(tmp_path, original)
    assert read_szse_st_snapshot(tmp_path, "300502.SZ", AVAILABLE).reason == "invalid_snapshot"
    original["sources"]["sweep2-page-1.json"].update(bytes=len(changed), sha256=sha256(changed).hexdigest())
    write_manifest(tmp_path, original)
    fact = read_szse_st_snapshot(tmp_path, "300502.SZ", AVAILABLE)
    assert fact.value is None and fact.reason == "conflicting_evidence"


def test_missing_manifest_or_raw_page_does_not_imply_normal_status(tmp_path):
    assert read_szse_st_snapshot(tmp_path, "300502.SZ", AVAILABLE).reason == "history_not_provided"
    snapshot(tmp_path)
    (tmp_path / "sweep1-page-1.json").unlink()
    assert read_szse_st_snapshot(tmp_path, "300502.SZ", AVAILABLE).reason == "invalid_snapshot"


def test_capture_checks_official_sources_and_publishes_completed_folder_once(tmp_path, monkeypatch):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    data, _ = snapshot(fixture)
    replies = {source["url"]: (fixture / filename).read_bytes() for filename, source in data["sources"].items()}
    client = httpx.Client
    hits = []

    def receive(request):
        hits.append(str(request.url))
        assert request.method == "GET" and request.url.host == "www.szse.cn"
        assert "Referer" in request.headers and "Authorization" not in request.headers
        return httpx.Response(200, content=replies[str(request.url)])

    monkeypatch.setattr(risk.httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(receive), **kwargs))
    result_path = tmp_path / "observation"
    captured = risk.capture_szse_st_snapshot(result_path)
    captured_at = datetime.fromisoformat(captured["availableAt"])
    assert result_path.is_dir() and len(hits) == 4
    assert read_szse_st_snapshot(result_path, "300502.SZ", captured_at).value is False
    assert read_szse_st_snapshot(result_path, "300502.SZ", captured_at - timedelta(milliseconds=1)).value is None
    with pytest.raises(FileExistsError):
        risk.capture_szse_st_snapshot(result_path)
    assert len(hits) == 4

    replies[next(url for url in replies if url.endswith("PAGENO=1"))] = b"malformed"
    rejected = tmp_path / "rejected"
    with pytest.raises(ValueError):
        risk.capture_szse_st_snapshot(rejected)
    assert not rejected.exists()
