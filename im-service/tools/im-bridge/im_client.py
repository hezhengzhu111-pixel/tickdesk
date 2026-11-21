"""TickFlow 本地分析 <-> im-foundation 桥接客户端 (标准库实现).

用途:
  1. 以"盯盘助手"账号身份, 把扫描结果/买卖点推送到 im-foundation (手机 App 可见/可推送)
  2. 轮询用户在手机会话里的操作回复 (买入/卖出/忽略/关注...), 形成"通知-操作-回调分析"闭环
  3. --selftest 用内置 mock IM 服务验证内部 HMAC 签名与收发流程

im-foundation 相关契约 (源码核对):
  - 内部服务签名: canonical = "method=POST&path=/api/...&bodyHash={sha256_urlsafe_nopad(body)}&ts={ms}&nonce={uuid}"
    签名 = urlsafe_b64_nopad(HMAC-SHA256(IM_INTERNAL_SECRET, canonical))
    请求头: X-Internal-Timestamp / X-Internal-Nonce / X-Internal-Signature
  - 换令牌: POST /api/auth/internal/token  {"user_id": <助手用户ID>, "tenant_id": <租户ID>}
    -> data.accessToken
  - 发私聊: POST /api/message/send/private  Bearer <助手token>
    {"receiver_id": <你的用户ID>, "message_type": "TEXT", "content": "...", "client_message_id": "..."}
    (需助手与你是好友; 群聊模式则用 /api/message/send/group {"group_id": ..., ...})
  - 拉记录: GET /api/message/private/{peer_id}/cursor?after_message_id=<N>  Bearer <助手token>
    (群聊: GET /api/message/group/{group_id}/cursor?after_message_id=<N>)
  - 文本上限 2000 字符, 客户端自动分条

用法:
  python im_client.py --selftest                     # 本地 mock 自测
  python im_client.py --print-token                  # 用配置换一次令牌
  python im_client.py --send "测试消息"               # 发一条私聊/群消息
  python im_client.py --poll-once                    # 拉一次新消息并解析操作
  python im_client.py --loop                         # 每 10s 轮询 (常驻监听操作)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
STATE_PATH = HERE / "state.json"
LOG_PATH = HERE / "im_bridge.log"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

TZ = timezone(timedelta(hours=8), "CST")
MAX_TEXT = 1900  # 服务端 Text 上限 2000, 留余量


def log(msg: str) -> None:
    line = f"{datetime.now(TZ):%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- 签名
def b64url_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def internal_headers(method: str, path: str, body: bytes, secret: str) -> dict:
    ts = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())
    body_hash = b64url_nopad(hashlib.sha256(body).digest())
    # 与 Rust normalize_path 一致: 去掉 query
    clean_path = path.split("?", 1)[0]
    canonical = f"method={method.upper()}&path={clean_path}&bodyHash={body_hash}&ts={ts}&nonce={nonce}"
    sig = b64url_nopad(hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).digest())
    return {
        "X-Internal-Timestamp": ts,
        "X-Internal-Nonce": nonce,
        "X-Internal-Signature": sig,
    }


# ---------------------------------------------------------------- HTTP
class ImClient:
    def __init__(self, cfg: dict):
        self.base = (cfg.get("base_url") or "").rstrip("/")
        self.secret = cfg.get("internal_secret") or ""
        self.tenant_id = int(cfg.get("tenant_id") or 0)
        self.assistant_user_id = int(cfg.get("assistant_user_id") or 0)
        self.owner_user_id = int(cfg.get("owner_user_id") or 0)
        self.mode = cfg.get("mode") or "private"
        self.group_id = int(cfg.get("group_id") or 0)
        self._token: str | None = None
        self._token_expire_at = 0.0

    # -- 基础请求
    def _request(self, method: str, path: str, payload=None, token: str | None = None,
                 timeout: float = 15.0) -> dict:
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        else:
            headers.update(internal_headers(method, path, body, self.secret))
        req = urllib.request.Request(self.base + path, data=body if payload is not None else None,
                                     method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read() or b"{}")
                if not data.get("success", False):
                    raise RuntimeError(f"IM API 失败: {data}")
                return data.get("data") or {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc

    # -- 内部签名接口
    def mint_token(self) -> str:
        if self._token and time.time() < self._token_expire_at - 60:
            return self._token
        data = self._request("POST", "/api/auth/internal/token",
                             {"user_id": self.assistant_user_id, "tenant_id": self.tenant_id})
        token = data.get("accessToken") or data.get("access_token")
        if not token:
            raise RuntimeError(f"未拿到 accessToken: {data}")
        self._token = token
        self._token_expire_at = time.time() + (data.get("expiresInMs") or 3600_000) / 1000.0
        return token

    # -- 业务接口
    def send_text(self, text: str, message_type: str = "TEXT") -> list[dict]:
        token = self.mint_token()
        chunks = [text[i:i + MAX_TEXT] for i in range(0, len(text), MAX_TEXT)] or [""]
        sent = []
        for chunk in chunks:
            if self.mode == "group":
                payload = {"group_id": self.group_id, "message_type": message_type,
                           "content": chunk, "client_message_id": uuid.uuid4().hex}
                path = "/api/message/send/group"
            else:
                payload = {"receiver_id": self.owner_user_id, "message_type": message_type,
                           "content": chunk, "client_message_id": uuid.uuid4().hex}
                path = "/api/message/send/private"
            sent.append(self._request("POST", path, payload, token=token))
        return sent

    def poll_new(self, after_message_id: int) -> tuple[list[dict], int]:
        """拉取 after 之后的新消息; 返回 (消息列表, 最新 message_id)。"""
        token = self.mint_token()
        if self.mode == "group":
            path = f"/api/message/group/{self.group_id}/cursor?after_message_id={after_message_id}"
        else:
            path = f"/api/message/private/{self.owner_user_id}/cursor?after_message_id={after_message_id}"
        data = self._request("GET", path, token=token)
        messages = data if isinstance(data, list) else (data.get("messages") or [])
        latest = after_message_id
        for m in messages:
            try:
                mid = int(m.get("id") or m.get("messageId") or 0)
            except (TypeError, ValueError):
                mid = 0
            latest = max(latest, mid)
        return messages, latest


# ---------------------------------------------------------------- 操作解析
SYMBOL_RE = re.compile(r"\b(\d{6})\.(SZ|SH|BJ)\b|(?<!\d)(\d{6})(?!\d)")
ACTION_WORDS = [
    ("清仓", "sell_all"), ("卖出", "sell"), ("减仓", "sell"),
    ("已卖", "sell"), ("买入", "buy"), ("加仓", "buy"), ("已买", "buy"),
    ("关注", "watch"), ("忽略", "ignore"), ("放弃", "ignore"), ("取消关注", "unwatch"),
]


def parse_action(text: str) -> dict | None:
    if not text:
        return None
    raw = text.strip()
    action = None
    for word, kind in ACTION_WORDS:
        if word in raw:
            action = kind
            break
    if not action:
        return None
    m = SYMBOL_RE.search(raw.upper())
    symbol = None
    if m:
        if m.group(1):
            symbol = f"{m.group(1)}.{m.group(2)}"
        else:
            code = m.group(3)
            symbol = f"{code}.{'SH' if code.startswith(('6', '9')) else 'BJ' if code.startswith(('4', '8')) else 'SZ'}"
    qty = None
    qm = re.search(r"(\d+)\s*(股|手|%)", raw)
    if qm:
        qty = int(qm.group(1))
    return {"action": action, "symbol": symbol, "value": qty, "raw": raw, "at": int(time.time())}


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"last_message_id": 0, "actions": [], "positions": {}, "watch": {}}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def apply_actions(state: dict, actions: list[dict]) -> list[str]:
    notes = []
    for a in actions:
        state["actions"].append(a)
        sym = a.get("symbol")
        if not sym:
            continue
        if a["action"] == "buy":
            state["positions"][sym] = {"status": "holding", "since": a["at"]}
            notes.append(f"{sym} 已记录为持仓")
        elif a["action"] in ("sell", "sell_all"):
            state["positions"][sym] = {"status": "sold", "since": a["at"]}
            notes.append(f"{sym} 已记录为卖出")
        elif a["action"] == "watch":
            state["watch"][sym] = a["at"]
            notes.append(f"{sym} 加入关注")
        elif a["action"] == "unwatch":
            state["watch"].pop(sym, None)
            notes.append(f"{sym} 取消关注")
        elif a["action"] == "ignore":
            notes.append(f"{sym} 已忽略本轮信号")
    state["actions"] = state["actions"][-500:]
    return notes


# ---------------------------------------------------------------- 自测 (内置 mock IM)
def selftest() -> int:
    secret = "selftest-secret-at-least-32-bytes-long!!"
    state = {"messages": [], "next_id": 1}

    class MockHandler(BaseHTTPRequestHandler):
        def _json(self, code: int, payload: dict):
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _ok(self, data):
            self._json(200, {"code": 200, "message": "success", "data": data,
                             "success": True, "timestamp": int(time.time() * 1000)})

        def _verify_internal(self, body: bytes) -> bool:
            ts = self.headers.get("X-Internal-Timestamp", "")
            nonce = self.headers.get("X-Internal-Nonce", "")
            sig = self.headers.get("X-Internal-Signature", "")
            path = self.path.split("?", 1)[0]
            body_hash = b64url_nopad(hashlib.sha256(body).digest())
            canonical = f"method=POST&path={path}&bodyHash={body_hash}&ts={ts}&nonce={nonce}"
            expect = b64url_nopad(hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).digest())
            return hmac.compare_digest(expect, sig) and abs(time.time() * 1000 - int(ts or 0)) < 300_000

        def do_GET(self):  # noqa: N802
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer selftest-token"):
                return self._json(401, {"success": False, "code": 401, "message": "unauthorized"})
            if "/cursor" in self.path:
                self._ok(state["messages"])
            else:
                self._json(404, {"success": False})

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            path = self.path.split("?", 1)[0]
            if path == "/api/auth/internal/token":
                if not self._verify_internal(body):
                    return self._json(401, {"success": False, "code": 401, "message": "bad signature"})
                return self._ok({"accessToken": "selftest-token", "expiresInMs": 3_600_000})
            if path == "/api/message/send/private":
                if not self.headers.get("Authorization", "").startswith("Bearer selftest-token"):
                    return self._json(401, {"success": False, "code": 401, "message": "unauthorized"})
                req = json.loads(body)
                msg = {"id": state["next_id"], "senderId": 1001, "receiverId": req["receiver_id"],
                       "messageType": req.get("message_type", "TEXT"), "content": req.get("content", "")}
                state["next_id"] += 1
                state["messages"].append(msg)
                return self._ok(msg)
            self._json(404, {"success": False})

        def log_message(self, *args):  # noqa: D401
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    client = ImClient({
        "base_url": f"http://127.0.0.1:{port}",
        "internal_secret": secret,
        "tenant_id": 1,
        "assistant_user_id": 1001,
        "owner_user_id": 2002,
        "mode": "private",
    })
    ok = True
    try:
        token = client.mint_token()
        assert token == "selftest-token", token
        log("SELFTEST 1/4 内部签名换令牌 OK")
        sent = client.send_text("扫描测试: 603366.SH 趋势突破 +5.54%")
        assert sent and sent[0]["id"] == 1, sent
        log("SELFTEST 2/4 发送私聊消息 OK")
        msgs, latest = client.poll_new(0)
        assert len(msgs) == 1 and latest == 1, (msgs, latest)
        log("SELFTEST 3/4 拉取消息 (cursor) OK")
        a = parse_action("已买入 300502，成本187.6")
        assert a and a["action"] == "buy" and a["symbol"] == "300502.SZ", a
        log("SELFTEST 4/4 操作解析 OK: " + json.dumps(a, ensure_ascii=False))
        client.send_text("长消息分条测试 " + "x" * 5000)
        assert len(client.send_text("y" * 5000)) == 3
        log("SELFTEST 额外: 超长消息自动分条 OK")
    except Exception as exc:  # noqa: BLE001
        ok = False
        log(f"SELFTEST 失败: {exc}")
    finally:
        server.shutdown()
    log("SELFTEST 全部通过" if ok else "SELFTEST 存在失败")
    return 0 if ok else 1


# ---------------------------------------------------------------- CLI
def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description="TickFlow <-> im-foundation 桥接客户端")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--print-token", action="store_true")
    parser.add_argument("--send", default=None)
    parser.add_argument("--poll-once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=10)
    args = parser.parse_args()

    if args.selftest:
        sys.exit(selftest())

    cfg = load_config()
    if not cfg.get("base_url") or not cfg.get("internal_secret"):
        print("请先填写 tools/im-bridge/config.json (base_url / internal_secret / 用户ID)")
        sys.exit(2)
    client = ImClient(cfg)
    state = load_state()

    if args.print_token:
        log("token: " + client.mint_token()[:24] + "...")
    if args.send:
        sent = client.send_text(args.send)
        log(f"已发送 {len(sent)} 条消息")
    if args.poll_once or args.loop:
        while True:
            try:
                msgs, latest = client.poll_new(int(state.get("last_message_id", 0)))
                mine = [m for m in msgs if str(m.get("senderId") or m.get("sender_id")) == str(cfg.get("owner_user_id"))]
                actions = [a for a in (parse_action(m.get("content", "")) for m in mine) if a]
                if actions:
                    notes = apply_actions(state, actions)
                    log("收到操作: " + json.dumps(actions, ensure_ascii=False))
                    if notes:
                        client.send_text("已记录: " + "; ".join(notes))
                state["last_message_id"] = latest
                save_state(state)
            except Exception as exc:  # noqa: BLE001
                log(f"轮询失败: {exc}")
            if not args.loop:
                break
            time.sleep(max(3, args.interval))


if __name__ == "__main__":
    main()

