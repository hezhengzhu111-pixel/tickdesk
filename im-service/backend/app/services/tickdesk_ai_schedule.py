"""Deterministic trigger policy for local read-only reports."""
from __future__ import annotations

import asyncio
import hashlib
import re
import sqlite3
from itertools import cycle

from starlette.concurrency import run_in_threadpool

from app.services.tickdesk_ai import (
    PROMPT_VERSION,
    AiError,
    CompletionModel,
    encode,
    local_request,
    now_ms,
    production_quant,
)
from app.services.tickdesk_ai_budget import BudgetError, ModelLimits
from app.services.tickdesk_ai_inputs import read_market_evidence

SYMBOL = re.compile(r"[0-9]{6}\.(?:SH|SZ)")
REPORT_INTERVAL = 30 * 60_000


def decide(evidence: dict, facts: list, model: str, *, previous: dict | None, now: int,
           market: list | None = None) -> dict | None:
    business = evidence["business"]
    if business.get("paused") or business.get("recovering"):
        return None
    quant = evidence.get("quant") or {}
    batch = quant.get("batch") or {}
    production = production_quant(quant, evidence["serverEpoch"], evidence.get("asOf", now))
    observations = [
        {key: row.get(key) for key in (
            "symbol", "group", "timeframe", "strategyId", "codeHash", "parametersHash",
            "dependenciesHash", "inputHash", "rulesHash", "admission", "bar", "entry", "exit", "score",
        )}
        for row in batch.get("observations", [])
        if row["group"] == "daily_swing" and row["bar"].get("status") == "complete"
    ]
    # Scope and archive health may permit a genuine risk update during an unavailable quant scan.
    if (not observations and not (production and production["candidates"]) and not facts and not market
            and not any(business.get("holdingDetails", []))):
        return None
    risk = sorted({row["factId"] for row in facts if "companyRisk" in row.get("categories", [])})
    identity = {
        "version": PROMPT_VERSION, "model": model, "epoch": evidence["serverEpoch"],
        "business": {key: value for key, value in business.items() if key != "positionRevision"},
        "quant": sorted(observations, key=encode),
        "facts": sorted({row["factId"] for row in facts}),
        "market": sorted(market or [], key=lambda row: row["source"]["ref"]),
    }
    if production:
        identity["production"] = {
            "status": production["status"], "reason": production["reason"],
            "candidates": sorted(({key: row[key] for key in ("symbol", "direction", "score", "strategies")}
                                  for row in production["candidates"]), key=encode),
        }
    stable = hashlib.sha256(encode(identity).encode()).hexdigest()
    if not previous:
        trigger = "initial"
    elif previous["state"] in ("pending", "running"):
        return None
    elif stable == previous["fingerprint"]:
        # Charging may be unknown after interruption. A matching input never replays that attempt.
        if previous["errorCode"] or now < previous["validUntil"]:
            return None
        trigger = "expiry"
    else:
        trigger = "risk" if risk != previous.get("riskVersions") else "changed"
    return {"fingerprint": stable, "riskVersions": risk, "trigger": trigger}


async def run_analysis_schedule(archive, repo=None):
    """Poll bounded Rust snapshots and enqueue eligible work. Rust performs the durable claim."""
    symbols = ()
    cursor = cycle(())
    while True:
        try:
            model = CompletionModel.configured("chat")
            if not model.loopback and not ModelLimits.configured("chat"):
                await asyncio.sleep(5)
                continue
            scope = await local_request("GET", "/internal/ai/v1/scope")
            current = tuple(symbol for symbol in scope["symbols"][:64] if SYMBOL.fullmatch(symbol))
            if current != symbols:
                symbols, cursor = current, cycle(current)
            symbol = next(cursor, None)
            if symbol:
                path = f"/internal/ai/v1/analysis/{symbol}"
                sample = await local_request("GET", path, limit=192 * 1024)
                evidence = sample["evidence"]
                facts, _ = await run_in_threadpool(archive.context, evidence["asOf"], symbol, evidence["items"])
                market, _ = await run_in_threadpool(read_market_evidence, repo, evidence["asOf"], [symbol])
                decision = decide(evidence, facts, model.model, previous=sample["latest"], now=now_ms(), market=market)
                if decision:
                    await local_request("POST", path, payload={
                        **decision, "version": 1, "serverEpoch": evidence["serverEpoch"],
                        "stamp": sample["stamp"], "asOf": evidence["asOf"],
                    })
        except (AiError, BudgetError, KeyError, OSError, sqlite3.Error, TypeError, ValueError):
            pass
        await asyncio.sleep(5)
