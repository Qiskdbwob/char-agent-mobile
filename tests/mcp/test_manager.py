"""Test del manager MCP: discovery, nomi dei tool, schema, invocazione, reset.

La discovery non parla con la rete: ``discover_mcp_tools`` e la policy di rete
vengono sostituite, e si verifica il comportamento del manager (salti, nomi,
dedupe). L'invocazione asincrona sostituisce ``MCPClient`` con un finto che
registra le chiamate.
"""

from __future__ import annotations

from typing import Any

import pytest

from jenny.agent.tools.base import Tool
from jenny.config.tool_schemas import MCPConfig, MCPServerConfig
from jenny.mcp import manager
from jenny.mcp.client import MCPConnectionError
from jenny.mcp.manager import call_mcp_tool, reset_mcp_state, sync_mcp_tools

TOOL_A = {"name": "create_issue", "description": "Create an issue",
          "inputSchema": {"$schema": "http://json-schema.org/draft-07/schema#",
                          "type": "object", "properties": {"title": {"type": "string"}},
                          "required": ["title"]}}
TOOL_B = {"name": "list-issues", "description": "", "inputSchema": {}}


def _config(*servers: dict[str, Any]) -> MCPConfig:
    return MCPConfig.model_validate({"servers": list(servers)})


DEFAULT_TOOLS = [TOOL_A, TOOL_B]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    reset_mcp_state()
    monkeypatch.setattr(manager, "validate_mcp_target", lambda url: (True, ""))
    # Discovery finta di default: i test che vogliono un server rotto la
    # risostituiscono da soli.
    monkeypatch.setattr(manager, "discover_mcp_tools", _fake_discover(DEFAULT_TOOLS))
    yield
    reset_mcp_state()


def _fake_discover(tools: list[dict[str, Any]]):
    def _discover(url: str, **kwargs: Any) -> list[dict[str, Any]]:
        return tools

    return _discover


class TestSyncMcpTools:
    def test_disabled_server_is_skipped(self, monkeypatch: pytest.MonkeyPatch):
        cfg = _config({"name": "off", "url": "https://x", "enabled": False})
        assert sync_mcp_tools(cfg) == []

    def test_unreachable_server_is_skipped_without_failing(self, monkeypatch: pytest.MonkeyPatch):
        def _boom(url: str, **kwargs: Any) -> list[dict[str, Any]]:
            if "dead" in url:
                raise MCPConnectionError("connection refused")
            return DEFAULT_TOOLS

        monkeypatch.setattr(manager, "discover_mcp_tools", _boom)
        cfg = _config(
            {"name": "dead", "url": "https://dead.example"},
            {"name": "alive", "url": "https://alive.example"},
        )
        tools = sync_mcp_tools(cfg)
        assert [t.name for t in tools] == ["mcp__alive__create_issue", "mcp__alive__list-issues"]

    def test_network_policy_refusal_skips_server(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(manager, "validate_mcp_target", lambda url: (False, "blocked"))
        cfg = _config({"name": "bad", "url": "http://127.0.0.1:9999"})
        assert sync_mcp_tools(cfg) == []

    def test_tool_names_and_schema(self):
        cfg = _config({"name": "gh", "url": "https://mcp.github.com", "headers": {"X-A": "1"}})
        tools = sync_mcp_tools(cfg)
        assert len(tools) == 2
        tool = tools[0]
        assert isinstance(tool, Tool)
        assert tool.name == "mcp__gh__create_issue"
        assert tool.description == "Create an issue"
        # $schema tolto, type garantito, proprietà conservate.
        assert tool.parameters == {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        }
        assert not tool.read_only
        assert "core" in tool._scopes

    def test_names_keep_safe_characters(self):
        # "-" è valido nei nomi di funzione (OpenAI) e resta intatto;
        # il sanitizer tocca solo i caratteri che non lo sono.
        cfg = _config({"name": "srv", "url": "https://x"})
        tools = sync_mcp_tools(cfg)
        names = sorted(t.name for t in tools)
        assert names == ["mcp__srv__create_issue", "mcp__srv__list-issues"]

    def test_sanitize_collision_keeps_first(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            manager, "discover_mcp_tools",
            _fake_discover([
                {"name": "a b", "inputSchema": {}},
                {"name": "a/b", "inputSchema": {}},
                {"name": "a_b", "inputSchema": {}},
            ]),
        )
        cfg = _config({"name": "srv", "url": "https://x"})
        tools = sync_mcp_tools(cfg)
        # "a b", "a/b" e "a_b" collassano tutti su "a_b" dopo la sanifica
        # (lo spazio e la slash non sono caratteri validi): resta il primo.
        assert [t._tool_name for t in tools] == ["a_b"]

    def test_empty_description_falls_back(self):
        cfg = _config({"name": "srv", "url": "https://x"})
        tool = next(t for t in sync_mcp_tools(cfg) if t._tool_name == "list-issues")
        assert tool.description == "MCP tool list-issues from server srv"


class TestCallMcpTool:
    async def test_happy_path_formats_text(self, monkeypatch: pytest.MonkeyPatch):
        calls: list[tuple[str, dict[str, Any]]] = []

        class _FakeClient:
            def __init__(self, *a: Any, **kw: Any) -> None:
                self._initialized = False

            async def initialize(self) -> None:
                self._initialized = True

            async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                calls.append((name, arguments))
                return {"content": [{"type": "text", "text": "ok: " + str(arguments)}]}

            async def aclose(self) -> None:
                pass

        monkeypatch.setattr(manager, "MCPClient", _FakeClient)
        result = await call_mcp_tool(
            "srv", "https://x", {}, 30, "create_issue", {"title": "t"}
        )
        assert result == "ok: {'title': 't'}"
        assert calls == [("create_issue", {"title": "t"})]
        # Il client resta in cache per la sessione.
        assert len(manager._CLIENTS) == 1

    async def test_connection_error_reconnects_once(self, monkeypatch: pytest.MonkeyPatch):
        attempts: list[int] = []

        class _FlakyClient:
            def __init__(self, *a: Any, **kw: Any) -> None:
                attempts.append(1)

            async def initialize(self) -> None:
                pass

            async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                # Solo la prima istanza (primo tentativo) fallisce.
                if len(attempts) == 1:
                    raise MCPConnectionError("session lost")
                return {"content": [{"type": "text", "text": "recovered"}]}

            async def aclose(self) -> None:
                pass

        monkeypatch.setattr(manager, "MCPClient", _FlakyClient)
        result = await call_mcp_tool("srv", "https://x", {}, 30, "t", {})
        assert result == "recovered"
        assert len(attempts) == 2  # primo client scartato, secondo creato

    async def test_tool_error_becomes_message(self, monkeypatch: pytest.MonkeyPatch):
        from jenny.mcp.client import MCPToolCallError

        class _ErrorClient:
            def __init__(self, *a: Any, **kw: Any) -> None:
                pass

            async def initialize(self) -> None:
                pass

            async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                raise MCPToolCallError("tool exploded")

            async def aclose(self) -> None:
                pass

        monkeypatch.setattr(manager, "MCPClient", _ErrorClient)
        result = await call_mcp_tool("srv", "https://x", {}, 30, "t", {})
        assert "tool exploded" in result

    async def test_connect_failure_returns_message(self, monkeypatch: pytest.MonkeyPatch):
        class _DeadClient:
            def __init__(self, *a: Any, **kw: Any) -> None:
                pass

            async def initialize(self) -> None:
                raise MCPConnectionError("cannot connect")

            async def aclose(self) -> None:
                pass

        monkeypatch.setattr(manager, "MCPClient", _DeadClient)
        result = await call_mcp_tool("srv", "https://x", {}, 30, "t", {})
        assert "cannot connect" in result
        assert manager._CLIENTS == {}

    async def test_is_error_result_is_marked(self, monkeypatch: pytest.MonkeyPatch):
        class _ErrResultClient:
            def __init__(self, *a: Any, **kw: Any) -> None:
                pass

            async def initialize(self) -> None:
                pass

            async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                return {"content": [{"type": "text", "text": "nope"}], "isError": True}

            async def aclose(self) -> None:
                pass

        monkeypatch.setattr(manager, "MCPClient", _ErrResultClient)
        result = await call_mcp_tool("srv", "https://x", {}, 30, "t", {})
        assert "returned an error" in result
        assert "nope" in result


class TestReset:
    def test_reset_clears_cache(self, monkeypatch: pytest.MonkeyPatch):
        manager._CLIENTS["srv"] = object()  # type: ignore[assignment]
        manager._SERVER_CONFIGS["srv"] = MCPServerConfig(name="srv", url="https://x")
        reset_mcp_state()
        assert manager._CLIENTS == {}
        assert manager._SERVER_CONFIGS == {}
