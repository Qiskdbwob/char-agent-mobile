"""Client MCP (Model Context Protocol) minimal — Streamable HTTP, soli tool.

Implementazione *tools-only* del protocollo MCP su Streamable HTTP (JSON-RPC
2.0 su POST, risposte JSON o SSE). Nessuna dipendenza oltre a ``httpx`` (già
nel bundle APK): l'SDK ufficiale ``mcp`` pretende pydantic v2 + componenti Rust
(``pydantic_core``) che su Chaquopy non sono garantiti, quindi qui il
protocollo è implementato a mano, limitato a quello che serve a Jenny:
``initialize``, ``notifications/initialized``, ``tools/list`` e ``tools/call``.

Due superfici, una sola logica di protocollo:

* :class:`MCPClient` — client **async** usato a runtime dai tool (sessioni
  lunghe, header ``Mcp-Session-Id`` riusato fra chiamate);
* :func:`discover_mcp_tools` — discovery **sync** usata alla registrazione dei
  tool (``manager.sync_mcp_tools``): il loader dell'agente è sincrono, quindi
  qui serve un ``httpx.Client`` bloccante con timeout corti, non un loop.

Entrambe parlano lo stesso JSON-RPC e parseggiano la stessa forma di risposta:
la parte di protocollo condivisa vive nelle funzioni ``_*`` di questo modulo.

Riferimento: https://modelcontextprotocol.io/specification/2025-03-26/basic/transports
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterable, Iterable
from typing import Any

import httpx
from loguru import logger

from jenny import __version__

# Versione del protocollo negoziata in ``initialize``. 2025-03-26 è supportata
# dalla stragrande maggioranza dei server MCP; una versione più nuova non ci
# serve (non usiamo resource/prompts/sampling) e una più vecchia la rifiuta
# quasi nessuno.
PROTOCOL_VERSION = "2025-03-26"

# Default/limiti temporali condivisi fra discovery e runtime.
_CONNECT_TIMEOUT_S = 5.0
_WRITE_TIMEOUT_S = 10.0
_DEFAULT_READ_TIMEOUT_S = 30.0
# Il discovery alla registrazione non deve poter tenere in ostaggio l'avvio
# del gateway: qualunque sia il timeout configurato, qui vale questo tetto.
_MAX_DISCOVERY_READ_S = 10.0

_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

_SESSION_HEADER = "mcp-session-id"

_CLIENT_NAME = "jenny"


class MCPError(Exception):
    """Errore generico MCP (protocollo, rete, risposta inattesa)."""


class MCPConnectionError(MCPError):
    """Errore di trasporto: rete, HTTP non-2xx, risposta non decodificabile."""


class MCPToolCallError(MCPError):
    """Errore applicativo: risposta JSON-RPC ``error`` o risultato ``isError``."""


# -- utilita' condivise -------------------------------------------------------


def _jsonrpc_request(method: str, params: dict[str, Any], request_id: int | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
    }
    if request_id is not None:
        payload["id"] = request_id
    return payload


def _merge_headers(user_headers: dict[str, str], session_id: str | None) -> dict[str, str]:
    """Header di protocollo + header utente + sessione.

    Quelli di protocollo vincono di proposito: un ``Content-Type`` utente
    diverso da ``application/json`` romperebbe il JSON-RPC, e un ``Accept``
    senza ``text/event-stream`` farebbe rispondere in JSON qualche server che
    invece potrebbe voler fare streaming. Le header utente (Authorization, …)
    si sommano a queste.
    """
    headers = {**user_headers, **_HEADERS}
    if session_id:
        headers[_SESSION_HEADER] = session_id
    return headers


def _extract_response_id(message: dict[str, Any]) -> Any:
    return message.get("id")


def _sse_messages(lines: Iterable[str]) -> Iterable[dict[str, Any]]:
    """Estrae i messaggi ``data:`` da un flusso SSE (variante sincrona)."""
    yield from _parse_sse(lines)


async def _sse_messages_async(lines: AsyncIterable[str]) -> AsyncIterable[dict[str, Any]]:
    """Estrae i messaggi ``data:`` da un flusso SSE (variante asincrona)."""
    async for message in _parse_sse_async(lines):
        yield message


def _parse_sse(lines: Iterable[str]) -> Iterable[dict[str, Any]]:
    """Parses an SSE byte/str stream into parsed JSON data payloads.

    La grammatica rilevante di SSE è solo ``event:`` e ``data:``: un messaggio
    finisce a una riga vuota, più righe ``data:`` si concatenano con ``\\n``.
    Gli eventi ``ping`` portano ``data: {}`` e vanno ignorati; ``error`` porta
    un oggetto JSON da sollevare come :class:`MCPConnectionError`.
    """
    data_lines: list[str] = []
    event: str | None = None

    def flush() -> dict[str, Any] | None:
        nonlocal data_lines, event
        if not data_lines:
            data_lines = []
            event = None
            return None
        payload = "\n".join(data_lines)
        data_lines = []
        ev = event
        event = None
        try:
            parsed: Any = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MCPConnectionError(f"invalid SSE data payload: {exc}") from exc
        if not isinstance(parsed, dict):
            raise MCPConnectionError("SSE data payload is not a JSON object")
        if ev == "error":
            raise MCPConnectionError(
                f"MCP server error event: {parsed.get('error') or parsed}"
            )
        return parsed

    for raw_line in lines:
        line = raw_line.rstrip("\r")
        if line == "":
            message = flush()
            if message is not None:
                yield message
            continue
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
            continue
        if line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())
            continue
        # Campi SSE che non ci riguardano (id, retry, commenti): ignorati.
    message = flush()
    if message is not None:
        yield message


async def _parse_sse_async(lines: AsyncIterable[str]) -> AsyncIterable[dict[str, Any]]:
    """Variante asincrona di :func:`_parse_sse` (stessa grammatica)."""
    data_lines: list[str] = []
    event: str | None = None

    def flush() -> dict[str, Any] | None:
        nonlocal data_lines, event
        if not data_lines:
            data_lines = []
            event = None
            return None
        payload = "\n".join(data_lines)
        data_lines = []
        ev = event
        event = None
        try:
            parsed: Any = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MCPConnectionError(f"invalid SSE data payload: {exc}") from exc
        if not isinstance(parsed, dict):
            raise MCPConnectionError("SSE data payload is not a JSON object")
        if ev == "error":
            raise MCPConnectionError(
                f"MCP server error event: {parsed.get('error') or parsed}"
            )
        return parsed

    async for raw_line in lines:
        line = raw_line.rstrip("\r")
        if line == "":
            message = flush()
            if message is not None:
                yield message
            continue
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
            continue
        if line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())
            continue
    message = flush()
    if message is not None:
        yield message


def _find_response(
    messages: Iterable[dict[str, Any]], request_id: int | None
) -> dict[str, Any]:
    """Ritorna il messaggio JSON-RPC con ``id`` richiesto.

    Fra le risposte arrivate può esserci di tutto (notification di progress,
    ping): l'unica che conta è quella con lo stesso id della richiesta. Se non
    c'è, il server ha chiuso il flusso senza rispondere.
    """
    for message in messages:
        if message.get("id") != request_id:
            continue
        if "error" in message:
            error = message.get("error") or {}
            raise MCPToolCallError(
                f"MCP request {request_id} failed: "
                f"{error.get('code')} {error.get('message')}"
            )
        return message
    raise MCPConnectionError(
        f"MCP server closed the stream without answering request {request_id}"
    )


def _result_of(message: dict[str, Any]) -> dict[str, Any]:
    result = message.get("result")
    if not isinstance(result, dict):
        raise MCPConnectionError(f"unexpected MCP result shape: {type(result).__name__}")
    return result


def _parse_http_response_headers(
    response: httpx.Response, session_holder: list[str] | None = None
) -> str | None:
    session_id = response.headers.get(_SESSION_HEADER)
    if session_id and session_holder is not None:
        session_holder.append(session_id)
    return session_id


# -- discovery sincrona (registrazione tool) ----------------------------------


def discover_mcp_tools(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = _DEFAULT_READ_TIMEOUT_S,
) -> list[dict[str, Any]]:
    """Discovery sincrona dei tool di un server (``initialize`` + ``tools/list``).

    Usata da ``manager.sync_mcp_tools`` durante la registrazione dei tool,
    che avviene in contesto sincrono dentro l'init dell'agente. Il timeout di
    lettura è il tetto fra il valore configurato e ``_MAX_DISCOVERY_READ_S``:
    un server irraggiungibile non deve tenere in ostaggio l'avvio del gateway
    più di qualche secondo.

    Ritorna la lista grezza di tool dal risultato di ``tools/list``
    (``[{"name": ..., "description": ..., "inputSchema": ...}]``).
    """
    read_timeout = min(max(float(timeout), 1.0), _MAX_DISCOVERY_READ_S)
    transport_timeout = httpx.Timeout(
        read_timeout, connect=_CONNECT_TIMEOUT_S, write=_WRITE_TIMEOUT_S
    )
    try:
        with httpx.Client(timeout=transport_timeout, follow_redirects=False) as client:
            session_holder: list[str] = []
            request_id = 0

            # initialize
            request_id += 1
            payload = _jsonrpc_request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": _CLIENT_NAME, "version": __version__},
                },
                request_id,
            )
            response = client.post(
                url,
                json=payload,
                headers=_merge_headers(headers or {}, session_holder[0] if session_holder else None),
            )
            response.raise_for_status()
            _parse_http_response_headers(response, session_holder)
            _read_response(response, request_id)

            # notifications/initialized — nessuna risposta attesa.
            request_id += 1
            notification = _jsonrpc_request("notifications/initialized", {}, None)
            with client.stream(
                "POST",
                url,
                json=notification,
                headers=_merge_headers(headers or {}, session_holder[0] if session_holder else None),
            ) as notif_response:
                if not notif_response.is_success:
                    logger.warning(
                        "MCP initialized notification rejected: {}",
                        notif_response.status_code,
                    )
                notif_response.read()

            # tools/list
            request_id += 1
            list_payload = _jsonrpc_request("tools/list", {}, request_id)
            list_response = client.post(
                url,
                json=list_payload,
                headers=_merge_headers(headers or {}, session_holder[0] if session_holder else None),
            )
            list_response.raise_for_status()
            _parse_http_response_headers(list_response, session_holder)
            list_message = _read_response(list_response, request_id)
            tools = _result_of(list_message).get("tools")
            if not isinstance(tools, list):
                raise MCPConnectionError("tools/list returned no tools array")
            return tools
    except httpx.HTTPError as exc:
        raise MCPConnectionError(f"MCP transport error: {exc}") from exc


def _read_response(response: httpx.Response, request_id: int) -> dict[str, Any]:
    """Legge un corpo (JSON o SSE) e ne estrae la risposta JSON-RPC attesa."""
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("text/event-stream"):
        messages = list(_parse_sse(response.iter_lines()))
        return _find_response(messages, request_id)
    try:
        message: Any = response.json()
    except json.JSONDecodeError as exc:
        raise MCPConnectionError(
            f"invalid JSON-RPC response (status {response.status_code}): {exc}"
        ) from exc
    if not isinstance(message, dict):
        raise MCPConnectionError("JSON-RPC response is not an object")
    if message.get("id") != request_id:
        raise MCPConnectionError(
            f"MCP server answered with mismatched id {message.get('id')!r} "
            f"(expected {request_id})"
        )
    if "error" in message:
        error = message.get("error") or {}
        raise MCPToolCallError(
            f"MCP request {request_id} failed: {error.get('code')} {error.get('message')}"
        )
    return message


# -- client asincrono (runtime) -----------------------------------------------


class MCPClient:
    """Client asincrono MCP Streamable HTTP per un server.

    Una istanza per server, condivisa fra tutti i suoi tool: la sessione
    (``Mcp-Session-Id``) e il client httpx vivono qui, così chiamate successive
    non ripetono l'handshake. Il client va creato **dentro** l'event loop che
    lo userà (httpx ci lega i socket) e chiuso con :meth:`aclose` quando il
    loop finisce — ``manager.reset_mcp_state`` fa entrambe le cose per conto
    del gateway.

    Il flusso è:

    #. :meth:`initialize` — handshake + ``notifications/initialized``;
    #. :meth:`list_tools` — discovery (usata anche dal test di Settings);
    #. :meth:`call_tool` — invocazione, quante volte serve.
    """

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = _DEFAULT_READ_TIMEOUT_S,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = max(float(timeout), 1.0)
        self._session_id: str | None = None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                self.timeout, connect=_CONNECT_TIMEOUT_S, write=_WRITE_TIMEOUT_S
            ),
            follow_redirects=False,
        )
        self._initialized = False
        self._next_id = 0

    @property
    def initialized(self) -> bool:
        return self._initialized

    async def aclose(self) -> None:
        await self._client.aclose()

    def _request_headers(self) -> dict[str, str]:
        return _merge_headers(self.headers, self._session_id)

    async def initialize(self) -> dict[str, Any]:
        """Handshake ``initialize`` + notifica ``initialized``."""
        self._next_id += 1
        payload = _jsonrpc_request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": _CLIENT_NAME, "version": __version__},
            },
            self._next_id,
        )
        message = await self._post(payload)
        result = _result_of(message)
        if self._session_id:
            await self._send_notification("notifications/initialized")
        self._initialized = True
        return result

    async def list_tools(self) -> list[dict[str, Any]]:
        """Ritorna i tool esposti dal server (schema JSON incluso)."""
        if not self._initialized:
            await self.initialize()
        self._next_id += 1
        message = await self._post(_jsonrpc_request("tools/list", {}, self._next_id))
        tools = _result_of(message).get("tools")
        if not isinstance(tools, list):
            raise MCPConnectionError("tools/list returned no tools array")
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoca un tool e ritorna il risultato grezzo (``content`` + ``isError``)."""
        if not self._initialized:
            await self.initialize()
        self._next_id += 1
        message = await self._post(
            _jsonrpc_request(
                "tools/call", {"name": name, "arguments": arguments}, self._next_id
            )
        )
        return _result_of(message)

    async def _send_notification(self, method: str) -> None:
        """Invia una notification JSON-RPC (nessun id, nessuna risposta attesa).

        Alcuni server rispondono comunque con un corpo: lo si legge e butta,
        perché un errore qui non deve fallire l'handshake già riuscito.
        """
        payload = _jsonrpc_request(method, {}, None)
        try:
            async with self._client.stream(
                "POST", self.url, json=payload, headers=self._request_headers()
            ) as response:
                await response.aread()
        except httpx.HTTPError as exc:
            logger.warning("MCP notification {!r} failed: {}", method, exc)

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST JSON-RPC e parsing della risposta (JSON o SSE)."""
        try:
            async with self._client.stream(
                "POST", self.url, json=payload, headers=self._request_headers()
            ) as response:
                response.raise_for_status()
                session_id = response.headers.get(_SESSION_HEADER)
                if session_id:
                    self._session_id = session_id
                request_id = payload.get("id")
                content_type = response.headers.get("content-type", "")
                if content_type.startswith("text/event-stream"):
                    messages = [
                        message
                        async for message in _sse_messages_async(response.aiter_lines())
                    ]
                    return _find_response(messages, request_id)
                body = await response.aread()
        except httpx.HTTPStatusError as exc:
            raise MCPConnectionError(
                f"MCP server answered {exc.response.status_code} for {payload.get('method')}"
            ) from exc
        except httpx.HTTPError as exc:
            raise MCPConnectionError(f"MCP transport error: {exc}") from exc
        try:
            message: Any = json.loads(body)
        except json.JSONDecodeError as exc:
            raise MCPConnectionError(f"invalid JSON-RPC response: {exc}") from exc
        if not isinstance(message, dict):
            raise MCPConnectionError("JSON-RPC response is not an object")
        if message.get("id") != request_id:
            raise MCPConnectionError(
                f"MCP server answered with mismatched id {message.get('id')!r} "
                f"(expected {request_id})"
            )
        if "error" in message:
            error = message.get("error") or {}
            raise MCPToolCallError(
                f"MCP request {request_id} failed: {error.get('code')} {error.get('message')}"
            )
        return message


__all__ = [
    "MCPClient",
    "MCPConnectionError",
    "MCPError",
    "MCPToolCallError",
    "PROTOCOL_VERSION",
    "discover_mcp_tools",
]
