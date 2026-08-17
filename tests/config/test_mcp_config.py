"""Validatori della config MCP: un config scritto a mano non deve far fallire l'avvio.

Le route di settings validano già con 400; qui si copre la strada che la route
non vede — una ``config.json`` modificata a mano — con lo stesso pattern degli
altri sub-config tool (v. ``test_ssh_config.py``).
"""

from __future__ import annotations

from jenny.config.tool_schemas import (
    MCPConfig,
    MCPServerConfig,
)


def _cfg(servers: list) -> MCPConfig:
    return MCPConfig.model_validate({"servers": servers})


class TestDropInvalidServers:
    def test_valid_servers_kept(self):
        cfg = _cfg([{"name": "gh", "url": "https://mcp.github.com"}])
        assert len(cfg.servers) == 1
        assert cfg.servers[0].name == "gh"

    def test_bad_name_dropped_with_warning(self):
        cfg = _cfg([
            {"name": "bad name!", "url": "https://x"},
            {"name": "ok", "url": "https://y"},
        ])
        assert [s.name for s in cfg.servers] == ["ok"]

    def test_non_http_url_dropped(self):
        cfg = _cfg([
            {"name": "ftp", "url": "ftp://x"},
            {"name": "ok", "url": "https://y"},
        ])
        assert [s.name for s in cfg.servers] == ["ok"]

    def test_non_dict_entry_dropped(self):
        cfg = _cfg(["nope", {"name": "ok", "url": "https://y"}])
        assert [s.name for s in cfg.servers] == ["ok"]

    def test_missing_servers_key_defaults_to_empty(self):
        assert MCPConfig().servers == []


class TestServerCoercion:
    def test_headers_coerced_to_str_dict(self):
        server = MCPServerConfig.model_validate(
            {"name": "s", "url": "https://x", "headers": {"X-A": 1}}
        )
        assert server.headers == {"X-A": "1"}

    def test_non_dict_headers_fall_back_to_empty(self):
        server = MCPServerConfig.model_validate(
            {"name": "s", "url": "https://x", "headers": "nope"}
        )
        assert server.headers == {}

    def test_timeout_out_of_range_is_clamped(self):
        server = MCPServerConfig.model_validate(
            {"name": "s", "url": "https://x", "timeout": 999}
        )
        assert server.timeout == 600

    def test_timeout_below_min_is_clamped(self):
        server = MCPServerConfig.model_validate(
            {"name": "s", "url": "https://x", "timeout": 0}
        )
        assert server.timeout == 1

    def test_non_numeric_timeout_falls_back_to_default(self):
        server = MCPServerConfig.model_validate(
            {"name": "s", "url": "https://x", "timeout": "abc"}
        )
        assert server.timeout == 30

    def test_enabled_coerced_from_int(self):
        server = MCPServerConfig.model_validate(
            {"name": "s", "url": "https://x", "enabled": 0}
        )
        assert server.enabled is False

    def test_defaults(self):
        server = MCPServerConfig.model_validate({"name": "s", "url": "https://x"})
        assert server.enabled is True
        assert server.timeout == 30
        assert server.headers == {}
