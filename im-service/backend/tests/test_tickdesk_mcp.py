import json

import httpx
import pytest


@pytest.mark.parametrize("failure", [None, "auth", "tool", "business", "oversized"])
async def test_mcp_availability_requires_a_real_successful_tool_call(tmp_path, failure):
    from app.services.tickdesk_mcp import FuyaoMcp

    key = tmp_path / "key"
    key.write_text("private-test-key")
    methods = []

    def reply(request):
        assert request.url == "https://fuyao.aicubes.cn/mcp/a-share"
        assert request.headers["X-api-key"] == "private-test-key"
        body = json.loads(request.content)
        method = body["method"]
        methods.append(method)
        if failure == "auth":
            return httpx.Response(401, text="private-test-key")
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fuyao-a-share-mcp", "version": "1"}}
        elif method == "tools/list":
            assert request.headers["Mcp-Session-Id"] == "test-session"
            result = {"tools": [{"name": "get_a_share_calendar_trading_days"}]}
        else:
            assert body["params"] == {"name": "get_a_share_calendar_trading_days", "arguments": {}}
            if failure == "oversized":
                return httpx.Response(200, content=b" " * (1024 * 1024 + 1))
            result = {"isError": failure == "tool", "content": [{"type": "text", "text": json.dumps({
                "code": 1 if failure == "business" else 0,
                "data": {"item": [{"date": "20261116", "date_ms": 1794758400000}]},
            })}]}
        return httpx.Response(200, headers={"Mcp-Session-Id": "test-session"},
                              json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    service = FuyaoMcp(key, transport=httpx.MockTransport(reply))
    await service.probe()
    status = service.status()
    assert status["status"] == ("online" if failure is None else "offline")
    assert status["checkedAt"] > 0
    assert "private-test-key" not in json.dumps(status)
    if failure is None:
        assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
        assert status["errorCode"] is None
    else:
        assert status["errorCode"].startswith("MCP_")


async def test_mcp_missing_key_and_arbitrary_tools_do_not_send_http(tmp_path):
    from app.services.tickdesk_mcp import FuyaoMcp, McpError

    def forbidden(request):
        pytest.fail("must not send HTTP")

    service = FuyaoMcp(tmp_path / "missing", transport=httpx.MockTransport(forbidden))
    await service.probe()
    assert service.status()["errorCode"] == "MCP_NOT_CONFIGURED"
    with pytest.raises(McpError, match="MCP_TOOL_NOT_ALLOWED"):
        await service.call("place_order", {})


def test_assistant_status_includes_mcp_probe_without_exposing_credentials(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.tickdesk import router

    token = tmp_path / "token"
    token.write_text("a" * 43)
    monkeypatch.setenv("TICKDESK_AI_TOKEN_FILE", str(token))
    app = FastAPI()
    app.include_router(router)
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        assert client.get("/api/tickdesk/ai/v1/status").status_code == 401
        result = client.get("/api/tickdesk/ai/v1/status", headers={"Authorization": "Bearer " + "a" * 43})
        assert result.json()["mcp"] == {"status": "unknown", "checkedAt": None, "errorCode": "MCP_NOT_CONFIGURED"}
