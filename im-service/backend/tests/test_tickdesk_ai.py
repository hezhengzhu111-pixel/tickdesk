import base64
import json
import time

import httpx
import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tickdesk_quant_fixture import CUTOFF_MS, DAY, SYMBOLS, build_fixture

from app.api.tickdesk import router
from app.services.tickdesk_ai import DIMENSIONS, AiError, ReportBody, validate_report


def vision_row():
    fields = ("name", "symbol", "qty", "availableQty", "cost", "lastPrice", "marketValue", "pnl")
    return {
        "rowId": "1", "raw": dict(zip(fields, ("新易盛", "300502.SZ", "100", "80", "10.00", None, None, None), strict=True)),
        "confidence": dict(zip(fields, ("1", "1", "1", "1", "1", None, None, None), strict=True)),
        "issues": [{"field": "lastPrice", "code": "missing_column", "message": "未包含现价列", "severity": "warning"}],
    }


async def recognize_response(monkeypatch, content):
    from app.services import tickdesk_ai

    dispatches = []

    async def dispatch(method, path, **kwargs):
        dispatches.append((method, path))

    class VisionModel:
        async def complete(self, messages, **kwargs):
            assert dispatches == [("POST", "/internal/ai/v1/vision/vision1/dispatch")]
            assert messages[1]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
            return content

    monkeypatch.setattr(tickdesk_ai, "local_request", dispatch)
    job = tickdesk_ai.VisionJob(version=1, jobId="vision1", fileId="file1", serverEpoch="epoch1",
                               deadline=tickdesk_ai.now_ms() + 30_000, mime="image/png",
                               image=base64.b64encode(b"\x89PNG\r\n\x1a\nsynthetic fixture").decode())
    return await tickdesk_ai.vision(job, VisionModel())


@pytest.mark.parametrize(("opening", "newline"), [("", "\n"), ("```json", "\n"), ("```", "\n"), ("```json", "\r\n")])
async def test_vision_accepts_only_plain_json_or_one_complete_json_fence(monkeypatch, opening, newline):
    rows = [vision_row()]
    body = json.dumps(rows, ensure_ascii=False, indent=2)
    content = f"{opening}{newline}{body}{newline}```" if opening else body
    assert await recognize_response(monkeypatch, " \t\n" + content + "\n\t ") == rows


@pytest.mark.parametrize("content", [
    "说明\n```json\n[]\n```", "```json\n[]\n```\n说明",
    "```json\n[]\n```\n```json\n[]\n```", "```json\n[][]\n```",
    "```python\n[]\n```", "```json\n[]", "```json []```",
    '```json\n[{"rowId":"a","rowId":"b"}]\n```',
    '```json\n[NaN]\n```', '```json\n{"rows":[]}\n```',
    '```json\n[{"rowId":"1"}]\n```',
])
async def test_vision_fence_does_not_hide_invalid_or_ambiguous_content(monkeypatch, content):
    with pytest.raises((ValueError, AiError)):
        await recognize_response(monkeypatch, content)


@pytest.mark.parametrize("change", ["extra", "wrong_type", "bad_confidence", "duplicate_row", "duplicate_property"])
async def test_fenced_vision_still_validates_every_recognition_row(monkeypatch, change):
    row = vision_row()
    rows = [row]
    if change == "extra":
        row["guessedQty"] = 100
    elif change == "wrong_type":
        row["raw"]["qty"] = 100
    elif change == "bad_confidence":
        row["confidence"]["cost"] = "1.1"
    elif change == "duplicate_row":
        rows.append(row.copy())
    content = json.dumps(rows)
    if change == "duplicate_property":
        content = content.replace('"qty": "100"', '"qty":"99","qty":"100"')
    with pytest.raises((ValueError, AiError)):
        await recognize_response(monkeypatch, "```json\n" + content + "\n```")


def test_shared_json_parser_still_rejects_markdown_and_duplicate_properties():
    from app.services.tickdesk_ai import strict_json

    for content in ('```json\n[]\n```', '{"qty":100,"qty":200}'):
        with pytest.raises(ValueError):
            strict_json(content)


@pytest.mark.parametrize(("remaining_ms", "expected"), [(60_000, 30.0), (8_000, 8.0)])
async def test_model_http_timeout_uses_remaining_job_budget(monkeypatch, remaining_ms, expected):
    from app.services import tickdesk_ai

    real_client = httpx.AsyncClient
    monkeypatch.setattr(tickdesk_ai, "now_ms", lambda: 1_000)

    def respond(request):
        # HTTPX otherwise silently uses its five-second read default before the job deadline.
        assert request.extensions["timeout"]["read"] == expected
        return httpx.Response(200, stream=httpx.ByteStream(tickdesk_ai.encode({"choices": [{
            "finish_reason": "stop", "message": {"content": '{"text":"ok","sourceRefs":[]}'},
        }]}).encode()))

    monkeypatch.setattr(tickdesk_ai.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    model = tickdesk_ai.CompletionModel("http://127.0.0.1/chat", "fixture", None)
    result = await model.complete([], deadline=1_000 + remaining_ms)
    assert result == '{"text":"ok","sourceRefs":[]}'


async def test_model_length_limit_is_a_distinct_failure_without_a_retry(monkeypatch, tmp_path):
    from app.services import tickdesk_ai
    from app.services.tickdesk_ai_budget import ModelBudget

    real_client = httpx.AsyncClient
    body = {
        "choices": [{"finish_reason": "length", "message": {"content": '{"text":"partial'}}],
        "usage": {"prompt_tokens": 11_603, "completion_tokens": 4_096},
    }
    attempts = []

    def respond(request):
        attempts.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(body).encode()))

    monkeypatch.setattr(tickdesk_ai.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    monkeypatch.setattr(tickdesk_ai, "now_ms", lambda: 1_000)
    budget = ModelBudget(tmp_path / "budget.sqlite3")
    model = tickdesk_ai.CompletionModel("http://127.0.0.1/chat", "fixture", None)
    model.accounting = budget, "chat", "epoch", "job", None

    with pytest.raises(AiError) as failed:
        await model.complete([], deadline=10_000)
    assert failed.value.code == "AI_RESPONSE_TOO_LARGE"
    assert len(attempts) == 1
    usage, = budget.status(now=1_000)["chat"]
    assert (usage["attempts"], usage["inputTokens"], usage["outputTokens"]) == (1, 11_603, 4_096)


@pytest.mark.parametrize(("kind", "host", "uses_json_mode"), [
    ("chat", "api.deepseek.com", True),
    ("vision", "api.deepseek.com", False),
    ("chat", "api.deepseek.com.example.test", False),
])
async def test_provider_json_mode_is_scoped_to_deepseek_text(monkeypatch, kind, host, uses_json_mode):
    from app.services import tickdesk_ai

    prefix = f"TICKDESK_AI_{kind.upper()}_"
    monkeypatch.setenv(prefix + "ENDPOINT", f"https://{host}/chat/completions")
    monkeypatch.setenv(prefix + "MODEL", "fixture")
    monkeypatch.delenv(prefix + "KEY_FILE", raising=False)
    monkeypatch.setattr(tickdesk_ai, "now_ms", lambda: 1_000)
    real_client = httpx.AsyncClient
    answer = '{"text":"ok","sourceRefs":[]}'
    attempts = []

    def respond(request):
        payload = json.loads(request.content)
        attempts.append(payload)
        if uses_json_mode:
            assert payload["response_format"] == {"type": "json_object"}
            assert "max_tokens" not in payload
            if payload.get("thinking") != {"type": "disabled"}:
                return httpx.Response(200, stream=httpx.ByteStream(json.dumps({"choices": [{
                    "finish_reason": "length", "message": {"content": ""},
                }]}).encode()))
        else:
            assert "response_format" not in payload
            assert "thinking" not in payload
            assert payload["max_tokens"] == 4096
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps({"choices": [{
            "finish_reason": "stop", "message": {"content": answer},
        }]}).encode()))

    monkeypatch.setattr(tickdesk_ai.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    result = await tickdesk_ai.CompletionModel.configured(kind).complete([], deadline=10_000)
    assert result == answer
    assert len(attempts) == 1


def test_analysis_text_keeps_long_reply_and_report_sections():
    from app.services.tickdesk_ai import Answer, Finding

    reply = "也有不同的业务因素需要看。" * 1800
    finding = "按核验资料逐项保留分析。" * 200
    assert Answer(text=reply, sourceRefs=[]).text == reply
    assert Finding(text=finding, sourceRefs=[]).text == finding


def test_deepseek_answer_accepts_supported_fields_and_keeps_exact_citations():
    from app.services.tickdesk_ai import _deepseek_answer

    body = {"answer": {"回答": ["核查成交与持仓。", "继续保留风险提示。"],
                       "sources": [{"ref": "local:real", "name": "模型自己的名称"}], "metric_refs": [0]}}
    result = _deepseek_answer(body)
    assert result.text == "核查成交与持仓。\n\n继续保留风险提示。"
    assert result.sourceRefs == ["local:real"] and result.metricRefs == ["m0"]

    with pytest.raises(ValueError):
        _deepseek_answer({"answer": body["answer"], "text": "与正文冲突"})
    with pytest.raises(ValueError):
        _deepseek_answer({"text": "核查成交。", "sourceRefs": [{"ref": "local:real", "sourceRef": "local:made-up"}]})


def test_deepseek_answer_trims_over_budget_sources_but_keeps_metric_dependencies():
    from app.services.tickdesk_ai import _deepseek_answer, _validate_metric_refs

    refs = [f"local:ref:{index}" for index in range(22)]
    metrics = {"m0": {"sourceRef": refs[21]}, "m1": {"sourceRef": refs[0]}}
    answer = _deepseek_answer({"text": "已核对资料。", "sourceRefs": refs, "metricRefs": ["m0", "m1"]}, metrics)
    assert len(answer.sourceRefs) == 20
    assert answer.sourceRefs[:2] == [refs[0], refs[21]]
    assert refs[18] in answer.sourceRefs and refs[19] not in answer.sourceRefs
    _validate_metric_refs(answer, metrics)


def test_deepseek_answer_keeps_exact_sources_within_contract_budget():
    from app.services.tickdesk_ai import _deepseek_answer

    refs = [f"local:ref:{index}" for index in range(20)]
    answer = _deepseek_answer({"text": "资料不足。", "sourceRefs": refs}, {})
    assert answer.sourceRefs == refs


def test_metric_sources_are_attached_within_the_contract_budget():
    from app.services.tickdesk_ai import _ensure_metric_sources

    catalog = {f"m{index}": {"sourceRef": f"local:metric:{index}"} for index in range(3)}
    result = _ensure_metric_sources(["local:a", "local:b"], ["m0", "m2"], catalog)
    assert result == ["local:a", "local:b", "local:metric:0", "local:metric:2"]


def test_metric_source_attachment_makes_room_without_dropping_pinned_sources():
    from app.services.tickdesk_ai import _ensure_metric_sources

    catalog = {"m0": {"sourceRef": "local:metric:0"}}
    refs = [f"local:ref:{index}" for index in range(20)]
    result = _ensure_metric_sources(refs, ["m0"], catalog)
    assert len(result) == 20 and "local:metric:0" in result
    assert "local:ref:19" not in result


def test_deepseek_preserves_all_valid_metrics_over_eight():
    from app.services.tickdesk_ai import (
        _deepseek_answer,
        _render_metrics,
        _report_from_answer,
        _validate_metric_refs,
    )

    metrics = {
        f"m{index}": {"sourceRef": "local:verified", "symbol": "300502.SZ", "label": f"指标{index}",
                      "value": str(index), "unit": "元"}
        for index in range(12)
    }
    answer = _deepseek_answer({
        "text": "见已核验指标。",
        "sourceRefs": ["local:verified", "local:verified"],
        "metricRefs": [*metrics, "m0"],
    })
    _validate_metric_refs(answer, metrics)
    report = _report_from_answer(answer, {})
    _validate_metric_refs(report.summary, metrics)
    assert answer.metricRefs == list(metrics) and report.summary.metricRefs == list(metrics)
    assert "指标11 11 元" in _render_metrics(report.summary, metrics)
    with pytest.raises(AiError, match="AI_UNVERIFIED_SOURCE"):
        _validate_metric_refs(answer, {**metrics, "m11": {"sourceRef": "local:different"}})


class FakeModel:
    model = "fixture"

    async def complete(self, messages, **kwargs):
        assert messages[0]["role"] == "system"
        assert "prior question" in messages[1]["content"]
        return '{"text":"资料不足","sourceRefs":[]}'


@pytest.mark.parametrize("question", ["今日复盘", "持仓风险"])
def test_portfolio_shortcuts_scope_real_holdings_and_pending_executions(question):
    from app.services.tickdesk_ai import ChatJob, analysis_subjects, get_analysis_context

    job = ChatJob(version=1, jobId="portfolio", requestId="portfolio", deadline=60_000, text=question,
        evidence={"asOf": 1500, "snapshotSeq": 3, "serverEpoch": "epoch", "items": [
            {"collection": "positions", "record": {"symbol": "300502.SZ", "qty": 300}},
            {"collection": "positions", "record": {"symbol": "600000.SH", "qty": 0}},
            {"collection": "executions", "record": {"symbol": "000063.SZ", "positionEffect": "needs_reconciliation"}},
            {"collection": "watchlist", "record": {"symbol": "510300.SH"}},
        ]})
    assert analysis_subjects(job) == ["000063.SZ", "300502.SZ"]
    context = get_analysis_context(job, model="fixture")
    assert context["symbols"] == ["000063.SZ", "300502.SZ"]
    assert context["localEvidence"] == job.evidence.items
    explicitly_selected = job.model_copy(update={"text": "持仓风险 600000.SH"})
    assert analysis_subjects(explicitly_selected) == ["600000.SH"]


@pytest.mark.parametrize(("failure", "expected_code"), [
    (None, None),
    ("fake_ref", "AI_UNVERIFIED_SOURCE"),
    # A selected metric is bound to its archived source even when the model forgot to cite it.
    ("wrong_source", None),
    ("invented_value", "AI_UNVERIFIED_SOURCE"),
    ("invalid", "AI_INVALID_RESPONSE"),
    ("wrong_shape", "AI_INVALID_RESPONSE"),
    ("timeout", "AI_TIMEOUT"),
])
def test_report_displays_only_source_bound_metrics(monkeypatch, tmp_path, failure, expected_code):
    token = "a" * 43
    path = tmp_path / "token"
    path.write_text(token)
    monkeypatch.setenv("TICKDESK_AI_TOKEN_FILE", str(path))
    app = FastAPI()
    app.include_router(router)
    now = time.time_ns() // 1_000_000
    source = {"ref": "local:holding", "name": "本地持仓", "asOf": now}
    job = {
        "version": 1, "jobId": "report1", "requestId": "req1", "deadline": now + 30_000,
        "text": "/分析 600000.SH",
        "evidence": {
            "items": [{"source": source, "collection": "positions", "record": {"symbol": "600000.SH", "qty": 120}}],
            "asOf": now, "snapshotSeq": 7, "serverEpoch": "epoch1",
            "business": {"holdingDetails": [{"symbol": "600000.SH", "qty": 120, "availableQty": 0,
                                             "sellableBasis": "confirmed_snapshot"}]},
        },
    }

    class ReportModel:
        model = "fixture"

        async def complete(self, messages, **kwargs):
            if failure == "timeout":
                raise TimeoutError
            if failure == "invalid":
                return "truncated"
            if failure == "wrong_shape":
                return '{"stance":"watch"}'
            missing = {"text": "资料不足", "sourceRefs": []}
            metrics = json.loads(messages[1]["content"])["context"].get("metrics", {})
            sellable = next((key for key, value in metrics.items() if value["field"] == "confirmedSellable"), "m0")
            summary = {"text": "优先核对当前约束", "sourceRefs": [source["ref"]], "metricRefs": [sellable]}
            if failure == "fake_ref":
                summary["metricRefs"] = ["m999"]
            if failure == "wrong_source":
                summary["sourceRefs"] = []
            if failure == "invented_value":
                summary["text"] = "可卖 800 股"
            return json.dumps({
                "stance": "watch", "summary": summary, "dimensions": dict.fromkeys(DIMENSIONS, missing),
                "opposingEvidence": [], "unknowns": [], "invalidateWhen": [], "reviewWhen": [],
                "quantAgreement": "unavailable", "quantReason": missing,
            })

    app.state.tickdesk_chat_model = ReportModel()
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        response = client.post("/api/tickdesk/ai/v1/chat", json=job, headers={"Authorization": f"Bearer {token}"})
    if expected_code:
        assert response.json() == {"detail": expected_code}
        if failure in ("invalid", "wrong_shape"):
            diagnostic = app.state.tickdesk_ai_outcomes["chat"]
            assert diagnostic["stage"] == ("model_json" if failure == "invalid" else "report_schema")
            assert "text" not in diagnostic and "payload" not in diagnostic
        return
    result = response.json()["result"]
    summary = result["artifact"]["report"]["body"]["summary"]
    explanation = result["text"].split("\n\nAI 解读\n", 1)[1]
    assert explanation.split("\n\n")[0].endswith("600000.SH 已确认可卖 0 股")
    assert result["sourceRefs"] == [source["ref"]]
    metric, = [result["artifact"]["context"]["metrics"][key] for key in summary["metricRefs"]]
    assert (metric["field"], metric["sourceRef"], metric["value"], metric["unit"]) == (
        "confirmedSellable", source["ref"], "0", "股",
    )


def test_local_assistant_checks_token_and_does_not_replay(monkeypatch, tmp_path):
    path = tmp_path / "token"
    token = "a" * 43
    path.write_text(token)
    monkeypatch.setenv("TICKDESK_AI_TOKEN_FILE", str(path))
    app = FastAPI()
    app.include_router(router)
    app.state.tickdesk_chat_model = FakeModel()
    now = time.time_ns() // 1_000_000
    job = {
        "version": 1, "jobId": "job1", "requestId": "req1", "deadline": now + 30_000,
        "text": "现在怎么看？",
        "evidence": {
            "items": [], "asOf": now, "snapshotSeq": 7, "serverEpoch": "epoch1",
            "history": [{"role": "user", "text": "prior question"}],
        },
    }
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        url = "/api/tickdesk/ai/v1/chat"
        assert client.post(url, json=job).status_code == 401
        headers = {"Authorization": f"Bearer {token}"}
        response = client.post(url, json=job, headers=headers)
        assert response.status_code == 200
        assert response.json()["result"]["artifact"]["context"]["quality"]["quant"] == "unavailable"
        assert client.post(url, json=job, headers=headers).status_code == 409


def test_analysis_uses_cutoff_verified_financial_and_daily_data(monkeypatch, tmp_path):
    from app.financial_history import merge_report_revisions

    token = "a" * 43
    token_file = tmp_path / "token"
    token_file.write_text(token)
    monkeypatch.setenv("TICKDESK_AI_TOKEN_FILE", str(token_file))
    root = tmp_path / "fixture"
    app = build_fixture(root)
    financial = root / "financials/metrics/part.parquet"
    merge_report_revisions(pl.read_parquet(financial), pl.DataFrame({
        "symbol": [SYMBOLS[0]], "period_end": ["2025-09-30"], "announce_date": ["2026-01-11"],
        "roe": [789.0], "_financial_source": ["fixture-revision"],
        "_financial_observed_at": ["2026-01-12T02:00:00Z"],
    })).write_parquet(financial)

    class ReportModel:
        model = "fixture"

        async def complete(self, messages, **kwargs):
            context = json.loads(messages[1]["content"])["context"]
            metric_id, metric = next((key, value) for key, value in context["metrics"].items()
                                     if value["field"] == "roe_latest")
            close_id, close = next((key, value) for key, value in context["metrics"].items()
                                   if value["field"] == "rawClose")
            missing = {"text": "资料不足", "sourceRefs": []}
            return json.dumps({
                "stance": "watch", "summary": missing,
                "dimensions": {**dict.fromkeys(DIMENSIONS, missing),
                               "financials": {"text": "盈利质量需复核", "sourceRefs": [metric["sourceRef"]],
                                              "metricRefs": [metric_id]},
                               "priceVolume": {"text": "收盘趋势待观察", "sourceRefs": [close["sourceRef"]],
                                               "metricRefs": [close_id]}},
                "opposingEvidence": [], "unknowns": [], "invalidateWhen": [], "reviewWhen": [],
                "quantAgreement": "unavailable", "quantReason": missing,
            })

    now = time.time_ns() // 1_000_000
    app.state.tickdesk_chat_model = ReportModel()
    try:
        with TestClient(app, client=("127.0.0.1", 5000)) as client:
            response = client.post("/api/tickdesk/ai/v1/chat", headers={"Authorization": f"Bearer {token}"}, json={
                "version": 1, "jobId": "job1", "requestId": "req1", "deadline": now + 30_000,
                "text": f"/分析 {SYMBOLS[0]}", "evidence": {
                    "items": [], "asOf": CUTOFF_MS, "snapshotSeq": 0, "serverEpoch": "epoch1",
                },
            })
    finally:
        app.state.repo.store.db.close()
    assert response.status_code == 200, response.json()
    result = response.json()["result"]
    assert "净资产收益率 1 %" in result["text"] and "不复权日收盘 11 元" in result["text"]
    assert "2025-09-30" in result["text"] and str(DAY) in result["text"]
    context = result["artifact"]["context"]
    finance = next(row for row in context["marketEvidence"] if row["record"].get("field") == "roe_latest")
    assert finance["record"]["revisionIds"] and finance["record"]["periodEnd"] == "2025-09-30"
    assert "789" not in str(finance)
    assert {row["source"]["ref"] for row in result["extraEvidence"]} == set(result["sourceRefs"])
    assert len(result["sourceRefs"]) == 3
    assert any(ref.startswith("local:financial:") for ref in result["sourceRefs"])
    assert any(ref.startswith("local:job1:quant:") for ref in result["sourceRefs"])


@pytest.mark.parametrize("deepseek", [False, True])
def test_manual_report_evaluates_requested_stock_outside_the_scan_pool(monkeypatch, tmp_path, deepseek):
    token = "a" * 43
    token_file = tmp_path / "token"
    token_file.write_text(token)
    monkeypatch.setenv("TICKDESK_AI_TOKEN_FILE", str(token_file))
    root = tmp_path / "fixture"
    app = build_fixture(root)
    settings = root / "tickdesk_research.json"
    pool = json.loads(settings.read_text(encoding="utf-8"))
    asset = pool["symbols"].pop()
    settings.write_text(json.dumps(pool), encoding="utf-8")
    previous = settings.read_bytes()
    captured = []
    from app.services import tickdesk_worker

    research = tickdesk_worker.research_in_worker
    budgets = []

    def observed_research(*args, **kwargs):
        budgets.append(kwargs["deadline"] - time.monotonic())
        return research(*args, **kwargs)

    monkeypatch.setattr(tickdesk_worker, "research_in_worker", observed_research)

    class ReportModel:
        model = "fixture"
        json_output = deepseek

        async def complete(self, messages, **kwargs):
            inputs = json.loads(messages[1]["content"])
            captured.append(inputs)
            missing = {"text": "需要补充证据", "sourceRefs": []}
            if deepseek:
                assert '"text":"完整分析正文"' in messages[0]["content"]
                assert "正文不要写任何数字" in messages[0]["content"]
                assert "不得超过 20 个" in messages[0]["content"]
                return json.dumps({
                    "text": "量化退出已经触发，入场资料不足。", "metricRefs": [],
                    "sourceRefs": inputs["quantRefs"],
                })
            return json.dumps({
                "stance": "watch", "summary": missing, "dimensions": dict.fromkeys(DIMENSIONS, missing),
                "opposingEvidence": [], "unknowns": [], "invalidateWhen": [], "reviewWhen": [],
                "quantAgreement": "unavailable", "quantReason": missing,
            })

    now = time.time_ns() // 1_000_000
    app.state.tickdesk_chat_model = ReportModel()
    try:
        with TestClient(app, client=("127.0.0.1", 5000)) as client:
            response = client.post("/api/tickdesk/ai/v1/chat", headers={"Authorization": f"Bearer {token}"}, json={
                "version": 1, "jobId": "manual", "requestId": "manual", "deadline": now + 30_000,
                "text": f"/分析 {asset}", "evidence": {
                    "items": [], "asOf": CUTOFF_MS, "snapshotSeq": 7, "serverEpoch": "epoch1",
                },
            })
    finally:
        app.state.repo.store.db.close()
    assert response.status_code == 200, response.json()
    [budget] = budgets
    assert 5 < budget <= 12
    result = response.json()["result"]
    [quant] = captured[0]["context"]["quant"]
    assert quant["symbol"] == asset and quant["entry"]["value"] is None and quant["exit"]["value"] is True
    assert result["artifact"]["report"]["quantOriginal"] == [quant]
    original, explanation = result["text"].split("\n\nAI 解读\n", 1)
    assert original.startswith("本地快照原值\n")
    assert "入场待核实" in original and "退出触发" in original
    if deepseek:
        assert result["artifact"]["report"]["body"]["quantAgreement"] == "partial"
        assert "量化退出已经触发，入场资料不足。" in explanation
        assert result["sourceRefs"] == captured[0]["quantRefs"]
    assert settings.read_bytes() == previous and not (root / "tickdesk_approvals.json").exists()


@pytest.mark.parametrize("case", ["published", "failed", "future", "wrong_epoch"])
async def test_report_uses_only_frozen_published_quant_with_its_original_score_and_source(case):
    from app.services import tickdesk_ai

    published = {"scanId": "formal-scan", "status": "succeeded", "finishedAt": 5000, "reason": None,
                 "candidates": [{"taskId": "formal-task", "symbol": "300502.SZ", "direction": "buy",
                                 "score": "88.123456789123456789", "strategies": ["one", "two"], "dataAsOf": 4990},
                                {"taskId": "other-task", "symbol": "600000.SH", "direction": "sell",
                                 "score": "97", "strategies": ["one"], "dataAsOf": 4990}]}
    if case == "failed":
        published.update(status="failed", reason="NO_PRODUCTION_APPROVAL")
    if case == "future":
        published.update(finishedAt=5100)
    captured = []
    class Model:
        model = "fixture"
        json_output = True
        async def complete(self, messages, **kwargs):
            body = json.loads(messages[1]["content"])
            captured.append(body)
            refs = body["quantRefs"]
            return json.dumps({"text": "证据覆盖待补。", "sourceRefs": refs,
                               "metricRefs": ["m0"] if refs else []})

    job = tickdesk_ai.ChatJob(version=1, jobId="formal-report", requestId="formal-request",
                              deadline=tickdesk_ai.now_ms() + 10_000, text="/分析 300502.SZ", evidence={
                                  "items": [], "asOf": 5001, "snapshotSeq": 3, "serverEpoch": "epoch",
                                  "quant": {"serverEpoch": "older" if case == "wrong_epoch" else "epoch",
                                            "batch": None, "production": published},
                              })
    result = await tickdesk_ai.chat(job, Model())
    context = captured[0]["context"]
    original, explanation = result["text"].split("\n\nAI 解读\n", 1)
    assert "证据覆盖待补。" in explanation
    assert context["quant"] == []
    if case == "published":
        ref = "local:scan:formal-scan:task:formal-task"
        assert captured[0]["quantRefs"] == [ref]
        assert context["productionQuant"]["candidates"] == published["candidates"][:1]
        assert result["artifact"]["report"]["productionQuant"] == context["productionQuant"]
        assert result["artifact"]["report"]["quantOriginal"] == []
        assert result["sourceRefs"] == [ref] and result["extraEvidence"][0]["source"]["asOf"] == 4990
        assert "正式买入候选" in original and "88.123456789123456789" in original
        assert "600000.SH" not in result["text"]
    else:
        assert captured[0]["quantRefs"] == []
        assert "当前没有可核对的量化观察。" in original
        assert "88.123456789123456789" not in result["text"]
        if case == "failed":
            assert "暂无已准入的正式策略" in original


@pytest.mark.parametrize("question", ["今日复盘", "持仓风险", "综合分析", "普通提问"])
@pytest.mark.parametrize("failure", [None, "blank", "flat_shape", "unverified", "AI_RATE_LIMITED", "AI_TIMEOUT"])
def test_chat_shows_quant_for_an_unscanned_holding(monkeypatch, tmp_path, question, failure):
    token = "a" * 43
    token_file = tmp_path / "token"
    token_file.write_text(token)
    monkeypatch.setenv("TICKDESK_AI_TOKEN_FILE", str(token_file))
    root = tmp_path / "fixture"
    app = build_fixture(root)
    settings = root / "tickdesk_research.json"
    pool = json.loads(settings.read_text(encoding="utf-8"))
    symbol = pool["symbols"].pop()
    settings.write_text(json.dumps(pool), encoding="utf-8")
    captured = []
    market_reads = []

    def read_market(_repo, cutoff, _symbols):
        market_reads.append(cutoff)
        return [], {}

    monkeypatch.setattr("app.api.tickdesk.read_market_evidence", read_market)

    class ChatModel:
        model = "fixture"
        json_output = True

        async def complete(self, messages, **kwargs):
            captured.append(json.loads(messages[1]["content"]))
            if failure == "blank":
                return " "
            if failure == "flat_shape":
                return '{"report":{"text":{"private_field":"private_provider_value"},"sourceRefs":[]}}'
            if failure == "unverified":
                return '{"text":"无来源数值 999 元","sourceRefs":["local:invented"]}'
            if failure:
                raise AiError(failure)
            return '{"text":"依据已归档资料进行复核。","sourceRefs":[]}'

    now = time.time_ns() // 1_000_000
    app.state.tickdesk_chat_model = ChatModel()
    job = {
        "version": 1, "jobId": "shortcut", "requestId": "shortcut", "deadline": now + 30_000,
        "text": {"综合分析": f"/分析 {symbol}", "普通提问": f"{symbol} 今日量化信号如何？"}.get(question, question),
        "evidence": {"items": [{
            "collection": "positions", "record": {"symbol": symbol, "qty": 300},
            "source": {"ref": "local:holding", "name": "本地持仓", "asOf": CUTOFF_MS},
        }], "asOf": CUTOFF_MS, "snapshotSeq": 7, "serverEpoch": "epoch1"},
    }
    try:
        with TestClient(app, client=("127.0.0.1", 5000)) as client:
            response = client.post("/api/tickdesk/ai/v1/chat", json=job,
                                   headers={"Authorization": f"Bearer {token}", "X-TickDesk-Quant-On-Error": "1"})
    finally:
        app.state.repo.store.db.close()

    assert response.status_code == 200, response.json()
    result = response.json()["result"]
    assert len(captured) == 1
    if failure:
        code = {"blank": "AI_INVALID_RESPONSE", "flat_shape": "AI_INVALID_RESPONSE",
                "unverified": "AI_UNVERIFIED_SOURCE"}.get(failure, failure)
        assert result["analysisError"] == code
        assert app.state.tickdesk_ai_outcomes["chat"]["status"] == code
        assert "999" not in result["text"] and "local:invented" not in result["sourceRefs"]
        assert market_reads == [CUTOFF_MS]
        if failure == "flat_shape":
            diagnostic = app.state.tickdesk_ai_outcomes["chat"]
            assert diagnostic["stage"] == ("report_schema" if question == "综合分析" else "chat_schema")
            assert diagnostic["schemaIssues"] == [{"path": "text", "type": "string_type"}]
            assert "schemaIssues" not in response.text and "private_" not in response.text
            assert "private_" not in json.dumps(diagnostic)
    else:
        assert "analysisError" not in result
    [observation] = captured[0]["context"]["quant"]
    assert observation["symbol"] == symbol
    assert observation["entry"]["value"] is None and observation["exit"]["value"] is True
    assert "入场待核实" in result["text"] and "退出触发" in result["text"]
    [citation] = result["extraEvidence"]
    assert citation["source"]["ref"] == result["sourceRefs"][0] == captured[0]["quantRefs"][0]
    if question == "综合分析" and failure is None:
        assert result["artifact"]["report"]["quantOriginal"] == [observation]
    else:
        assert result["artifact"]["report"] is None


@pytest.mark.parametrize(("source_refs", "expected"), [
    (["local:market"], "unavailable"),
    (["local:quant"], "partial"),
])
def test_missing_entry_with_untriggered_exit_cannot_be_reported_as_agreement(source_refs, expected):
    from app.services.tickdesk_ai import _ground_quant_comparison

    missing = {"text": "资料不足", "sourceRefs": []}
    report = ReportBody.model_validate({
        "stance": "watch", "summary": missing, "dimensions": dict.fromkeys(DIMENSIONS, missing),
        "opposingEvidence": [], "unknowns": [], "invalidateWhen": [], "reviewWhen": [],
        "quantAgreement": "agree", "quantReason": {"text": "量化判断一致", "sourceRefs": source_refs},
    })
    result = _ground_quant_comparison(report, {"local:quant": {"entry": {"value": None}, "exit": {"value": False}}})
    assert result.quantAgreement == expected
    assert ("local:quant" in result.quantReason.sourceRefs) == (expected == "partial")


def test_timestamp_cannot_be_cited_as_an_account_balance():
    missing = {"text": "资料不足", "sourceRefs": []}
    report = ReportBody.model_validate({
        "stance": "insufficient",
        "summary": {"text": "现金余额 1700000000000 元。", "sourceRefs": ["cash"]},
        "dimensions": dict.fromkeys(DIMENSIONS, missing), "opposingEvidence": [],
        "unknowns": [], "invalidateWhen": [], "reviewWhen": [],
        "quantAgreement": "unavailable", "quantReason": missing,
    })
    sources = {"cash": {"source": {"asOf": 1700000000000}, "record": {"cash": None}}}
    with pytest.raises(AiError, match="AI_UNVERIFIED_SOURCE"):
        validate_report(report, sources, quant_available=False)


@pytest.mark.parametrize(("text", "benchmark", "allowed"), [
    ("个股表现与沪深300指数有分歧。", "000300.SH", True),
    ("可结合沪深 300 走势复查。", "000300.SH", True),
    ("个股表现与沪深300指数有分歧。", None, False),
    ("个股表现与沪深300指数有分歧。", "000001.SH", False),
    ("沪深3000指数有分歧。", "000300.SH", False),
    ("沪深300点就能确立趋势。", "000300.SH", False),
    ("沪深300指数会上涨3.5%。", "000300.SH", False),
    ("沪深300指数的安全仓位是800股。", "000300.SH", False),
])
def test_model_numbers_allow_only_the_cited_benchmarks_name(text, benchmark, allowed):
    from app.services.tickdesk_ai import validate_model_numbers

    sources = [] if benchmark is None else [{
        "record": {"kind": "marketIndex", "symbol": "300502.SZ", "indexCode": benchmark},
    }]
    if allowed:
        validate_model_numbers(text, iter(sources))
    else:
        with pytest.raises(AiError, match="AI_UNVERIFIED_SOURCE"):
            validate_model_numbers(text, iter(sources))


def test_model_numbers_allow_cited_index_identifiers_only():
    from app.services.tickdesk_ai import validate_model_numbers

    cited = [{"source": {"ref": "local:fuyao-index:abc"},
              "record": {"kind": "industryIndex", "symbol": "300502.SZ", "industryIndexCode": "881129.TI"}}]
    validate_model_numbers("通信设备行业指数逐日可得。", iter(cited))
    validate_model_numbers("881129.TI 行业指数逐日可得。", iter(cited))
    with pytest.raises(AiError, match="AI_UNVERIFIED_SOURCE"):
        validate_model_numbers("000300.SH 大盘逐日可得。", iter(cited))


def test_benchmark_name_binds_the_archived_index_record_without_model_citation():
    from app.services.tickdesk_ai import Answer, _bind_benchmark_name, validate_model_numbers

    evidence = [{"source": {"ref": "local:fuyao-index:abc", "name": "沪深 300 日收盘", "asOf": 1500},
                 "record": {"kind": "marketIndex", "symbol": "300502.SZ", "indexCode": "000300.SH"}}]
    answer = Answer(text="行业指数与沪深300均有可得日收盘数据。", sourceRefs=[])
    with pytest.raises(AiError, match="AI_UNVERIFIED_SOURCE"):
        validate_model_numbers(answer.text, ())
    _bind_benchmark_name(answer, {"marketEvidence": evidence})
    assert answer.sourceRefs == ["local:fuyao-index:abc"]
    validate_model_numbers(answer.text, iter([evidence[0]]))
