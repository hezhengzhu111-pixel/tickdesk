"""Verify the Rust WSS path and real Python assistant with loopback model fixtures.

Build relay and local-service with cargo build --locked. The invoking Python
needs scripts/requirements-integration.txt. The assistant uses the backend
.venv by default and requires the backend dependencies.
"""

import argparse
import base64
from contextlib import contextmanager
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import tomllib
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from integration_services import AppPeer, Harness, HOST as RUST_HOST, ROOT, identifier, query, require, test_png, wait_for
from testing_providers import BACKEND_PYTHON, FixtureProviders, assistant_fixture


HOST = "127.0.0.1"
PNG = test_png()
SYMBOL = "300502.SZ"
QUESTIONS = (f"{SYMBOL} 当前有哪些仓位证据？", "上次你引用了什么？", "验证伪造引用。")
ANSWERS = ("隔离模型：已保存本地仓位证据。", "隔离模型：先前回复是会话材料。")
MANUAL_QUESTION = f"/分析 {SYMBOL}"
HISTORICAL_QUESTION = "现在回顾，原报告为什么信息不足？"
CURRENT_QUESTION = "解释判断依据。"
INVALID_QUESTION = "验证模型回复不完整。"
TIMEOUT_QUESTION = "验证模型超时。"
DIMENSIONS = ("operations", "financials", "industry", "events", "macro", "valuation", "priceVolume", "portfolio")
RAW = dict(name="隔离联调证券", symbol=SYMBOL, qty="200", availableQty=None, cost="10.00",
           lastPrice=None, marketValue=None, pnl=None)
ROW = {"rowId": "row1", "raw": RAW,
       "confidence": {key: "0.9" if value is not None else None for key, value in RAW.items()},
       "issues": []}


class ModelFixture(FixtureProviders):
    def do_POST(self):
        if self.path not in ("/chat", "/vision") or self.headers.get("Authorization"):
            self.respond({}, 400)
            return
        try:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            kind = self.path[1:]
            require(request["model"] == f"fixture-{kind}", "fixture model")
            content = request["messages"][-1]["content"]
            if kind == "chat":
                prompt = json.loads(content)
                question = prompt["question"]
                if question == QUESTIONS[0]:
                    answer = {"text": ANSWERS[0], "sourceRefs": [
                        item["source"]["ref"] for item in prompt["context"]["localEvidence"]
                        if item["collection"] == "positions" and item["record"]["symbol"] == SYMBOL
                    ]}
                elif question == QUESTIONS[1]:
                    answer = {"text": ANSWERS[1], "sourceRefs": []}
                elif question == MANUAL_QUESTION:
                    missing = {"text": "证据不足。", "sourceRefs": []}
                    answer = {"stance": "insufficient", "summary": missing,
                              "dimensions": dict.fromkeys(DIMENSIONS, missing), "opposingEvidence": [],
                              "unknowns": ["没有完整外部证据。"], "invalidateWhen": ["原证据修订。"],
                              "reviewWhen": ["新增相关披露。"], "quantAgreement": "unavailable", "quantReason": missing}
                    if self.server.risk_case and prompt["context"]["purpose"] == "automatic":
                        refs = [row["source"]["ref"] for row in prompt["context"]["localEvidence"]
                                if row["collection"] == "positions" and row["record"]["symbol"] == SYMBOL]
                        require(len(refs) == 1, "isolated risk must cite the stored position")
                        answer.update(stance="restricted", summary={"text": "可卖依据待复核。", "sourceRefs": refs})
                elif question in (HISTORICAL_QUESTION, CURRENT_QUESTION):
                    answer = {"text": "隔离模型：仍需核对缺失证据。", "sourceRefs": []}
                elif question == INVALID_QUESTION:
                    answer = None
                elif question == TIMEOUT_QUESTION:
                    self.server.chat_prompts.append(prompt)
                    time.sleep(32)
                    return
                else:
                    require(question == QUESTIONS[2], "fixture question")
                    answer = {"text": "伪造来源不可发布。", "sourceRefs": ["forged-fixture-ref"]}
                self.server.chat_prompts.append(prompt)
            else:
                image = content[0]["image_url"]["url"]
                mime, encoded = image.split(",", 1)
                require(mime == "data:image/png;base64" and base64.b64decode(encoded, validate=True) == PNG,
                        "unchanged original fixture image")
                self.server.vision_calls += 1
                if self.server.vision_calls == 2:
                    require(self.server.release_vision.wait(15), "test did not release pending vision")
                answer = [ROW]
        except (AssertionError, KeyError, TypeError, ValueError):
            self.respond({}, 422)
            return
        self.respond({"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps(answer, ensure_ascii=False) if answer is not None else "{",
        }}], "usage": {"prompt_tokens": 330, "completion_tokens": 45}})


@contextmanager
def models(*, risk_case=False):
    with ThreadingHTTPServer((HOST, 0), ModelFixture) as server:
        server.chat_prompts = []
        server.vision_calls = 0
        server.risk_case = risk_case
        server.release_vision = threading.Event()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.release_vision.set()
            server.shutdown()
            thread.join()


def event(peer, kind, *, seconds=20, **fields):
    return peer.wait(lambda frame: frame["type"] == "deliver" and frame["category"] == "business"
                     and frame["payload"]["kind"] == kind
                     and all(frame["payload"]["data"].get(key) == value for key, value in fields.items()), seconds=seconds)


def reject(peer, name, fields, code):
    ack = peer.transact(peer.command(name, fields))["payload"]["data"]
    require(not ack["ok"] and ack["error"]["code"] == code, f"{name} must reject with {code}")


def positions(harness):
    return [json.loads(body) for (body,) in query(harness.database, "SELECT body FROM objects WHERE collection='positions'")]


def upload(peer):
    result = peer.call("begin_upload", {"name": "ai-fixture.png", "mime": "image/png",
                                        "sizeBytes": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()})
    upload = result["upload"]
    chunks = [PNG[i:i + 32768] for i in range(0, len(PNG), 32768)]
    for index, chunk in enumerate(chunks):
        frame = peer.frame({"transferId": upload["uploadId"], "fileId": upload["fileId"],
                            "index": index, "totalChunks": len(chunks),
                            "chunkHash": hashlib.sha256(chunk).hexdigest(),
                            "bytes": base64.b64encode(chunk).decode()}, "file_chunk")
        peer.send(frame)
        peer.wait(lambda receipt: receipt["type"] == "receipt" and receipt["deliveryId"] == frame["deliveryId"])
    file_id = peer.call("finalize_upload", {"uploadId": upload["uploadId"]})["file"]["fileId"]
    require((peer.h.path / "data" / "files" / file_id).read_bytes() == PNG, "local original")
    return file_id


def reject_unapproved_dispatch(harness, file_id):
    with (harness.path / "local.toml").open("rb") as file:
        config = tomllib.load(file)["assistant"]
    token = (harness.path / config["token_file"]).read_text(encoding="ascii")
    request = Request(f"http://{config['address']}/api/tickdesk/ai/v1/vision", headers={"Authorization": f"Bearer {token}"},
                      data=json.dumps({"version": 1, "jobId": identifier(), "fileId": file_id,
                                       "serverEpoch": harness.identity()["server_epoch"],
                                       "deadline": int(time.time() * 1000) + 8000, "mime": "image/png",
                                       "image": base64.b64encode(PNG).decode()}).encode())
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=5):
            raise AssertionError("Python dispatched an image without a valid Rust job")
    except HTTPError as error:
        with error:
            require(error.status == 422 and json.load(error) == {"detail": "VISION_CONSENT_REQUIRED"},
                    "Python must reject dispatch without consent and a valid Rust job")


def verify_vision(peer, server):
    file_id = upload(peer)
    register = {"fileId": file_id, "importId": identifier()}
    notice = {"noticeVersion": query(peer.h.database, "SELECT version FROM vision_notice WHERE singleton=1")[0][0]}
    reject(peer, "register_image", register, "VISION_CONSENT_REQUIRED")
    reject_unapproved_dispatch(peer.h, file_id)
    require(server.vision_calls == 0, "image request before consent")
    peer.call("accept_vision_notice", notice | {"accepted": True})
    require(peer.call("register_image", register)["importStatus"] == "pending", "async vision accepted")
    result = event(peer, "import_result", importId=register["importId"])["payload"]["data"]
    require(result["status"] == "recognized" and result["recognizedRows"] == [ROW] and server.vision_calls == 1,
            f"Python recognition must be published as a draft; status={result['status']} error={result['errorCode']}")
    require(positions(peer.h) == [], "OCR must not write a position")
    row = {key: RAW[key] for key in ("name", "symbol", "cost", "availableQty")}
    row.update(qty=int(RAW["qty"]), rowId=ROW["rowId"], pendingLots=None, resolvedExecutionIds=[])
    preview = peer.call("apply_import", {"phase": "preview", "importId": register["importId"],
                                         "snapshotAsOf": int(time.time() * 1000) - 1000, "rows": [row]})
    require(preview["readyForConfirm"] and preview["diff"]["added"] == [SYMBOL] and positions(peer.h) == [],
            "saved preview must not write a position")
    confirmed = peer.call("apply_import", {"phase": "confirm", **{
        key: preview[key] for key in ("importId", "importVersion", "diffHash")
    }})
    event(peer, "import_result", importId=register["importId"], status="confirmed")
    result = positions(peer.h)
    require(confirmed["addedCount"] == 1 and len(result) == 1 and result[0]["symbol"] == SYMBOL
            and result[0]["qty"] == 200 and result[0]["availableQty"] is None and result[0]["pendingLots"] is None,
            "confirmed import commits one position and preserves unknown sellable quantity/batches")

    cancelled = register | {"importId": identifier()}
    peer.call("register_image", cancelled)
    wait_for(lambda: server.vision_calls == 2, "second image reaches vision fixture")
    reject(peer, "delete_file", {"fileId": file_id, "baseVersion": 1, "confirm": True}, "FILE_BUSY")
    peer.call("accept_vision_notice", notice | {"accepted": False})
    withdrawn = event(peer, "import_result", importId=cancelled["importId"], status="cancelled",
                      errorCode="VISION_CONSENT_REQUIRED")["payload"]["data"]
    check = query(peer.h.database, "SELECT checked_at FROM provider_health WHERE name='vision'")[0][0]
    server.release_vision.set()
    wait_for(lambda: bool(query(peer.h.database, "SELECT checked_at FROM provider_health "
                               "WHERE name='vision' AND checked_at>?", (check,))), "late vision completion")
    stored = query(peer.h.database, "SELECT body FROM objects WHERE collection='imports' AND object_id=?",
                   (cancelled["importId"],))
    require(len(stored) == 1 and json.loads(stored[0][0]) == withdrawn, "late vision result cannot replace cancellation")
    reject(peer, "register_image", register | {"importId": identifier()}, "VISION_CONSENT_REQUIRED")
    require(positions(peer.h) == result and server.vision_calls == 2, "withdrawal preserves ledger and blocks dispatch")
    print("PASS: WSS upload, Python OCR, consent, image lease, preview, Rust confirm, and withdrawal", flush=True)


def send_chat(peer, text, *, seconds=20):
    command = peer.command("chat", {"text": text})
    ack = peer.transact(command)["payload"]
    request_id = command["data"]["clientMsgId"]
    require(ack["data"]["ok"] and ack["data"]["result"] == {"accepted": True, "requestId": request_id},
            "asynchronous, durable chat acknowledgement")
    return command, ack, event(peer, "reply", requestId=request_id, seconds=seconds)


def verify_report(peer, prompts):
    command, _, frame = send_chat(peer, MANUAL_QUESTION)
    request_id = command["data"]["clientMsgId"]
    reply = frame["payload"]["data"]
    require(reply["status"] == "completed" and "信息不足" in reply["text"] and "量化比较" in reply["text"],
            f"manual analysis reply over WSS; status={reply['status']} error={reply['errorCode']}")
    (raw, stored), = query(peer.h.database, "SELECT raw_output, reply FROM chat_jobs WHERE request_id=?", (request_id,))
    artifact = json.loads(raw)["artifact"]
    context, report = artifact["context"], artifact["report"]
    require(context == prompts[-1]["context"] and report["contextId"] == context["contextId"]
            and report["body"]["stance"] == "insufficient" and json.loads(stored) == frame["payload"],
            "structured report and its frozen context are persisted with the WSS reply")
    archive = peer.call("list_reports", {"symbol": SYMBOL, "limit": 1, "subscribe": True})
    header, = archive["items"]
    require(header["reportId"] == request_id and header["state"] == "current"
            and header["origin"] == "manual" and header["stance"] == "insufficient"
            and header["coverage"]["external"] == "unavailable"
            and header["validUntil"] == report["validUntil"] and archive["nextCursor"] is None,
            "report archive shows a current manual analysis with provenance and expiry")
    detail = peer.call("get_report", {"reportId": request_id})
    require(detail["header"] == header and detail["text"] == reply["text"] and detail["sources"] == reply["sources"],
            "report detail resolves the actual saved analysis and its sources through WSS")

    prefix = f"【TickDesk 报告追问】{request_id}\n追问：\n"
    _, _, historical = send_chat(peer, prefix + "【按原报告】" + HISTORICAL_QUESTION)
    original = prompts[-1]["context"]
    require("依据原报告冻结证据" in historical["payload"]["data"]["text"] and original["historical"]
            and original["originalContextId"] == context["contextId"]
            and original["localEvidence"] == context["localEvidence"],
            "historical follow-up uses the original report evidence")
    _, _, current = send_chat(peer, prefix + "【按当前情况】" + CURRENT_QUESTION)
    fresh = prompts[-1]["context"]
    require(current["payload"]["data"]["status"] == "completed" and not fresh.get("historical")
            and fresh["contextId"] != original["contextId"]
            and fresh["decisionCutoffMs"] > original["decisionCutoffMs"] and fresh["symbols"] == [SYMBOL],
            "current follow-up captures current evidence separately")
    print("PASS: durable structured analysis and separate historical/current follow-up through WSS", flush=True)


def verify_scheduled_report(peer, prompts, schedule_gate, *, risk_case=False):
    harness = peer.h
    with (harness.path / "local.toml").open("rb") as file:
        config = tomllib.load(file)["assistant"]
    token = (harness.path / config["token_file"]).read_text(encoding="ascii")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = f"http://{RUST_HOST}:{harness.local_port}/internal/ai/v1"
    opener = build_opener(ProxyHandler({}))

    def request(path, data=None):
        body = json.dumps(data).encode() if data is not None else None
        with opener.open(Request(base + path, data=body, headers=headers), timeout=4) as response:
            return json.load(response)

    snapshot = request(f"/analysis/{SYMBOL}")
    require(snapshot["latest"] is None, "no scheduled report before reservation")
    previous_count = len(prompts)
    path = f"/analysis/{SYMBOL}"
    schedule_gate.touch()
    wait_for(lambda: (request(path)["latest"] or {}).get("state") == "completed", "scheduled analysis durable finish")
    snapshot = request(path)
    decision = {
        "version": 1, "serverEpoch": snapshot["evidence"]["serverEpoch"],
        "asOf": snapshot["evidence"]["asOf"], "stamp": snapshot["stamp"],
        "fingerprint": snapshot["latest"]["fingerprint"],
        "trigger": "initial", "riskVersions": [],
    }
    require(request(path, decision)["status"] == "deduplicated",
            "exact same scheduled input gets one Rust lease")
    saved = request(f"/report/{SYMBOL}")
    require(saved["artifact"]["context"]["purpose"] == "automatic"
            and saved["artifact"]["report"]["contextId"] == saved["artifact"]["context"]["contextId"]
            and len(prompts) == previous_count + 1, "scheduled job uses the real Python reporter exactly once")
    _, _, result = send_chat(peer, f"/报告 {SYMBOL}")
    require(result["payload"]["data"]["status"] == "completed"
            and any(label in result["payload"]["data"]["text"] for label in ("定时报告", "历史报告")),
            "WSS chat can retrieve the scheduled report")
    require(len(prompts) == previous_count + 1, "reading a saved report does not call the provider")
    archive = peer.call("list_reports", {"symbol": SYMBOL, "limit": 1})
    latest, = archive["items"]
    published = event(peer, "analysis_report", header=latest)
    deadline = published["payload"]["data"]["notifyUntil"]
    if risk_case:
        require(latest["stance"] == "restricted" and saved["reply"]["sources"]
                and deadline == min(published["payload"]["ts"] + 60_000, latest["validUntil"]),
                "material sourced risk has a bounded notification deadline")
    else:
        require(deadline is None, "ordinary report publication is quiet")
    require(query(harness.database, "SELECT event_id FROM transport_outbox WHERE delivery_id=?",
                  (published["deliveryId"],)), "report result and publication are durable in SQLite")
    page = peer.call("list_reports", {"symbol": SYMBOL, "limit": 1, "cursor": archive["nextCursor"]})
    previous, = page["items"]
    require(latest["origin"] == "automatic" and latest["state"] == "current" and previous["origin"] == "manual"
            and previous["state"] == "historical" and page["nextCursor"] is None,
            "newest report and history page contain one entry per original report, without saved-report aliases")
    detail = peer.call("get_report", {"reportId": latest["reportId"]})
    require(detail["header"] == latest and detail["text"] == saved["reply"]["text"]
            and len(prompts) == previous_count + 1, "automatic report details are readable without another model call")
    print("PASS: scheduled analysis reservation, deduplication, local report, and WSS retrieval", flush=True)
    if risk_case:
        print("PASS: sourced risk qualifies for a bounded report notification; same input remains deduplicated", flush=True)


def verify_chat(peer, server, local_binary, schedule_gate):
    harness = peer.h
    baseline = positions(harness)
    peer.hold_receipts = True
    first, ack, reply = send_chat(peer, QUESTIONS[0])
    request_id = first["data"]["clientMsgId"]
    body = reply["payload"]["data"]
    position_qty = baseline[0]["qty"] if baseline else None
    require(body["status"] == "completed" and ANSWERS[0] in body["text"]
            and "本地快照原值" in body["text"]
            and (position_qty is None or f"{SYMBOL} 持仓 {position_qty} 股" in body["text"])
            and body["unverified"] and len(body["sources"]) == 1,
            f"Python reply must preserve the model text and local snapshot citation; status={body['status']}")
    require(body["sources"][0]["ref"] in server.chat_prompts[0]["allowedSourceRefs"], "reply citation provenance")
    require(query(harness.database, "SELECT state,model FROM chat_jobs WHERE request_id=?", (request_id,))
            == [("completed", "fixture-chat")], "job durable before reply receipt")
    identity = harness.identity()
    peer.close_socket()
    harness.stop("local")
    harness.start("local", local_binary)
    peer.connect()
    replay = peer.wait(lambda frame: frame.get("deliveryId") == reply["deliveryId"])
    require(replay == reply and harness.identity() == identity, "restart must replay the same reply delivery")
    peer.hold_receipts = False
    for frame in peer.frames:
        if frame["type"] == "deliver":
            peer.receipt(frame)
    wait_for(lambda: query(harness.database, "SELECT state FROM transport_outbox WHERE delivery_id=?",
                           (reply["deliveryId"],)) == [("delivered",)], "durable App receipt")

    require(peer.transact(first)["payload"] == ack, "idempotent chat retry after restart")
    _, _, second = send_chat(peer, QUESTIONS[1])
    require(ANSWERS[1] in second["payload"]["data"]["text"], "second reply")
    prompts = server.chat_prompts
    require([prompt["question"] for prompt in prompts] == list(QUESTIONS[:2]), "duplicate must not call provider")
    require(prompts[0]["history"] == [] and prompts[1]["history"] == [
        {"role": "user", "text": QUESTIONS[0]}, {"role": "assistant", "text": body["text"]},
    ], "follow-up obtains durable prior turns")
    require(prompts[1]["context"]["serverEpoch"] == identity["server_epoch"]
            and prompts[1]["context"]["quality"]["external"] == "unavailable",
            "frozen context shows epoch and evidence availability")
    history = peer.call("get_messages")["items"]
    require(len(history) == 4 and any(item.get("reply") == body for item in history), "v2 chat history after restart")
    print("PASS: durable WSS chat, matching citation, restart replay, idempotency, and multi-turn history", flush=True)

    verify_report(peer, prompts)
    before_budget = len(prompts)
    verify_scheduled_report(peer, prompts, schedule_gate)
    _, _, invalid = send_chat(peer, QUESTIONS[2])
    body = invalid["payload"]["data"]
    require(body["status"] == "failed" and body["errorCode"] == "AI_UNVERIFIED_SOURCE",
            "forged model citation must produce an explicit error")
    for question, code, explanation in (
        (INVALID_QUESTION, "AI_INVALID_RESPONSE", "完整"), (TIMEOUT_QUESTION, "AI_TIMEOUT", "超时"),
    ):
        _, _, frame = send_chat(peer, question, seconds=40)
        failure = frame["payload"]["data"]
        require(failure["status"] == "failed" and failure["errorCode"] == code and explanation in failure["text"],
                f"{code} reaches the App as an actionable failure")
        history = peer.call("get_messages", {"limit": 4})
        require(any(row.get("reply", {}).get("requestId") == failure["requestId"]
                    and row["reply"]["errorCode"] == code for row in history["items"]), "failure is persisted in history")
    budget_path = harness.path / "python-data" / "tickdesk-ai" / "model-budget.sqlite3"
    usage = query(budget_path, "SELECT kind,count(*),sum(input_tokens IS NULL),sum(input_tokens) FROM model_calls GROUP BY kind")
    chat_usage = {kind: (attempts, unknown, tokens) for kind, attempts, unknown, tokens in usage}["chat"]
    metered = len(prompts) - before_budget
    require(chat_usage == (metered, 1, 330 * (metered - 1)), "durable model ledger counts attempts by kind")
    before_outage = len(prompts)
    harness.stop("assistant")
    _, _, offline = send_chat(peer, "Python 不可用时，检查错误状态。")
    require(offline["payload"]["data"]["errorCode"] == "AI_UNAVAILABLE" and len(prompts) == before_outage,
            "unavailable assistant must fail closed without a second model route")
    require(positions(harness) == baseline, "chat did not change durable positions")
    print("PASS: citation, invalid response, timeout and outage surface as durable WSS failures; usage recorded once",
          flush=True)


def run(path, backend_python, local_binary, *, risk_case=False):
    suffix = ".exe" if os.name == "nt" else ""
    bin_dir = path / "bin"
    bin_dir.mkdir()
    local_binary = Path(shutil.copy2(local_binary, bin_dir / f"tickdesk-local{suffix}"))
    relay_binary = Path(shutil.copy2(ROOT / "local-service" / "relay" / "target" / "debug" / f"tickdesk-relay{suffix}", bin_dir))
    harness = Harness(path)
    local_address = f"{RUST_HOST}:{harness.local_port}"
    schedule_gate = path / "start-fixture-scheduler"
    try:
        with (models(risk_case=risk_case) as server,
              assistant_fixture(path, local_address, server.server_port, backend_python,
                                schedule_gate=schedule_gate) as assistant):
            harness.children["assistant"] = assistant
            harness.start("relay", relay_binary)
            harness.start("local", local_binary)
            peer = AppPeer(harness)
            peer.connect()
            verify_vision(peer, server)
            if risk_case:
                verify_report(peer, server.chat_prompts)
                verify_scheduled_report(peer, server.chat_prompts, schedule_gate, risk_case=True)
            else:
                verify_chat(peer, server, local_binary, schedule_gate)
    finally:
        harness.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-python", type=Path, default=BACKEND_PYTHON)
    local_exe = "tickdesk-local.exe" if os.name == "nt" else "tickdesk-local"
    parser.add_argument("--local-binary", type=Path, default=ROOT / "local-service" / "target" / "debug" / local_exe)
    parser.add_argument("--evidence-root", type=Path, help="preserve isolated databases and process logs here")
    parser.add_argument("--risk-case-only", action="store_true", help="verify a sourced scheduled risk report publication")
    options = parser.parse_args()
    if options.evidence_root:
        path = Path(tempfile.mkdtemp(prefix="tickdesk-local-ai-", dir=options.evidence_root)).resolve()
        print(f"Evidence directory: {path}", flush=True)
        run(path, options.backend_python.resolve(), options.local_binary.resolve(), risk_case=options.risk_case_only)
    else:
        with tempfile.TemporaryDirectory(prefix="tickdesk-local-ai-") as directory:
            run(Path(directory).resolve(), options.backend_python.resolve(), options.local_binary.resolve(),
                risk_case=options.risk_case_only)
