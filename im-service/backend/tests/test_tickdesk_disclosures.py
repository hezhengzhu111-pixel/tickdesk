import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.services import tickdesk_ai_disclosures as disclosures
from app.services.tickdesk_adapter import timestamp

SYMBOL = "300502.SZ"
ORIGINAL = "https://static.cninfo.com.cn/finalpage/2026-10-30/1234567890.PDF"
REVISED = "https://static.cninfo.com.cn/finalpage/2026-10-30/1234567891.PDF"


def test_public_document_archive_orders_actual_observation_and_explicit_edition(tmp_path, monkeypatch):
    client = httpx.Client
    facts = {"observed": datetime(2026, 11, 16, 8, 30, tzinfo=UTC),
             "text": "证券代码：300502\n2026年第三季度报告（修订版）\n"
             "本公司属计算机设备制造行业。主要经营活动为设备生产和销售。"}

    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return facts["observed"].astimezone(tz)

    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, content=b"%PDF-test-only " + request.url.path.encode(),
    ))
    monkeypatch.setattr(disclosures.httpx, "Client", lambda **kwargs: client(transport=transport, **kwargs))
    monkeypatch.setattr(disclosures.shutil, "which", lambda _: "fixture-pdftotext")
    monkeypatch.setattr(disclosures.subprocess, "run",
                        lambda argv, **_: Path(argv[-1]).write_text(facts["text"], encoding="utf-8"))
    monkeypatch.setattr(disclosures, "datetime", FrozenClock)
    revision = disclosures.capture_disclosure(tmp_path, REVISED, SYMBOL)
    assert revision["explicitEdition"] == "修订版" and revision["reportPeriodEnd"] == "2026-09-30"
    before, quality = disclosures.read_disclosures(tmp_path, [SYMBOL], timestamp(facts["observed"]) - 1)
    assert before == [] and quality[SYMBOL]["industry"] == "no_industry_at_cutoff"
    facts.update(observed=datetime(2026, 11, 16, 8, 40, tzinfo=UTC),
                 text="股票代码：300502\n2026年第三季度报告\n")
    disclosures.capture_disclosure(tmp_path, ORIGINAL, SYMBOL)
    [story], quality = disclosures.read_disclosures(tmp_path, [SYMBOL], timestamp(facts["observed"]))
    assert story["source"]["ref"] == REVISED and story["record"]["revisionId"] == revision["revisionId"]
    assert quality[SYMBOL]["industry"] == "issuer_disclosed"
    assert disclosures.capture_disclosure(tmp_path, REVISED, SYMBOL)["observedAt"] == revision["observedAt"]
    with pytest.raises(ValueError, match="issuer"):
        disclosures.capture_disclosure(tmp_path, ORIGINAL, "000001.SZ")
    with pytest.raises(ValueError, match="official"):
        disclosures.capture_disclosure(tmp_path, ORIGINAL.replace(".cn/", ".cn.example/"), SYMBOL)


def test_comparison_report_uses_prior_year_same_period_and_first_observation(tmp_path):
    archive = disclosures._archive(tmp_path)
    archive.parent.mkdir()
    current_period = "2026-09-30"
    prior_url = "https://static.cninfo.com.cn/finalpage/2025-10-30/1224761867.PDF"
    data = [
        (current_period, ORIGINAL, "2026-11-16T09:00:00+00:00", {}),
        ("2026-06-30", "https://static.cninfo.com.cn/finalpage/2026-08-25/1234567892.PDF",
         "2026-11-16T09:00:01+00:00", {"industryClassification": "设备制造行业"}),
        ("2025-12-31", "https://static.cninfo.com.cn/finalpage/2026-04-01/1234567893.PDF",
         "2026-11-16T09:00:02+00:00", {}),
        ("2025-09-30", prior_url, "2026-11-16T09:01:00+00:00", {}),
    ]
    with closing(sqlite3.connect(archive)) as db, db:
        db.execute("CREATE TABLE documents (symbol, period, published, observed, digest, url, name, body)")
        for period, url, observed, extra in data:
            name, published = disclosures._source(url, SYMBOL)
            record = {"symbol": SYMBOL, "reportPeriodEnd": period, "publishedDate": str(published),
                      "observedAt": observed, "reportTitle": f"{period}定期报告", **extra}
            record["revisionId"] = disclosures._revision(url, record)
            db.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?)",
                       (SYMBOL, period, str(published), timestamp(observed), period, url, name, json.dumps(record)))
    cutoff = timestamp(data[-1][2])
    before, _ = disclosures.read_disclosures(tmp_path, [SYMBOL], cutoff - 1)
    now, _ = disclosures.read_disclosures(tmp_path, [SYMBOL], cutoff)
    assert [item["record"]["reportPeriodEnd"] for item in before] == [current_period, "2026-06-30"]
    assert [item["record"]["reportPeriodEnd"] for item in now] == [current_period, "2026-06-30", "2025-09-30"]
    assert now[-1]["source"]["ref"] == prior_url
