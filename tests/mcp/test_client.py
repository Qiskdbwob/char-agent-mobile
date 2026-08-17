"""Test del client MCP Streamable HTTP: JSON-RPC, SSE, sessione, errori.

Si testa il protocollo senza rete: ``httpx.MockTransport`` risponde alle
richieste con corpi JSON o SSE preparati, e si verifica cosa il client invia
(header, payload, session id) e come interpreta le risposte.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from jenny.mcp import client as mcp_client
from jenny.mcp.client import (
    PROTOCOL_VERSION,
    MCPClient,
    MCPConnectionError,
    MCPToolCallError,
    discover_mcp_tools,
)


def _json_response(payload: dict[str, Any], headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(200, json=payload, headers=headers or {})


def _sse_response(
    events: list[tuple[str | None, str]],
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Costruisce una risposta SSE da una lista di (event, data)."""
    body = ""
    for event, data in events:
        if event:
            body += f"event: {event}\n"
        body += f"data: {data}\n\n"
    hdrs = {"Content-Type": "text/event-stream", **(headers or {})}
    return httpx.Response(200, text=body, headers=hdrs)


def _client_with(handler: Any) -> MCPClient:
    return MCPClient(
        "https://mcp.example.com/mcp",
        headers={"Authorization": "Bearer sekret"},
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


class TestSseParsing:
    def test_message_with_event_and_data(self):
        lines = ["event: message", 'data: {"jsonrpc":"2.0","id":1}', "", ""]
        messages = list(mcp_client._parse_sse(lines))
        assert messages == [{"jsonrpc": "2.0", "id": 1}]

    def test_multiline_data_concatenated(self):
        lines = ['data: {"a":', "data: 1}", "", ""]
        messages = list(mcp_client._parse_sse(lines))
        assert messages == [{"a": 1}]

    def test_ping_is_ignored_by_response_matcher(self):
        # Il parser conserva `{}` (ping): a filtrarlo è _find_response, che
        # cerca il messaggio con l'id della richiesta.
        lines = ['data: {}', "", "", 'data: {"id":1}', ""]
        messages = list(mcp_client._parse_sse(lines))
        assert messages == [{}, {"id": 1}]
        assert mcp_client._find_response(messages, 1) == {"id": 1}

    def test_error_event_raises(self):
        lines = ["event: error", 'data: {"error":"boom"}', "", ""]
        with pytest.raises(MCPConnectionError, match="boom"):
            list(mcp_client._parse_sse(lines))

    def test_invalid_json_raises(self):
        with pytest.raises(MCPConnectionError, match="invalid SSE"):
            list(mcp_client._parse_sse(["data: not-json", ""]))

    def test_no_trailing_blank_line(self):
        messages = list(mcp_client._parse_sse(['data: {"id":1}']))
        assert messages == [{"id": 1}]


class TestMCPClient:
    async def test_initialize_json_response_and_session_id(self):
        """La sessione torna come header e viene riusata nella notifica."""
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/mcp":
                pass
            body = json.loads(request.content)
            if body.get("method") == "initialize":
                return _json_response(
                    {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}},
                    },
                    headers={"mcp-session-id": "sess-123"},
                )
            if body.get("method") == "tools/list":
                assert request.headers.get("mcp-session-id") == "sess-123"
                return _json_response(
                    {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {
                            "tools": [
                                {"name": "create_issue", "description": "Create an issue",
                                 "inputSchema": {"type": "object", "properties": {}}},
                            ]
                        },
                    }
                )
            # notifications/initialized: niente risposta (200 vuoto)
            assert body.get("method") == "notifications/initialized"
            assert "id" not in body
            return _json_response({})

        client = _client_with(handler)
        result = await client.initialize()
        assert result["protocolVersion"] == PROTOCOL_VERSION
        tools = await client.list_tools()
        assert tools[0]["name"] == "create_issue"
        assert client.initialized

        methods = [json.loads(r.content).get("method") for r in requests]
        assert methods == ["initialize", "notifications/initialized", "tools/list"]

    async def test_initialize_over_sse(self):
        """Il server che risponde in SSE va letto fino al messaggio giusto."""
        requests: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            requests.append(body)
            if body.get("method") == "initialize":
                return _sse_response(
                    [
                        ("message", json.dumps({
                            "jsonrpc": "2.0", "id": body["id"],
                            "result": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}},
                        })),
                    ]
                )
            return _json_response({})  # notification

        client = _client_with(handler)
        result = await client.initialize()
        assert result["protocolVersion"] == PROTOCOL_VERSION

    async def test_call_tool_with_progress_notification_first(self):
        """Una notification di progress davanti alla risposta non confonde."""
        calls: dict[str, int] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if body.get("method") == "initialize":
                return _json_response(
                    {"jsonrpc": "2.0", "id": body["id"],
                     "result": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}}}
                )
            if body.get("method") == "tools/call":
                calls["tools/call"] = calls.get("tools/call", 0) + 1
                return _sse_response(
                    [
                        ("message", json.dumps(
                            {"jsonrpc": "2.0", "method": "notifications/progress",
                             "params": {"progress": 0.5}}
                        )),
                        ("message", json.dumps(
                            {"jsonrpc": "2.0", "id": body["id"],
                             "result": {"content": [{"type": "text", "text": "done"}]}}
                        )),
                    ]
                )
            return _json_response({})

        client = _client_with(handler)
        result = await client.call_tool("run", {"x": 1})
        assert result["content"][0]["text"] == "done"
        assert calls["tools/call"] == 1

    async def test_jsonrpc_error_raises_tool_call_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if body.get("method") == "initialize":
                return _json_response(
                    {"jsonrpc": "2.0", "id": body["id"],
                     "result": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}}}
                )
            return _json_response(
                {"jsonrpc": "2.0", "id": body["id"],
                 "error": {"code": -32602, "message": "invalid params"}}
            )

        client = _client_with(handler)
        with pytest.raises(MCPToolCallError, match="invalid params"):
            await client.call_tool("run", {})

    async def test_http_error_raises_connection_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        client = _client_with(handler)
        with pytest.raises(MCPConnectionError, match="500"):
            await client.initialize()

    async def test_mismatched_id_raises_connection_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            return _json_response(
                {"jsonrpc": "2.0", "id": body["id"] + 1000,
                 "result": {"protocolVersion": PROTOCOL_VERSION}}
            )

        client = _client_with(handler)
        with pytest.raises(MCPConnectionError, match="mismatched id"):
            await client.initialize()

    async def test_authorization_header_is_sent(self):
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["authorization"] = request.headers.get("authorization", "")
            body = json.loads(request.content)
            return _json_response(
                {"jsonrpc": "2.0", "id": body["id"],
                 "result": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}}}
            )

        client = _client_with(handler)
        await client.initialize()
        assert captured["authorization"] == "Bearer sekret"


class TestDiscoverMcpTools:
    """Discovery sincrona: stessa grammatica, trasporto bloccante."""

    def test_discovers_tools_and_initializes(self, monkeypatch: pytest.MonkeyPatch):
        # La classe vera va catturata PRIMA del patch: `mcp_client.httpx` è il
        # modulo httpx stesso, quindi patchare `Client` lì dentro cambia anche
        # `httpx.Client` usato dentro la fake stessa (e nelle import interne).
        real_client = httpx.Client
        requests: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            requests.append(body)
            if body.get("method") == "initialize":
                return _json_response(
                    {"jsonrpc": "2.0", "id": body["id"],
                     "result": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}}},
                    headers={"mcp-session-id": "sess-sync"},
                )
            if body.get("method") == "tools/list":
                assert request.headers.get("mcp-session-id") == "sess-sync"
                return _json_response(
                    {"jsonrpc": "2.0", "id": body["id"],
                     "result": {"tools": [{"name": "a", "inputSchema": {}}]}}
                )
            return _json_response({})

        def fake_client(**kwargs: Any) -> httpx.Client:
            # httpx costruisce un DEFAULT_CLIENT di modulo (passando transport)
            # al primo uso: quel client non serve a nulla qui, ma la firma deve
            # accettare il kwarg senza collisioni.
            kwargs.pop("transport", None)
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(mcp_client.httpx, "Client", fake_client)
        tools = discover_mcp_tools("https://mcp.example.com/mcp", timeout=10)
        assert [t["name"] for t in tools] == ["a"]
        methods = [r.get("method") for r in requests]
        assert methods == ["initialize", "notifications/initialized", "tools/list"]

    def test_sse_response_during_discovery(self, monkeypatch: pytest.MonkeyPatch):
        real_client = httpx.Client

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if body.get("method") == "initialize":
                return _sse_response(
                    [("message", json.dumps(
                        {"jsonrpc": "2.0", "id": body["id"],
                         "result": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}}}
                    ))]
                )
            if body.get("method") == "tools/list":
                return _sse_response(
                    [("message", json.dumps(
                        {"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}}
                    ))]
                )
            return _json_response({})

        def fake_client(**kwargs: Any) -> httpx.Client:
            kwargs.pop("transport", None)
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(mcp_client.httpx, "Client", fake_client)
        assert discover_mcp_tools("https://mcp.example.com/mcp", timeout=10) == []

    def test_network_error_propagates_as_connection_error(self, monkeypatch: pytest.MonkeyPatch):
        real_client = httpx.Client

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        def fake_client(**kwargs: Any) -> httpx.Client:
            kwargs.pop("transport", None)
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(mcp_client.httpx, "Client", fake_client)
        with pytest.raises(MCPConnectionError):
            discover_mcp_tools("https://mcp.example.com/mcp", timeout=10)
