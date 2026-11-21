"""Loopback-only fixtures for integration tests; never loaded by normal deployment."""
import contextlib
import datetime
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


BACKEND = Path(__file__).resolve().parents[1] / "im-service" / "backend"
BACKEND_PYTHON = BACKEND / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


class FixtureProviders(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Never log request contents or the image data URL.

    def respond(self, body, status=200):
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/health":
            self.respond({"status": "ok", "version": "integration-fixture"})
        elif self.path == "/api/settings/capability-matrix":
            self.respond({"capabilities": [{"id": "daily", "usable": True, "effective": "fixture"}]})
        elif self.path.startswith("/api/strategies"):
            self.respond({"strategies": [{"id": "fixture", "research_only": False}], "load_errors": []})
        else:
            self.respond({"error": "not_found"}, 404)

    def do_POST(self):
        demo = getattr(self.server, "demo", False)
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 30 * 1024 * 1024:
            self.respond({"error": "request_size"}, 413)
            return
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeError):
            self.respond({"error": "invalid_json"}, 400)
            return
        if self.path == "/chat":
            content = {"text": ("【模拟回复】这是联调数据。你可以确认或忽略事项、录入成交、调整持仓、上传截图并预览导入。操作会保存到演示本地服务，重新打开 App 后仍然保留。现金余额未知，模拟数据不代表真实行情。" if demo else "隔离联调答复：未知现金保持未知；这是测试供应商返回的解释。"), "sourceRefs": []}
            # The Python report path requests its strict eight-dimension schema.
            # Keep the fixture in that path instead of bypassing report validation.
            try:
                prompt = json.loads(body["messages"][-1]["content"])
            except (KeyError, IndexError, TypeError, ValueError):
                prompt = {}
            if not demo and isinstance(prompt, dict) and prompt.get("context", {}).get("purpose") in ("manual", "automatic"):
                missing = {"text": "隔离资料不足。", "sourceRefs": []}
                content = {"stance": "insufficient", "summary": missing,
                           "dimensions": dict.fromkeys(("operations", "financials", "industry", "events", "macro",
                                                        "valuation", "priceVolume", "portfolio"), missing),
                           "opposingEvidence": [], "unknowns": ["外部资料未配置。"],
                           "invalidateWhen": ["原证据发生变化。"], "reviewWhen": ["新增披露。"],
                           "quantAgreement": "unavailable", "quantReason": missing}
            self.respond({"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(content, ensure_ascii=False)}}]})
        elif self.path == "/vision":
            time.sleep(0.5)  # Allows exercising cancellation while external work is pending.
            raw = dict(name="隔离联调证券", symbol="300502.SZ", qty="200", availableQty="200",
                       cost="10.00", lastPrice=None, marketValue=None, pnl=None)
            rows = [{"rowId": "row1", "raw": raw,
                     "confidence": {key: "0.9" if value is not None else None for key, value in raw.items()}, "issues": []}]
            if demo:
                rows = []
                for index, (code, name, qty, cost) in enumerate([
                        ("000001.SZ", "模拟平安", "500", "11.00"),
                        ("300502.SZ", "模拟科技", "300", "10.75"),
                        ("600000.SH", "模拟银行", "1000", "10.00")]):
                    raw = dict(name=name, symbol=code, qty=qty, availableQty=qty, cost=cost,
                               lastPrice=None, marketValue=None, pnl=None)
                    rows.append({"rowId": f"row{index + 1}", "raw": raw,
                                 "confidence": {key: "0.9" if value is not None else None for key, value in raw.items()}, "issues": []})
            self.respond({"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(rows, ensure_ascii=False)}}]})
        elif self.path == "/api/tickdesk/prepare":
            now = int(time.time() * 1000)
            data = body["data"]
            source = {"name": "隔离联调数据", "ref": "fixture://market/300502", "asOf": now}
            symbol = {"symbol": "300502.SZ", "name": "隔离联调证券", "exchange": "SZSE",
                      "instrument": "stock", "board": "创业板", "supported": True, "source": source}
            if demo:
                source["name"] = "模拟行情（联调专用）"
                symbol["name"] = "模拟科技"
            quotes = [{"symbol": code, "price": price, "asOf": now, "source": source, "status": "fresh"}
                      for code, price in ([('300502.SZ', '12.25'), ('600000.SH', '9.80'), ('000001.SZ', '11.50')]
                                          if demo else [('300502.SZ', '12.25')])]
            command = data.get("cmd")
            if command in ("search_symbols", "watch", "unwatch"):
                self.respond({"symbols": [symbol]})
            elif command == "get_chart":
                before = data.get("beforeDate", "9999-12-31")
                dates = ["2026-11-11", "2026-11-12", "2026-11-13"]
                eligible = [day for day in dates if day < before]
                page = eligible[-data.get("limit", 20):]
                candles = [{"date": day, "open": "10.00", "high": "10.50", "low": "9.80",
                            "close": "10.20", "volume": 100000, "amount": "1020000.00"} for day in page]
                more = len(page) < len(eligible)
                self.respond({"chart": {"symbol": data["symbol"], "adjustment": data["adjustment"],
                             "asOf": now, "dataStatus": "fresh", "candles": candles,
                             "nextBeforeDate": page[0] if more else None, "hasMore": more, "source": source}})
            elif command == "quotes":
                self.respond({"quotes": quotes})
            else:
                day = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
                # Explicit scenarios avoid depending on retries or scheduled scan counts.
                opened = demo or (command == "scan_now" and body.get("profile") == "aggressive")
                candidate = {"symbol": "300502.SZ", "name": "隔离联调证券", "score": "88",
                             "strategies": ["fixture-a", "fixture-b", "fixture-c"], "changeRatio": "0.02",
                             "volumeRatio": "1.2", "marketCap": None, "lastPrice": "12.25", "dataAsOf": now, "riskStatus": "unknown"}
                if demo:
                    candidate["name"] = "模拟科技"
                self.respond({"fixture": "tickdesk-integration-v1", "frozen": {"tradeDate": day, "isTradingDay": opened, "generation": "fixture-v1",
                              "generationAfter": "fixture-v1", "asOf": now, "strategies": {"total": 3, "succeeded": 3, "failed": []},
                              "policy": {"minScore": "70"}, "candidates": [{"candidate": candidate, "direction": "buy", "approved": True,
                              "supported": True, "liquidityPassed": True, "source": source}] if opened else []},
                              "quotes": quotes})
        else:
            self.respond({"error": "not_found"}, 404)


@contextlib.contextmanager
def fixture_providers():
    with ThreadingHTTPServer(("127.0.0.1", 0), FixtureProviders) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address[1]
        finally:
            server.shutdown()
            thread.join(timeout=5)


@contextlib.contextmanager
def assistant_fixture(folder, local_address, model_port, python=BACKEND_PYTHON, *, schedule_gate=None, vision_settings=None):
    """Mount the real Python API with loopback-only model configuration."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    address = f"127.0.0.1:{port}"
    token_file = folder / "assistant.token"
    with os.fdopen(os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                   "w", encoding="ascii") as token:
        token.write(secrets.token_urlsafe(48))
    with (folder / "local.toml").open("a", encoding="utf-8") as config:
        config.write(f"\n[assistant]\naddress='{address}'\ntoken_file='assistant.token'\n")
    data = folder / "python-data"
    data.mkdir()
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("TICKDESK_AI_", "TICKDESK_FACTS_"))}
    env.update(DATA_DIR=str(data), PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=str(BACKEND), TICKDESK_AI_TOKEN_FILE=str(token_file),
               TICKDESK_AI_LOCAL_ADDRESS=local_address)
    for kind in ("chat", "vision"):
        env[f"TICKDESK_AI_{kind.upper()}_ENDPOINT"] = f"http://127.0.0.1:{model_port}/{kind}"
        env[f"TICKDESK_AI_{kind.upper()}_MODEL"] = f"fixture-{kind}"
    if vision_settings is not None:
        allowed = {"TICKDESK_AI_VISION_ENDPOINT", "TICKDESK_AI_VISION_MODEL", "TICKDESK_AI_VISION_KEY_FILE"}
        if not set(vision_settings) <= allowed:
            raise ValueError("Unsupported real-vision test setting")
        env.update(vision_settings)
    server_init = "application = FastAPI()\n"
    if schedule_gate is not None:
        server_init = f"""
import asyncio
import contextlib
import os
from pathlib import Path
from app.api.tickdesk import assistant_lifespan

@contextlib.asynccontextmanager
async def lifespan(application):
    async def gated_start():
        while not Path({str(schedule_gate)!r}).is_file():
            await asyncio.sleep(0.1)
        os.environ["TICKDESK_AI_AUTO_ENABLED"] = "1"
        async with assistant_lifespan(application, {str(data)!r}):
            await asyncio.Event().wait()

    task = asyncio.create_task(gated_start())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

application = FastAPI(lifespan=lifespan)
"""
    serve = (
        "from fastapi import FastAPI\n"
        "import uvicorn\n"
        "from app.api.tickdesk import router\n"
        + server_init +
        "application.include_router(router)\n"
        f"uvicorn.run(application, host='127.0.0.1', port={port}, proxy_headers=False, access_log=False)\n"
    )
    with (folder / "assistant.log").open("wb") as log:
        child = subprocess.Popen([str(python), "-c", serve], cwd=folder, env=env,
                                 stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            opener = build_opener(ProxyHandler({}))
            request = Request(f"http://{address}/api/tickdesk/ai/v1/chat", data=b"{}")
            deadline = time.monotonic() + 30
            while child.poll() is None and time.monotonic() < deadline:
                try:
                    with opener.open(request, timeout=1):
                        raise RuntimeError("Python bridge allowed an unauthenticated request")
                except HTTPError as error:
                    with error:
                        if error.status != 401 or json.load(error) != {"detail": "AI_AUTH_REQUIRED"}:
                            raise RuntimeError(f"Unexpected Python bridge authentication status: {error.status}") from None
                    break
                except URLError:
                    time.sleep(0.1)
            else:
                raise RuntimeError(f"Python assistant did not start. See {folder / 'assistant.log'}")
            yield child
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Loopback mock providers for the persistent demo")
    parser.add_argument("--serve", action="store_true", required=True)
    parser.add_argument("--port", type=int, default=18767)
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    with ThreadingHTTPServer(("127.0.0.1", args.port), FixtureProviders) as server:
        server.demo = args.demo
        print(f"Mock providers listening on loopback:{args.port}; demo={args.demo}", flush=True)
        server.serve_forever()
