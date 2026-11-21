import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import httpx
import pytest

from app.services.tickdesk_ai import AiError, CompletionModel, now_ms
from app.services.tickdesk_ai_budget import BudgetError, ModelBudget, ModelLimits


def test_budget_reserves_and_recovers_unknown_charge_without_refunding(tmp_path):
    path = tmp_path / "usage.sqlite3"
    limits = ModelLimits("CNY", Decimal("0.004"), Decimal("1"), Decimal("1"), 1000, 5)
    ledger = ModelBudget(path)
    failed = ledger.reserve("chat", "epoch1", "job1", 800, 1000, limits, now=2000)
    assert failed is not None
    ledger = ModelBudget(path)
    with pytest.raises(BudgetError, match="AI_REPLAY"):
        ledger.reserve("chat", "epoch1", "job1", 800, 1000, limits, now=2100)
    returned = ledger.reserve("chat", "epoch1", "job2", 800, 1000, limits, now=2200)
    with pytest.raises(BudgetError, match="AI_BUDGET_EXHAUSTED"):
        ledger.reserve("chat", "epoch1", "job3", 800, 1000, limits, now=2300)
    ledger.settle(returned, {"prompt_tokens": 600, "completion_tokens": 200}, limits)
    usage = ledger.status(now=2400)["chat"][0]
    assert usage == {"currency": "CNY", "attempts": 2, "unknownUsage": 1, "requestBytes": 1600,
                     "inputTokens": 600, "outputTokens": 200, "conservativeCost": "0.0028"}
    with pytest.raises(BudgetError, match="AI_BUDGET_EXHAUSTED"):
        ledger.reserve("chat", "epoch1", "job3", 800, 1000, limits, now=2400)
    assert ledger.reserve("vision", "epoch1", "job3", 1200, 1000, limits, now=2400) is not None


def test_output_ceiling_is_allocated_under_the_same_write_lock(tmp_path):
    ledger = ModelBudget(tmp_path / "usage.sqlite3")
    limits = ModelLimits("CNY", Decimal("0.06"), Decimal("1"), Decimal("1"), 10_000, 5)

    def book(job):
        try:
            return ledger.reserve_up_to("chat", "epoch", job, lambda _: 800, 8192, 393_216, limits, now=2000)[1]
        except BudgetError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as workers:
        allocations = list(workers.map(book, ("first", "second")))
    assert set(allocations) == {50_000, "AI_BUDGET_EXHAUSTED"}
    usage, = ledger.status(now=2100)["chat"]
    assert usage["attempts"] == 1 and usage["conservativeCost"] == "0.06"


async def test_http_limit_keeps_unknown_cost_and_blocks_the_next_call(monkeypatch, tmp_path):
    for name, value in {
        "CURRENCY": "CNY", "DAILY": "0.02", "INPUT_PER_MILLION": "1",
        "OUTPUT_PER_MILLION": "1", "MAX_INPUT_TOKENS": "10000", "MAX_CALLS": "10",
    }.items():
        monkeypatch.setenv(f"TICKDESK_AI_CHAT_BUDGET_{name}", value)
    clients = httpx.AsyncClient
    attempts = []

    def limited(request):
        attempts.append(request.method)
        return httpx.Response(429)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: clients(transport=httpx.MockTransport(limited), **kwargs))
    ledger = ModelBudget(tmp_path / "usage.sqlite3")
    model = CompletionModel("https://example.com/chat", "fixture", None)
    model.configure_accounting(ledger, "chat", "epoch1", "job1", scheduled=True)
    with pytest.raises(AiError, match="AI_RATE_LIMITED"):
        await model.complete([], deadline=now_ms() + 10000)
    model.configure_accounting(ledger, "chat", "epoch1", "job2", scheduled=True)
    with pytest.raises(AiError, match="AI_BUDGET_EXHAUSTED"):
        await model.complete([], deadline=now_ms() + 10000)
    assert attempts == ["POST"]
    usage = ledger.status(now=now_ms())["chat"][0]
    assert usage["attempts"] == 1 and usage["unknownUsage"] == 1


async def test_deepseek_finishes_long_output_and_reserves_the_sent_ceiling(monkeypatch, tmp_path):
    limits = ModelLimits("CNY", Decimal("0.06"), Decimal("1"), Decimal("1"), 10_000, 3)
    ledger = ModelBudget(tmp_path / "usage.sqlite3")
    model = CompletionModel("https://api.deepseek.com/chat/completions", "deepseek-flash", None, json_output=True)
    client = httpx.AsyncClient
    attempts = []
    long_answer = json.dumps({"text": "长回复正文。" * 6_000, "sourceRefs": []}, ensure_ascii=False)

    def capture(request):
        payload = json.loads(request.content)
        attempts.append((payload.get("max_tokens"), len(request.content), request.extensions["timeout"]["read"]))
        if len(attempts) == 2:
            return httpx.Response(429)
        enough = payload.get("max_tokens", 8192) >= 10_000
        choice = {"finish_reason": "stop" if enough else "length",
                  "message": {"content": long_answer if enough else '{"text":"尚未完成'}}
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps({"choices": [choice],
            "usage": {"prompt_tokens": 2_000, "completion_tokens": 10_000 if enough else 8192},
        }, ensure_ascii=False).encode()))

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(capture), **kwargs))
    for job_id in ("long", "limited", "denied"):
        model.accounting = ledger, "chat", "epoch", job_id, limits
        if job_id == "long":
            assert await model.complete([], deadline=now_ms() + 60_000) == long_answer
        else:
            expected = "AI_RATE_LIMITED" if job_id == "limited" else "AI_BUDGET_EXHAUSTED"
            with pytest.raises(AiError, match=expected):
                await model.complete([], deadline=now_ms() + 60_000)
    assert [ceiling for ceiling, _, _ in attempts] == [50_000, 38_000]
    assert all(timeout > 30 for _, _, timeout in attempts)
    usage, = ledger.status(now=now_ms())["chat"]
    assert usage["conservativeCost"] == "0.06" and usage["unknownUsage"] == 1
    assert usage["requestBytes"] == sum(size for _, size, _ in attempts)
    assert usage["outputTokens"] == 10_000


async def test_deepseek_length_finish_accepts_only_a_complete_json_object(monkeypatch):
    client = httpx.AsyncClient
    complete = '{"text":"完整结论","sourceRefs":[]}'
    contents = iter((complete, '{"text":"未完成', '{"text":"重复","text":"字段"}'))
    calls = []

    def capture(request):
        assert "max_tokens" not in json.loads(request.content)
        calls.append(True)
        body = {"choices": [{"finish_reason": "length", "message": {"content": next(contents)}}]}
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(body).encode()))

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(capture), **kwargs))
    model = CompletionModel("https://api.deepseek.com/chat/completions", "deepseek-flash", None, json_output=True)
    assert await model.complete([], deadline=now_ms() + 10_000) == complete
    for _ in range(2):
        with pytest.raises(AiError, match="AI_RESPONSE_TOO_LARGE"):
            await model.complete([], deadline=now_ms() + 10_000)
    assert len(calls) == 3


def test_unpriced_remote_model_requires_explicit_automatic_budget(monkeypatch, tmp_path):
    for name in ("CURRENCY", "DAILY", "INPUT_PER_MILLION", "OUTPUT_PER_MILLION", "MAX_INPUT_TOKENS", "MAX_CALLS"):
        monkeypatch.delenv(f"TICKDESK_AI_CHAT_BUDGET_{name}", raising=False)
    ledger = ModelBudget(tmp_path / "usage.sqlite3")
    model = CompletionModel("https://example.com/chat", "fixture", None)
    model.configure_accounting(ledger, "chat", "epoch1", "manual1")
    with pytest.raises(AiError, match="AI_BUDGET_NOT_CONFIGURED"):
        model.configure_accounting(ledger, "chat", "epoch1", "automatic1", scheduled=True)
