"""Handler degli stati FSM del turno per ``AgentLoop``.

`StateHandlersMixin` raccoglie i metodi ``_state_*`` (RESTORE→COMPACT→COMMAND→
BUILD→RUN→SAVE→RESPOND) più i due helper di media/documenti che alimentano lo
stato BUILD. Mixato in ``AgentLoop``: il driver FSM li risolve via
``getattr(self, f"_state_{name}")`` attraverso l'MRO, comportamento identico.
Non contiene logica di scheduling/concorrenza: quella resta in ``loop.py``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from functools import partial
from typing import TYPE_CHECKING, Any

from loguru import logger

from jenny.agent.tools.message import MessageTool
from jenny.agent.turn_types import TurnState
from jenny.bus.progress import build_silent_progress_callback
from jenny.command import CommandContext
from jenny.session import turn_continuation
from jenny.session.goal_state import note_goal_turn
from jenny.utils.document import extract_documents, prepare_attachments
from jenny.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE

if TYPE_CHECKING:
    import asyncio
    from typing import Awaitable, Callable

    from jenny.agent.autocompact import AutoCompact
    from jenny.agent.context import ContextBuilder
    from jenny.agent.memory import Consolidator
    from jenny.agent.tools.app_actions import AppToolsSyncer
    from jenny.agent.tools.registry import ToolRegistry
    from jenny.agent.turn_epochs import TurnToken
    from jenny.agent.turn_types import TurnContext
    from jenny.bus.events import InboundMessage, OutboundMessage
    from jenny.bus.queue import MessageBus
    from jenny.bus.runtime_events import RuntimeEventPublisher
    from jenny.command import CommandRouter
    from jenny.security.workspace_access import WorkspaceScopeResolver
    from jenny.session.manager import Session, SessionManager
    from jenny.utils.llm_runtime import LLMRuntime


class StateHandlersMixin:
    """Handler ``_state_*`` del turno (mixin di AgentLoop)."""

    if TYPE_CHECKING:
        # Contratto host↔mixin (solo per il type-checker; nessun effetto a
        # runtime). Attributi forniti da ``AgentLoop.__init__`` e usati qui.
        _app_tools_syncer: AppToolsSyncer
        _max_messages: int
        auto_compact: AutoCompact
        bus: MessageBus
        commands: CommandRouter
        consolidator: Consolidator
        context: ContextBuilder
        extract_document_text: bool
        sessions: SessionManager
        tools: ToolRegistry
        workspace_scopes: WorkspaceScopeResolver

        # Metodi dell'host (loop.py) invocati da questi handler.
        def _assemble_outbound(
            self,
            msg: InboundMessage,
            final_content: str,
            all_msgs: list[dict[str, Any]],
            stop_reason: str,
            had_injections: bool,
            on_stream: Callable[[str], Awaitable[None]] | None,
            *,
            turn_latency_ms: int | None = None,
        ) -> OutboundMessage | None: ...
        async def _build_bus_progress_callback(
            self, msg: InboundMessage
        ) -> Callable[..., Awaitable[None]]: ...
        def _build_initial_messages(
            self,
            msg: InboundMessage,
            session: Session,
            history: list[dict[str, Any]],
            pending_summary: str | None,
            include_memory_recent_history: bool = True,
            tools: ToolRegistry | None = None,
        ) -> list[dict[str, Any]]: ...
        async def _build_retry_wait_callback(
            self, msg: InboundMessage
        ) -> Callable[[str], Awaitable[None]]: ...
        def _persist_user_message_early(
            self, msg: InboundMessage, session: Session, **kwargs: Any
        ) -> bool: ...
        def _replay_token_budget(self) -> int: ...
        async def _run_agent_loop(
            self,
            initial_messages: list[dict],
            on_progress: Callable[..., Awaitable[None]] | None = None,
            on_stream: Callable[[str], Awaitable[None]] | None = None,
            on_stream_end: Callable[..., Awaitable[None]] | None = None,
            on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
            *,
            session: Session | None = None,
            channel: str = ...,
            chat_id: str = "direct",
            message_id: str | None = None,
            metadata: dict[str, Any] | None = None,
            session_key: str | None = None,
            pending_queue: asyncio.Queue | None = None,
            ephemeral: bool = False,
            tools: ToolRegistry | None = None,
            turn_token: TurnToken | None = None,
        ) -> tuple[str | None, list[str], list[dict], str, bool]: ...
        def _runtime_events(self) -> RuntimeEventPublisher: ...
        def _set_tool_context(
            self,
            channel: str,
            chat_id: str,
            message_id: str | None = None,
            metadata: dict | None = None,
            session_key: str | None = None,
        ) -> None: ...
        async def llm_runtime(self) -> LLMRuntime: ...

        # Metodi forniti dagli altri mixin (TurnPersistence / LoopTasks).
        def _clear_pending_user_turn(self, session: Session) -> None: ...
        def _clear_runtime_checkpoint(self, session: Session) -> None: ...
        def _restore_pending_user_turn(self, session: Session) -> bool: ...
        def _restore_runtime_checkpoint(self, session: Session) -> bool: ...
        def _save_turn(
            self,
            session: Session,
            messages: list[dict],
            skip: int,
            *,
            turn_latency_ms: int | None = None,
        ) -> None: ...
        def _schedule_background(self, coro) -> None: ...

    async def _state_restore(self, ctx: TurnContext) -> TurnState:
        """Restore checkpoint / pending user turn; extract documents."""
        msg = ctx.msg

        if msg.media:
            new_content, image_only = self._prepare_message_media(msg.content, msg.media)
            ctx.msg = dataclasses.replace(msg, content=new_content, media=image_only)
            msg = ctx.msg

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        # Session is already fetched by the caller (_process_message) but
        # ensure it exists in case this handler is invoked independently.
        if ctx.session is None:
            ctx.session = self.sessions.get_or_create(ctx.session_key)
        await self._runtime_events().session_turn_started(msg, ctx.session_key)

        if self._restore_runtime_checkpoint(ctx.session):
            self.sessions.save(ctx.session)
        if self._restore_pending_user_turn(ctx.session):
            self.sessions.save(ctx.session)

        return "ok"

    def _prepare_message_media(self, content: str, media: list[str]) -> tuple[str, list[str]]:
        # ``extract_document_text=True`` forza l'estrazione totale (legacy, senza
        # cap). Il default (False) usa l'estrazione ibrida: inline dei documenti
        # brevi, riferimento per path del resto — così l'agente considera gli
        # allegati testuali senza inlinare blob enormi ogni turno.
        if self._should_extract_document_text():
            return extract_documents(content, media)
        return prepare_attachments(content, media)

    def _should_extract_document_text(self) -> bool:
        return self.extract_document_text

    async def _state_compact(self, ctx: TurnContext) -> str:
        ctx.session, pending = self.auto_compact.prepare_session(ctx.session, ctx.session_key)
        ctx.pending_summary = pending
        return "ok"

    async def _state_command(self, ctx: TurnContext) -> str:
        raw = ctx.msg.content.strip()
        cmd_ctx = CommandContext(
            msg=ctx.msg, session=ctx.session, key=ctx.session_key, raw=raw, loop=self
        )
        result = await self.commands.dispatch(cmd_ctx)
        if result is not None:
            ctx.outbound = result
            # Shortcut commands skip BUILD and SAVE, so we must persist the
            # turn here so WebUI history hydration after _turn_end sees the
            # message.  Mark messages with _command so get_history can filter
            # them out of LLM context.  /new is excluded because it
            # intentionally clears the session.
            if raw.lower() != "/new":
                ctx.user_persisted_early = self._persist_user_message_early(
                    ctx.msg, ctx.session, _command=True
                )
                ctx.session.add_message(
                    "assistant", result.content, _command=True
                )
                self.sessions.save(ctx.session)
                self._clear_pending_user_turn(ctx.session)
            return "shortcut"
        return "dispatch"

    async def _sync_apps_and_notify(self) -> None:
        """Sync app tools and notify WebUI if the app list changed."""
        _app_tools, apps_changed = self._app_tools_syncer.sync(self.tools)
        if apps_changed:
            from jenny.bus.events import OutboundMessage

            await self.bus.publish_outbound(
                OutboundMessage(
                    channel="websocket",
                    chat_id="webui",
                    content="",
                    metadata={"_apps_list_changed": True},
                )
            )

    async def _begin_turn_tooling(
        self,
        channel: str,
        chat_id: str,
        message_id: str | None,
        metadata: dict | None,
        session_key: str,
    ) -> None:
        """Prelude di tooling condiviso da BUILD (FSM) e dal path di sistema.

        Sincronizza i tool delle app (prima che il runner legga le definizioni),
        imposta il contesto tool e azzera lo stato per-turno del ``MessageTool``.
        Vive in un unico posto così i due path non possono divergere.
        """
        await self._sync_apps_and_notify()
        self._set_tool_context(
            channel, chat_id, message_id, metadata, session_key=session_key,
        )
        if (message_tool := self.tools.get("message")) and isinstance(message_tool, MessageTool):
            message_tool.start_turn()

    def _finalize_turn_save(
        self,
        session: Session,
        all_messages: list[dict[str, Any]],
        save_skip: int,
        *,
        turn_latency_ms: int,
        session_key: str,
        ephemeral: bool = False,
        clear_pending: bool = True,
    ) -> None:
        """Persistenza di fine turno condivisa da SAVE (FSM) e path di sistema.

        Scrive il turno, registra la latenza, applica file-cap + consolidazione
        (salvo turni effimeri), pulisce checkpoint (ed eventuale pending) e —
        punto critico — chiama ``note_goal_turn`` prima del salvataggio così un
        goal sostenuto mantiene ``last_turn_at`` fresco anche quando il turno è
        innescato da un subagent, evitando l'expire prematuro.
        """
        self._save_turn(
            session, all_messages, save_skip, turn_latency_ms=turn_latency_ms,
        )
        self._runtime_events().record_turn_latency(session_key, turn_latency_ms)
        if not ephemeral:
            session.enforce_file_cap(
                on_archive=partial(self.context.memory.raw_archive, session_key=session_key)
            )
            self._schedule_background(
                self.consolidator.maybe_consolidate_by_tokens(
                    session,
                    replay_max_messages=self._max_messages,
                )
            )
        if clear_pending:
            self._clear_pending_user_turn(session)
        self._clear_runtime_checkpoint(session)
        note_goal_turn(session.metadata)
        self.sessions.save(session)
        # Event-based Dream trigger: incrementa il contatore e, se necessario,
        # pianifica un run Dream. Solo per turni non effimeri (cron, heartbeat,
        # dream non devono incrementare il contatore).
        if not ephemeral:
            self._maybe_trigger_event_dream()

    def _maybe_trigger_event_dream(self) -> None:
        """Incrementa il contatore turni e, se la soglia è raggiunta, pianifica
        un run Dream event-driven.

        Il trigger è event-based (turn-count) come integrazione al trigger
        wall-clock (ogni 2h). L'intervallo wall-clock resta il fallback per
        sessioni molto lunghe. La deduplicazione è garantita da
        ``dream_lock.try_acquire_dream_lock``: se un run Dream è già in corso,
        il trigger event-based viene saltato.
        """
        from jenny.config.runtime_env import dream_turn_threshold
        from jenny.runtime.dream_lock import dream_lock_locked

        threshold = dream_turn_threshold()
        if threshold <= 0:
            return  # event-based trigger disabilitato
        current = self.context.memory.increment_turn_counter()
        if current < threshold:
            return  # soglia non raggiunta
        # Soglia raggiunta: pianifica un run Dream se non ce n'è già uno in
        # corso. Resetta il contatore per il prossimo ciclo.
        if dream_lock_locked():
            logger.debug(
                "Event-based Dream trigger: threshold reached ({}) but a Dream "
                "run is already in progress; skipping",
                current,
            )
            return
        logger.info(
            "Event-based Dream trigger: {} turns since last Dream, "
            "scheduling consolidation",
            current,
        )
        self.context.memory.reset_turn_counter()
        self._schedule_background(self._trigger_event_dream())

    async def _trigger_event_dream(self) -> None:
        """Esegue un Dream run event-driven (chiamato come background task)."""
        from jenny.runtime.dream_lock import (
            release_dream_lock,
            try_acquire_dream_lock,
        )

        if not await try_acquire_dream_lock():
            logger.debug("Event-based Dream: another Dream run is already in progress")
            return
        try:
            from jenny.agent.memory import MemoryStore

            store = self.context.memory
            result = await asyncio.to_thread(store.build_dream_prompt)
            if result is None:
                logger.info("Event-based Dream: nothing to process")
                return
            prompt, last_cursor = result
            key = MemoryStore.dream_session_key()
            dream_tools = store.build_dream_tools()
            resp = await self.process_direct(
                prompt,
                session_key=key,
                ephemeral=True,
                tools=dream_tools,
            )
            dream_file_states = getattr(dream_tools, "file_states", None)
            if MemoryStore.dream_should_advance_cursor(resp, dream_file_states):
                store.set_last_dream_cursor(last_cursor)
                logger.info("Event-based Dream completed, cursor advanced to {}", last_cursor)
            elif MemoryStore.dream_run_completed(resp):
                logger.warning(
                    "Event-based Dream completed without writing; cursor remains at {}",
                    store.get_last_dream_cursor(),
                )
            else:
                logger.warning(
                    "Event-based Dream did not complete; cursor remains at {}",
                    store.get_last_dream_cursor(),
                )
        except Exception:
            logger.exception("Event-based Dream failed")
        finally:
            release_dream_lock()
            await asyncio.to_thread(store.compact_history)

    async def _state_build(self, ctx: TurnContext) -> str:
        if not ctx.ephemeral:
            await self.consolidator.maybe_consolidate_by_tokens(
                ctx.session,
                replay_max_messages=self._max_messages,
            )
        # Pick up app manifest changes before the runner reads tool definitions
        # (mirror of the per-turn skills rescan; cheap stat-only when unchanged).
        await self._begin_turn_tooling(
            ctx.msg.channel,
            ctx.msg.chat_id,
            ctx.msg.metadata.get("message_id"),
            ctx.msg.metadata,
            ctx.session_key,
        )

        _hist_kwargs: dict[str, Any] = {
            "max_messages": self._max_messages,
            "max_tokens": self._replay_token_budget(),
            "include_timestamps": True,
            "extend_to_user": False,
        }
        ctx.history = ctx.session.get_history(**_hist_kwargs)
        self._runtime_events().record_turn_runtime(
            ctx.session_key,
            await self.llm_runtime(),
        )

        ctx.initial_messages = self._build_initial_messages(
            ctx.msg,
            ctx.session,
            ctx.history,
            ctx.pending_summary,
            include_memory_recent_history=not ctx.ephemeral,
            # Il registry del turno, non quello del loop: e cio che il runner
            # ricevera, quindi e cio che il prompt deve dichiarare.
            tools=ctx.tools,
        )
        ctx.user_persisted_early = self._persist_user_message_early(
            ctx.msg, ctx.session
        )

        # Un turno silenzioso non installa i callback che pubblicano sul bus:
        # progress e retry-wait sono indirizzati alla chat d'origine e vengono
        # persistiti nel transcript, quindi da soli basterebbero a riempire la
        # conversazione di righe di un lavoro che l'utente non ha chiesto.
        if ctx.silent:
            if ctx.on_progress is None:
                ctx.on_progress = build_silent_progress_callback()
            if ctx.on_retry_wait is None:
                ctx.on_retry_wait = build_silent_progress_callback()
            return "ok"

        if ctx.on_progress is None:
            ctx.on_progress = await self._build_bus_progress_callback(ctx.msg)
        if ctx.on_retry_wait is None:
            ctx.on_retry_wait = await self._build_retry_wait_callback(ctx.msg)

        return "ok"

    async def _state_run(self, ctx: TurnContext) -> str:
        if ctx.visible_run_started_at is None:
            ctx.visible_run_started_at = time.time()
        await self._runtime_events().run_status_changed(
            ctx.msg,
            ctx.session_key,
            "running",
            started_at=ctx.visible_run_started_at,
        )
        result = await self._run_agent_loop(
            ctx.initial_messages,
            on_progress=ctx.on_progress,
            on_stream=ctx.on_stream,
            on_stream_end=ctx.on_stream_end,
            on_retry_wait=ctx.on_retry_wait,
            session=ctx.session,
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            message_id=ctx.msg.metadata.get("message_id"),
            metadata=ctx.msg.metadata,
            session_key=ctx.session_key,
            pending_queue=ctx.pending_queue,
            ephemeral=ctx.ephemeral,
            tools=ctx.tools,
            turn_token=ctx.turn_token,
        )
        final_content, tools_used, all_msgs, stop_reason, had_injections = result
        ctx.final_content = final_content
        ctx.tools_used = tools_used
        ctx.all_messages = all_msgs
        ctx.stop_reason = stop_reason
        ctx.had_injections = had_injections
        # Re-sync app tools after execution: tools may have created/deleted
        # apps (e.g. python_exec + shutil.rmtree) during this turn.
        await self._sync_apps_and_notify()
        await turn_continuation.maybe_continue_turn(ctx)
        return "ok"

    async def _state_save(self, ctx: TurnContext) -> str:
        turn_continuation.prepare_save_boundary(ctx)

        if (
            (ctx.final_content is None or not ctx.final_content.strip())
            and not ctx.suppress_response
        ):
            ctx.final_content = EMPTY_FINAL_RESPONSE_MESSAGE

        latency_started_at = (
            ctx.visible_run_started_at
            if turn_continuation.internal_continuation_inbound(ctx.msg.metadata)
            and ctx.visible_run_started_at is not None
            else ctx.turn_wall_started_at
        )
        ctx.turn_latency_ms = max(0, int((time.time() - latency_started_at) * 1000))
        self._finalize_turn_save(
            ctx.session,
            ctx.all_messages,
            ctx.save_skip,
            turn_latency_ms=ctx.turn_latency_ms,
            session_key=ctx.session_key,
            ephemeral=ctx.ephemeral,
        )
        return "ok"

    async def _state_respond(self, ctx: TurnContext) -> str:
        # "Ha parlato" si legge dal MessageTool: una consegna verso il target
        # d'origine in questo turno. Il registry del turno viene prima di quello
        # di default — il flag ``_sent_in_turn`` sta in una ContextVar *per
        # istanza*, quindi con un registry sostituito (l'idioma di Dream/Atlas)
        # leggere l'istanza di default darebbe sempre ``False`` e un turno
        # silenzioso che ha parlato risulterebbe muto. Calcolato sempre, non solo
        # per i monitor: e' cio da cui ``_process_message`` costruisce il
        # ``TurnOutcome``, e prima viaggiava contrabbandato nei metadata inbound.
        message_tool = (ctx.tools or self.tools).get("message")
        ctx.spoke_via_tool = bool(
            isinstance(message_tool, MessageTool) and message_tool._sent_in_turn
        )
        if ctx.suppress_response:
            ctx.outbound = None
            return "ok"
        ctx.outbound = self._assemble_outbound(
            ctx.msg,
            ctx.final_content,
            ctx.all_messages,
            ctx.stop_reason,
            ctx.had_injections,
            ctx.on_stream,
            turn_latency_ms=ctx.turn_latency_ms,
        )
        if ctx.ephemeral and ctx.outbound is not None:
            ctx.outbound.metadata["_stop_reason"] = ctx.stop_reason
        return "ok"
