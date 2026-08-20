"""Client minimale della Telegram Bot API basato su httpx.

Nessuna dipendenza nuova: usa lo stesso ``httpx`` dei provider LLM (già nel
lockfile Android). Copre solo i metodi necessari al canale: ``getMe``,
``getUpdates`` (long polling) e ``sendMessage``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from loguru import logger

TELEGRAM_API_BASE = "https://api.telegram.org"

# Tentativi extra dopo un 429 (l'attesa è dettata da retry_after del server).
_MAX_RATE_LIMIT_RETRIES = 2
# Cap difensivo sull'attesa suggerita dal server per non bloccare il poller.
_MAX_RETRY_AFTER_S = 30.0


class TelegramAPIError(Exception):
    """Errore applicativo della Bot API (ok=false o HTTP non-2xx)."""

    def __init__(self, status_code: int, description: str):
        self.status_code = status_code
        self.description = description
        super().__init__(f"Telegram API error {status_code}: {description}")


class TelegramAPI:
    """Wrapper asincrono e minimale della Bot API.

    Il client httpx è iniettabile per i test (``httpx.MockTransport``).
    """

    def __init__(
        self,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = TELEGRAM_API_BASE,
    ):
        self._base = f"{base_url.rstrip('/')}/bot{token}"
        self._client = client or httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        files: dict[str, Any] | None = None,
    ) -> Any:
        """Invoca *method* e ritorna il campo ``result`` della risposta.

        Su 429 attende ``retry_after`` (con cap) e riprova; ogni altro errore
        applicativo o HTTP diventa :class:`TelegramAPIError`. Con ``files`` la
        richiesta è multipart/form-data (upload media) e ``payload`` diventa il
        form dei campi testuali; senza ``files`` è JSON come di consueto.
        """
        url = f"{self._base}/{method}"
        attempts = _MAX_RATE_LIMIT_RETRIES + 1
        for attempt in range(attempts):
            if files is not None:
                resp = await self._client.post(
                    url, data=payload or {}, files=files, timeout=timeout
                )
            else:
                resp = await self._client.post(url, json=payload or {}, timeout=timeout)
            if resp.status_code == 429 and attempt < attempts - 1:
                retry_after = _MAX_RETRY_AFTER_S
                try:
                    body = resp.json()
                    retry_after = float(body.get("parameters", {}).get("retry_after", 5))
                except Exception:
                    pass
                retry_after = min(max(retry_after, 0.5), _MAX_RETRY_AFTER_S)
                logger.warning("Telegram rate limit on {}, retrying in {}s", method, retry_after)
                await asyncio.sleep(retry_after)
                continue
            try:
                body = resp.json()
            except Exception:
                raise TelegramAPIError(resp.status_code, resp.text[:200]) from None
            if not body.get("ok"):
                raise TelegramAPIError(
                    resp.status_code, str(body.get("description", "unknown error"))
                )
            return body.get("result")
        raise TelegramAPIError(429, "rate limited")  # pragma: no cover - difensivo

    async def get_me(self) -> dict[str, Any]:
        """Ritorna le info del bot; usato anche per validare il token."""
        return await self._call("getMe")

    async def get_updates(self, offset: int | None, timeout_s: int) -> list[dict[str, Any]]:
        """Long-poll degli update; il timeout HTTP supera quello lato server."""
        payload: dict[str, Any] = {
            "timeout": timeout_s,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = await self._call("getUpdates", payload, timeout=timeout_s + 10)
        return result if isinstance(result, list) else []

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return await self._call("sendMessage", payload)

    async def send_media_file(
        self,
        chat_id: str,
        *,
        method: str,
        field: str,
        filename: str,
        data: bytes,
        caption: str | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        """Carica un file locale via multipart (``sendPhoto``/``sendDocument``)."""
        form: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            form["caption"] = caption
            if parse_mode:
                form["parse_mode"] = parse_mode
        return await self._call(
            method, form, files={field: (filename, data)}, timeout=60.0
        )

    async def send_media_url(
        self,
        chat_id: str,
        *,
        method: str,
        field: str,
        url: str,
        caption: str | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        """Invia un media referenziandolo per URL (Telegram lo scarica lato server)."""
        payload: dict[str, Any] = {"chat_id": chat_id, field: url}
        if caption:
            payload["caption"] = caption
            if parse_mode:
                payload["parse_mode"] = parse_mode
        return await self._call(method, payload)

    async def send_chat_action(
        self,
        chat_id: str,
        action: str,
    ) -> bool:
        """Invia un'azione di chat (es. ``typing``); ritorna True a successo.

        Usata per mostrare l'indicatore "scrivendo..." durante un turno.
        Telegram impone max 1 ogni 5 secondi per chat.
        """
        try:
            await self._call(
                "sendChatAction",
                {"chat_id": chat_id, "action": action},
            )
            return True
        except TelegramAPIError:
            return False

    async def set_my_commands(self, commands: list[dict[str, str]]) -> Any:
        """Registra il menu comandi del bot (chiamata best-effort lato caller)."""
        return await self._call("setMyCommands", {"commands": commands})
