"""TickFlow 面板通知中转服务 (标准库实现, 无第三方依赖).

面板「设置 → 通知 → 通用 Webhook」POST 一条 JSON 信封到:
    http://127.0.0.1:8899/tsp
信封格式:
    {"event": "...", "timestamp": 123, "title": "...", "body": "...", "data": {...}}
本服务按 relay_config.json 里的渠道转发:
    - pushplus   微信 (https://www.pushplus.plus)
    - serverchan 微信 (https://sct.ftqq.com)
    - wecom_bot  企业微信群机器人
    - smtp       邮件 (QQ 邮箱/163 等, 同时覆盖 QQ 通知)
所有渠道都可在配置里留空, 留空即跳过; 修改配置后无需重启 (每次请求热加载).

启动: python relay.py   监听 127.0.0.1:8899
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import smtplib
import ssl
import sys
import urllib.parse
import urllib.request
from email.header import Header
from email.mime.text import MIMEText
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "relay_config.json"
LOG_FILE = HERE / "relay.log"
HOST = "127.0.0.1"
PORT = 8899

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("relay")


def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取配置失败: %s", exc)
        return {}


def _http_post_json(url: str, payload: dict, timeout: float = 8.0) -> tuple[bool, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return 200 <= resp.status < 300, f"HTTP {resp.status} {body[:200]}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def push_pushplus(cfg: dict, title: str, content: str) -> tuple[bool, str]:
    token = (cfg.get("pushplus") or {}).get("token", "").strip()
    if not token:
        return False, "pushplus token 未配置"
    data = json.dumps(
        {"token": token, "title": title, "content": content, "template": "markdown"},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://www.pushplus.plus/send", data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(body)
            except Exception:  # noqa: BLE001
                parsed = {}
            code = parsed.get("code")
            msg = parsed.get("msg", "")
            if resp.status == 200 and code in (200, "200"):
                return True, f"code={code} msg={msg}"
            return False, f"HTTP {resp.status} code={code} body={body[:200]}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def push_serverchan(cfg: dict, title: str, content: str) -> tuple[bool, str]:
    sendkey = (cfg.get("serverchan") or {}).get("sendkey", "").strip()
    if not sendkey:
        return False, "serverchan sendkey 未配置"
    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    data = urllib.parse.urlencode({"title": title, "desp": content}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return 200 <= resp.status < 300, f"HTTP {resp.status} {resp.read()[:200].decode('utf-8', 'replace')}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def push_wecom_bot(cfg: dict, title: str, content: str) -> tuple[bool, str]:
    url = (cfg.get("wecom_bot") or {}).get("url", "").strip()
    if not url:
        return False, "wecom_bot url 未配置"
    return _http_post_json(url, {"msgtype": "markdown", "markdown": {"content": f"**{title}**\n{content}"}})


def push_smtp(cfg: dict, title: str, content: str) -> tuple[bool, str]:
    s = cfg.get("smtp") or {}
    host = (s.get("host") or "").strip()
    to_addresses = [a.strip() for a in (s.get("to_addresses") or []) if a and a.strip()]
    if not host or not to_addresses:
        return False, "smtp 未配置 host/to_addresses"
    port = int(s.get("port") or 465)
    security = (s.get("security") or "ssl").lower()
    username = (s.get("username") or "").strip()
    password = (s.get("password") or "").strip()
    from_address = (s.get("from_address") or username).strip()
    msg = MIMEText(content, "plain", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = from_address
    msg["To"] = ", ".join(to_addresses)
    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=10, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=10)
            if security == "starttls":
                server.starttls(context=ssl.create_default_context())
        if username:
            server.login(username, password)
        server.sendmail(from_address, to_addresses, msg.as_string())
        server.quit()
        return True, "sent"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


CHANNELS = [
    ("pushplus", push_pushplus),
    ("serverchan", push_serverchan),
    ("wecom_bot", push_wecom_bot),
    ("smtp", push_smtp),
]


def verify_signature(cfg: dict, raw: bytes, headers) -> bool:
    secret = (cfg.get("hmac_secret") or "").strip()
    if not secret:
        return True
    sig = headers.get("X-TickFlow-Signature", "")
    ts = headers.get("X-TickFlow-Timestamp", "")
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        logger.warning("HMAC 校验失败 (timestamp=%s)", ts)
        return False
    return True


def build_content(envelope: dict) -> tuple[str, str]:
    """把面板信封整理成标题 + 正文。"""
    title = str(envelope.get("title") or "TickFlow 通知")
    body = str(envelope.get("body") or "")
    event = str(envelope.get("event") or "")
    data = envelope.get("data") or {}
    lines = [body] if body else []
    if event:
        lines.append(f"\n事件: {event}")
    if isinstance(data, dict) and data:
        interesting = {k: v for k, v in data.items() if not isinstance(v, (dict, list))}
        if interesting:
            lines.append("详情: " + json.dumps(interesting, ensure_ascii=False)[:1500])
    lines.append(f"时间: {envelope.get('timestamp', '')}")
    return title, "\n".join(lines)


class Handler(BaseHTTPRequestHandler):
    server_version = "TickFlowRelay/1.0"

    def _send(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send(200, {"ok": True, "channels": [name for name, _ in CHANNELS]})
        else:
            self._send(404, {"ok": False, "detail": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            envelope = json.loads(raw or b"{}")
        except Exception:  # noqa: BLE001
            self._send(400, {"ok": False, "detail": "invalid json"})
            return
        cfg = load_config()
        if not verify_signature(cfg, raw, self.headers):
            self._send(401, {"ok": False, "detail": "bad signature"})
            return
        title, content = build_content(envelope)
        logger.info("收到通知 [%s] %s | %s", envelope.get("event", ""), title, content.replace("\n", " ")[:300])
        forwarded, errors = [], []
        for name, fn in CHANNELS:
            try:
                ok, detail = fn(cfg, title, content)
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, str(exc)
            if ok:
                forwarded.append(name)
                logger.info("  -> %s OK", name)
            elif detail and "未配置" not in detail:
                errors.append(f"{name}: {detail}")
                logger.warning("  -> %s 失败: %s", name, detail)
        self._send(200, {"ok": True, "received": True, "forwarded": forwarded, "errors": errors})

    def log_message(self, fmt: str, *args) -> None:  # noqa: D401
        logger.debug("%s - %s", self.address_string(), fmt % args)


def main() -> None:
    logger.info("通知中转服务启动: http://%s:%d/tsp", HOST, PORT)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
