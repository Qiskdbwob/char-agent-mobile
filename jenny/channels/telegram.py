"""Canale Telegram: bot personale con pairing a codice e long polling.

Contratto duck-typed del dispatcher (come ``WebSocketChannel``): attributi di
gating, ``start()``/``stop()`` e ``send()``. Niente streaming: il canale non
setta ``_wants_stream`` sull'inbound, quindi riceve solo messaggi finali.

Il canale è pura consegna: non conosce il transcript WebUI. La proiezione dei
turni Telegram sulla vista WebUI è responsabilità del runtime (user echo via
``WebuiTurnCoordinator``, finale via dispatcher, ``turn_end`` via runtime
events); qui resta solo il turn-id nei metadata inbound per correlare le righe.
"""

from __future__ import annotations

import asyncio
import hmac
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from loguru import logger

from jenny.bus.events import COORDINATION_FLAGS, InboundMessage, OutboundMessage
from jenny.bus.queue import MessageBus
from jenny.channels.telegram_api import TelegramAPI, TelegramAPIError
from jenny.channels.telegram_format import markdown_to_telegram_html, split_message
from jenny.config.schema import TelegramConfig
from jenny.runtime.power import keep_awake
from jenny.webui.metadata import WEBUI_TURN_METADATA_KEY

# Intervallo fra sendChatAction (max 1 ogni 5s secondo Telegram Bot API).
_TYPING_INTERVAL_S = 4.5
# Durata massima di sicurezza del typing indicator: se per qualunque
# ragione il messaggio finale (o un _turn_end) non arriva a fermarlo,
# il loop si auto-termina invece di mostrare "sta scrivendo..." per sempre.
_TYPING_MAX_DURATION_S = 300.0
# Intervallo minimo tra due aggiornamenti del messaggio di stato: protegge
# dalla rate-limit di Telegram (1 edit/chat/s) e riduce il rumore visivo.
_STATUS_UPDATE_INTERVAL_S = 2.0
# Prefissi emoji per i diversi stati del turno
_STATUS_PREFIX_THINKING = "🧠"
_STATUS_PREFIX_TOOL = "🔧"

# Testo di default del messaggio di stato "thinking": compare subito
# all'arrivo del messaggio utente, senza attendere lo streaming del
# reasoning (su Telegram non arriva comunque, canale receive-final-only).
_STATUS_THINKING_DEFAULT = "Jenny sedang berpikir…"

# Limite prudente sul testo grezzo: la conversione HTML può allungare il chunk.
_RAW_CHUNK_LIMIT = 3500
_POLL_BACKOFF_MAX_S = 60.0
_CHUNK_RETRY_DELAYS = (1, 2, 4)

# Scadenza del wakelock che copre la lavorazione di un update ricevuto.
# Generosa rispetto al lavoro che c'è dentro (pairing, reverse-geocoding di una
# posizione, una risposta di servizio con i suoi retry), ma finita: qui non gira
# mai un turno LLM: quello parte dal bus e si prende il proprio lock "turn".
#
# Il long-poll di ``getUpdates`` NON è coperto, ed è una scelta: l'attesa è
# inattiva per costruzione e dura fino a ``poll_timeout_s`` (50s di default),
# quindi un lock che la copra sarebbe tenuto ~sempre — cioè la modalità
# "always" travestita da "turns". Il costo è che a schermo spento un messaggio
# Telegram può restare in coda finché il device non si sveglia da solo; il
# rimedio è il risveglio programmato, non il wakelock.
_TELEGRAM_WAKELOCK_TIMEOUT_S = 180.0

# Estensioni inviabili come foto (anteprima nativa Telegram); tutto il resto
# (SVG, PDF, ecc.) va come documento. Cap prudente sotto il limite Bot API.
_RASTER_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_TG_MEDIA_MAX_BYTES = 10 * 1024 * 1024

# Onboarding in finestra di pairing: budget di risposte di servizio per chat e
# bound fail-closed sul numero di chat tracciate. Superato il cap (o pieno il
# dict) la chat diventa INELEGGIBILE al pairing anche col codice giusto: è
# questo che rende il throttle una difesa reale contro il brute-force del
# codice a 6 cifre (budget totale ≈ cap × bound su 10^6 per vita del canale).
_MAX_PAIR_ATTEMPTS = 5
_MAX_TRACKED_CHATS = 512

# Chiavi di update Telegram che indicano contenuto non testuale (v1: non gestito).
# ``location``/``venue`` sono gestite a parte (vedi _maybe_handle_location) e
# restano qui solo per la fallback "media_soon" quando la posizione è off.
_MEDIA_KEYS = (
    "photo", "voice", "document", "sticker", "video", "audio",
    "video_note", "animation", "location", "contact", "poll",
)

# Contenuto sintetico (LLM-facing, non mostrato all'utente) di un turno
# innescato da una posizione condivisa: la posizione vera arriva nel runtime
# context come "User location (shared via Telegram): …".
_LOCATION_TURN_MARKER = "📍 [The user just shared their current location via Telegram.]"

# Risposte lato bot, localizzate come i WELCOME_TEMPLATES dell'onboarding.
_BOT_STRINGS: dict[str, dict[str, str]] = {
    "it": {
        "paired": "✅ Collegato! Da ora puoi parlare con Jenny da questa chat.",
        "welcome": (
            "Scrivimi come in una chat normale e ti risponde Jenny.\n\n"
            "• /new — inizia una nuova conversazione\n"
            "• 📎 Foto, vocali e documenti arriveranno presto"
        ),
        "start_prompt": (
            "Per collegarti, inviami il codice a 6 cifre che vedi nella WebUI di Jenny."
        ),
        "wrong_code": (
            "Codice non valido. Controlla il codice a 6 cifre nella WebUI di Jenny e riprova."
        ),
        "media_soon": "📎 Foto, vocali e documenti arriveranno presto: per ora solo testo.",
    },
    "en": {
        "paired": "✅ Paired! You can now talk to Jenny from this chat.",
        "welcome": (
            "Message me like a normal chat and Jenny replies.\n\n"
            "• /new — start a new conversation\n"
            "• 📎 Photos, voice notes and documents are coming soon"
        ),
        "start_prompt": (
            "To pair, send me the 6-digit code shown in Jenny's WebUI."
        ),
        "wrong_code": (
            "Invalid code. Check the 6-digit code in Jenny's WebUI and try again."
        ),
        "media_soon": "📎 Photos, voice notes and documents are coming soon: text only for now.",
    },
}


class TelegramChannel:
    """Canale bot Telegram con pairing a codice singolo owner."""

    name = "telegram"
    display_name = "Telegram"
    send_progress = True
    # Tool-hint e reasoning arrivano come MESSAGGIO DI STATO edit-in-place
    # (🔧 / 🧠) sul canale Telegram, non come streaming: il canale e'
    # receive-final-only ma lo status indicator e' un meccanismo a parte
    # (editMessageText sullo stesso _status_msg_id). Prima erano False e
    # il dispatcher li scartava -> nessuna animazione di pensiero/tool.
    send_tool_hints = True
    show_reasoning = True
    # I retry sono gestiti per-chunk internamente: un retry esterno del
    # dispatcher rispedirebbe anche i chunk già consegnati (duplicati).
    send_max_retries = 1

    def __init__(
        self,
        config: TelegramConfig,
        bus: MessageBus,
        *,
        api: TelegramAPI | None = None,
        on_paired: Callable[[str, str | None], Awaitable[None]] | None = None,
        language: str = "en",
    ):
        self.config = config
        self.bus = bus
        self.api = api or TelegramAPI(config.bot_token or "")
        self._on_paired = on_paired
        self._language = language if language in _BOT_STRINGS else "en"
        self._paired_chat_id = config.paired_chat_id
        self._pairing_code = config.pairing_code
        self._offset: int | None = None
        self._poll_task: asyncio.Task | None = None
        # Tentativi di pairing per chat (in-memory: si azzera al reload del
        # canale, che rigenera comunque il codice nei percorsi che contano).
        self._pair_attempts: dict[str, int] = {}
        # Typing indicator: task periodico che invia sendChatAction("typing")
        # finché un turno è in corso.  Il token change di ``_typing_token``
        # fa da cancellazione implicita: un turno nuovo con un token diverso
        # sostituisce il task precedente, e l'ultimo stop avviene quando
        # arriva il messaggio finale dello stesso turno.
        self._typing_task: asyncio.Task | None = None
        self._typing_token: object = None
        self._last_turn_id: str | None = None
        # Streaming status indicator: messaggio temporaneo durante il turno
        # che mostra cosa sta facendo l'agente.  Viene editato via
        # editMessageText e cancellato quando il turno termina.
        self._status_msg_id: str | int | None = None
        self._status_last_update: float = 0.0

    def _t(self, key: str) -> str:
        return _BOT_STRINGS[self._language][key]

    @property
    def paired_chat_id(self) -> str | None:
        """Chat accoppiata corrente (stato vivo, aggiornato al pairing)."""
        return self._paired_chat_id

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Avvia il long polling; ritorna quando il task termina (stop)."""
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("Telegram channel started (paired={})", bool(self._paired_chat_id))
        with suppress(asyncio.CancelledError):
            await self._poll_task

    async def stop(self) -> None:
        self._stop_typing()
        self._status_msg_id = None
        self._status_last_update = 0.0
        if self._poll_task is not None:
            self._poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None
        await self.api.close()

    async def _poll_loop(self) -> None:
        """Long-poll di ``getUpdates`` con backoff su errori di rete.

        Il backoff cresce fino a 60s e si azzera al primo successo: cadute
        Wi-Fi o doze temporaneo si riassorbono senza intervento.
        """
        backoff = 1.0
        while True:
            try:
                updates = await self.api.get_updates(self._offset, self.config.poll_timeout_s)
                backoff = 1.0
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        self._offset = update_id + 1
                    try:
                        # Il lock si prende qui, per-update, e non attorno a
                        # ``get_updates``: vedi _TELEGRAM_WAKELOCK_TIMEOUT_S.
                        async with keep_awake(
                            "telegram", timeout_s=_TELEGRAM_WAKELOCK_TIMEOUT_S
                        ):
                            await self._handle_update(update)
                    except Exception:
                        logger.exception("Telegram: error handling update")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    "Telegram poll error ({}), retrying in {:.0f}s", type(e).__name__, backoff
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _POLL_BACKOFF_MAX_S)

    # ------------------------------------------------------------------ #
    # Inbound                                                            #
    # ------------------------------------------------------------------ #

    async def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        if not chat_id:
            return
        sender = message.get("from") or {}
        text = message.get("text")

        if not self._paired_chat_id:
            await self._maybe_pair(chat_id, sender, text)
            return
        if chat_id != str(self._paired_chat_id):
            # Mittente estraneo: silenzio totale, nessun oracle sull'esistenza
            # del bot o dello stato di pairing.
            logger.info("Telegram: ignoring message from unpaired chat {}", chat_id)
            return
        if not isinstance(text, str) or not text.strip():
            if await self._maybe_handle_location(chat_id, sender, message):
                return
            if any(key in message for key in _MEDIA_KEYS):
                await self._send_raw(chat_id, self._t("media_soon"))
            return
        if self._parse_start(text) is not None:
            # /start dal proprietario: guida rapida di servizio, non un turno
            # LLM (e niente rumore nella vista WebUI). /new e /stop invece
            # proseguono verso il command router.
            await self._send_raw(chat_id, self._t("welcome"))
            return

        # Turn-id per correlare le righe della vista WebUI (user echo, finale,
        # turn_end) allo stesso turno: stesso ruolo del turn-id dei client WS.
        # Avvia l'indicatore "sta scrivendo..." non appena parte il turno
        # dell'utente: Telegram e receive-final-only, quindi send() riceve
        # solo la risposta finale e non puo accendere il typing prima.
        # Lo stop avviene in send() alla consegna del messaggio finale.
        self._start_typing()
        # Messaggio di stato "thinking" GENERICO: compare subito, senza
        # attendere lo streaming del reasoning (che su Telegram non arriva).
        # Se il modello emette reasoning, send() lo sovrascrivera con il
        # testo reale; se no, resta questo finche' non arriva la risposta.
        # I comandi (/new, /stop, ...) non sono turni di pensiero: saltati.
        if not text.startswith("/"):
            await self._update_status(
                chat_id, f"{_STATUS_PREFIX_THINKING} {_STATUS_THINKING_DEFAULT}"
            )
        metadata: dict[str, Any] = {WEBUI_TURN_METADATA_KEY: str(uuid.uuid4())}
        await self.bus.publish_inbound(
            InboundMessage(
                channel=self.name,
                sender_id=str(sender.get("id", chat_id)),
                chat_id=chat_id,
                content=text,
                metadata=metadata,
            )
        )

    async def _maybe_handle_location(
        self, chat_id: str, sender: dict[str, Any], message: dict[str, Any]
    ) -> bool:
        """Gestisce una posizione condivisa via Telegram (``location``/``venue``).

        La registra come override per-canale (usata solo dalle risposte
        Telegram entro il TTL) e innesca un turno LLM così Jenny reagisce, con
        la posizione già iniettata nel runtime context. Ritorna ``False`` se il
        messaggio non è una posizione o se il toggle posizione è off — in quel
        caso il chiamante ricade sulla fallback "media_soon".
        """
        raw_loc = message.get("location")
        venue = message.get("venue") if isinstance(message.get("venue"), dict) else None
        if venue and isinstance(venue.get("location"), dict):
            raw_loc = venue["location"]
        if not isinstance(raw_loc, dict):
            return False
        try:
            lat = float(raw_loc["latitude"])
            lng = float(raw_loc["longitude"])
        except (KeyError, TypeError, ValueError):
            return False

        # Toggle posizione: caricato lazy (le condivisioni sono rare). Se off,
        # non registriamo nulla e lasciamo rispondere la fallback media_soon.
        try:
            from jenny.config.loader import load_config

            cfg = load_config().tools.location
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).debug("Telegram: could not load location config")
            cfg = None
        if cfg is not None and not getattr(cfg, "enable", True):
            return False

        from dataclasses import replace

        from jenny.runtime.location import build_telegram_fix, record_telegram_location

        fix = await build_telegram_fix(cfg, lat, lng)
        # Un venue porta già un nome/indirizzo leggibile: preferiamolo al
        # reverse-geocoding delle coordinate.
        if venue:
            label = ", ".join(
                str(x) for x in (venue.get("title"), venue.get("address")) if x
            )
            if label:
                fix = replace(fix, place=label)
        record_telegram_location(chat_id, fix)

        # Stesso avvio del typing indicator di un turno testuale (vedi sopra).
        self._start_typing()
        # Messaggio di stato "thinking" generico anche per gli share di
        # posizione (non sono comandi).
        await self._update_status(
            chat_id, f"{_STATUS_PREFIX_THINKING} {_STATUS_THINKING_DEFAULT}"
        )
        metadata: dict[str, Any] = {WEBUI_TURN_METADATA_KEY: str(uuid.uuid4())}
        await self.bus.publish_inbound(
            InboundMessage(
                channel=self.name,
                sender_id=str(sender.get("id", chat_id)),
                chat_id=chat_id,
                content=_LOCATION_TURN_MARKER,
                metadata=metadata,
            )
        )
        return True

    @staticmethod
    def _parse_start(text: str) -> str | None:
        """Riconosce un comando ``/start``; ritorna il payload (anche vuoto).

        Case-insensitive sul comando, con l'eventuale suffisso ``@botusername``
        rimosso. Ritorna ``None`` se il testo non è un /start.
        """
        parts = text.strip().split(maxsplit=1)
        if not parts:
            return None
        command = parts[0].split("@", 1)[0]
        if command.lower() != "/start":
            return None
        return parts[1].strip() if len(parts) > 1 else ""

    async def _maybe_pair(self, chat_id: str, sender: dict[str, Any], text: Any) -> None:
        """Onboarding in finestra di pairing: solo il codice esatto accoppia.

        Senza ``pairing_code`` attivo il bot resta muto (regola no-oracle,
        vedi ``.agent/security.md``). In finestra risponde con prompt/feedback
        entro un budget per chat; una chat oltre il cap (o oltre il bound del
        dict, fail-closed) è ineleggibile al pairing anche col codice giusto.
        """
        if not self._pairing_code or not isinstance(text, str):
            return

        start_payload = self._parse_start(text)
        candidate = start_payload if start_payload is not None else text.strip()

        # Eleggibilità PRIMA del confronto col codice: è il blocco del
        # pairing, non solo del feedback, a fermare il brute-force.
        attempts = self._pair_attempts.get(chat_id, 0)
        if attempts >= _MAX_PAIR_ATTEMPTS:
            logger.info("Telegram: chat {} exceeded pairing attempts, ignoring", chat_id)
            return
        if chat_id not in self._pair_attempts and len(self._pair_attempts) >= _MAX_TRACKED_CHATS:
            logger.warning("Telegram: pairing attempt table full, ignoring chat {}", chat_id)
            return

        if candidate and hmac.compare_digest(candidate, self._pairing_code):
            username = sender.get("username")
            self._paired_chat_id = chat_id
            self._pairing_code = None
            self._pair_attempts.clear()
            if self._on_paired is not None:
                try:
                    await self._on_paired(
                        chat_id, username if isinstance(username, str) else None
                    )
                except Exception:
                    logger.exception("Telegram: on_paired callback failed")
            logger.info("Telegram: paired with chat {}", chat_id)
            await self._send_raw(
                chat_id, self._t("paired") + "\n\n" + self._t("welcome")
            )
            return

        self._pair_attempts[chat_id] = attempts + 1
        if start_payload == "":
            # /start nudo: è l'inizio dell'onboarding, chiedi il codice.
            logger.info("Telegram: /start during pairing window from chat {}", chat_id)
            await self._send_raw(chat_id, self._t("start_prompt"))
        else:
            logger.info("Telegram: pairing attempt with wrong code from chat {}", chat_id)
            await self._send_raw(chat_id, self._t("wrong_code"))

    # ------------------------------------------------------------------ #
    # Outbound                                                           #
    # ------------------------------------------------------------------ #

    async def send(
        self,
        msg: OutboundMessage,
        *,
        only_conns: list[Any] | None = None,
        skip_persist: bool = False,
    ) -> list[Any]:
        """Consegna un messaggio finale al chat accoppiato.

        Ritorna sempre ``[]``: non esiste fan-out parziale su Telegram.
        Gli eventi di solo coordinamento WebUI vengono ignorati, eccetto i
        progress events che vengono usati per aggiornare il messaggio di
        stato durante il turno (streaming indicator).
        """
        meta = msg.metadata or {}
        chat_id = str(self._paired_chat_id) if self._paired_chat_id else None

        # ── Streaming status indicator ─────────────────────────────────
        # I progress events vengono intercettati PRIMA del filtro webui-only
        # e convertiti in aggiornamenti al messaggio di stato temporaneo.
        if meta.get("_progress") and chat_id:
            content = (msg.content or "").strip()
            if content:
                # Aggiunge un prefisso emoji in base al tipo di evento.
                if meta.get("_tool_hint"):
                    display = f"{_STATUS_PREFIX_TOOL} {content}"
                else:
                    display = f"{_STATUS_PREFIX_THINKING} {content}"
                await self._update_status(chat_id, display)
            return []

        # ── Turn end: pulisci lo status ────────────────────────────────
        if meta.get("_turn_end"):
            await self._clear_status()
            return []

        # ── Filtro eventi webui-only ───────────────────────────────────
        if self._is_webui_only_event(meta):
            return []
        if not self._paired_chat_id:
            logger.warning("Telegram: dropping outbound, no paired chat")
            return []

        # ── Typing indicator ───────────────────────────────────────────
        # Avvio automatico all'arrivo del primo messaggio di un nuovo turno;
        # stop al messaggio finale di quello stesso turno.
        turn_id = meta.get(WEBUI_TURN_METADATA_KEY)
        is_new_turn = (
            isinstance(turn_id, str)
            and turn_id
            and turn_id != self._last_turn_id
        )
        if is_new_turn:
            self._last_turn_id = turn_id
            # Pulisci eventuale stato residuo di un turno precedente.
            await self._clear_status()
            self._start_typing()
        content = msg.content or ""
        media = [m for m in (msg.media or []) if isinstance(m, str) and m.strip()]
        if not content.strip() and not media:
            await self._clear_status()
            self._stop_typing()
            return []

        # ── Pulisci lo stato PRIMA di inviare il messaggio finale ────────
        # L'utente deve vedere: [status] → [risposta finale].
        # Il message order di Telegram garantisce l'ordinamento corretto.
        await self._clear_status()

        chat_id = str(self._paired_chat_id)
        if content.strip():
            for chunk in split_message(content, _RAW_CHUNK_LIMIT):
                await self._send_chunk(chat_id, chunk)
        # Gli allegati seguono il testo, ciascuno come foto (raster) o documento
        # (SVG e formati non-foto). Un media non inviabile viene loggato e
        # saltato, senza abbattere la consegna del resto.
        for item in media:
            await self._send_media_item(chat_id, item)
        self._stop_typing()
        return []

    async def _send_media_item(self, chat_id: str, path: str) -> None:
        """Invia un singolo allegato come foto o documento, best-effort."""
        is_url = path.startswith(("http://", "https://"))
        ext = Path(urlparse(path).path if is_url else path).suffix.lower()
        as_photo = ext in _RASTER_EXTS
        method = "sendPhoto" if as_photo else "sendDocument"
        field = "photo" if as_photo else "document"
        try:
            if is_url:
                await self.api.send_media_url(
                    chat_id, method=method, field=field, url=path
                )
                return
            p = Path(path)
            if not p.is_file():
                logger.warning("Telegram: media not found, skipping: {}", path)
                return
            data = await asyncio.to_thread(p.read_bytes)
            if len(data) > _TG_MEDIA_MAX_BYTES:
                logger.warning(
                    "Telegram: media too large ({} bytes), skipping: {}", len(data), path
                )
                return
            try:
                await self.api.send_media_file(
                    chat_id, method=method, field=field, filename=p.name, data=data
                )
            except TelegramAPIError as e:
                # Un raster rifiutato dall'elaborazione foto (400) passa come
                # documento: meglio consegnare il file che perderlo.
                if as_photo and e.status_code == 400:
                    await self.api.send_media_file(
                        chat_id, method="sendDocument", field="document",
                        filename=p.name, data=data,
                    )
                else:
                    raise
        except Exception as e:
            logger.error("Telegram: media send failed for {}: {}", path, type(e).__name__)

    @staticmethod
    def _is_webui_only_event(meta: dict[str, Any]) -> bool:
        return any(meta.get(key) for key in COORDINATION_FLAGS)

    async def _send_chunk(self, chat_id: str, chunk: str) -> None:
        """Invia un chunk con retry interno; HTML → fallback plain su 400."""
        last_error: Exception | None = None
        for attempt, delay in enumerate((0, *_CHUNK_RETRY_DELAYS)):
            if delay:
                await asyncio.sleep(delay)
            try:
                try:
                    await self.api.send_message(
                        chat_id, markdown_to_telegram_html(chunk), parse_mode="HTML"
                    )
                except TelegramAPIError as e:
                    if e.status_code != 400:
                        raise
                    # HTML rifiutato (tag spezzati da chunking o markdown
                    # inatteso): il testo grezzo passa sempre.
                    await self.api.send_message(chat_id, chunk)
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_error = e
                logger.warning(
                    "Telegram send failed (attempt {}): {}", attempt + 1, type(e).__name__
                )
        logger.error("Telegram: giving up on chunk after retries: {}", last_error)

    async def _send_raw(self, chat_id: str, text: str) -> None:
        """Risposta di servizio (pairing/media): best-effort, senza transcript."""
        try:
            await self.api.send_message(chat_id, text)
        except Exception:
            logger.exception("Telegram: service reply failed")

    # ------------------------------------------------------------------ #
    # Typing indicator ("Jenny sta scrivendo...")                       #
    # ------------------------------------------------------------------ #

    def _start_typing(self) -> None:
        """Avvia l'invio periodico di ``sendChatAction("typing")``.

        Ogni chiamata a ``_start_typing`` con un turno nuovo sostituisce il
        task precedente tramite il token di cancellazione: il loop vecchio
        si accorge che il suo token è cambiato e si ferma.
        """
        if not self._paired_chat_id:
            return
        self._stop_typing()  # cancella un eventuale task di un turno precedente
        chat_id = str(self._paired_chat_id)
        token = object()
        self._typing_token = token

        async def _typing_loop() -> None:
            start = time.monotonic()
            try:
                while self._typing_token is token:
                    await self.api.send_chat_action(chat_id, "typing")
                    await asyncio.sleep(_TYPING_INTERVAL_S)
                    if time.monotonic() - start > _TYPING_MAX_DURATION_S:
                        # Rete morta / turno senza risposta finale: spegni
                        # il typing invece di tenerlo acceso all'infinito.
                        self._typing_token = object()
                        break
            except asyncio.CancelledError:
                pass

        self._typing_task = asyncio.create_task(_typing_loop())

    def _stop_typing(self) -> None:
        """Ferma il task periodico di typing indicator.

        Cambia il token di cancellazione e cancella il task.  Il task
        corrente si accorge che il token è diverso e si ferma da solo,
        quindi non c'è bisogno di ``await`` (il task è fire-and-forget).
        """
        self._typing_token = object()

    # ------------------------------------------------------------------ #
    # Streaming status indicator                                         #
    # ------------------------------------------------------------------ #

    async def _update_status(self, chat_id: str, text: str) -> None:
        """Aggiorna (o crea) il messaggio di stato durante il turno.

        Il messaggio è temporaneo: viene editato ad ogni progress event e
        cancellato quando il turno termina.  ``_STATUS_UPDATE_INTERVAL_S``
        protegge dalla rate-limit di Telegram (1 edit/chat/s).
        """
        now = time.monotonic()
        if now - self._status_last_update < _STATUS_UPDATE_INTERVAL_S:
            return
        self._status_last_update = now
        if not text or not text.strip():
            return
        # Tronca a 100 caratteri: Telegram ha un limite a 4096, ma un
        # messaggio di stato non deve mai essere lungo.
        display = text.strip()
        if len(display) > 100:
            display = display[:97] + "…"
        try:
            if self._status_msg_id is not None:
                ok = await self.api.edit_message_text(
                    self._status_msg_id, chat_id, display
                )
                if not ok:
                    # Il messaggio è stato cancellato dall'utente o è
                    # diventato invalido: invia uno nuovo.
                    self._status_msg_id = None
            if self._status_msg_id is None:
                result = await self.api.send_message(chat_id, display)
                # _call() ritorna il campo "result" della response Telegram,
                # che contiene message_id.
                if isinstance(result, dict) and "message_id" in result:
                    self._status_msg_id = result["message_id"]
        except Exception:
            logger.debug("Telegram: status message update failed", exc_info=True)

    async def _clear_status(self) -> None:
        """Cancella il messaggio di stato al termine del turno.

        Il fallimento è non-fatale: il messaggio resta visibile ma non
        blocca la consegna del messaggio finale.
        """
        if self._status_msg_id is None:
            return
        chat_id = str(self._paired_chat_id)
        msg_id = self._status_msg_id
        self._status_msg_id = None
        self._status_last_update = 0.0
        try:
            await self.api.delete_message(msg_id, chat_id)
        except Exception:
            logger.debug("Telegram: status message delete failed", exc_info=True)
        if self._typing_task is not None and not self._typing_task.done():
            self._typing_task.cancel()
        self._typing_task = None

    # ------------------------------------------------------------------ #
    # No-op per il contratto dispatcher (nessuno streaming su Telegram)  #
    # ------------------------------------------------------------------ #

    async def send_delta(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def send_reasoning_delta(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def send_reasoning_end(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def send_file_edit_events(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    def discard_stream_buffer(self, *args: Any, **kwargs: Any) -> None:
        return None
