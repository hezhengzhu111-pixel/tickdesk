"""Read-only TickDesk boundary, composing the repository and existing strategy engine.

Approval manifests are local reviewed artifacts, never inferred from public/published flags.
No missing board, price basis, timestamp, rule or strategy score is guessed.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as wall_time
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from zoneinfo import ZoneInfo

CN = ZoneInfo("Asia/Shanghai")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MILLISECOND = timedelta(milliseconds=1)
_WIRE_DECIMAL_LENGTH = 48  # TickDesk v2 Decimal schema.
_EXACT_DECIMAL_LIMIT = 1024  # Bounds integer allocation and arithmetic inside the scan worker.


def fingerprint(value):
    payload = value if isinstance(value, bytes) else json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
        allow_nan=False).encode()
    return hashlib.sha256(payload).hexdigest()


def _finite_decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def decimal(value):
    number = _finite_decimal(value)
    return format(number, "f") if number is not None else None


def _decimal_parts(value):
    sign, digits, exponent = value.as_tuple()
    end = len(digits)
    while end > 1 and digits[end - 1] == 0:
        end -= 1
    return sign, digits[:end], exponent + len(digits) - end


def _wire_decimal(value):
    if value.is_zero():
        return "0"
    sign, digits, exponent = _decimal_parts(value)
    length = sign + max(1, len(digits) + exponent)
    if exponent < 0:
        length += 1 - exponent
    if length > _WIRE_DECIMAL_LENGTH:
        return None
    return format(Decimal((sign, digits, exponent)), "f")


def _bounded_fraction(value, *, scale=0):
    if value.is_zero():
        return Fraction(0)
    sign, digits, exponent = _decimal_parts(value)
    exponent -= scale
    if max(len(digits), abs(exponent)) > _EXACT_DECIMAL_LIMIT:
        raise ValueError("weighted score exceeds exact arithmetic bounds")
    return Fraction(Decimal((sign, digits, exponent)))


def _weighted_score(hits):
    first_score = hits[0][1]
    if all(score == first_score for _, score, _ in hits):
        rendered = _wire_decimal(first_score)
        if rendered is not None:
            return rendered
        average = _bounded_fraction(first_score)
    else:
        # A common power of ten keeps even large absolute weights exact.
        scale = max(weight.adjusted() for _, _, weight in hits)
        terms = [(_bounded_fraction(score), _bounded_fraction(weight, scale=scale)) for _, score, weight in hits]
        average = sum(score * weight for score, weight in terms) / sum(weight for _, weight in terms)
    # The greatest wire decimal below or equal to the exact score preserves
    # eligibility at every wire-representable threshold.
    whole = average.numerator // average.denominator
    places = _WIRE_DECIMAL_LENGTH - len(str(whole)) - 1
    base = 10 ** places
    scaled = average.numerator * base // average.denominator
    if scaled == 0 and average:
        raise ValueError("positive weighted score is below wire precision")
    return f"{whole}.{scaled % base:0{places}d}".rstrip("0").rstrip(".")


def timestamp(value):
    """Convert an instant to milliseconds, interpreting dates and naive times in Shanghai."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, date):
            parsed = datetime.combine(value, wall_time())
        else:
            parsed = datetime.fromisoformat(str(value))
        if parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=CN)
        delta = parsed - _EPOCH
        return -((-delta) // _MILLISECOND) if delta.days < 0 else delta // _MILLISECOND
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def render_candle(day, prices, volume, amount):
    candle = {key: decimal(value) for key, value in prices.items()}
    if any(value is None for value in candle.values()):
        return None
    if not (0 < Decimal(candle["low"]) <= min(Decimal(candle["open"]), Decimal(candle["close"]))
            <= max(Decimal(candle["open"]), Decimal(candle["close"])) <= Decimal(candle["high"])):
        raise ValueError("invalid OHLC")
    volume = _finite_decimal(volume)
    if volume is None or volume < 0 or volume != volume.to_integral_value() or volume > 9_007_199_254_740_991:
        raise ValueError("invalid volume")
    return {"date": str(day)[:10], **candle, "volume": int(volume), "amount": decimal(amount)}


class MarketAdapter:
    def __init__(self, repo, engine, *, context_builder=None, override_loader=None, calendar=None, quote_snapshot=None,
                 execution_quotes=None):
        self.repo, self.engine = repo, engine
        self.context_builder = context_builder
        self.override_loader = override_loader
        self.calendar = calendar
        self.quote_snapshot = quote_snapshot or {}
        self.execution_quotes = execution_quotes or {}

    def symbols(self, now):
        from app.services.tickdesk_exchange_master import read_exchange_catalog
        from app.services.tickdesk_fund_chart import read_fund_master

        root = self.repo.store.data_dir
        result = []
        for asset, observed in (("stock", read_exchange_catalog(root, now)), ("etf", read_fund_master(root, now))):
            rows = {row["symbol"]: row for row in self.repo.get_instruments_asset(asset).to_dicts() if row.get("symbol")}
            for record in observed:
                known = rows.get(record["symbol"])
                if asset == "stock" or not (known and known.get("status") in ("listed", "active", "上市", "L")):
                    rows[record["symbol"]] = record
            for row in rows.values():
                symbol = row.get("symbol", "")
                exchange = {"SH": "SSE", "SZ": "SZSE", "SSE": "SSE", "SZSE": "SZSE"}.get(row.get("exchange"))
                if not re.fullmatch(r"[0-9]{6}\.(SH|SZ)", symbol) or exchange is None:
                    continue
                board = row.get("board") or "unknown"
                as_of = timestamp(row.get("as_of"))
                source = ({"name": str(row["source"]), "ref": row.get("source_ref") or f"kernel:instruments:{asset}:{symbol}", "asOf": as_of}
                          if row.get("source") and as_of is not None and as_of <= now else None)
                supported = bool(source and row.get("status") in ("listed", "active", "上市", "L")
                                 and (asset == "etf" or board in ("沪主板", "深主板", "创业板")))
                result.append({"symbol": symbol, "name": str(row.get("name") or symbol),
                               "exchange": exchange, "instrument": asset, "board": board,
                               "supported": supported, "source": source})
        return sorted(result, key=lambda item: item["symbol"])

    def chart(self, data, now):
        from app.services.tickdesk_chart_periods import completed_periods, period_chart_page
        from app.services.tickdesk_fund_chart import fund_chart_page
        from app.services.tickdesk_research import FINALITY, _verified_row, _witnesses

        symbol, adjustment = data["symbol"], data["adjustment"]
        timeframe = data.get("timeframe", "1d")
        if timeframe not in ("1d", "1w", "1mo", "1m", "5m"):
            raise ValueError("unsupported chart timeframe")
        missing = {"symbol": symbol, "adjustment": adjustment, "asOf": None, "dataStatus": "missing",
                   "candles": [], "nextBeforeDate": None, "hasMore": False, "source": None,
                   "nextCursor": None}
        if timeframe != "1d":
            missing["timeframe"] = timeframe
        master = next((s for s in self.symbols(now) if s["symbol"] == symbol), None)
        if master is None:
            return missing
        if timeframe in ("1m", "5m"):
            return self._minute_chart(data, now, master, missing)
        end = date.fromisoformat(data["beforeDate"]) - timedelta(days=1) if data.get("beforeDate") else datetime.fromtimestamp(now / 1000, CN).date()
        limit = min(20, max(1, data.get("limit", 20)))
        periods = completed_periods(self.repo.store.data_dir, now, end, timeframe) if timeframe != "1d" else None
        if periods == []:
            return missing
        expected = self._latest_completed_session(now, end)
        if master["instrument"] == "etf" and adjustment == "forward":
            fund = fund_chart_page(self.repo.store.data_dir, missing, limit, end, expected, now, periods=periods)
            if fund is not None:
                return fund
        frame = self.repo.get_daily_asset(master["instrument"], symbol, date(1990, 1, 1), end)
        if frame.is_empty():
            return missing
        columns = {key: f"raw_{key}" if adjustment == "none" else key for key in ("open", "high", "low", "close")}
        snapshot = getattr(self.repo, "get_quant_evidence_snapshot", None)
        witnesses = _witnesses(snapshot({FINALITY}), now) if callable(snapshot) else {}
        rows = sorted(frame.to_dicts(), key=lambda row: str(row["date"]))
        dates = [str(row["date"])[:10] for row in rows]
        if len(dates) != len(set(dates)):
            raise ValueError("duplicate chart date")
        rows = [row for row in rows if str(row["date"])[:10] <= end.isoformat()]
        if expected:
            rows = [row for row in rows if str(row["date"])[:10] <= expected]
        candles = []
        verified = []
        daily = {}
        required = {day for period in periods for day in period} if periods is not None else set()
        for row in (rows if periods is not None else rows[-limit:]):
            day = str(row["date"])[:10]
            if periods is not None and day not in required:
                continue
            witness = _verified_row(row, witnesses) if row.get("symbol") == symbol else None
            prices = {key: row.get(column) for key, column in columns.items()}
            if adjustment == "none":
                raw_open = witness["value"].get("raw_open") if witness else None
                if raw_open is not None:
                    prices["open"] = raw_open
                else:
                    witness = None
            if periods is not None and witness is None:
                continue
            candle = render_candle(row["date"], prices, row.get("volume"), row.get("amount"))
            if candle is None:
                return missing
            if periods is not None:
                daily[day] = (candle, {"name": witness["source"], "ref": f"kernel:daily:{symbol}:{adjustment}",
                                       "asOf": timestamp(witness["available_at"])})
                continue
            candles.append(candle)
            verified.append(witness)
        if periods is not None:
            return period_chart_page(missing, limit, periods, daily)
        if not candles:
            return missing
        as_of = source = None
        if all(verified) and len({item["source"] for item in verified}) == 1:
            as_of = timestamp(verified[-1]["value"]["end_at"])
            source = {"name": verified[-1]["source"], "ref": f"kernel:daily:{symbol}:{adjustment}",
                      "asOf": max(timestamp(item["available_at"]) for item in verified)}
        status = "fresh" if source is not None and candles[-1]["date"] == expected else "stale"
        return {**missing, "dataStatus": status, "candles": candles, "asOf": as_of, "source": source,
                "nextBeforeDate": candles[0]["date"] if len(rows) > limit else None,
                "hasMore": len(rows) > limit}

    def _minute_chart(self, data, now, master, missing):
        symbol, timeframe = data["symbol"], data["timeframe"]
        if data["adjustment"] != "none":
            # Stored minute bars are raw; do not label them as adjusted.
            return missing
        limit = min(20, max(1, data.get("limit", 20)))
        today = datetime.fromtimestamp(now / 1000, CN).date()
        end = today
        start = end - timedelta(days=30)
        try:
            frame = self.repo.get_minute_range(
                [symbol], start, end, asset_type=master["instrument"]
            )
        except Exception:
            return missing
        if frame.is_empty() or "datetime" not in frame.columns:
            return missing

        def stamp_value(value):
            if isinstance(value, datetime):
                stamp = value if value.tzinfo else value.replace(tzinfo=CN)
            elif isinstance(value, date):
                stamp = datetime.combine(value, wall_time(0, 0), tzinfo=CN)
            else:
                try:
                    stamp = datetime.fromisoformat(str(value))
                except (TypeError, ValueError):
                    return None
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=CN)
            return stamp

        def available_at(row):
            for key in ("available_at", "availableAtMs", "availableAt"):
                if key not in row or row[key] is None:
                    continue
                value = row[key]
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    number = int(value)
                    return number if number > 0 else None
                stamp = stamp_value(value)
                return timestamp(stamp) if stamp is not None else None
            return None

        try:
            from app.services.tickdesk_market_data import read_calendar
            calendar = read_calendar(Path(self.repo.store.data_dir), now)
        except (AttributeError, OSError, ValueError, TypeError):
            calendar = None

        def trading_day(stamp):
            day = stamp.date().isoformat()
            if stamp.weekday() >= 5:
                return False
            if calendar and calendar["sessions"][0] <= day <= calendar["through"]:
                return day in calendar["sessions"]
            return True

        def session_minute(stamp):
            clock = stamp.time()
            return (wall_time(9, 30) <= clock < wall_time(11, 30)
                    or wall_time(13, 0) <= clock < wall_time(15, 0))

        by_minute = {}
        duplicate = set()
        for row in frame.to_dicts():
            if row.get("symbol") not in (None, symbol):
                continue
            stamp = stamp_value(row.get("datetime"))
            if stamp is None:
                continue
            at = timestamp(stamp)
            # Only rows that belong to this security, this trading session and
            # started no later than the request cutoff may enter the chart.
            if (at is None or at > now or at % 60_000 != 0
                    or not trading_day(stamp) or not session_minute(stamp)):
                continue
            available = available_at(row)
            if available is not None and (available > now or available < at):
                continue
            open_, high, low, close = (_finite_decimal(row.get(key)) for key in ("open", "high", "low", "close"))
            volume = _finite_decimal(row.get("volume"))
            amount = _finite_decimal(row.get("amount"))
            if (None in (open_, high, low, close) or volume is None or volume < 0
                    or not (0 < low <= min(open_, close) <= max(open_, close) <= high)):
                continue
            normalized = {
                "at": at, "open": open_, "high": high, "low": low, "close": close,
                "volume": volume,
                "amount": amount if amount is not None and amount >= 0 else None,
                "available": available,
            }
            prior = by_minute.get(at)
            if prior is None:
                by_minute[at] = normalized
            elif prior != normalized:
                # Two different bars claim the same minute. Do not pick one
                # and call it final; keep the minute marked as a conflict.
                duplicate.add(at)
        if not by_minute:
            return missing
        interval = 5 if timeframe == "5m" else 1
        step = interval * 60_000
        candles = []
        gaps = []
        buckets = sorted({at - (at % step) for at in by_minute})
        for key in buckets:
            end_at = key + step
            if interval == 1:
                members = [(key, by_minute[key])]
            else:
                members = [
                    (key + offset * 60_000, by_minute[key + offset * 60_000])
                    for offset in range(interval)
                    if key + offset * 60_000 in by_minute
                ]
            missing_offsets = [
                offset for offset in range(interval) if key + offset * 60_000 not in by_minute
            ]
            if interval == 5 and 0 in missing_offsets:
                # Without the first minute there is no known opening price for
                # this window. Expose a gap instead of borrowing 09:31's price.
                gaps.append({
                    "startAt": key, "endMs": end_at, "quality": "missing_first_minute",
                    "missingMinutes": [key + offset * 60_000 for offset in missing_offsets],
                    "observedMinutes": len(members),
                })
                continue
            if any(minute in duplicate for minute, _ in members):
                quality = "duplicate_minute"
            elif missing_offsets:
                quality = "incomplete_group"
            elif any(member[1]["amount"] is None for member in members):
                quality = "incomplete_amount"
            else:
                quality = "ok"
            explicit = [member[1]["available"] for member in members if member[1]["available"] is not None]
            available = max([end_at, *explicit]) if explicit else end_at
            complete = (
                len({minute for minute, _ in members}) == interval
                and len(members) == interval
                and quality == "ok"
                and end_at <= now
                and available <= now
            )
            sample = members[0][1]
            last = members[-1][1]
            amounts = [member[1]["amount"] for member in members]
            amount = sum(amounts) if all(value is not None for value in amounts) else None
            candles.append({
                "date": datetime.fromtimestamp(key / 1000, CN).date().isoformat(),
                "open": _wire_decimal(sample["open"]),
                "high": _wire_decimal(max(member[1]["high"] for member in members)),
                "low": _wire_decimal(min(member[1]["low"] for member in members)),
                "close": _wire_decimal(last["close"]),
                "volume": int(sum(member[1]["volume"] for member in members)),
                "amount": _wire_decimal(amount) if amount is not None else None,
                "startAt": key, "endMs": end_at,
                "availableAtMs": available if complete else None,
                "complete": complete, "interval": timeframe, "quality": quality,
            })
        if not candles and not gaps:
            return missing
        digest = fingerprint({
            "symbol": symbol, "timeframe": timeframe, "adjustment": data["adjustment"],
            "candles": candles, "gaps": gaps,
        })
        offset = None
        if isinstance(data.get("cursor"), str):
            parts = data["cursor"].split(":")
            if len(parts) != 4 or parts[0] != "v2" or parts[3] != digest:
                return {**missing, "timeframe": timeframe, "cursorExpired": True}
            try:
                offset = int(parts[1])
                page_limit = int(parts[2])
            except ValueError:
                return {**missing, "timeframe": timeframe, "cursorExpired": True}
            if offset < 0 or offset > len(candles) or page_limit < 1 or page_limit > 20:
                return {**missing, "timeframe": timeframe, "cursorExpired": True}
        else:
            page_limit = limit
            offset = len(candles)
        page_start = max(0, offset - page_limit)
        page = candles[page_start:offset]
        next_offset = page_start
        next_cursor = (f"v2:{next_offset}:{page_limit}:{digest}"
                       if next_offset > 0 else None)
        if page:
            lower, upper = page[0]["startAt"] - step, page[-1]["endMs"]
            page_gaps = [gap for gap in gaps if gap["endMs"] > lower and gap["startAt"] < upper]
        else:
            page_gaps = gaps
        as_of = page[-1]["availableAtMs"] if page and page[-1]["complete"] else None
        partial = any(candle["quality"] != "ok" for candle in page) or bool(page_gaps)
        return {
            **missing, "timeframe": timeframe,
            "dataStatus": ("partial" if partial else
                           "fresh" if as_of is not None and now - as_of < 900_000 else "stale"),
            "candles": page, "gaps": page_gaps[:200], "asOf": as_of,
            "nextBeforeDate": None, "nextCursor": next_cursor,
            "hasMore": next_cursor is not None,
            "source": ({"name": "local-minute-store", "ref": f"local:minute:{symbol}:{timeframe}",
                        "asOf": as_of} if as_of is not None and as_of <= now else None),
        }

    def _latest_completed_session(self, now, end):
        from app.services.tickdesk_market_data import read_calendar

        local = datetime.fromtimestamp(now / 1000, CN)
        through = min(end, local.date() if local.time() >= wall_time(15, 30) else local.date() - timedelta(days=1))
        calendar = read_calendar(self.repo.store.data_dir, now)
        if calendar and calendar["through"] >= through.isoformat():
            return max((day for day in calendar["sessions"] if day <= through.isoformat()), default=None)
        return None

    def quotes(self, symbols, now):
        quotes = []
        for symbol in sorted(set(symbols)):
            if not re.fullmatch(r"[0-9]{6}\.(SH|SZ)", symbol):
                continue
            row = self.quote_snapshot.get(symbol, {})
            source = row.get("name")
            at = row.get("market_timestamp")
            number = _finite_decimal(row.get("price"))
            if (not isinstance(source, str) or not 0 < len(source) <= 100
                    or type(at) is not int or not 0 < at <= now or number is None or number <= 0):
                continue
            price = _wire_decimal(number)
            if price is not None:
                quote = {"symbol": symbol, "price": price, "asOf": at,
                         "source": {"name": source, "ref": f"kernel:realtime:{symbol}", "asOf": at}}
                if row.get("timeBasis") in {"snapshot", "trade", "settlement"}:
                    quote["timeBasis"] = row["timeBasis"]
                quotes.append(quote)
        return quotes

    def scan(self, request, approval):
        from app.services.tickdesk_admission import approval_policy, validate_approval

        started = time.monotonic()
        now = request["requestedAt"]
        day = datetime.fromtimestamp(now / 1000, CN).date()
        if self.override_loader:
            overrides = self.override_loader()
        else:
            from app.strategy import config
            overrides = config.list_overrides(self.repo.store.data_dir)
        revision = self.engine.definition_revision
        accepted = validate_approval(
            self.engine, self.repo.store.data_dir, approval, overrides, requested_at=now, profile=request["profile"],
        )
        policy = approval_policy(approval["profiles"], request["profile"])
        if self.trading_verdict(now) is not True:
            raise ValueError("trading calendar unavailable or closed")
        if approval["version"] == 3:
            from app.services.tickdesk_v3_scan import freeze_v3_scan

            return freeze_v3_scan(self, now, approval, {sid: entry[1] for sid, entry in accepted.items()}, policy, overrides)
        definitions = {sid: definition for sid, (definition, _, _) in accepted.items()}
        weights = {sid: weight for sid, (_, weight, _) in accepted.items()}
        minimum_amount = Decimal(policy["minimumAmount"])
        specs = approval["strategies"]
        master = {s["symbol"]: s for s in self.symbols(now) if s["supported"]}
        if not master:
            raise ValueError("supported instrument metadata missing")
        before = {asset: self.repo.get_matrix_data_generation(asset) for asset in {s["instrument"] for s in master.values()}}
        results, failed, inputs, sources = {}, set(), {}, {}
        groups = {}
        for asset in sorted(before):
            ids = [s["id"] for s in specs if asset in definitions[s["id"]].meta.get("asset_types", ["stock"])]
            if not ids:
                continue
            params = {sid: dict(overrides.get(sid, {}).get("params", {})) for sid in ids}
            builder = self.context_builder
            if builder is None:
                from app.services.screener import ScreenerService
                builder = ScreenerService(self.repo, asset_type=asset).build_strategy_context
            context = builder(self.engine, day, ids, timeframe="1d", params_map=params,
                              overrides_map={sid: overrides.get(sid, {}) for sid in ids})
            if context.current is None or context.current.is_empty() or context.data_generation != before[asset]:
                raise ValueError("frozen context generation or data unavailable")
            pool = [symbol for symbol, item in master.items() if item["instrument"] == asset]
            rows = {r["symbol"]: r for r in context.current.to_dicts() if r["symbol"] in pool}
            if not rows or any(timestamp(row.get("quote_ts")) is None or not 0 <= now - timestamp(row["quote_ts"]) <= 90000 for row in rows.values()):
                raise ValueError("quote timestamps stale or missing")
            inputs[asset] = {"generation": before[asset], "currentHash": fingerprint(context.current.to_dicts()),
                             "historyHash": fingerprint(context.history.to_dicts()) if context.history is not None else None,
                             "universeHash": fingerprint(pool)}
            sources.update(rows)
            for sid in ids:
                if time.monotonic() - started > 5:
                    raise ValueError("scan deadline exceeded")
                try:
                    result = self.engine.run(sid, context, pool=pool, params=params[sid], overrides=overrides.get(sid))
                except Exception:  # noqa: BLE001 -- failure is explicit, never interpreted as no hit
                    failed.add(sid)
                    continue
                results[sid] = result
                spec = next(s for s in specs if s["id"] == sid)
                for row in result.rows:
                    # The engine's score map replaces a missing row score with zero.
                    score = _finite_decimal(row.get("score", result.scores.get(row["symbol"])))
                    if score is None or not 0 <= score <= 100:
                        continue
                    groups.setdefault((row["symbol"], spec["direction"]), []).append((sid, score, weights[sid]))
        after = {asset: self.repo.get_matrix_data_generation(asset) for asset in before}
        if before != after or self.engine.definition_revision != revision:
            raise ValueError("generation changed during evaluation")
        if self.override_loader and fingerprint(self.override_loader()) != fingerprint(overrides):
            raise ValueError("parameter generation changed during evaluation")
        candidates = []
        for (symbol, direction), hits in sorted(groups.items()):
            hits = [hit for hit in hits if hit[0] not in failed]
            if not hits:
                continue
            row = sources[symbol]
            amount = _finite_decimal(row.get("amount"))
            stamp = timestamp(row.get("quote_ts"))
            score = _weighted_score(hits)
            asset_generation = before[master[symbol]["instrument"]]
            # Current enriched rows can carry bar provenance without identifying the quote feed.
            source = {"name": "unknown", "ref": f"kernel:quote:generation:{asset_generation}", "asOf": stamp}
            candidate = {"symbol": symbol, "name": master[symbol]["name"], "score": score,
                         "strategies": sorted(sid for sid, _, _ in hits), "changeRatio": decimal(row.get("change_pct")),
                         "volumeRatio": decimal(row.get("vol_ratio_5d")), "marketCap": decimal(row.get("market_cap")),
                         "lastPrice": decimal(row.get("raw_close")), "dataAsOf": stamp, "riskStatus": "unknown"}
            candidates.append({"candidate": candidate, "direction": direction, "approved": True, "supported": True,
                               "liquidityPassed": amount is not None and amount >= minimum_amount, "source": source})
        generation = fingerprint(before)
        succeeded = results.keys() - failed
        return {"tradeDate": day.isoformat(), "isTradingDay": True, "generation": generation,
                "generationAfter": generation, "asOf": min(timestamp(r["quote_ts"]) for r in sources.values()),
                "strategies": {"total": len(specs), "succeeded": len(succeeded),
                               "failed": sorted({s["id"] for s in specs} - succeeded)},
                "policy": policy, "candidates": candidates, "manifest": {"inputs": inputs, "approvalHash": fingerprint(approval),
                                                                           "strategyHashes": specs, "parametersHash": fingerprint(overrides)}}

    def trading_verdict(self, now):
        if self.calendar:
            return self.calendar(datetime.fromtimestamp(now / 1000, CN).date())
        from app.services.tickdesk_market_data import calendar_verdict

        return calendar_verdict(Path(self.repo.store.data_dir), now)

    def archived_holding_batch(self, request):
        from app.services.tickdesk_holding_rules import prepare_holding_rule_batch

        with contextlib.suppress(OSError, ValueError, KeyError, TypeError, RuntimeError, MemoryError):
            batch = prepare_holding_rule_batch(self.repo, request)
            if batch is not None:
                return {"holdingRuleBatch": batch}
        return {}

    def prepare(self, request):
        data, now = request["data"], request["requestedAt"]
        command = data["cmd"]
        if command == "get_quote_status":
            return {"quotes": self.quotes(data["symbols"], now)}
        if command in ("scan_now", "resume"):
            return self._prepare_scan(request, now)

        symbols = {obj["symbol"] for obj in request.get("objects", []) if obj.get("symbol")}
        if command == "watch":
            symbols.update(data["symbols"])
        quotes = self.quotes(symbols, now) if symbols else []
        if command == "quotes":
            version = request.get("holdingRuleVersion")
            holding = self.archived_holding_batch(request) if type(version) is int and version == 3 else {}
            return {"quotes": quotes, **holding}
        if command in ("watch", "unwatch", "search_symbols"):
            return {"symbols": self.symbols(now), "quotes": quotes}
        if command == "get_chart":
            chart = self.chart(data, now)
            response = {"chart": chart, "quotes": quotes}
            if chart.get("cursorExpired"):
                chart.pop("cursorExpired", None)
                response["error"] = "CHART_CURSOR_EXPIRED"
            return response
        raise ValueError("unsupported market command")

    def _prepare_scan(self, request, now):
        from app.services.tickdesk_market_data import scan_calendar_evidence

        version = request.get("quantObservationVersion")
        if version is not None and (type(version) is not int or version != 3):
            raise ValueError("unsupported quant observation version")
        root = Path(self.repo.store.data_dir)
        calendar = scan_calendar_evidence(root, now, calendar_override=self.calendar)
        response = {"quotes": [], "tradingCalendar": calendar}
        if calendar["state"] == "closed":
            return {**response, "error": "SCAN_OUTSIDE_WINDOW"}
        if calendar["state"] == "unknown":
            return {**response, "error": "DATA_UNAVAILABLE", "productionReason": "TRADING_CALENDAR_UNAVAILABLE"}

        from app.services.tickdesk_admission import AdmissionBlockedError, read_approval
        from app.services.tickdesk_worker import scan_in_worker

        symbols = {obj["symbol"] for obj in request.get("objects", []) if obj.get("symbol")}
        if symbols:
            response["quotes"] = self.quotes(symbols, now)
        # Both scans share the original worker budget. A worker that reaches
        # the deadline leaves up to two seconds of termination grace under the
        # existing local HTTP deadline.
        deadline = time.monotonic() + 5.0
        response["error"] = "DATA_UNAVAILABLE"
        scan_failures = (OSError, ValueError, KeyError, TypeError, RuntimeError, MemoryError)
        holding = self.archived_holding_batch(request) if version == 3 else {}
        try:
            approval = read_approval(root)
            frozen = scan_in_worker(self, request, approval, trading=True, deadline=deadline)
            if fingerprint(read_approval(root)) != fingerprint(approval):
                raise ValueError("approval changed during scan")
            response["frozen"] = frozen
            del response["error"]
        except AdmissionBlockedError as exc:
            response["productionReason"] = ("NO_PRODUCTION_APPROVAL" if exc.reason == "NO_PRODUCTION_APPROVAL"
                                            else "PRODUCTION_EVALUATION_FAILED")
            response["strategyAdmission"] = exc.diagnostic()
        except TimeoutError:
            response["error"] = "SCAN_DEADLINE_EXCEEDED"
        except scan_failures:
            response["productionReason"] = "PRODUCTION_EVALUATION_FAILED"

        if version == 3:
            from app.services.tickdesk_research import read_research_config
            from app.services.tickdesk_worker import research_in_worker

            try:
                config = read_research_config(root)
                if config is not None:
                    response["quantObservationBatch"] = research_in_worker(self, request, config, deadline=deadline)
            except scan_failures:
                response["quantObservationError"] = "DATA_UNAVAILABLE"
        return {**response, **holding}
