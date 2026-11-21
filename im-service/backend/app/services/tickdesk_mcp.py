"""Bounded, read-only Fuyao MCP transport and an actual tool-call health probe.

Health does not certify market-data freshness, strategy approval, or AI fact coverage.
The probe runs once per five minutes; it never invokes a language model.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

import httpx

ENDPOINT = "https://fuyao.aicubes.cn/mcp/a-share"
PROTOCOL = "2024-11-05"
CALENDAR = "get_a_share_calendar_trading_days"
SNAPSHOT = "get_a_share_prices_snapshot"
BODY_LIMIT = 1024 * 1024
UNCONFIGURED = {"status": "unknown", "checkedAt": None, "errorCode": "MCP_NOT_CONFIGURED"}


class McpError(Exception):
    """Only stable codes, never vendor text, headers, credentials or URLs."""


class FuyaoMcp:
    def __init__(self, key_file: Path, *, transport=None):
        self.key_file = key_file
        self.transport = transport
        self._health = dict(UNCONFIGURED)
        self._lock = asyncio.Lock()

    def status(self):
        return dict(self._health)

    async def _rpc(self, client, method, params, request_id=None):
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            body["id"] = request_id
        async with client.stream("POST", ENDPOINT, json=body) as response:
            if response.status_code in (401, 403):
                raise McpError("MCP_AUTH_REQUIRED")
            if response.status_code not in (200, 202, 204):
                raise McpError("MCP_UNAVAILABLE")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                if len(raw) + len(chunk) > BODY_LIMIT:
                    raise McpError("MCP_RESPONSE_TOO_LARGE")
                raw.extend(chunk)
            if request_id is None:
                return None
            message = json.loads(raw)
            if (not isinstance(message, dict) or message.get("id") != request_id
                    or "error" in message or not isinstance(message.get("result"), dict)):
                raise McpError("MCP_INVALID_RESPONSE")
            if method == "initialize":
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    if len(session) > 512 or not session.isascii() or any(ord(c) < 33 for c in session):
                        raise McpError("MCP_INVALID_RESPONSE")
                    client.headers["Mcp-Session-Id"] = session
            return message["result"]

    async def call(self, tool, arguments):
        if (tool == CALENDAR and arguments == {}) or (tool == SNAPSHOT and isinstance(arguments, dict)
              and set(arguments) == {"thscodes", "limit"}
              and isinstance(arguments["thscodes"], str)
              and re.fullmatch(r"[0-9]{6}\.(SH|SZ)(,[0-9]{6}\.(SH|SZ)){0,63}", arguments["thscodes"])
              and type(arguments["limit"]) is int and 1 <= arguments["limit"] <= 64):
            pass
        else:
            raise McpError("MCP_TOOL_NOT_ALLOWED")
        try:
            key = self.key_file.read_text(encoding="utf-8").strip()
            if not key or len(key) > 4096 or not key.isascii() or any(ord(c) < 33 for c in key):
                raise ValueError
        except (OSError, ValueError):
            raise McpError("MCP_NOT_CONFIGURED") from None
        async with self._lock:
            try:
                async with asyncio.timeout(20), httpx.AsyncClient(
                    timeout=10, trust_env=False, follow_redirects=False, transport=self.transport,
                    headers={"X-api-key": key, "Accept": "application/json, text/event-stream"},
                ) as client:
                    initialized = await self._rpc(client, "initialize", {
                        "protocolVersion": PROTOCOL, "capabilities": {},
                        "clientInfo": {"name": "tickdesk", "version": "0.1.0"},
                    }, 1)
                    if initialized.get("protocolVersion") != PROTOCOL:
                        raise McpError("MCP_PROTOCOL_UNSUPPORTED")
                    client.headers["MCP-Protocol-Version"] = PROTOCOL
                    await self._rpc(client, "notifications/initialized", {})
                    catalog = await self._rpc(client, "tools/list", {}, 2)
                    tools = catalog.get("tools")
                    if not isinstance(tools, list) or not any(isinstance(t, dict) and t.get("name") == tool for t in tools):
                        raise McpError("MCP_TOOL_UNAVAILABLE")
                    result = await self._rpc(client, "tools/call", {"name": tool, "arguments": arguments}, 3)
                    if result.get("isError", False) is not False:
                        raise McpError("MCP_TOOL_FAILED")
                    content = result.get("content")
                    if not isinstance(content, list) or len(content) != 1 or content[0].get("type") != "text":
                        raise McpError("MCP_INVALID_RESPONSE")
                    payload = json.loads(content[0]["text"])
                    if (not isinstance(payload, dict) or type(payload.get("code")) is not int
                            or payload["code"] != 0 or not isinstance(payload.get("data"), dict)):
                        raise McpError("MCP_TOOL_FAILED")
                    return payload["data"]
            except (TimeoutError, httpx.TimeoutException):
                raise McpError("MCP_TIMEOUT") from None
            except httpx.HTTPError:
                raise McpError("MCP_UNAVAILABLE") from None
            except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
                raise McpError("MCP_INVALID_RESPONSE") from None

    async def probe(self):
        code = None
        try:
            data = await self.call(CALENDAR, {})
            rows = data.get("item")
            if (not isinstance(rows, list) or not rows or not all(
                    isinstance(row, dict) and re.fullmatch(r"[0-9]{8}", str(row.get("date", "")))
                    and type(row.get("date_ms")) is int for row in rows)):
                raise McpError("MCP_INVALID_RESPONSE")
        except McpError as error:
            code = str(error)
        self._health = {"status": "online" if code is None else "offline",
                        "checkedAt": time.time_ns() // 1_000_000, "errorCode": code}

    async def run(self):
        while True:
            await self.probe()
            await asyncio.sleep(300)
