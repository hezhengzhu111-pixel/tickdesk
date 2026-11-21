"""Capture dated issuer disclosures for the existing local AI evidence context."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.services.tickdesk_adapter import CN, fingerprint, timestamp

_SYMBOL = re.compile(r"[0-9]{6}\.(?:SZ|SH)")
_HOSTS = {"disc.static.szse.cn": "深交所披露", "static.cninfo.com.cn": "巨潮资讯披露"}
_DOCUMENT = re.compile(r"/(?:disc/disk[0-9]{2}/)?finalpage/(\d{4}-\d{2}-\d{2})/[a-fA-F0-9-]{10,64}\.pdf",
                       re.IGNORECASE)
_REPORT = re.compile(r"(20\d{2})年(第一季度|半年度|第三季度|年度)报告")
_PERIOD = {"第一季度": (3, 31), "半年度": (6, 30), "第三季度": (9, 30), "年度": (12, 31)}
_EDITION = re.compile(r"报告(?:全文)?[（(]?(更正版|更正后|修订版|修订后|更新版|修正后|修正稿)")
_BUSINESS = re.compile(r"本公司(?:属|属于)\s*([\u3400-\u9fff、]{4,60}行业)。主要经营活动为([^。]{4,100})。")
_MAX_PDF = 12 * 1024 * 1024


def _source(url, symbol):
    parsed = urlsplit(url)
    match = _DOCUMENT.fullmatch(parsed.path)
    if (not _SYMBOL.fullmatch(symbol) or parsed.scheme != "https" or parsed.netloc not in _HOSTS or not match
            or parsed.query or parsed.fragment
            or (parsed.netloc == "disc.static.szse.cn" and not symbol.endswith(".SZ"))):
        raise ValueError("unsupported official disclosure URL or symbol")
    return _HOSTS[parsed.netloc], date.fromisoformat(match[1])


def _parse_pdf(text, symbol, published):
    pages = text.split("\f")
    front = "\n".join(pages[:8])
    issuer_codes = set(re.findall(r"(?:证券|股票)\s*代码\s*[:：]?\s*(\d{6})(?!\d)", front))
    title = re.sub(r"\s+", "", pages[0][:1600])
    report = _REPORT.search(title)
    if issuer_codes != {symbol[:6]} or not report or "披露的提示性公告" in title:
        raise ValueError("issuer and periodic report must be verified in the document")
    period = date(int(report[1]), *_PERIOD[report[2]])
    if period >= published:
        raise ValueError("periodic report publication precedes the reporting period")
    result = {"reportPeriodEnd": str(period), "reportTitle": report[0]}
    if edition := _EDITION.search(title):
        result["explicitEdition"] = edition[1]
    if announcement := re.search(r"公告编号\s*[:：]\s*(20\d{2}-\d{3})(?!\d)", front):
        result["announcementId"] = announcement[1]
    industry = {}
    for page, body in enumerate(pages, 1):
        for label, activity in _BUSINESS.findall(re.sub(r"\s+", "", body)):
            industry.setdefault((label, activity), page)
    if len(industry) == 1:
        (label, activity), page = next(iter(industry.items()))
        result.update(industryClassification=label, businessActivity=activity, industryDisclosurePage=page,
                      classificationBasis="issuer_report")
    return result


def _archive(root):
    return Path(root) / "tickdesk-ai" / "disclosures.sqlite3"


def _revision(url, record):
    return fingerprint([url, {key: value for key, value in record.items() if key not in {"observedAt", "revisionId"}}])


def capture_disclosure(root, url, symbol):
    """Read one public official PDF. The assistant itself uses the local archive only."""
    source_name, published = _source(url, symbol)
    extractor = shutil.which("pdftotext")
    if not extractor:
        raise ValueError("pdftotext is required for disclosure capture")
    body = bytearray()
    deadline = time.monotonic() + 12
    with httpx.Client(follow_redirects=False, timeout=8) as client, client.stream("GET", url) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if time.monotonic() > deadline:
                raise TimeoutError("official disclosure request timed out")
            if len(body) > _MAX_PDF:
                raise ValueError("official disclosure exceeds the PDF size limit")
    observed = datetime.now(UTC)
    if remainder := observed.microsecond % 1000:
        observed += timedelta(microseconds=1000 - remainder)
    if not body.startswith(b"%PDF-") or published > observed.astimezone(CN).date():
        raise ValueError("invalid or future-dated disclosure")
    with tempfile.TemporaryDirectory(prefix="tickdesk-disclosure-") as work:
        document, output = Path(work) / "source.pdf", Path(work) / "source.txt"
        document.write_bytes(body)
        subprocess.run([extractor, "-enc", "UTF-8", "-f", "1", "-l", "80", "-layout", str(document), str(output)],
                       timeout=10, check=True, capture_output=True)
        if output.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("official disclosure exceeds the text size limit")
        record = _parse_pdf(output.read_text(encoding="utf-8"), symbol, published)
    digest = fingerprint(bytes(body))
    record.update(symbol=symbol, publishedDate=str(published), observedAt=observed.isoformat(),
                  documentSha256=digest)
    record["revisionId"] = _revision(url, record)
    archive = _archive(root)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(archive, timeout=.2)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS documents (
            symbol TEXT, period TEXT, published TEXT, observed INTEGER, digest TEXT, url TEXT,
            name TEXT, body TEXT, PRIMARY KEY(symbol, url, digest))""")
        db.execute("INSERT OR IGNORE INTO documents VALUES (?,?,?,?,?,?,?,?)",
                   (symbol, record["reportPeriodEnd"], str(published), timestamp(observed), digest, url,
                    source_name, json.dumps(record, ensure_ascii=False)))
        saved, = db.execute("SELECT body FROM documents WHERE symbol=? AND url=? AND digest=?",
                            (symbol, url, digest)).fetchone()
    return json.loads(saved)


def read_disclosures(root, symbols, cutoff_ms):
    path = _archive(root)
    if not path.is_file():
        return [], {symbol: {"report": "no_archive", "industry": "no_archive"} for symbol in symbols}
    rows, status = [], {}
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=.1)) as db:
        for symbol in symbols:
            records = []
            for observed, url, name, body in db.execute(
                "SELECT observed,url,name,body FROM documents WHERE symbol=? AND observed<=? "
                "ORDER BY period DESC,published DESC,observed DESC,digest LIMIT 24",
                (symbol, cutoff_ms),
            ):
                record = json.loads(body)
                expected, published = _source(url, symbol)
                if (record["symbol"] != symbol or name != expected or str(published) != record["publishedDate"]
                        or timestamp(record["observedAt"]) != observed
                        or record["revisionId"] != _revision(url, record)):
                    continue
                records.append({"source": {"ref": url, "name": f"{record['reportTitle']} · {name}", "asOf": observed},
                                "record": record, "metrics": {}})
            records.sort(key=lambda row: (row["record"]["reportPeriodEnd"], row["record"]["publishedDate"],
                                          "explicitEdition" in row["record"], row["source"]["asOf"]), reverse=True)
            industry = next((row for row in records if row["record"].get("industryClassification")), None)
            if records:
                latest = records[0]
                selected = [latest]
                if industry and industry != latest:
                    selected.append(industry)
                period = latest["record"]["reportPeriodEnd"]
                prior_period = f"{int(period[:4]) - 1:04d}{period[4:]}"
                comparison = next((row for row in records if row["record"]["reportPeriodEnd"] == prior_period), None)
                if comparison and comparison not in selected:
                    selected.append(comparison)
                rows.extend(selected)
            status[symbol] = {"report": "available" if records else "no_disclosure_at_cutoff",
                              "industry": "issuer_disclosed" if industry else "no_industry_at_cutoff"}
    return rows, status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("symbol")
    parser.add_argument("url")
    args = parser.parse_args()
    print(json.dumps(capture_disclosure(args.data_dir, args.url, args.symbol), ensure_ascii=False))


if __name__ == "__main__":
    main()
