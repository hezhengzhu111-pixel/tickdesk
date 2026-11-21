"""Dated official ETF secondary-market documents for read-only analysis."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from app.services.tickdesk_adapter import CN, fingerprint
from app.services.tickdesk_holding_rules import _available_ms

SSE_ETF_RULES = "https://www.sse.com.cn/lawandrules/sselawsrules2025/fund/trading/c/c_20250606_10781071.shtml"
_PDF = r"https://www\.sse\.com\.cn/disclosure/fund/announcement/c/new/(\d{4}-\d{2}-\d{2})/(\d{6})_\d{8}_[A-Z0-9]+\.pdf"
_ARCHIVE = Path("tickdesk-ai/etf-trade-rules.sqlite3")


def record_etf_trade_evidence(root, symbol, same_day_sale, documents):
    """Store a reviewed product-specific rule together with the official raw documents."""
    if not re.fullmatch(r"[0-9]{6}\.SH", symbol) or type(same_day_sale) is not bool or not 1 <= len(documents) <= 3:
        raise ValueError("invalid ETF policy scope")
    checked = []
    sessions = set()
    has_product = False
    now = datetime.now(CN)
    for url, title, observed, raw in documents:
        pdf = re.fullmatch(_PDF, url)
        product = pdf is not None and pdf[2] == symbol[:6] and raw.startswith(b"%PDF-")
        official_rule = url == SSE_ETF_RULES and raw.lstrip().lower().startswith((b"<!doctype", b"<html"))
        if (observed.tzinfo is None or observed > now or not 0 < len(title) <= 100
                or not 0 < len(raw) <= 12 * 1024 * 1024 or not (product or official_rule)):
            raise ValueError("invalid official ETF document or receipt")
        day = observed.astimezone(CN).date().isoformat()
        if product and pdf[1] > day:
            raise ValueError("ETF document date is after the receipt")
        sessions.add(day)
        has_product |= product
        checked.append((url, title, _available_ms(observed.isoformat()), hashlib.sha256(raw).hexdigest(), raw))
    urls = {doc[0] for doc in checked}
    if not has_product or len(sessions) != 1 or len(urls) != len(checked) or (same_day_sale and SSE_ETF_RULES not in urls):
        raise ValueError("ETF rule requires same-day, distinct evidence including a product document")

    [day] = sessions
    proof_refs = [url for url, *_ in checked]
    observed = max(doc[2] for doc in checked)
    record = {"kind": "etfAuctionSellRule", "symbol": symbol, "tradeDate": day, "sameDaySale": same_day_sale,
              "appliesTo": "secondary_market_auction_purchase", "proofRefs": proof_refs}
    identity = fingerprint([record, [doc[:4] for doc in checked]])
    path = Path(root) / _ARCHIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=3)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS rules (
            symbol TEXT NOT NULL, day TEXT NOT NULL, observed INTEGER NOT NULL,
            identity TEXT PRIMARY KEY, body TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS sources (
            identity TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL, observed INTEGER NOT NULL,
            sha256 TEXT NOT NULL, body BLOB NOT NULL, PRIMARY KEY(identity,url))""")
        db.execute("INSERT OR IGNORE INTO rules VALUES (?,?,?,?,?)", (symbol, day, observed, identity, json.dumps(record)))
        db.executemany("INSERT OR IGNORE INTO sources VALUES (?,?,?,?,?,?)", [(identity, *doc) for doc in checked])
    return identity


def read_etf_trade_evidence(root, symbols, cutoff_ms):
    path = Path(root) / _ARCHIVE
    if not path.is_file():
        return []
    output = []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.2)) as db:
        for symbol in dict.fromkeys(symbols):
            matches = db.execute("""SELECT day,observed,identity,body FROM rules WHERE symbol=? AND observed<=?
                                    ORDER BY observed DESC LIMIT 2""", (symbol, cutoff_ms)).fetchall()
            if not matches or (len(matches) > 1 and matches[0][1] == matches[1][1]):
                continue
            day, observed, identity, body = matches[0]
            rule = json.loads(body)
            docs = {doc[0]: doc for doc in db.execute("SELECT url,title,observed,sha256,body FROM sources WHERE identity=?", (identity,))}
            ordered = [docs[ref] for ref in rule["proofRefs"] if ref in docs]
            if (rule["symbol"] != symbol or rule["tradeDate"] != day or not ordered
                    or len(ordered) != len(rule["proofRefs"]) or max(doc[2] for doc in ordered) != observed
                    or fingerprint([rule, [doc[:4] for doc in ordered]]) != identity
                    or any(hashlib.sha256(doc[4]).hexdigest() != doc[3] for doc in ordered)):
                continue
            rule["verifiedOn"] = rule.pop("tradeDate")
            for index, (url, title, received, sha, _) in enumerate(ordered):
                record = rule if index == 0 else {"kind": "etfTradeRuleSource", "symbol": symbol}
                output.append({"source": {"ref": url, "name": title, "asOf": received},
                               "record": {**record, "revisionId": identity, "documentSha256": sha}, "metrics": {}})
    return output
