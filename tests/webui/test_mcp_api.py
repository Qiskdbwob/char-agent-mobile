"""Impostazioni MCP lato WebUI: payload, CRUD, test di connessione.

Si misura la parte che l'utente tocca con le dita, e in particolare le cose
che non devono cedere: i valori delle header non entrano mai in un payload,
ogni scrittura passa dal funnel della config, e la policy di rete si applica
al salvataggio (e di nuovo alla connessione).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jenny.config.loader import load_config, save_config
from jenny.config.schema import Config
from jenny.runtime.context import get_runtime_context
from jenny.webui import mcp_api
from jenny.webui.settings_api import WebUISettingsError

QueryParams = dict[str, list[str]]


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Workspace e config isolati per test (stesso schema di test_ssh_api)."""
    from jenny.config import paths as paths_mod

    previous = get_runtime_context().workspace_dir
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    paths_mod.set_workspace_dir(str(workspace))

    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr(get_runtime_context(), "config_path", config_path)

    # La policy di rete risolve davvero il DNS: qui non c'è niente da risolvere.
    monkeypatch.setattr(mcp_api, "validate_mcp_target", lambda url: (True, ""))
    mcp_api._LAST_TEST.clear()
    try:
        yield config_path
    finally:
        mcp_api._LAST_TEST.clear()
        paths_mod.set_workspace_dir(str(previous))


def _save_query(name: str = "gh", url: str = "https://mcp.github.com", **extra: Any) -> QueryParams:
    query: QueryParams = {"name": [name], "url": [url]}
    for key, value in extra.items():
        query[key] = [str(value)]
    return query


class TestPayload:
    def test_empty_by_default(self, env):
        payload = mcp_api.mcp_settings_payload()
        assert payload == {"servers": [], "requires_restart": False}

    def test_header_values_never_leave(self, env):
        payload = mcp_api.mcp_settings_payload(load_config())
        assert payload["servers"] == []
        save_config(
            Config.model_validate(
                {"tools": {"mcp": {"servers": [
                    {"name": "gh", "url": "https://mcp.github.com",
                     "headers": {"Authorization": "Bearer sekret", "X-A": "1"}},
                ]}}}
            ),
            env,
        )
        payload = mcp_api.mcp_settings_payload()
        server = payload["servers"][0]
        assert server["header_keys"] == ["Authorization", "X-A"]
        body = json.dumps(payload)
        assert "sekret" not in body
        assert "X-A" in body


class TestSaveMcpServer:
    async def test_creates_server(self, env):
        payload = await mcp_api.save_mcp_server(_save_query())
        assert payload["servers"][0]["name"] == "gh"
        assert payload["servers"][0]["url"] == "https://mcp.github.com"
        assert payload["requires_restart"] is True
        cfg = load_config()
        assert cfg.tools.mcp.servers[0].headers == {}

    async def test_upsert_updates_url_and_timeout(self, env):
        await mcp_api.save_mcp_server(_save_query())
        await mcp_api.save_mcp_server(
            _save_query(url="https://new.example/mcp", timeout=45, enabled="0")
        )
        cfg = load_config()
        assert len(cfg.tools.mcp.servers) == 1
        server = cfg.tools.mcp.servers[0]
        assert server.url == "https://new.example/mcp"
        assert server.timeout == 45
        assert server.enabled is False

    async def test_no_change_no_restart_flag(self, env):
        await mcp_api.save_mcp_server(_save_query())
        payload = await mcp_api.save_mcp_server(_save_query())
        assert payload["requires_restart"] is False

    async def test_headers_kept_blank_means_keep(self, env):
        await mcp_api.save_mcp_server(
            _save_query(headers=json.dumps([["Authorization", "Bearer sekret"]]))
        )
        # Modifica con valore vuoto: la header salvata resta.
        await mcp_api.save_mcp_server(
            _save_query(headers=json.dumps([["Authorization", ""]]))
        )
        cfg = load_config()
        assert cfg.tools.mcp.servers[0].headers == {"Authorization": "Bearer sekret"}

    async def test_headers_removed_row_is_deleted(self, env):
        await mcp_api.save_mcp_server(
            _save_query(headers=json.dumps([["Authorization", "Bearer sekret"]]))
        )
        await mcp_api.save_mcp_server(_save_query(headers=json.dumps([])))
        cfg = load_config()
        assert cfg.tools.mcp.servers[0].headers == {}

    async def test_bad_name_rejected(self, env):
        with pytest.raises(WebUISettingsError, match="name must be"):
            await mcp_api.save_mcp_server(_save_query(name="bad name!"))

    async def test_missing_url_rejected(self, env):
        with pytest.raises(WebUISettingsError, match="url is required"):
            await mcp_api.save_mcp_server({"name": ["gh"]})

    async def test_network_policy_applied_at_save(self, env, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(mcp_api, "validate_mcp_target", lambda url: (False, "blocked"))
        with pytest.raises(WebUISettingsError, match="network policy"):
            await mcp_api.save_mcp_server(_save_query())

    async def test_timeout_out_of_bounds_rejected(self, env):
        with pytest.raises(WebUISettingsError, match="timeout"):
            await mcp_api.save_mcp_server(_save_query(timeout=9999))

    async def test_invalid_headers_json_rejected(self, env):
        with pytest.raises(WebUISettingsError, match="headers"):
            await mcp_api.save_mcp_server(_save_query(headers="not json"))


class TestDeleteMcpServer:
    async def test_delete_removes_server(self, env):
        await mcp_api.save_mcp_server(_save_query())
        payload = await mcp_api.delete_mcp_server({"name": ["gh"]})
        assert payload["servers"] == []
        assert load_config().tools.mcp.servers == []

    async def test_delete_unknown_is_404(self, env):
        with pytest.raises(WebUISettingsError, match="unknown MCP server") as exc:
            await mcp_api.delete_mcp_server({"name": ["nope"]})
        assert exc.value.status == 404


class TestTestMcpServer:
    async def test_success_records_tool_count(self, env, monkeypatch: pytest.MonkeyPatch):
        await mcp_api.save_mcp_server(_save_query())

        class _FakeClient:
            def __init__(self, *a: Any, **kw: Any) -> None:
                self.closed = False

            async def initialize(self) -> None:
                pass

            async def list_tools(self) -> list[dict[str, Any]]:
                return [{"name": "a"}, {"name": "b"}]

            async def aclose(self) -> None:
                self.closed = True

        monkeypatch.setattr(mcp_api, "MCPClient", _FakeClient)
        payload = await mcp_api.test_mcp_server({"name": ["gh"]})
        server = payload["servers"][0]
        assert server["status"] == "ok"
        assert server["tools"] == 2
        assert mcp_api._LAST_TEST["gh"]["status"] == "ok"

    async def test_failure_records_error(self, env, monkeypatch: pytest.MonkeyPatch):
        await mcp_api.save_mcp_server(_save_query())

        from jenny.mcp.client import MCPConnectionError

        class _DeadClient:
            def __init__(self, *a: Any, **kw: Any) -> None:
                pass

            async def initialize(self) -> None:
                raise MCPConnectionError("connection refused")

            async def list_tools(self) -> list[dict[str, Any]]:
                raise AssertionError("must not be reached")

            async def aclose(self) -> None:
                pass

        monkeypatch.setattr(mcp_api, "MCPClient", _DeadClient)
        with pytest.raises(WebUISettingsError, match="connection refused"):
            await mcp_api.test_mcp_server({"name": ["gh"]})
        assert mcp_api._LAST_TEST["gh"]["status"] == "error"

    async def test_unknown_server_is_404(self, env):
        with pytest.raises(WebUISettingsError) as exc:
            await mcp_api.test_mcp_server({"name": ["nope"]})
        assert exc.value.status == 404
