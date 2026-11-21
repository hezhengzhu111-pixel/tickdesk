"""Point-in-time market inputs for the local TickDesk assistant."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl

from app.enriched_generation import EnrichedGenerationUnavailableError
from app.financial_history import OBSERVED_AT, REVISION_ID, SOURCE, point_in_time_report_timeline
from app.quant_evidence import FINANCIAL_FIELDS
from app.services.tickdesk_adapter import CN, _finite_decimal, _wire_decimal, fingerprint, timestamp
from app.services.tickdesk_ai_disclosures import read_disclosures
from app.services.tickdesk_ai_industry_evidence import read_archived_market_context
from app.services.tickdesk_company_actions import (
    CNINFO_COMPANY,
    SSE_COMPANY,
    read_sse_cash_dividends,
    read_szse_distributions,
)
from app.services.tickdesk_etf_trade_evidence import read_etf_trade_evidence
from app.services.tickdesk_regime_archive import INDEX_ARCHIVE
from app.services.tickdesk_research import BAR_COLUMNS, FINALITY, _verified_row, _witnesses

FINANCIAL_METRICS = {
    "roe_latest": ("净资产收益率", "%"),
    "gross_margin_latest": ("毛利率", "%"),
    "net_margin_latest": ("净利率", "%"),
    "revenue_yoy_latest": ("营收同比", "%"),
    "net_income_yoy_latest": ("归母净利同比", "%"),
    "debt_ratio_latest": ("资产负债率", "%"),
}
FINANCIAL_CONTEXT_FIELDS = {
    "metrics": {field: (FINANCIAL_FIELDS[field], label, unit) for field, (label, unit) in FINANCIAL_METRICS.items()},
    "income": {
        "revenue": ("revenue", "营业收入", "元"),
        "net_income_attributable": ("net_income_attributable", "归母净利润", "元"),
    },
}
DAILY_METRICS = {"rawClose": ("不复权日收盘", "元"), "volume": ("日成交量", "手"), "amount": ("日成交额", "元")}
SZSE_DISTRIBUTION_METRICS = {
    "cashPerShare": ("每股现金分派（报表口径）", "元/股"),
    "bonusSharesPerShare": ("每股送股", "股/股"),
    "capitalReserveSharesPerShare": ("每股资本公积转增", "股/股"),
}
SSE_DISTRIBUTION_METRICS = {"cashGrossPerShare": ("每股税前现金分红", "元/股")}
MAX_SYMBOLS = 4
MAX_BARS = 20
AI_INDEX_ARCHIVE = INDEX_ARCHIVE


def _numeric(value):
    number = _finite_decimal(value)
    try:
        return _wire_decimal(number) if number is not None else None
    except ValueError:
        return None


def _financial_evidence(root, cutoff, symbols):
    rows, missing = [], {symbol: {} for symbol in symbols}
    cutoff = cutoff.astimezone(UTC)
    for table, fields in FINANCIAL_CONTEXT_FIELDS.items():
        path = root / "financials" / table / "part.parquet"
        failure, selected = "history_not_provided", {}
        try:
            if path.is_file():
                frame = pl.scan_parquet(path).filter(pl.col("symbol").is_in(symbols)).collect()
                columns = [column for column, _, _ in fields.values()]
                timeline = point_in_time_report_timeline(frame, columns, include_evidence=True, observed_clock=True)
                if not timeline.is_empty():
                    selected = {row["symbol"]: row for row in
                                timeline.filter(pl.col("_available") <= cutoff).iter_rows(named=True)}
                    failure = "no_report_at_cutoff"
        except (OSError, ValueError, pl.exceptions.PolarsError):
            failure = "financial_archive_unavailable"
        for symbol in symbols:
            report = selected.get(symbol)
            for field, (column, label, unit) in fields.items():
                evidence = report["_field_evidence"][column] if report else None
                if not evidence:
                    missing[symbol][field] = "missing_value" if report else failure
                    continue
                contributor = json.loads(evidence)
                original = json.loads(contributor.get("_evidence") or "{}")
                value = _numeric(report[column])
                provider, revision = original.get(SOURCE), original.get(REVISION_ID)
                period, available = original.get("period_end"), contributor.get("_available")
                currency = original.get("currency") if unit == "元" else None
                if value is None or not all((provider, revision, period, available)) or (unit == "元" and currency != "CNY"):
                    missing[symbol][field] = "missing_provenance"
                    continue
                basis = original.get("period_basis")
                if unit == "元":
                    label = ("年初至报告期末" if basis == "year_to_date" else "报告披露") + label
                available_at = datetime.fromisoformat(available)
                identity = fingerprint([table, symbol, field, revision, available])
                source = {"ref": f"local:financial:{identity}",
                          "name": f"{label} · {period} · {provider}"[:100], "asOf": timestamp(available_at)}
                record = {
                    "symbol": symbol, "field": field, "value": value, "unit": unit, "table": table,
                    "periodEnd": period, "currency": currency, "announcedDate": original.get("announce_date"),
                    "periodBasis": basis,
                    "source": provider, "observedAt": original.get(OBSERVED_AT), "availableAt": available,
                    "availableDate": available_at.astimezone(CN).date().isoformat(),
                    "revisionIds": [identity], "sourceRevisionId": revision,
                }
                rows.append({"source": source, "record": record, "metrics": {field: [label, value, unit, period]}})
    return rows, missing


def _daily_evidence(repo, snapshot, cutoff, symbols):
    witnesses = _witnesses(snapshot, timestamp(cutoff))
    earliest = (cutoff.date() - timedelta(days=60)).isoformat()
    days = [day for day, symbol in witnesses if symbol in symbols and day >= earliest]
    if not days:
        return [], dict.fromkeys(symbols, 0)
    bars = repo.get_enriched_range(date.fromisoformat(min(days)), date.fromisoformat(max(days)),
                                  symbols=symbols, columns=["date", "symbol", *BAR_COLUMNS])
    series = {symbol: [] for symbol in symbols}
    if bars is not None:
        for row in bars.sort("date").iter_rows(named=True):
            proof = _verified_row(row, witnesses)
            if proof and row["symbol"] in series:
                series[row["symbol"]].append({
                    "date": str(row["date"]), "rawClose": _numeric(row["raw_close"]),
                    "volume": _numeric(row["volume"]), "amount": _numeric(row["amount"]),
                    "source": proof["source"], "publishedAt": proof["available_at"],
                    "revisionId": proof["revision_id"], "asOf": timestamp(proof["value"]["end_at"]),
                })
    rows = []
    for symbol, history in series.items():
        if not history:
            continue
        last = history[-1]
        rows.append({
            "source": {"ref": f"local:daily:{last['revisionId']}", "asOf": last["asOf"],
                       "name": f"{symbol} · 已核验日线 · {last['source']} · {last['date']}"[:100]},
            "record": {"symbol": symbol, "verifiedBars": history[-MAX_BARS:], "priceBasis": "raw", "currency": "CNY"},
            "metrics": {key: [label, last[key], unit, last["date"]] for key, (label, unit) in DAILY_METRICS.items()
                        if last[key] is not None},
        })
    return rows, {symbol: min(len(history), MAX_BARS) for symbol, history in series.items()}


def _distribution_evidence(root, cutoff_ms, symbols):
    rows, coverage = [], {}
    for symbol in symbols:
        shanghai = symbol.endswith(".SH")
        read = read_sse_cash_dividends if shanghai else read_szse_distributions
        fields = SSE_DISTRIBUTION_METRICS if shanghai else SZSE_DISTRIBUTION_METRICS
        ref = f"{SSE_COMPANY}?COMPANY_CODE={symbol[:6]}" if shanghai else f"{CNINFO_COMPANY}?code={symbol[:6]}"
        name = "上海证券交易所" if shanghai else "巨潮资讯网"
        try:
            events = read(root, symbol, cutoff_ms)
        except (OSError, TypeError, ValueError, KeyError, AttributeError):
            coverage[symbol] = "distribution_archive_unavailable"
            continue
        if not isinstance(events, list) or len(events) > 10:
            coverage[symbol] = "distribution_archive_unavailable"
            continue
        coverage[symbol] = "no_distribution_at_cutoff"
        latest = None
        for event in events:
            try:
                source = event["source"]
                observed = source["asOf"]
                effective = date.fromisoformat(event["effectiveDate"])
                registered = date.fromisoformat(event["recordDate"])
                if (event["symbol"] != symbol or event["currency"] != "CNY" or source["ref"] != ref
                        or source["name"] != name or type(observed) is not int or not 0 < observed <= cutoff_ms
                        or not registered < effective <= datetime.fromtimestamp(observed / 1000, CN).date()):
                    continue
                terms = {field: value for field in fields if (value := _numeric(event.get(field))) is not None
                         and not value.startswith("-") and value != "0"}
                if not terms:
                    continue
                dates = {key: date.fromisoformat(event[key]) for key in
                         ("announcementDate", "cashDistributionDate", "shareDistributionDate") if event.get(key)}
                if dates.get("announcementDate", registered) > registered or any(
                    dates.get(key, effective) < effective for key in ("cashDistributionDate", "shareDistributionDate")
                ):
                    continue
            except (OverflowError, TypeError, ValueError, KeyError):
                continue
            order = (effective, observed)
            if latest is not None and order <= latest[0]:
                continue
            record = {
                "symbol": symbol, "effectiveDate": str(effective), "recordDate": str(registered),
                "eventType": "issuer_distribution", "termsBasis": "provider_reported", "currency": "CNY",
                **{key: str(value) for key, value in dates.items()}, **terms,
            }
            record["revisionId"] = fingerprint([ref, observed, record])
            latest = order, {
                "source": {"ref": ref, "name": name, "asOf": observed},
                "record": record,
                "metrics": {field: [label, terms[field], unit, str(effective)]
                            for field, (label, unit) in fields.items() if field in terms},
            }
        if latest is not None:
            rows.append(latest[1])
            coverage[symbol] = "partial"
    return rows, coverage


def read_market_evidence(repo, cutoff_ms: int, symbols: list[str]) -> tuple[list[dict], dict]:
    if repo is None or not symbols:
        return [], {"status": "unavailable", "reason": "repository_unavailable" if repo is None else "no_symbol"}
    cutoff = datetime.fromtimestamp(cutoff_ms / 1000, CN)
    total = len(symbols)
    symbols = symbols[:MAX_SYMBOLS]
    try:
        financial, gaps = _financial_evidence(repo.store.data_dir, cutoff, symbols)
    except (OSError, ValueError, AttributeError, KeyError, TypeError, pl.exceptions.PolarsError):
        financial, gaps = [], {"reason": "financial_archive_unavailable"}
    try:
        snapshot = repo.get_quant_evidence_snapshot({FINALITY})
        bars, counts = _daily_evidence(repo, snapshot, cutoff, symbols)
    except (OSError, ValueError, AttributeError, KeyError, TypeError, EnrichedGenerationUnavailableError,
            pl.exceptions.PolarsError):
        bars, counts = [], {"reason": "daily_data_unavailable"}
    try:
        disclosures, disclosure_status = read_disclosures(repo.store.data_dir, symbols, cutoff_ms)
    except (OSError, sqlite3.Error, ValueError, AttributeError, KeyError, TypeError):
        disclosures, disclosure_status = [], {"reason": "disclosure_archive_unavailable"}
    try:
        actions, action_status = _distribution_evidence(repo.store.data_dir, cutoff_ms, symbols)
    except AttributeError:
        actions, action_status = [], {"reason": "distribution_archive_unavailable"}
    try:
        industry, industry_status = read_archived_market_context(
            Path(repo.store.data_dir) / AI_INDEX_ARCHIVE, cutoff_ms, symbols,
        )
    except (AttributeError, TypeError, ValueError):
        industry, industry_status = [], {"status": "unavailable", "reason": "industry_archive_unavailable"}
    try:
        trade_rules = read_etf_trade_evidence(repo.store.data_dir, symbols, cutoff_ms)
    except (OSError, sqlite3.Error, ValueError, AttributeError, KeyError, TypeError):
        trade_rules = []
    rows = financial + bars + disclosures + actions + trade_rules + industry
    return rows, {
        "status": "partial" if rows else "unavailable", "requestedSymbols": total,
        "requestedLimit": MAX_SYMBOLS, "financialMissing": gaps, "verifiedDailyBars": counts,
        "disclosures": disclosure_status, "companyActions": action_status, "industryAndIndexes": industry_status,
    }
