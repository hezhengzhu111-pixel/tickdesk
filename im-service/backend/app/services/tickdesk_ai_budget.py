"""Durable per-channel accounting for explicitly configured model budgets."""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path

from app.market_time import CN_TZ

NANO = 10**9
MAX_TOKENS = 100_000_000
BOOKING = """CREATE TABLE IF NOT EXISTS model_calls (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, epoch TEXT NOT NULL,
    job_id TEXT NOT NULL, day TEXT NOT NULL, currency TEXT,
    request_bytes INTEGER NOT NULL, cost_nano INTEGER,
    input_tokens INTEGER, output_tokens INTEGER, UNIQUE (kind,epoch,job_id));
    CREATE INDEX IF NOT EXISTS day_channel ON model_calls(day,kind,currency);"""


class BudgetError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelLimits:
    currency: str
    daily: Decimal
    input_per_million: Decimal
    output_per_million: Decimal
    input_ceiling: int
    calls: int

    @classmethod
    def configured(cls, kind: str):
        prefix = f"TICKDESK_AI_{kind.upper()}_BUDGET_"
        keys = ("CURRENCY", "DAILY", "INPUT_PER_MILLION", "OUTPUT_PER_MILLION", "MAX_INPUT_TOKENS", "MAX_CALLS")
        currency, daily, input_rate, output_rate, ceiling, calls = [os.getenv(prefix + key) for key in keys]
        if not any(value is not None for value in (currency, daily, input_rate, output_rate, ceiling, calls)):
            return None
        try:
            if currency not in ("CNY", "USD"):
                raise ValueError
            amounts = tuple(Decimal(value) for value in (daily, input_rate, output_rate))
            if not all(amount.is_finite() and Decimal("0.000000001") <= amount <= 1_000_000 for amount in amounts):
                raise ValueError
            input_ceiling, call_limit = int(ceiling), int(calls)
            if not 1 <= input_ceiling <= MAX_TOKENS or not 1 <= call_limit <= 100_000:
                raise ValueError
        except (ValueError, TypeError, InvalidOperation):
            raise BudgetError("AI_BUDGET_NOT_CONFIGURED") from None
        return cls(currency, *amounts, input_ceiling, call_limit)

    def cost_nano(self, input_tokens: int, output_tokens: int) -> int:
        cost = (input_tokens * self.input_per_million + output_tokens * self.output_per_million) * 1000
        return int(cost.to_integral_value(rounding=ROUND_CEILING))


def _trading_day(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1000, CN_TZ).date().isoformat()


class ModelBudget:
    def __init__(self, path: Path):
        self.path = path
        with self._db() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(BOOKING)

    @contextmanager
    def _db(self):
        with closing(sqlite3.connect(self.path, timeout=2)) as conn, conn:
            conn.execute("PRAGMA synchronous=FULL")
            yield conn

    def reserve(self, kind, epoch, job_id, request_bytes, output_ceiling, limits, *, now) -> int:
        reservation, _ = self._reserve(kind, epoch, job_id, lambda _: request_bytes,
                                       output_ceiling, output_ceiling, limits, now=now)
        return reservation

    def reserve_up_to(self, kind, epoch, job_id, request_size, minimum, maximum, limits: ModelLimits,
                      *, now) -> tuple[int, int]:
        """Choose and book the largest permitted output with the same write lock."""
        return self._reserve(kind, epoch, job_id, request_size, minimum, maximum, limits, now=now)

    def _reserve(self, kind, epoch, job_id, request_size, minimum, output_ceiling, limits, *, now):
        day = _trading_day(now)
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM model_calls WHERE kind=? AND epoch=? AND job_id=?",
                            (kind, epoch, job_id)).fetchone():
                raise BudgetError("AI_REPLAY")
            reserve = currency = None
            if limits:
                currency = limits.currency
                calls, existing_cost, other_currency, unmetered = conn.execute(
                    """SELECT count(*), coalesce(sum(cost_nano),0), coalesce(sum(currency!=?),0),
                              coalesce(sum(cost_nano IS NULL),0)
                       FROM model_calls WHERE day=? AND kind=?""", (currency, day, kind),
                ).fetchone()
                if other_currency:
                    raise BudgetError("AI_BUDGET_NOT_CONFIGURED")
                if calls >= limits.calls:
                    raise BudgetError("AI_BUDGET_EXHAUSTED")
                carry = unmetered * limits.cost_nano(limits.input_ceiling, minimum)
                remaining = int(limits.daily * NANO) - existing_cost - carry
                reserved_input = limits.input_ceiling * limits.input_per_million * 1000
                affordable = int((remaining - reserved_input) // (limits.output_per_million * 1000))
                output_ceiling = min(output_ceiling, affordable)
                if output_ceiling < minimum:
                    raise BudgetError("AI_BUDGET_EXHAUSTED")
                reserve = limits.cost_nano(limits.input_ceiling, output_ceiling)
            request_bytes = request_size(output_ceiling)
            if limits and kind == "chat" and request_bytes > limits.input_ceiling:
                raise BudgetError("AI_BUDGET_EXHAUSTED")
            reservation = conn.execute(
                """INSERT INTO model_calls(kind,epoch,job_id,day,currency,request_bytes,cost_nano)
                   VALUES (?,?,?,?,?,?,?) RETURNING id""",
                (kind, epoch, job_id, day, currency, request_bytes, reserve),
            ).fetchone()[0]
            return reservation, output_ceiling

    def settle(self, reservation: int, usage, limits) -> None:
        if not isinstance(usage, dict):
            return
        inputs, outputs = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if not all(type(value) is int and 0 <= value <= MAX_TOKENS for value in (inputs, outputs)):
            return
        estimated = limits.cost_nano(inputs, outputs) if limits else None
        with self._db() as conn:
            conn.execute("""UPDATE model_calls SET input_tokens=?,output_tokens=?,cost_nano=?
                WHERE id=? AND input_tokens IS NULL""", (inputs, outputs, estimated, reservation))

    def status(self, *, now) -> dict:
        with self._db() as conn:
            rows = conn.execute(
                """SELECT kind,currency,count(*),sum(input_tokens IS NULL),sum(request_bytes),
                          sum(input_tokens),sum(output_tokens),sum(cost_nano)
                   FROM model_calls WHERE day=? GROUP BY kind,currency ORDER BY kind,currency""",
                (_trading_day(now),),
            ).fetchall()
        data = {"chat": [], "vision": []}
        for kind, currency, calls, unknown, request_bytes, inputs, outputs, cost in rows:
            data[kind].append({"currency": currency, "attempts": calls, "unknownUsage": unknown,
                               "requestBytes": request_bytes, "inputTokens": inputs, "outputTokens": outputs,
                               "conservativeCost": str(Decimal(cost) / NANO) if cost is not None else None})
        return data
