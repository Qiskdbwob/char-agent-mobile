"""Impostazioni MCP per la WebUI: server registrati, CRUD, test di connessione.

Controparte *umana* di ``jenny/mcp/manager.py``: lì si decide quali tool il
modello può chiamare, qui si raccolgono le decisioni che solo una persona può
prendere — quali server esistono, con quale URL e quali header. Il modello non
può dichiarare server: può solo nominare quelli già salvati qui.

Le regole sono le stesse di ``ssh_api``:

* **le header non escono mai di qui.** Il payload di lettura dichiara solo i
  *nomi* (``header_keys``): un valore che torna al client finisce nella
  cronologia della WebView e in qualunque log tocchi quel corpo. Il parametro
  di scrittura si chiama ``headers`` e porta un JSON; è uno dei marcatori di
  ``http_utils.redact_query_secrets``, quindi il suo valore risulta già
  mascherato in ogni riga di log che stampi il path della richiesta;
* **ogni scrittura della config passa da** :func:`jenny.config.store.mutate`,
  e l'I/O lento (DNS, connessione di test) sta **prima** di entrarci: il lock
  resta preso per tutta la durata del callback;
* un cambio di server vale dal **prossimo riavvio del gateway**: i tool MCP
  vengono registrati all'avvio dell'agente (``sync_mcp_tools``), quindi il
  payload di risposta porta ``requires_restart`` quando qualcosa è cambiato e
  la UI lo dice a parole, come per ``power.keepAwake``.
"""

from __future__ import annotations

import json
import time
from typing import Any

from jenny.config import store
from jenny.config.loader import load_config
from jenny.config.schema import Config
from jenny.config.tool_schemas import (
    MCP_MAX_TIMEOUT_S,
    MCP_MIN_TIMEOUT_S,
    MCP_NAME_RE,
    MCPServerConfig,
)
from jenny.mcp.client import MCPClient, MCPError
from jenny.security.network import validate_mcp_target
from jenny.webui.settings_api import WebUISettingsError

QueryParams = dict[str, list[str]]

# Esito dell'ultimo test di connessione, per nome di server (in-memory, come
# ``ssh_api._PENDING_PROBES``): ``{status: "ok"|"error", tools: int, error: str,
# at: float}``. Non persistito: al riavvio del gateway la UI riparte da
# \"non testato\", che è la verità — i tool registrati in quel momento li ha
# visti solo l'utente che ha premuto Test.
_LAST_TEST: dict[str, dict[str, Any]] = {}


# -- helper di query ---------------------------------------------------------


def _query_first(query: QueryParams, key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _required(query: QueryParams, key: str) -> str:
    value = (_query_first(query, key) or "").strip()
    if not value:
        raise WebUISettingsError(f"{key} is required")
    return value


def _flag(query: QueryParams, key: str) -> bool:
    return (_query_first(query, key) or "").strip().lower() in ("1", "true", "yes", "on")


def _parse_name(query: QueryParams) -> str:
    name = _required(query, "name")
    if not MCP_NAME_RE.match(name):
        raise WebUISettingsError(
            "name must be 1-64 characters, letters, digits, '-' or '_' only"
        )
    return name


def _parse_timeout_or_keep(value: str | None) -> int | None:
    """Timeout richiesto, o ``None`` per \"lascia quello che c'è\"."""
    if value is None or not value.strip():
        return None
    try:
        timeout = int(value.strip())
    except ValueError:
        raise WebUISettingsError("timeout must be an integer") from None
    if not MCP_MIN_TIMEOUT_S <= timeout <= MCP_MAX_TIMEOUT_S:
        raise WebUISettingsError(
            f"timeout must be between {MCP_MIN_TIMEOUT_S} and {MCP_MAX_TIMEOUT_S}"
        )
    return timeout


def _parse_headers(query: QueryParams) -> list[tuple[str, str]] | None:
    """Header richieste come lista ``(nome, valore)``, o ``None`` per \"come prima\".

    Il client le manda come JSON: un array di coppie ``[name, value]``, un
    array di oggetti ``{name, value}`` o un dict ``{name: value}``. Un valore
    vuoto significa \"tieni quella salvata\" (stessa semantica della password
    SSH): la UI non conosce i valori correnti, quindi non può rimandarli.
    """
    raw = _query_first(query, "headers")
    if raw is None or not raw.strip():
        return None
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        raise WebUISettingsError("headers must be a JSON array or object") from None

    pairs: list[tuple[str, str]] = []
    if isinstance(parsed, dict):
        parsed = [[str(k), v] for k, v in parsed.items()]
    if not isinstance(parsed, list):
        raise WebUISettingsError("headers must be a JSON array or object")
    for entry in parsed:
        if isinstance(entry, dict):
            name = entry.get("name")
            value = entry.get("value")
        elif isinstance(entry, (list, tuple)) and len(entry) == 2:
            name, value = entry
        else:
            raise WebUISettingsError("each header must be a [name, value] pair")
        if not isinstance(name, str) or not name.strip():
            raise WebUISettingsError("header name must be a non-empty string")
        pairs.append((name.strip(), "" if value is None else str(value)))
    return pairs


# -- payload ------------------------------------------------------------------


def _server_payload(server: MCPServerConfig) -> dict[str, Any]:
    last = _LAST_TEST.get(server.name)
    return {
        "name": server.name,
        "url": server.url,
        # Solo i nomi: i valori non escono mai (v. docstring di modulo).
        "header_keys": sorted(server.headers.keys()),
        "enabled": server.enabled,
        "timeout": server.timeout,
        "status": last["status"] if last else "untested",
        "tools": last.get("tools", 0) if last else 0,
        "last_error": last.get("error") if last and last["status"] == "error" else None,
    }


def mcp_settings_payload(config: Any = None, *, requires_restart: bool = False) -> dict[str, Any]:
    """Stato MCP per Settings, con l'esito dell'ultimo test per server.

    ``requires_restart`` è True solo sulla risposta di una scrittura che ha
    cambiato qualcosa: i tool vengono registrati all'avvio dell'agente, quindi
    la UI deve dire che il cambio vale dal prossimo riavvio.
    """
    if config is None:
        config = load_config()
    return {
        "servers": [_server_payload(s) for s in config.tools.mcp.servers],
        "requires_restart": requires_restart,
    }


def _find_server(config: Config, name: str) -> MCPServerConfig:
    for server in config.tools.mcp.servers:
        if server.name == name:
            return server
    raise WebUISettingsError(f"unknown MCP server: {name}", status=404)


# -- CRUD ---------------------------------------------------------------------


async def save_mcp_server(query: QueryParams) -> dict[str, Any]:
    """Crea o aggiorna un server. Il nome è l'identità: non si rinomina.

    La policy di rete viene applicata **qui**, al salvataggio (DNS fuori dal
    lock), e di nuovo alla discovery/connessione: questo controllo dice subito
    all'utente che l'URL non è raggiungibile per policy, quello dopo copre il
    nome che comincia a risolvere a un indirizzo vietato più tardi.
    """
    name = _parse_name(query)
    url = _required(query, "url")
    requested_timeout = _parse_timeout_or_keep(_query_first(query, "timeout"))
    requested_enabled = _query_first(query, "enabled")
    requested_headers = _parse_headers(query)

    ok, error = await _validate_url(url)
    if not ok:
        raise WebUISettingsError(f"server refused by the network policy: {error}")

    changed: dict[str, bool] = {}

    def _apply(config: Config) -> None:
        current = next((s for s in config.tools.mcp.servers if s.name == name), None)
        if current is None:
            config.tools.mcp.servers.append(
                MCPServerConfig(
                    name=name,
                    url=url,
                    headers=_resolve_headers(None, requested_headers or []),
                    enabled=_flag(query, "enabled") if requested_enabled is not None else True,
                    timeout=requested_timeout or 30,
                )
            )
            changed["changed"] = True
            return
        if current.url != url:
            current.url = url
            changed["changed"] = True
        if requested_timeout is not None and current.timeout != requested_timeout:
            current.timeout = requested_timeout
            changed["changed"] = True
        if requested_enabled is not None:
            value = _flag(query, "enabled")
            if current.enabled != value:
                current.enabled = value
                changed["changed"] = True
        if requested_headers is not None:
            resolved = _resolve_headers(dict(current.headers), requested_headers)
            if resolved != dict(current.headers):
                current.headers = resolved
                changed["changed"] = True

    await store.mutate(_apply)
    _LAST_TEST.pop(name, None)
    return mcp_settings_payload(requires_restart=changed.get("changed", False))


def _resolve_headers(
    existing: dict[str, str] | None, requested: list[tuple[str, str]]
) -> dict[str, str]:
    """Fonde le header richieste con quelle salvate.

    Un valore vuoto significa \"tieni quella salvata\": la UI non conosce i
    valori correnti (non escono mai dal server), quindi l'unico modo di non
    cancellare un Authorization è lasciare il campo vuoto. Le righe rimosse
    dalla UI spariscono del tutto: è così che si cancella una header.
    """
    existing = dict(existing or {})
    if requested is None:
        return existing
    resolved: dict[str, str] = {}
    for name, value in requested:
        if value.strip():
            resolved[name] = value
        elif name in existing:
            resolved[name] = existing[name]
    return resolved


async def delete_mcp_server(query: QueryParams) -> dict[str, Any]:
    """Rimuove un server e i suoi tool dal prossimo avvio."""
    name = _required(query, "name")

    def _apply(config: Config) -> None:
        found = any(s.name == name for s in config.tools.mcp.servers)
        if not found:
            raise WebUISettingsError(f"unknown MCP server: {name}", status=404)
        config.tools.mcp.servers = [
            s for s in config.tools.mcp.servers if s.name != name
        ]

    await store.mutate(_apply)
    _LAST_TEST.pop(name, None)
    return mcp_settings_payload(requires_restart=True)


# -- test di connessione ------------------------------------------------------


async def test_mcp_server(query: QueryParams) -> dict[str, Any]:
    """Connette a un server e conta i tool (``initialize`` + ``tools/list``).

    Nessuna scrittura della config: il risultato sta in ``_LAST_TEST`` (v.
    docstring di modulo) e la UI lo mostra come badge sulla card. Il timeout
    del test è quello configurato del server, già validato fra i limiti.
    """
    name = _required(query, "name")
    config = load_config()
    server = _find_server(config, name)

    ok, error = await _validate_url(server.url)
    if not ok:
        _record_test(name, {"status": "error", "tools": 0, "error": error})
        raise WebUISettingsError(f"server refused by the network policy: {error}")

    client = MCPClient(server.url, headers=server.headers, timeout=server.timeout)
    try:
        await client.initialize()
        tools = await client.list_tools()
    except MCPError as exc:
        _record_test(name, {"status": "error", "tools": 0, "error": str(exc)})
        raise WebUISettingsError(f"connection test failed: {exc}") from exc
    finally:
        await client.aclose()

    _record_test(name, {"status": "ok", "tools": len(tools), "error": ""})
    return mcp_settings_payload()


def _record_test(name: str, outcome: dict[str, Any]) -> None:
    _LAST_TEST[name] = {**outcome, "at": time.time()}


async def _validate_url(url: str) -> tuple[bool, str]:
    """DNS + policy di rete fuori dal lock di ``mutate`` (I/O lento)."""
    import asyncio

    return await asyncio.to_thread(validate_mcp_target, url)


def reset_mcp_settings_state() -> None:
    """Scarta la cache degli esiti di test (nessun oggetto legato al loop)."""
    _LAST_TEST.clear()
