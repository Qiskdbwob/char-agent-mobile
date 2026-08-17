"""Minimal MCP (Model Context Protocol) client — tools-only, Streamable HTTP.

Nessuna dipendenza esterna oltre a ``httpx`` (già nel bundel APK): il protocollo
MCP è JSON-RPC 2.0 su POST, con risposte JSON o SSE. L'SDK ufficiale ``mcp``
pretende pydantic v2 + componenti Rust (``pydantic_core``), che su Chaquopy non
sono garantiti; questo client li evita del tutto.
"""

from __future__ import annotations

from jenny.mcp.client import (
    PROTOCOL_VERSION,
    MCPClient,
    MCPConnectionError,
    MCPError,
    MCPToolCallError,
)
from jenny.mcp.manager import MCPTool, reset_mcp_state, sync_mcp_tools

__all__ = [
    "MCPClient",
    "MCPConnectionError",
    "MCPError",
    "MCPTool",
    "MCPToolCallError",
    "PROTOCOL_VERSION",
    "reset_mcp_state",
    "sync_mcp_tools",
]
