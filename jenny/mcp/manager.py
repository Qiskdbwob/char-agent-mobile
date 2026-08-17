"""Gestione dei tool MCP per l'agente: registrazione, invocazione, reset.

Questo modulo fa da ponte fra la config (``config.tools.mcp.servers``, dichiarata
a mano dall'utente in Settings → Tools) e il registry dei tool dell'agente.

Due momenti, due strategie:

* **Registrazione** (:func:`sync_mcp_tools`) — sincrona, chiamata dall'init
  dell'``AgentLoop`` come ``AppToolsSyncer``. Per ogni server abilitato fa la
  discovery (``initialize`` + ``tools/list``) con un ``httpx.Client`` bloccante
  a timeout corti: un server irraggiungibile viene saltato con un warning,
  mai un errore che butta giù il gateway. I nomi dei tool esposti al modello
  sono ``mcp__<server>__<tool>``: il server è sempre il primo componente, così
  il modello non può indovinare un endpoint, può solo nominare un server già
  dichiarato.
* **Esecuzione** (:func:`call_mcp_tool`) — asincrona, dentro il turno. Il
  client per server è creato pigramente al primo uso e tenuto in cache
  (sessione ``Mcp-Session-Id`` riusata); se la connessione muore, si chiude e
  si riparte da un handshake nuovo. La cache è una globale di modulo legata
  all'event loop, quindi :func:`reset_mcp_state` va chiamato a ogni riavvio
  del gateway (vedi ``android_entry.run_gateway``) — stessa regola dei lock di
  ``power``/``android_web``/``notifier``.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from loguru import logger

from jenny.agent.tools.base import Tool
from jenny.config.tool_schemas import MCPServerConfig
from jenny.mcp.client import (
    MCPClient,
    MCPConnectionError,
    MCPError,
    MCPToolCallError,
    discover_mcp_tools,
)
from jenny.security.network import validate_mcp_target

# Un nome di tool MCP può contenere quasi qualunque cosa; un nome di funzione
# per il modello no (lettere, cifre, '-', '_'). Si sanifica in modo
# deterministico; se due tool collassano sullo stesso nome dopo la sanifica,
# si tiene il primo e si salta il resto con un warning.
_TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")

# Config dei server registrati all'ultimo sync: nome -> config. Serve a
# ricreare il client asincrono al primo uso (lazy) e dopo un errore di rete.
_SERVER_CONFIGS: dict[str, MCPServerConfig] = {}

# Client asincroni vivi, per nome di server. Legati all'event loop corrente:
# ``reset_mcp_state`` li scarta tutti (vedi modulo docstring).
_CLIENTS: dict[str, MCPClient] = {}


class MCPTool(Tool):
    """Un tool di un server MCP, esposto al modello come ``mcp__<server>__<tool>``.

    L'istanza porta tutto ciò che serve a invocarlo: nome server e tool
    (per la chiamata) e lo schema JSON dei parametri (per il modello). La
    connessione vera è delegata a :func:`call_mcp_tool`, che la condivide per
    server.
    """

    _scopes = {"core", "subagent"}

    def __init__(
        self,
        *,
        server_name: str,
        server_url: str,
        headers: dict[str, str],
        timeout: int,
        tool_name: str,
        description: str,
        input_schema: dict[str, Any],
    ) -> None:
        self._server_name = server_name
        self._server_url = server_url
        self._headers = dict(headers)
        self._timeout = timeout
        self._tool_name = tool_name
        self._description = description
        self._input_schema = _normalize_input_schema(input_schema)

    @property
    def name(self) -> str:
        return f"mcp__{self._server_name}__{self._tool_name}"

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return deepcopy(self._input_schema)

    async def execute(self, **kwargs: Any) -> str:
        return await call_mcp_tool(
            self._server_name,
            self._server_url,
            self._headers,
            self._timeout,
            self._tool_name,
            kwargs,
        )


def _normalize_input_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Schema JSON di un tool MCP pronto per ``Tool.parameters``.

    Toglie ``$schema`` (sintassi JSON Schema, non usata dal validator) e
    garantisce ``type: object``: il validator dei tool assume un oggetto di
    parametri, e un server che dichiara uno schema senza type non deve far
    fallire l'invocazione con un errore criptico.
    """
    normalized = {k: v for k, v in (schema or {}).items() if k != "$schema"}
    if normalized.get("type") is None:
        normalized["type"] = "object"
    return normalized


def _sanitize_tool_name(name: str | None) -> str:
    return _TOOL_NAME_RE.sub("_", name or "tool").strip("_") or "tool"


def sync_mcp_tools(mcp_config: Any) -> list[MCPTool]:
    """Crea i tool dei server MCP abilitati (discovery sincrona).

    * un server disabilitato viene ignorato;
    * un server che non passa la policy di rete o che non risponde alla
      discovery viene saltato con un warning — stessa filosofia del loader:
      ``enabled()``/``create()`` che falliscono escludono quel solo tool;
    * i tool il cui nome sanitizzato colliderebbe vengono deduplicati.

    La discovery è l'unica parte che parla con la rete in fase di
    registrazione: il budget è bloccato a pochi secondi per server
    (``discover_mcp_tools``), quindi l'avvio del gateway non può essere tenuto
    in ostaggio da un endpoint morto.
    """
    if mcp_config is None:
        return []
    servers = getattr(mcp_config, "servers", None) or []
    tools: list[MCPTool] = []
    _SERVER_CONFIGS.clear()
    for server in servers:
        if not getattr(server, "enabled", True):
            logger.debug("MCP server {!r} disabled, skipping", server.name)
            continue
        _SERVER_CONFIGS[server.name] = server
        try:
            ok, error = validate_mcp_target(server.url)
            if not ok:
                logger.warning(
                    "MCP server {!r} refused by the network policy: {}", server.name, error
                )
                continue
            discovered = discover_mcp_tools(
                server.url, headers=server.headers, timeout=server.timeout
            )
        except MCPError as exc:
            logger.warning("MCP server {!r} unreachable, tools skipped: {}", server.name, exc)
            continue
        except Exception as exc:  # noqa: BLE001 — un server non deve far cadere tutto
            logger.warning("MCP server {!r} discovery failed: {}", server.name, exc)
            continue

        seen: set[str] = set()
        for tool in discovered:
            tool_name = _sanitize_tool_name(tool.get("name"))
            if tool_name in seen:
                logger.warning(
                    "MCP server {!r}: tool name {!r} collides after sanitization, skipped",
                    server.name, tool.get("name"),
                )
                continue
            seen.add(tool_name)
            description = (
                str(tool.get("description") or "").strip()
                or f"MCP tool {tool.get('name')} from server {server.name}"
            )
            tools.append(
                MCPTool(
                    server_name=server.name,
                    server_url=server.url,
                    headers=server.headers,
                    timeout=server.timeout,
                    tool_name=tool_name,
                    description=description,
                    input_schema=tool.get("inputSchema") or {},
                )
            )
    if tools:
        logger.info(
            "Registered {} MCP tools from {} servers",
            len(tools),
            len({t._server_name for t in tools}),
        )
    return tools


async def call_mcp_tool(
    server_name: str,
    server_url: str,
    headers: dict[str, str],
    timeout: int,
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    """Invoca un tool MCP, creando il client asincrono al primo uso.

    Ritorna sempre una stringa da consegnare al modello: gli errori di rete e
    di protocollo diventano messaggi di errore, non eccezioni — un tool che
    fallisce non deve far fallire il turno.
    """
    client = _CLIENTS.get(server_name)
    if client is None:
        client = MCPClient(server_url, headers=headers, timeout=timeout)
        _CLIENTS[server_name] = client
        try:
            await client.initialize()
        except MCPError as exc:
            await _drop_client(server_name, client)
            return (
                f"Error: cannot connect to MCP server {server_name!r}: {exc}\n"
                "Check the server URL and headers in Settings > Tools > MCP servers."
            )
    try:
        result = await client.call_tool(tool_name, arguments)
    except MCPToolCallError as exc:
        return f"Error: MCP tool {tool_name!r} failed: {exc}"
    except MCPConnectionError:
        # Connessione/sessione persa: si chiude e si riprova una volta da zero.
        await _drop_client(server_name, client)
        client = MCPClient(server_url, headers=headers, timeout=timeout)
        _CLIENTS[server_name] = client
        try:
            await client.initialize()
            result = await client.call_tool(tool_name, arguments)
        except MCPError as exc:
            await _drop_client(server_name, client)
            return f"Error: MCP connection to {server_name!r} lost: {exc}"
    except MCPError as exc:
        return f"Error: MCP tool {tool_name!r} failed: {exc}"
    return _format_tool_result(result)


async def _drop_client(server_name: str, client: MCPClient) -> None:
    """Chiude e rimuove un client fallito (best-effort: loop già morto incluso)."""
    if _CLIENTS.get(server_name) is client:
        del _CLIENTS[server_name]
    try:
        await client.aclose()
    except Exception:  # noqa: BLE001
        pass


def _format_tool_result(result: dict[str, Any]) -> str:
    """Contenuto di un risultato ``tools/call`` → stringa per il modello.

    I blocchi di testo si concatenano; i blocchi non testuali (immagini,
    risorse) vengono serializzati in JSON così niente va perso in silenzio.
    Un risultato ``isError`` viene segnalato come tale: il modello deve poter
    distinguere un output normale da un fallimento del tool remoto.
    """
    content = result.get("content")
    is_error = bool(result.get("isError"))
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            block_type = block.get("type")
            if block_type == "text":
                parts.append(str(block.get("text", "")))
            else:
                parts.append(f"[{block_type}] {_json_dump(block)}")
        text = "\n".join(parts).strip()
    else:
        text = _json_dump(content or result)
    if is_error:
        return f"MCP tool returned an error:\n{text}"
    return text or "(empty result)"


def _json_dump(value: Any) -> str:
    import json

    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(value)


def reset_mcp_state() -> None:
    """Scarta client e config dei server MCP (cache di modulo, v. docstring).

    Da chiamare a ogni avvio del gateway, nello stesso blocco degli altri
    reset: i client asincroni sono legati all'event loop del run precedente e
    riusarli dopo un restart in-process fallirebbe con errori criptici di
    loop chiuso. Lo stato vero sta in ``config.json``; qui si scorda solo la
    cache.
    """
    _CLIENTS.clear()
    _SERVER_CONFIGS.clear()


__all__ = [
    "MCPTool",
    "call_mcp_tool",
    "reset_mcp_state",
    "sync_mcp_tools",
]
