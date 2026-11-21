"""Local-only, bounded input boundary for TickDesk. Existing gateway auth still applies."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import json
import logging
import os
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from app.services import tickdesk_ai
from app.services.tickdesk_adapter import MarketAdapter
from app.services.tickdesk_ai_budget import BudgetError, ModelBudget, ModelLimits
from app.services.tickdesk_ai_inputs import read_market_evidence

router = APIRouter(prefix="/api/tickdesk", tags=["tickdesk"])
AI_PATHS = {"/api/tickdesk/ai/v1/chat", "/api/tickdesk/ai/v1/vision"}
AI_DIAGNOSTICS = "/api/tickdesk/ai/v1/status"
logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def assistant_lifespan(app, data_dir):
    if not os.getenv("TICKDESK_AI_TOKEN_FILE"):
        yield
        return
    from app.services.tickdesk_facts import FactArchive, run_fact_collection

    archive = budget = None
    try:
        path = Path(data_dir) / "tickdesk-ai" / "facts.sqlite3"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        archive = await run_in_threadpool(FactArchive, path)
    except (OSError, sqlite3.Error):
        logger.warning("TickDesk fact archive unavailable")
    try:
        budget_path = Path(data_dir) / "tickdesk-ai" / "model-budget.sqlite3"
        budget_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        budget = await run_in_threadpool(ModelBudget, budget_path)
    except (OSError, sqlite3.Error):
        logger.warning("TickDesk model accounting unavailable")
    app.state.tickdesk_facts = archive
    app.state.tickdesk_model_budget = budget
    try:
        outcomes_path = Path(data_dir) / "tickdesk-ai" / "ai-outcomes.sqlite3"
        from app.services.tickdesk_ai_outcomes import AiOutcomeStore

        app.state.tickdesk_ai_outcome_cache = await run_in_threadpool(AiOutcomeStore, outcomes_path)
    except (OSError, sqlite3.Error):
        app.state.tickdesk_ai_outcome_cache = None
        logger.warning("TickDesk AI outcome replay unavailable")
    workers = []
    if os.getenv("TICKDESK_QUANT_DATA_ENABLED") == "1" and getattr(app.state, "repo", None) is not None:
        from app.services.tickdesk_exchange_holding import run_holding_evidence

        workers.append(asyncio.create_task(run_holding_evidence(app, app.state.repo), name="tickdesk-holding-evidence"))
    mcp_key = os.getenv("TICKDESK_MCP_KEY_FILE")
    if mcp_key:
        from app.services.tickdesk_mcp import FuyaoMcp

        app.state.tickdesk_mcp = FuyaoMcp(Path(mcp_key))
        workers.append(asyncio.create_task(app.state.tickdesk_mcp.run(), name="tickdesk-mcp-health"))
    if archive is not None:
        async def scope():
            try:
                return await tickdesk_ai.local_request("GET", "/internal/ai/v1/scope")
            except tickdesk_ai.AiError:
                raise ValueError("scope unavailable") from None

        workers.append(asyncio.create_task(run_fact_collection(archive, scope), name="tickdesk-fact-collection"))
        if os.getenv("TICKDESK_AI_AUTO_ENABLED") == "1":
            from app.services.tickdesk_ai_schedule import run_analysis_schedule

            workers.append(asyncio.create_task(run_analysis_schedule(archive, getattr(app.state, "repo", None)),
                                               name="tickdesk-analysis-schedule"))
    try:
        yield
    finally:
        for worker in workers:
            worker.cancel()
        for worker in workers:
            try:
                await worker
            except asyncio.CancelledError:
                pass
            except (OSError, sqlite3.Error, RuntimeError, ValueError):
                logger.error("TickDesk AI background worker stopped with an error")


def _ai_authorize(request: Request):
    if not request.client or not ipaddress.ip_address(request.client.host).is_loopback:
        raise HTTPException(403, "LOCAL_ONLY")
    try:
        token = tickdesk_ai.credential(os.environ["TICKDESK_AI_TOKEN_FILE"])
        if len(token) < 43:
            raise ValueError("short bridge token")
    except (KeyError, OSError, ValueError):
        raise HTTPException(503, "AI_NOT_CONFIGURED") from None
    actual = request.headers.get("authorization", "").encode("latin1")
    if not hmac.compare_digest(actual, f"Bearer {token}".encode("ascii")):
        raise HTTPException(401, "AI_AUTH_REQUIRED")


@router.get("/ai/v1/status", include_in_schema=False)
async def ai_status(request: Request):
    _ai_authorize(request)
    state = request.app.state
    archive = getattr(state, "tickdesk_facts", None)
    models = {}
    for kind in ("chat", "vision"):
        try:
            model = tickdesk_ai.CompletionModel.configured(kind)
            if model.key_file:
                tickdesk_ai.credential(model.key_file)
            models[kind] = "configured"
        except (OSError, ValueError, tickdesk_ai.AiError):
            models[kind] = "unavailable"
    running = getattr(state, "tickdesk_ai_running", set())
    ledger = getattr(state, "tickdesk_model_budget", None)
    try:
        usage = await run_in_threadpool(ledger.status, now=tickdesk_ai.now_ms()) if ledger else None
    except (OSError, sqlite3.Error):
        usage = None
    try:
        fact_budget = await run_in_threadpool(archive.budget_diagnostics, now=tickdesk_ai.now_ms()) if archive else None
    except (OSError, sqlite3.Error):
        fact_budget = None
    limits = {}
    for kind in ("chat", "vision"):
        try:
            limits[kind] = "active" if ModelLimits.configured(kind) else "unconfigured"
        except BudgetError:
            limits[kind] = "invalid_configuration"
    from app.services.tickdesk_mcp import UNCONFIGURED

    mcp = getattr(state, "tickdesk_mcp", None)
    release = getattr(state, "tickdesk_release", None)
    return {"version": 1, "models": models, "running": sorted(running),
            "mcp": mcp.status() if mcp else dict(UNCONFIGURED),
            "quantData": getattr(state, "tickdesk_market_data", {"status": "disabled"}),
            "marketHistory": getattr(state, "tickdesk_history", {"status": "disabled"}),
            "regimeCollection": getattr(state, "tickdesk_regime_collection", {"status": "disabled"}),
            "szseRiskSnapshot": getattr(state, "tickdesk_szse_risk", {"status": "disabled"}),
            "holdingEvidence": getattr(state, "tickdesk_holding_evidence", {"status": "unavailable"}),
            "facts": archive.configuration if archive else "unavailable",
            "factBudget": fact_budget,
            "modelBudget": {"configuration": limits, "dailyUsage": usage},
            "outcomes": getattr(state, "tickdesk_ai_outcomes", {}),
            **({"release": release} if release is not None else {})}


async def _ai_job(request: Request, kind: str, dto, execute, body_limit: int):
    _ai_authorize(request)
    body = bytearray()
    try:
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                if len(body) + len(chunk) > body_limit:
                    raise HTTPException(413, "AI_REQUEST_TOO_LARGE")
                body.extend(chunk)
        job = dto.model_validate(tickdesk_ai.strict_json(body))
    except TimeoutError:
        raise HTTPException(408, "AI_TIMEOUT") from None
    except (ValueError, RecursionError):
        raise HTTPException(422, "AI_INVALID_REQUEST") from None
    now = tickdesk_ai.now_ms()
    if not now < job.deadline <= now + 120_000:
        raise HTTPException(408, "AI_TIMEOUT")

    state = request.app.state
    if not hasattr(state, "tickdesk_ai_claims"):
        state.tickdesk_ai_claims = {}
        state.tickdesk_ai_running = set()
        state.tickdesk_ai_outcomes = {}
    claims = state.tickdesk_ai_claims
    running = state.tickdesk_ai_running
    for key, deadline in list(claims.items()):
        if deadline <= now:
            del claims[key]
    epoch = job.evidence.serverEpoch if kind == "chat" else job.serverEpoch
    key = kind, epoch, job.jobId
    outcomes = getattr(state, "tickdesk_ai_outcome_cache", None)
    cached = outcomes.get(kind, epoch, job.jobId, now) if outcomes is not None else None
    if cached is not None:
        status, body = cached
        if status == 200:
            return Response(content=body.encode("utf-8"), media_type="application/json")
        try:
            detail = json.loads(body).get("detail", "AI_UNAVAILABLE")
        except ValueError:
            detail = "AI_UNAVAILABLE"
        raise HTTPException(status, detail)
    if key in claims:
        raise HTTPException(409, "AI_REPLAY")

    def remember_http(status: int, payload) -> None:
        if outcomes is None:
            return
        with contextlib.suppress(OSError, sqlite3.Error):
            outcomes.put(kind, epoch, job.jobId, tickdesk_ai.now_ms(), status, payload)
    if kind in running or len(claims) >= 512:
        raise HTTPException(429, "AI_BUSY")
    claims[key] = job.deadline
    running.add(kind)
    outcome = "AI_INVALID_RESPONSE"
    stage = None
    issues = None
    try:
        model = getattr(state, f"tickdesk_{kind}_model", None)
        saved_report = kind == "chat" and tickdesk_ai.REPORT_QUERY.fullmatch(job.text.strip())
        if model is None and not saved_report:
            model = tickdesk_ai.CompletionModel.configured(kind)
        if isinstance(model, tickdesk_ai.CompletionModel):
            model.configure_accounting(getattr(state, "tickdesk_model_budget", None), kind, epoch,
                                       job.jobId, scheduled=kind == "chat" and bool(job.evidence.scheduled))
        async with asyncio.timeout((job.deadline - now) / 1000):
            result = await execute(job, model)
        envelope = {"version": 1, "jobId": job.jobId,
                    "model": model.model if model else "local-report-archive", "result": result}
        if kind == "chat":
            envelope["providerResponded"] = result.pop("providerResponded", False)
            issues = result.pop("analysisSchemaIssues", None)
        raw = tickdesk_ai.encode(envelope).encode("utf-8")
        result_limit = tickdesk_ai.CHAT_BRIDGE_RESULT_LIMIT if kind == "chat" else tickdesk_ai.RESULT_LIMIT
        if len(raw) > result_limit:
            raise tickdesk_ai.AiError("AI_RESPONSE_TOO_LARGE")
        outcome = result.get("analysisError", "completed") if kind == "chat" else "completed"
        if kind == "chat":
            stage = result.get("analysisErrorStage")
        remember_http(200, raw.decode("utf-8"))
        return Response(content=raw, media_type="application/json")
    except tickdesk_ai.AiError as error:
        outcome = error.code
        stage = error.stage
        issues = error.issues
        remember_http(422, json.dumps({"detail": error.code}))
        raise HTTPException(422, error.code) from None
    except TimeoutError:
        outcome = "AI_TIMEOUT"
        remember_http(408, json.dumps({"detail": "AI_TIMEOUT"}))
        raise HTTPException(408, "AI_TIMEOUT") from None
    except (ValueError, KeyError, TypeError, RecursionError):
        outcome = "VISION_INVALID_RESPONSE" if kind == "vision" else "AI_INVALID_RESPONSE"
        remember_http(422, json.dumps({"detail": outcome}))
        raise HTTPException(422, outcome) from None
    finally:
        running.remove(kind)
        state.tickdesk_ai_outcomes[kind] = {"status": outcome, "elapsedMs": tickdesk_ai.now_ms() - now}
        if stage is not None:
            state.tickdesk_ai_outcomes[kind]["stage"] = stage
        if issues:
            state.tickdesk_ai_outcomes[kind]["schemaIssues"] = issues


@router.post("/ai/v1/chat", include_in_schema=False)
async def ai_chat(request: Request):
    async def run(job, model):
        archive = getattr(request.app.state, "tickdesk_facts", None)
        symbols = tickdesk_ai.analysis_subjects(job)
        scope = " ".join(symbols) or tickdesk_ai.analysis_question(job.text)
        async def read(cutoff):
            if archive:
                try:
                    return await run_in_threadpool(archive.context, cutoff, scope, job.evidence.items)
                except (OSError, sqlite3.Error):
                    pass
            return [], {"reason": "FACT_ARCHIVE_UNAVAILABLE"}

        facts, coverage = await read(job.evidence.asOf)
        saved = tickdesk_ai.REPORT_QUERY.fullmatch(job.text.strip())
        if saved:
            return await tickdesk_ai.read_saved_report(
                job, saved[1].upper(), current_facts=facts,
                facts_available=coverage.get("status") == "active",
            )
        historical = tickdesk_ai.historical_question(job.text)
        repo = getattr(request.app.state, "repo", None)
        market, market_coverage = await run_in_threadpool(
            read_market_evidence, repo, job.evidence.asOf, symbols if not historical else [],
        )
        if (not historical and not job.evidence.scheduled and symbols
                and not any(group in job.text for group in ("[short_term]", "[long_term]"))):
            engine = getattr(request.app.state, "strategy_engine", None)
            job = await run_in_threadpool(tickdesk_ai.prepare_current_quant, job, repo, engine, symbols, market)
        result = await tickdesk_ai.chat(job, model, external=facts, coverage=coverage,
                                        market=market, market_coverage=market_coverage,
                                        quant_on_error=request.headers.get("X-TickDesk-Quant-On-Error") == "1")
        if not historical and "analysisError" not in result:
            now = tickdesk_ai.now_ms()
            current, _ = await read(now)
            latest_market, _ = await run_in_threadpool(read_market_evidence, repo, now, symbols)
            if {row["factId"] for row in current} != {row["factId"] for row in facts} or latest_market != market:
                result["artifact"]["staleReason"] = "ANALYSIS_EVIDENCE_CHANGED"
        return result

    return await _ai_job(request, "chat", tickdesk_ai.ChatJob, run, 384 * 1024)


@router.post("/ai/v1/vision", include_in_schema=False)
async def ai_vision(request: Request):
    return await _ai_job(request, "vision", tickdesk_ai.VisionJob, tickdesk_ai.vision, 28 * 1024 * 1024)


@router.post("/prepare")
async def prepare(request: Request):
    if not request.client or not ipaddress.ip_address(request.client.host).is_loopback:
        raise HTTPException(403, "local adapter only")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 1024 * 1024:
            raise HTTPException(413, "adapter input limit")
    try:
        data = json.loads(body)
        cmd = data["data"]
        if cmd["cmd"] not in ("watch", "unwatch", "search_symbols", "get_chart", "get_quote_status", "scan_now", "resume", "quotes"):
            raise ValueError("unsupported command")
        scope = {obj["symbol"] for obj in data.get("objects", []) if obj.get("symbol")}
        requested = set(cmd.get("symbols", [])) if cmd["cmd"] in {"watch", "get_quote_status"} else set()
        if cmd["cmd"] == "get_quote_status" and not 1 <= len(requested) <= 20:
            raise ValueError("invalid quote request scope")
        if cmd["cmd"] == "get_chart":
            requested.add(cmd["symbol"])
        feed = getattr(request.app.state, "tickdesk_quote_feed", None)
        if feed is not None:
            feed.update(scope, requested)
        snapshot = feed.rows() if feed is not None else getattr(request.app.state, "tickdesk_quotes", None)
        qualified = getattr(feed, "execution_rows", None)
        execution_quotes = qualified() if callable(qualified) and cmd["cmd"] in ("scan_now", "resume") else None
        selection = requested if cmd["cmd"] == "get_quote_status" else scope | requested
        observation = feed.diagnostics(selection, snapshot.keys(), data["requestedAt"]) if feed is not None else None
        adapter = MarketAdapter(request.app.state.repo, getattr(request.app.state, "strategy_engine", None),
                                quote_snapshot=snapshot, execution_quotes=execution_quotes)
        result = await run_in_threadpool(adapter.prepare, data)
        if (cmd["cmd"] == "quotes" and type(data.get("historyCoverageVersion")) is int
                and data["historyCoverageVersion"] in (1, 2)):
            from app.services.tickdesk_daily_history import coverage_diagnostic

            baseline = {"status": "starting" if os.getenv("TICKDESK_QUANT_DATA_ENABLED") == "1" else "disabled"}
            result["historyCoverage"] = coverage_diagnostic(
                getattr(request.app.state, "tickdesk_history", baseline),
                include_attribution=data["historyCoverageVersion"] == 2)
        if observation is not None:
            returned = {quote["symbol"] for quote in result.get("quotes", [])}
            for symbol, delivery in observation["delivery"].items():
                if delivery == "available" and symbol not in returned:
                    observation["delivery"][symbol] = "missing"
            result["quoteFeed"] = observation
        demand = getattr(request.app.state, "tickdesk_chart_preparation", None)
        if demand is not None:
            if cmd["cmd"] == "quotes":
                demand.update_scope(sorted(scope))
            if (cmd["cmd"] == "get_chart" and not cmd.get("beforeDate")
                    and result.get("chart", {}).get("dataStatus") != "fresh"):
                symbols = await run_in_threadpool(adapter.symbols, data["requestedAt"])
                if any(row["symbol"] == cmd["symbol"] and row["source"]
                       and (row["instrument"] == "stock" or cmd["adjustment"] == "forward") for row in symbols):
                    status = demand.request(cmd["symbol"])
                    if not result["chart"]["candles"]:
                        return {**result, "chartPreparation": status}
        return result
    except (ValueError, KeyError, TypeError, RuntimeError, TimeoutError, MemoryError):
        # Never return raw provider errors/paths/credentials across the boundary.
        return {"error": "DATA_UNAVAILABLE"}
