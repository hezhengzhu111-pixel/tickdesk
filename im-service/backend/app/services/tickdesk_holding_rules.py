"""Select effective-dated holding rules from TickDesk's reference evidence archive."""
from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta

FIELD = "holding_rules_v3"
SSE_SUSPENSION = "https://www.sse.com.cn/disclosure/dealinstruc/suspension/stock/"
SSE_SUSPENSION_NAME = "上交所停复牌信息"
SZSE_MEMO = "https://www.szse.cn/disclosure/memo/index.html"
SZSE_SUSPENSION_NAME = "深圳证券交易所停复牌提示"
SZSE_AUCTION_OPEN = time(9, 25)
_OFFICIAL_PARTIAL_SOURCES = {"SSE-official-holding-v1", "SZSE-official-holding-v1"}
MAX_INTEGER = 9_007_199_254_740_991
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MS = timedelta(milliseconds=1)
_SYMBOL = re.compile(r"[0-9]{6}\.(SH|SZ)")
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_DECIMAL = re.compile(r"(0|[1-9][0-9]*)(\.[0-9]+)?")
_ENUMS = {
    "channel": {"auction", "after_hours_fixed"}, "exchange": {"SSE", "SZSE"},
    "instrumentType": {"stock", "etf"}, "board": {"main", "chinext", "fund"},
    "dayTrading": {"YES", "NO", "UNKNOWN"}, "settlement": {"T+0", "T+1", "UNKNOWN"},
    "riskStatus": {"normal", "risk_warning", "other", "unknown"},
    "tradingStatus": {"trading", "suspended", "other", "unknown"},
    "dataStatus": {"complete", "missing", "conflict"},
}
_DECIMALS = {"minPriceTick", "limitRatio", "adjustedPrevClose", "referencePrice", "upperLimit", "lowerLimit"}
_QUANTITIES = {"quantityUnit", "maxOrderQty"}
_RULE_KEYS = set(_ENUMS) | _DECIMALS | _QUANTITIES | {
    "ruleSnapshotId", "symbol", "tradeDate", "ruleVersion", "sources", "sourceTime", "issueFields",
}


def _available_ms(iso: str) -> int:
    stamp = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("holding rule needs an exact availability timestamp")
    quotient, remainder = divmod(stamp - _EPOCH, _MS)
    return quotient + bool(remainder)


def _detect_time(value: object) -> bool:
    return type(value) is int and 0 <= value <= MAX_INTEGER


def _named(value: object, bound: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= bound


def _source(value, cutoff):
    return (isinstance(value, dict) and set(value) == {"name", "ref", "asOf"}
            and _named(value["name"], 100) and _named(value["ref"], 512)
            and _detect_time(value["asOf"]) and value["asOf"] <= cutoff)


def _date(value):
    result = date.fromisoformat(value)
    if value != result.isoformat():
        raise ValueError("noncanonical holding rule date")
    return result


def _shanghai_stamp(ms):
    from app.quant_evidence import SHANGHAI

    try:
        return (_EPOCH + ms * _MS).astimezone(SHANGHAI)
    except OverflowError as error:
        raise ValueError("holding evidence timestamp out of range") from error


def _validate_calendar(calendar, day, available):
    if not isinstance(calendar, dict) or set(calendar) != {
        "revisionId", "source", "availableAtMs", "coverageStart", "coverageEnd", "sessions",
    }:
        raise ValueError("invalid holding calendar")
    known_at = calendar["availableAtMs"]
    if not (_detect_time(known_at) and known_at <= available and _source(calendar["source"], known_at)
            and _named(calendar["revisionId"], 128)):
        raise ValueError("invalid holding calendar provenance")
    start, end = _date(calendar["coverageStart"]), _date(calendar["coverageEnd"])
    records = calendar["sessions"]
    if not isinstance(records, list) or not 1 <= len(records) <= 370 or not start <= day <= end:
        raise ValueError("invalid holding calendar coverage")
    sessions = []
    for session in records:
        if not isinstance(session, dict) or set(session) != {"date", "firstExecutionAtMs"}:
            raise ValueError("invalid holding calendar session")
        at = session["firstExecutionAtMs"]
        session_day = _date(session["date"])
        if not (_detect_time(at) and start <= session_day <= end
                and _shanghai_stamp(at).date() == session_day):
            raise ValueError("invalid holding calendar session time")
        sessions.append(session_day)
    if sessions != sorted(set(sessions)) or day not in sessions:
        raise ValueError("invalid holding calendar sessions")


def szse_auction_evidence(rule, available):
    if (rule["exchange"] != "SZSE" or rule["instrumentType"] != "stock" or rule["board"] not in {"main", "chinext"}
            or rule["channel"] != "auction" or available != rule["sourceTime"]):
        return False
    observed = _shanghai_stamp(available)
    if observed.date() != _date(rule["tradeDate"]) or observed.time() < SZSE_AUCTION_OPEN:
        return False
    actions = {("missing", "suspended", False): "suspension", ("missing", "unknown", True): "withdrawal",
               ("conflict", "unknown", True): "conflict"}
    action = actions.get((rule["dataStatus"], rule["tradingStatus"], "tradingStatus" in rule["issueFields"]))
    if not action:
        return False
    ref = f"{SZSE_MEMO}#{action}-{rule['symbol']}-{observed:%Y%m%d}"
    return any(source["name"] == SZSE_SUSPENSION_NAME and source["ref"] == ref and source["asOf"] == available
               for source in rule["sources"])


def _unavailability(symbol, day, available, reason, sources):
    entry = {"symbol": symbol, "tradeDate": day, "availableAtMs": available, "reason": reason}
    if sources <= _OFFICIAL_PARTIAL_SOURCES:
        entry["scope"] = "auctionStatus"
    return entry


def validate_holding_reference(row) -> None:
    """Validate an archive row before it participates in cutoff selection."""
    available = _available_ms(row["available_at"])
    symbol, day, records = row["symbol"], _date(row["effective_date"]), row["value"]
    if not _SYMBOL.fullmatch(symbol) or not _detect_time(available):
        raise ValueError("invalid holding rule scope")
    if records is None:  # A fresh unknown can withdraw an older effective revision.
        return
    if not isinstance(records, list) or not 1 <= len(records) <= 2:
        raise ValueError("invalid holding rule evidence list")
    channels = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {"rule", "calendar", "companyActionsCheckedThrough"}:
            raise ValueError("invalid holding rule evidence")
        rule = record["rule"]
        if not isinstance(rule, dict) or set(rule) != _RULE_KEYS:
            raise ValueError("invalid holding rule definition")
        if any(not isinstance(rule[key], str) or rule[key] not in values for key, values in _ENUMS.items()):
            raise ValueError("invalid holding rule enum")
        if (rule["symbol"] != symbol or _date(rule["tradeDate"]) != day
                or rule["exchange"] != ("SSE" if symbol.endswith(".SH") else "SZSE")
                or rule["channel"] in channels):
            raise ValueError("holding rule does not match symbol, date or channel")
        channels.add(rule["channel"])
        sources = rule["sources"]
        if not (_named(rule["ruleSnapshotId"], 64) and _ID.fullmatch(rule["ruleSnapshotId"])
                and _named(rule["ruleVersion"], 100)
                and _detect_time(rule["sourceTime"]) and rule["sourceTime"] <= available
                and isinstance(sources, list) and 1 <= len(sources) <= 20
                and all(_source(source, available) for source in sources)):
            raise ValueError("invalid holding rule provenance")
        if any(value is not None and not (_named(value, 48) and _DECIMAL.fullmatch(value))
               for value in (rule[key] for key in _DECIMALS)):
            raise ValueError("invalid holding rule decimal")
        if any(value is not None and not (_detect_time(value) and value > 0)
               for value in (rule[key] for key in _QUANTITIES)):
            raise ValueError("invalid holding rule quantity")
        issues = rule["issueFields"]
        if not isinstance(issues, list) or len(issues) > 100 or any(not _named(i, 64) for i in issues):
            raise ValueError("invalid holding rule issues")
        through = record["companyActionsCheckedThrough"]
        if through is not None and _date(through) > day:
            raise ValueError("holding rule actions are verified through a future date")
        _validate_calendar(record["calendar"], day, available)


def prepare_holding_rule_batch(repo, request):
    """Return rules for known positions only, with their exact archived availability."""
    from app.quant_evidence import _plain

    requested_at = request["requestedAt"]
    if not _detect_time(requested_at):
        return None
    symbols = {row["symbol"] for row in request.get("objects", []) if isinstance(row, dict)
               and _detect_time(row.get("qty")) and isinstance(row.get("symbol"), str)
               and _SYMBOL.fullmatch(row["symbol"])}
    if not symbols or len(symbols) > 500:
        return None
    cutoff = _shanghai_stamp(requested_at)
    day = cutoff.date().isoformat()
    snapshot = repo.get_quant_evidence_snapshot({FIELD})
    evidence = repo.get_quant_input_evidence(cutoff, symbols, {FIELD}, snapshot=snapshot)
    rules = []
    unavailable = []
    for symbol in sorted(symbols):
        fact = evidence.fact(symbol, FIELD)
        sources = {row["source"] for row in snapshot.reference_by_symbol.get(symbol, ())
                   if row["field"] == FIELD and row["effective_date"] == day and row["revision_id"] in fact.revision_ids}
        if not sources or fact.available_at is None:
            continue
        available = _available_ms(fact.available_at)
        if available > requested_at:
            continue
        if fact.reason in {"conflicting_evidence", "unknown_value"}:
            reason = "conflict" if fact.reason == "conflicting_evidence" else "withdrawn"
            unavailable.append(_unavailability(symbol, day, available, reason, sources))
            continue
        if fact.reason is not None:
            continue
        usable = []
        for record in fact.value:
            rule = record["rule"]
            complete = rule["dataStatus"] == "complete" and rule["settlement"] != "UNKNOWN" and not rule["issueFields"]
            positive_suspension = (rule["dataStatus"] == "missing" and rule["tradingStatus"] == "suspended"
                                   and rule["exchange"] == "SSE" and any(
                                       source["name"] == SSE_SUSPENSION_NAME
                                       and re.fullmatch(re.escape(f"{SSE_SUSPENSION}#{symbol}-") + r"[0-9]{8}", source["ref"])
                                       for source in rule["sources"]))
            szse_status = rule["dataStatus"] == "missing" and szse_auction_evidence(rule, available)
            if rule["tradeDate"] == day and (complete or positive_suspension or szse_status):
                usable.append({**_plain(record), "ruleAvailableAtMs": available})
        if not usable:
            if sources <= _OFFICIAL_PARTIAL_SOURCES and all(
                item["rule"]["dataStatus"] == "missing" for item in fact.value
            ):
                continue
            conflict = any(item["rule"]["dataStatus"] == "conflict" for item in fact.value)
            unavailable.append(_unavailability(symbol, day, available, "conflict" if conflict else "withdrawn", sources))
        rules.extend(usable)
    if not 0 < len(rules) + len(unavailable) <= 500:
        return None
    batch = {"version": 3, "rules": rules}
    if unavailable:
        batch["unavailable"] = unavailable
    return batch
