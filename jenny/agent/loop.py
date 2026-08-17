"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import time
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from jenny.agent.autocompact import AutoCompact
from jenny.agent.context import ContextBuilder
from jenny.agent.cron_turns import CronTurnCoordinator
from jenny.agent.hook import AgentHook, CompositeHook
from jenny.agent.loop_provider import ProviderPresetMixin
from jenny.agent.loop_tasks import LoopTasksMixin
from jenny.agent.memory import Consolidator
from jenny.agent.progress_hook import AgentProgressHook
from jenny.agent.runner import _MAX_INJECTIONS_PER_TURN, AgentRunner, AgentRunSpec
from jenny.agent.session_locks import SessionLocks
from jenny.agent.subagent import SubagentManager
from jenny.agent.tools.context import (
    RequestContext,
    bind_request_context,
    bind_turn_id,
    current_turn_id,
    reset_request_context,
    reset_turn_id,
)
from jenny.agent.tools.file_state import FileStateStore, bind_file_states, reset_file_states
from jenny.agent.tools.message import MessageTool
from jenny.agent.tools.registry import ToolRegistry
from jenny.agent.tools.self import MyTool
from jenny.agent.turn_epochs import TurnEpochs, TurnToken
from jenny.agent.turn_persistence import TurnPersistenceMixin
from jenny.agent.turn_states import StateHandlersMixin
from jenny.agent.turn_types import (
    StateTraceEntry as StateTraceEntry,
)
from jenny.agent.turn_types import (
    TurnContext,
    TurnOutcome,
    TurnState,
)
from jenny.bus.events import INTERNAL_CHANNEL, InboundMessage, OutboundMessage
from jenny.bus.progress import build_bus_progress_callback
from jenny.bus.queue import MessageBus
from jenny.bus.runtime_events import (
    RuntimeEventBus,
    RuntimeEventPublisher,
)
from jenny.command import CommandContext, CommandRouter, register_builtin_commands
from jenny.config.schema import AgentDefaults
from jenny.cron.session_turns import (
    cron_history_overrides,
)
from jenny.providers.base import LLMProvider
from jenny.runtime.context import get_android_context
from jenny.runtime.location import location_runtime_line
from jenny.runtime.power import keep_awake
from jenny.security.workspace_access import (
    WorkspaceScopeResolver,
    bind_workspace_scope,
    reset_workspace_scope,
)
from jenny.session import turn_continuation
from jenny.session.goal_state import (
    clear_goal_awaiting_input,
    expire_stale_goal,
    goal_awaiting_input,
    goal_state_runtime_lines,
    mark_goal_awaiting_input,
    runner_wall_llm_timeout_s,
    sustained_goal_active,
)
from jenny.session.keys import UNIFIED_SESSION_KEY, session_key_for_channel
from jenny.session.manager import Session, SessionManager
from jenny.session.turn_visibility import (
    TurnVisibility,
    is_silent_turn,
    mark_silent_turn,
    resolve_turn_visibility,
)
from jenny.utils.helpers import CONTEXT_BUDGET_SAFETY_BUFFER, reserved_output_tokens
from jenny.utils.llm_runtime import LLMRuntime

if TYPE_CHECKING:
    from jenny.config.schema import (
        ToolsConfig,
    )
    from jenny.cron.heartbeat_followup import HeartbeatFollowup
    from jenny.cron.service import CronService


def _load_current_tools_config() -> "ToolsConfig":
    """Rilegge ``config.tools`` dal disco, per chi deve sapere com'e *ora*.

    Import ritardato: ``config.loader`` non e importabile a livello di modulo
    da qui senza chiudere un ciclo con lo startup.
    """
    from jenny.config.loader import load_config

    return load_config().tools


def _new_turn_id(session_key: str) -> str:
    """Identita di un turno: session key + istante d'avvio in nanosecondi.

    Unico posto in cui si conia. La forma e quella che compare nei log
    (``unified:default:1785845855643649792``), cosi l'identita che i tool vedono
    in ``RequestContext.turn_id`` e letteralmente il turno tracciato nei log.
    """
    return f"{session_key}:{time.time_ns()}"


# Scadenza del wakelock per-turno. Non e' il timeout del turno: e' la rete di
# sicurezza che l'OS applica se il processo muore prima del `finally` che
# rilascia. Va tenuta abbondantemente sopra la durata di un turno lungo (catena
# di tool, subagent, retry del provider) perche' scadere a meta' turno
# rimetterebbe la CPU a dormire proprio dove serve; e comunque finita, perche'
# un wakelock eterno scarica la batteria senza dare spiegazioni.
_TURN_WAKELOCK_TIMEOUT_S = 1800.0


class AgentLoop(StateHandlersMixin, ProviderPresetMixin, TurnPersistenceMixin, LoopTasksMixin):
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    @property
    def current_iteration(self) -> int:
        return self._current_iteration

    @property
    def tool_names(self) -> list[str]:
        return self.tools.tool_names

    async def llm_runtime(self) -> LLMRuntime:
        """Return the current provider/model pair owned by this loop."""
        return LLMRuntime(self.provider, self.model)

    _RUNTIME_CHECKPOINT_KEY = "runtime_checkpoint"
    _PENDING_USER_TURN_KEY = "pending_user_turn"
    # Session TTL sweeps scan every session file on disk; throttle them well
    # below the 1s bus-poll tick so idle Android battery isn't spent on I/O.
    _TTL_CHECK_INTERVAL_S = 60.0

    # Event-driven state transition table.
    # Handlers return an event string; the driver looks up the next state here.
    _TRANSITIONS: dict[tuple[TurnState, str], TurnState] = {
        (TurnState.RESTORE, "ok"): TurnState.COMPACT,
        (TurnState.COMPACT, "ok"): TurnState.COMMAND,
        (TurnState.COMMAND, "dispatch"): TurnState.BUILD,
        (TurnState.COMMAND, "shortcut"): TurnState.DONE,
        (TurnState.BUILD, "ok"): TurnState.RUN,
        (TurnState.RUN, "ok"): TurnState.SAVE,
        (TurnState.SAVE, "ok"): TurnState.RESPOND,
        (TurnState.RESPOND, "ok"): TurnState.DONE,
    }

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int | None = None,
        max_concurrent_subagents: int | None = None,
        subagent_stall_threshold_seconds: float | None = None,
        subagent_tool_error_budget: int | None = None,
        context_window_tokens: int | None = None,
        context_block_limit: int | None = None,
        max_tool_result_chars: int | None = None,
        provider_retry_mode: str = "standard",
        tool_hint_max_length: int | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        extract_document_text: bool = False,
        session_manager: SessionManager | None = None,
        timezone: str | None = None,
        session_ttl_minutes: int = 0,
        consolidation_ratio: float = 0.5,
        max_messages: int = 120,
        hooks: list[AgentHook] | None = None,
        disabled_skills: list[str] | None = None,
        wiki_directory_max_tokens: int | None = None,
        tools_config: ToolsConfig | None = None,
        runtime_events: RuntimeEventBus | None = None,
        model_presets_config: dict[str, Any] | None = None,
        initial_model_preset: str | None = None,
        ui_query: Any | None = None,
        orchestrator_mode: bool | None = None,
    ):
        from jenny.config.schema import ToolsConfig

        _tc = tools_config or ToolsConfig()
        defaults = AgentDefaults()
        self.bus = bus
        self.runtime_events = runtime_events or RuntimeEventBus()
        self._ui_query = ui_query
        self.runtime_event_publisher = RuntimeEventPublisher(self.runtime_events)
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = (
            max_iterations if max_iterations is not None else defaults.max_tool_iterations
        )
        self.context_window_tokens = (
            context_window_tokens
            if context_window_tokens is not None
            else defaults.context_window_tokens
        )
        self.context_block_limit = context_block_limit
        self.max_tool_result_chars = (
            max_tool_result_chars
            if max_tool_result_chars is not None
            else defaults.max_tool_result_chars
        )
        self.provider_retry_mode = provider_retry_mode
        self.tool_hint_max_length = (
            tool_hint_max_length if tool_hint_max_length is not None
            else defaults.tool_hint_max_length
        )
        # ToolsConfig.restrict_to_workspace è l'unica fonte per il tool-layer
        # (incluso SubagentManager): allinealo al valore risolto dal loop.
        _tc.restrict_to_workspace = restrict_to_workspace
        self.tools_config = _tc
        self.android_web_config = _tc.android_web
        self.exec_config = _tc.python_exec
        self.tool_choice = defaults.tool_choice
        # Scope del registry principale: "orchestrator" (delega, nessun tool che
        # gonfia la sessione) oppure "core" (comportamento storico). Il default
        # viene da AgentDefaults, unica fonte del valore.
        self.orchestrator_mode = (
            orchestrator_mode if orchestrator_mode is not None
            else defaults.orchestrator_mode
        )
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self.extract_document_text = extract_document_text
        self.workspace_scopes = WorkspaceScopeResolver(
            default_workspace=workspace,
            default_restrict_to_workspace=restrict_to_workspace,
        )
        self._start_time = time.time()
        self._last_usage: dict[str, int] = {}
        self._extra_hooks: list[AgentHook] = hooks or []

        self.context = ContextBuilder(
            workspace,
            timezone=timezone,
            disabled_skills=disabled_skills,
            orchestrator=self.orchestrator_mode,
            available_tools=lambda: self.tools.tool_names,
            wiki_directory_max_tokens=wiki_directory_max_tokens,
        )
        self.sessions = session_manager or SessionManager(workspace)
        self.tools = ToolRegistry()
        # One file-read/write tracker per logical session. The tool registry is
        # shared by this loop, so tools resolve the active state via contextvars.
        self._file_state_store = FileStateStore()
        self.runner = AgentRunner(provider)
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            tools_config=_tc,
            # ``_tc`` e la copia presa all'avvio: basta a far partire il
            # manager, non a decidere quali tool esistono *adesso*. Un host SSH
            # aggiunto dalle impostazioni ad app accesa vive solo su disco —
            # ``store.mutate`` scrive il file, non questo oggetto — quindi il
            # prossimo subagent deve rileggerlo.
            tools_config_provider=_load_current_tools_config,
            max_tool_result_chars=self.max_tool_result_chars,
            disabled_skills=disabled_skills,
            max_iterations=self.max_iterations,
            max_concurrent_subagents=max_concurrent_subagents,
            stall_threshold_s=subagent_stall_threshold_seconds,
            tool_error_budget=subagent_tool_error_budget,
            llm_wall_timeout_for_session=lambda sk: runner_wall_llm_timeout_s(self.sessions, sk),
            # LO STESSO SessionManager del loop, non uno nuovo: la storia Tier-2
            # dei subagent (``subagent:<lineage_id>``) vive nella stessa
            # directory delle sessioni, e due istanze avrebbero due cache
            # divergenti sugli stessi file.
            session_manager=self.sessions,
        )
        self._max_messages = max_messages if max_messages > 0 else 120
        self._running = False
        self._last_ttl_check = 0.0
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._background_tasks: list[asyncio.Task] = []
        # Dominio di lock per-sessione UNICO, condiviso col Consolidator (sotto):
        # turno e consolidation acquisiscono lo stesso lock rientrante per key,
        # quindi non possono mutare session.messages in concorrenza.
        self._session_locks = SessionLocks()
        # Epoch di turno per-sessione: /stop e /new "ripudiano" i turni in volo
        # bumpando l'epoch; un turno abbandonato (task che non muore) scarta i
        # propri effetti ai punti di rientro (stream, checkpoint, save, outbound).
        self._turn_epochs = TurnEpochs()
        self._turn_tokens_by_task: dict[asyncio.Task, TurnToken] = {}
        # Per-session pending queues for mid-turn message injection.
        # When a session has an active task, new messages for that session
        # are routed here instead of creating a new task.
        self._pending_queues: dict[str, asyncio.Queue] = {}
        # Per-session monotonic timestamp dell'ultima attività del turno
        # (progress/stream/reasoning/retry). Alimenta il watchdog di inattività
        # in ``_dispatch`` che sblocca i turni bloccati (UI ferma su "running").
        self._turn_activity: dict[str, float] = {}
        self._cron_turns = CronTurnCoordinator(
            publish_inbound=self.bus.publish_inbound,
            dispatch=self._dispatch,
            is_running=lambda: self._running,
        )
        # <=0 means unlimited; default 3 (env: JENNY_MAX_CONCURRENT_REQUESTS).
        from jenny.config.runtime_env import max_concurrent_requests

        _max = max_concurrent_requests()
        self._concurrency_gate: asyncio.Semaphore | None = (
            asyncio.Semaphore(_max) if _max > 0 else None
        )
        self.consolidator = Consolidator(
            store=self.context.memory,
            provider=provider,
            model=self.model,
            sessions=self.sessions,
            context_window_tokens=self.context_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            max_completion_tokens=provider.generation.max_tokens,
            consolidation_ratio=consolidation_ratio,
            session_locks=self._session_locks,
        )
        self.auto_compact = AutoCompact(
            sessions=self.sessions,
            consolidator=self.consolidator,
            session_ttl_minutes=session_ttl_minutes,
        )
        self.model_presets: dict[str, Any] = dict(model_presets_config) if model_presets_config else {}
        self._active_preset: str | None = None
        if initial_model_preset:
            try:
                self._apply_model_preset(initial_model_preset, publish_update=False)
            except KeyError:
                logger.warning(
                    "Startup model preset {!r} is not defined in modelPresets; "
                    "using agents.defaults model settings",
                    initial_model_preset,
                )
        self._register_default_tools()
        self._runtime_vars: dict[str, Any] = {}
        self._current_iteration: int = 0
        self.commands = CommandRouter()
        register_builtin_commands(self.commands)

    @classmethod
    def from_config(
        cls,
        config: Any,
        bus: MessageBus | None = None,
        **extra: Any,
    ) -> AgentLoop:
        """Create an AgentLoop from config with the common parameter set.

        Extra keyword arguments are forwarded to ``AgentLoop.__init__``,
        allowing callers to override or extend the standard config-derived
        parameters (e.g. ``cron_service``, ``session_manager``).
        """
        from jenny.providers.factory import make_provider

        if bus is None:
            bus = MessageBus()
        defaults = config.agents.defaults
        provider = extra.pop("provider", None) or make_provider(config)
        model = extra.pop("model", None) or defaults.model
        context_window_tokens = extra.pop("context_window_tokens", None) or defaults.context_window_tokens
        initial_model_preset = extra.pop("initial_model_preset", None) or defaults.model_preset
        return cls(
            bus=bus,
            provider=provider,
            workspace=config.workspace_path,
            model=model,
            max_iterations=defaults.max_tool_iterations,
            max_concurrent_subagents=defaults.max_concurrent_subagents,
            subagent_stall_threshold_seconds=defaults.subagent_stall_threshold_seconds,
            subagent_tool_error_budget=defaults.subagent_tool_error_budget,
            context_window_tokens=context_window_tokens,
            context_block_limit=defaults.context_block_limit,
            max_tool_result_chars=defaults.max_tool_result_chars,
            provider_retry_mode=defaults.provider_retry_mode,
            tool_hint_max_length=defaults.tool_hint_max_length,
            restrict_to_workspace=config.security.restrict_to_workspace,
            extract_document_text=config.extract_document_text,
            timezone=defaults.timezone,
            disabled_skills=defaults.disabled_skills,
            wiki_directory_max_tokens=defaults.atlas.max_context_tokens,
            session_ttl_minutes=defaults.session_ttl_minutes,
            consolidation_ratio=defaults.consolidation_ratio,
            max_messages=defaults.max_messages,
            tools_config=config.tools,
            model_presets_config=config.model_presets,
            initial_model_preset=initial_model_preset,
            orchestrator_mode=defaults.orchestrator_mode,
            **extra,
        )


    @property
    def tool_scope(self) -> str:
        """Scope con cui viene caricato il registry dell'agente principale."""
        return "orchestrator" if self.orchestrator_mode else "core"

    def _register_default_tools(self) -> None:
        """Register the default set of tools via plugin loader."""
        from jenny.agent.tools.context import ToolContext
        from jenny.agent.tools.loader import ToolLoader

        ctx = ToolContext(
            config=self.tools_config,
            workspace=str(self.workspace),
            bus=self.bus,
            subagent_manager=self.subagents,
            cron_service=self.cron_service,
            sessions=self.sessions,
            timezone=self.context.timezone or "UTC",
            workspace_sandbox=self.workspace_scopes.sandbox_status,
            runtime_events=self.runtime_events,
            android_context=get_android_context(),
            ui_query_service=self._ui_query,
            orchestrator=self.orchestrator_mode,
        )
        loader = ToolLoader()
        registered = loader.load(ctx, self.tools, scope=self.tool_scope)

        # MyTool needs runtime state reference — manual registration
        if self.tools_config.my.enable:
            self.tools.register(
                MyTool(runtime_state=self, modify_allowed=self.tools_config.my.allow_set)
            )
            registered.append("my")

        # Jenny App actions — manifest-driven, kept in sync per turn
        from jenny.agent.tools.app_actions import AppToolsSyncer

        self._app_tools_syncer = AppToolsSyncer(Path(self.workspace), bus=self.bus)
        app_tools, _changed = self._app_tools_syncer.sync(self.tools)
        if app_tools:
            registered.append(f"apps:{len(app_tools)}")

        # MCP server tools — server dichiarati in Settings, discovery alla
        # registrazione (stesso momento di AppToolsSyncer). Un server rotto
        # viene saltato da sync_mcp_tools, mai un errore che butta giù il
        # gateway; i nomi (mcp__<server>__<tool>) sono unici per costruzione.
        from jenny.mcp.manager import sync_mcp_tools

        mcp_cfg = getattr(self.tools_config, "mcp", None)
        try:
            mcp_tools = sync_mcp_tools(mcp_cfg)
        except Exception:
            logger.exception("MCP tool discovery failed")
            mcp_tools = []
        for tool in mcp_tools:
            if self.tools.has(tool.name):
                logger.warning(
                    "MCP tool '{}' collides with an existing tool; skipped", tool.name
                )
                continue
            self.tools.register(tool)
        if mcp_tools:
            registered.append(f"mcp:{len(mcp_tools)}")

        logger.info("Registered {} tools: {}", len(registered), registered)

    def _set_tool_context(
        self, channel: str, chat_id: str,
        message_id: str | None = None, metadata: dict | None = None,
        session_key: str | None = None,
    ) -> None:
        """Update context for all tools that need routing info."""
        from jenny.agent.tools.context import ContextAware

        effective_key = session_key or session_key_for_channel(channel, chat_id)
        request_ctx = RequestContext(
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            session_key=effective_key,
            metadata=dict(metadata or {}),
            # Letta dal ContextVar e non dai parametri: questo metodo viene
            # richiamato anche dal progress hook a ogni iterazione, che non ha
            # (e non deve avere) l'identita del turno nella propria firma.
            turn_id=current_turn_id(),
        )

        for name in self.tools.tool_names:
            tool = self.tools.get(name)
            if tool and isinstance(tool, ContextAware):
                tool.set_context(request_ctx)

    @staticmethod
    def _runtime_chat_id(msg: InboundMessage) -> str:
        """Return the chat id shown in runtime metadata for the model."""
        return str(msg.metadata.get("context_chat_id") or msg.chat_id)

    @staticmethod
    def _is_silent_turn(msg: InboundMessage, session_key: str) -> bool:
        """True se questo turno non deve raggiungere l'utente da se'.

        Wrapper sul resolver unico (:mod:`jenny.session.turn_visibility`) per i
        punti del dispatch che hanno il messaggio e la session key sotto mano.
        """
        return resolve_turn_visibility(
            msg.metadata, channel=msg.channel, session_key=session_key
        ).silent

    async def _build_bus_progress_callback(
        self, msg: InboundMessage
    ) -> Callable[..., Awaitable[None]]:
        """Build a progress callback that publishes to the message bus."""
        return build_bus_progress_callback(self.bus, msg)

    async def _build_retry_wait_callback(
        self, msg: InboundMessage
    ) -> Callable[[str], Awaitable[None]]:
        """Build a retry-wait callback that publishes to the message bus."""

        async def _on_retry_wait(content: str) -> None:
            meta = dict(msg.metadata or {})
            meta["_retry_wait"] = True
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    metadata=meta,
                )
            )

        return _on_retry_wait

    def _runtime_events(self) -> RuntimeEventPublisher:
        # Costruito esplicitamente in __init__ (self.runtime_event_publisher).
        return self.runtime_event_publisher

    def _heartbeat_followup(self) -> "HeartbeatFollowup | None":
        """Il registratore dell'esito di un controllo dell'heartbeat delegato.

        Registrato sul servizio cron dal ``CronDispatcher``, che è chi conosce
        l'heartbeat; qui si legge soltanto. ``None`` per un ``AgentLoop`` senza
        servizio cron (test, comandi) e finché il dispatcher non è stato
        costruito — in entrambi i casi non c'è nessun heartbeat che possa avere
        delegato qualcosa.
        """
        return getattr(self.cron_service, "heartbeat_followup", None)

    async def submit_cron_turn(self, msg: InboundMessage) -> TurnOutcome:
        return await self._cron_turns.submit(msg)

    def pending_cron_job_ids_for_session(self, session_key: str) -> set[str]:
        return self._cron_turns.pending_job_ids_for_session(session_key)

    def _persist_user_message_early(
        self,
        msg: InboundMessage,
        session: Session,
        **kwargs: Any,
    ) -> bool:
        """Persist the triggering user message before the turn starts.

        Returns True if the message was persisted.
        """
        if not turn_continuation.should_persist_user_message(msg.metadata):
            return False
        media_paths = [p for p in (msg.media or []) if isinstance(p, str) and p]
        has_text = isinstance(msg.content, str) and msg.content.strip()
        if has_text or media_paths:
            extra: dict[str, Any] = ({"media": list(media_paths)} if media_paths else {}) | {}
            extra.update(kwargs)
            text = msg.content if isinstance(msg.content, str) else ""
            text_override, cron_extra = cron_history_overrides(msg.metadata)
            if text_override is not None:
                text = text_override
            extra.update(cron_extra)
            session.add_message("user", text, **extra)
            self._mark_pending_user_turn(session)
            self.sessions.save(session)
            return True
        return False

    def _location_runtime_lines(self, channel: str | None, chat_id: str | None) -> list[str]:
        """Riga posizione (last-known GPS, o override Telegram) per il runtime
        context. Lista vuota se disattivata o senza fix — vedi runtime.location."""
        line = location_runtime_line(channel, chat_id, self.tools_config.location)
        return [line] if line else []

    def _build_initial_messages(
        self,
        msg: InboundMessage,
        session: Session,
        history: list[dict[str, Any]],
        pending_summary: str | None,
        include_memory_recent_history: bool = True,
        tools: ToolRegistry | None = None,
    ) -> list[dict[str, Any]]:
        """Build the initial message list for the LLM turn.

        ``tools`` e il registry *di questo turno*, che non sempre e quello del
        loop: Dream e Atlas ne portano uno proprio. Va passato perche il prompt
        dichiari i tool che il modello ricevera davvero — sono la stessa cosa
        detta due volte, e devono venire dalla stessa fonte.
        """
        scope = self.workspace_scopes.for_message(msg, session.metadata)
        chat_id = self._runtime_chat_id(msg)
        turn_tools = tools or self.tools
        return self.context.build_messages(
            available_tools=turn_tools.tool_names,
            # Un registry sostituito non e l'orchestratore: e un altro agente
            # che passa da qui. Dire a Dream o ad Atlas "non puoi scrivere file,
            # delega con `spawn`" e falso due volte — scrivere e il loro unico
            # mestiere, e `spawn` non ce l'hanno.
            orchestrator=self.orchestrator_mode and turn_tools is self.tools,
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=chat_id,
            sender_id=msg.sender_id,
            session_summary=pending_summary,
            session_metadata=session.metadata,
            workspace=scope.project_path,
            include_memory_recent_history=include_memory_recent_history,
            session_key=session.key,
            current_runtime_lines=self._location_runtime_lines(msg.channel, chat_id),
        )

    async def _dispatch_command_inline(
        self,
        msg: InboundMessage,
        key: str,
        raw: str,
        dispatch_fn: Callable[[CommandContext], Awaitable[OutboundMessage | None]],
    ) -> None:
        """Dispatch a command directly from the run() loop and publish the result."""
        ctx = CommandContext(msg=msg, session=None, key=key, raw=raw, loop=self)
        result = await dispatch_fn(ctx)
        if result:
            await self.bus.publish_outbound(result)
        else:
            logger.warning("Command '{}' matched but dispatch returned None", raw)


    def _effective_session_key(self, msg: InboundMessage) -> str:
        """Return the session key used for task routing and mid-turn injections."""
        return msg.session_key_override or UNIFIED_SESSION_KEY

    def _replay_token_budget(self) -> int:
        """Derive a token budget for session history replay from the context window."""
        if self.context_window_tokens <= 0:
            return 0
        reserved_output = reserved_output_tokens(self.provider)
        budget = (
            self.context_window_tokens
            - max(1, reserved_output)
            - CONTEXT_BUDGET_SAFETY_BUFFER
        )
        return budget if budget > 0 else max(128, self.context_window_tokens // 2)

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
        *,
        session: Session | None = None,
        channel: str = INTERNAL_CHANNEL,
        chat_id: str = "direct",
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
        pending_queue: asyncio.Queue | None = None,
        ephemeral: bool = False,
        tools: ToolRegistry | None = None,
        turn_token: TurnToken | None = None,
    ) -> tuple[str | None, list[str], list[dict], str, bool]:
        """Run the agent iteration loop.

        *on_stream*: called with each content delta during streaming.
        *on_stream_end(resuming)*: called when a streaming session finishes.
        ``resuming=True`` means tool calls follow (spinner should restart);
        ``resuming=False`` means this is the final response.

        Returns (final_content, tools_used, messages, stop_reason, had_injections).
        """
        self._sync_subagent_runtime_limits()

        def _guarded(cb):
            """No-oppa la callback se il turno è stato ripudiato (epoch bumpato):
            uno zombie abbandonato non deve più emettere delta/progress verso
            l'utente né rientrare nello stato condiviso. ``functools.wraps``
            preserva la firma originale, sondata da ``_on_progress_accepts``
            per capire quali kwargs la callback supporta."""
            if cb is None:
                return None

            @functools.wraps(cb)
            async def _wrapper(*args, **kwargs):
                if not self._turn_epochs.is_current(turn_token):
                    return None
                return await cb(*args, **kwargs)

            return _wrapper

        on_progress = _guarded(on_progress)
        on_stream = _guarded(on_stream)
        on_stream_end = _guarded(on_stream_end)
        on_retry_wait = _guarded(on_retry_wait)

        loop_hook = AgentProgressHook(
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            metadata=metadata,
            session_key=session_key,
            tool_hint_max_length=self.tool_hint_max_length,
            set_tool_context=self._set_tool_context,
            on_iteration=lambda iteration: setattr(self, "_current_iteration", iteration),
        )
        hook: AgentHook = loop_hook
        if not ephemeral and self._extra_hooks:
            hook = CompositeHook([loop_hook] + self._extra_hooks)

        async def _checkpoint(payload: dict[str, Any]) -> None:
            if session is None:
                return
            # Uno zombie ripudiato non deve riscrivere il checkpoint che
            # /stop ha già materializzato e ripulito.
            if not self._turn_epochs.is_current(turn_token):
                return
            self._set_runtime_checkpoint(session, payload)

        async def _drain_pending(*, limit: int = _MAX_INJECTIONS_PER_TURN) -> list[dict[str, Any]]:
            """Drain follow-up messages from the pending queue.

            When no messages are immediately available but sub-agents
            spawned in this dispatch are still running, blocks until at
            least one result arrives (or timeout).  This keeps the runner
            loop alive so subsequent sub-agent completions are consumed
            in-order rather than dispatched separately.
            """
            if pending_queue is None:
                return []

            def _to_user_message(pending_msg: InboundMessage) -> dict[str, Any]:
                content = pending_msg.content
                media = pending_msg.media if pending_msg.media else None
                if media:
                    content, media = self._prepare_message_media(content, media)
                    media = media or None
                user_content = self.context._build_user_content(content, media)
                return {"role": "user", "content": user_content}

            items: list[dict[str, Any]] = []
            while len(items) < limit:
                try:
                    items.append(_to_user_message(pending_queue.get_nowait()))
                except asyncio.QueueEmpty:
                    break

            # Block if nothing drained but sub-agents spawned in this dispatch
            # are still running.  Keeps the runner loop alive so subsequent
            # completions are injected in-order rather than dispatched separately.
            if (not items
                    and session is not None
                    and self.subagents.get_running_count_by_session(session.key) > 0):
                try:
                    msg = await asyncio.wait_for(pending_queue.get(), timeout=300)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Timeout waiting for sub-agent completion in session {}",
                        session.key,
                    )
                    return items
                items.append(_to_user_message(msg))
                while len(items) < limit:
                    try:
                        items.append(_to_user_message(pending_queue.get_nowait()))
                    except asyncio.QueueEmpty:
                        break

            return items

        active_session_key = session.key if session else session_key
        effective_scope = self.workspace_scopes.for_turn(
            channel=channel,
            message_metadata=metadata,
            session_metadata=session.metadata if session is not None else None,
        )
        request_ctx = RequestContext(
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            session_key=active_session_key,
            metadata=dict(metadata or {}),
            turn_id=current_turn_id(),
        )
        file_state_token = bind_file_states(self._file_state_store.for_session(active_session_key))
        request_token = bind_request_context(request_ctx)
        workspace_token = bind_workspace_scope(effective_scope)
        # Compute lazily because long_task may create goal metadata during this run.
        def _goal_continue() -> str | None:
            _goal_lines = goal_state_runtime_lines(session.metadata if session is not None else None)
            if not _goal_lines:
                return None
            return (
                "You have an active sustained goal:\n\n"
                + "\n".join(_goal_lines)
                + "\n\nPlease continue working toward the objective using your tools, "
                "or call complete_goal if the work is truly finished."
            )

        # Lazy repair: a goal left ``active`` by a process kill (normal on Android)
        # would otherwise stay zombie forever and permanently disable the LLM
        # wall-timeout for the session. Expire only when idle past the TTL, so a goal
        # that keeps advancing (it stamps ``last_turn_at`` each turn) is never touched.
        if session is not None:
            from jenny.config.runtime_env import goal_inactivity_ttl_h

            expire_stale_goal(session.metadata, ttl_h=goal_inactivity_ttl_h())
            # Un messaggio vero dell'utente *è* la fine dell'attesa: il goal
            # parcheggiato torna spronabile da questo turno in poi. Le
            # continuazioni interne e i turni che non persistono input utente
            # (heartbeat, lavoro di sistema) non contano come risposta.
            if (
                turn_continuation.should_persist_user_message(metadata)
                and not is_silent_turn(metadata)
            ):
                clear_goal_awaiting_input(session.metadata)
        session_metadata = session.metadata if session is not None else None

        async def _on_context_overflow(new_window: int) -> None:
            """Called by the runner when a context_length error occurs.

            Shrinks the consolidator budget and triggers compaction so the
            next retry has a chance of fitting within the model's limit.
            """
            old_window = self.context_window_tokens
            self.context_window_tokens = new_window
            self.consolidator.set_provider(self.provider, self.model, new_window)
            logger.info(
                "Context window reduced {} -> {}, triggering compaction",
                old_window, new_window,
            )
            if session is not None:
                try:
                    await self.consolidator.maybe_consolidate_by_tokens(
                        session,
                        replay_max_messages=self._max_messages,
                    )
                except Exception:
                    logger.debug("Post-overflow compaction failed", exc_info=True)

        try:
            result = await self.runner.run(AgentRunSpec(
                initial_messages=initial_messages,
                # ``tools`` arriva gia risolto da ``_process_message``; il
                # fallback resta per i chiamanti diretti (test) che entrano qui
                # senza passare dalla FSM.
                tools=tools or self.tools,
                model=self.model,
                max_iterations=self.max_iterations,
                max_tool_result_chars=self.max_tool_result_chars,
                hook=hook,
                error_message="Sorry, I encountered an error calling the AI model.",
                concurrent_tools=True,
                workspace=effective_scope.project_path,
                session_key=session.key if session else None,
                context_window_tokens=self.context_window_tokens,
                context_block_limit=self.context_block_limit,
                provider_retry_mode=self.provider_retry_mode,
                progress_callback=on_progress,
                stream_progress_deltas=on_stream is not None,
                retry_wait_callback=on_retry_wait,
                checkpoint_callback=_checkpoint,
                injection_callback=_drain_pending,
                # Sustained goals may legitimately exceed JENNY_LLM_TIMEOUT_S; idle stall
                # is still capped by JENNY_STREAM_IDLE_TIMEOUT_S in streaming providers.
                llm_timeout_s=runner_wall_llm_timeout_s(
                    self.sessions,
                    session.key if session is not None else session_key,
                    metadata=session_metadata,
                    message_metadata=metadata,
                ),
                tool_choice=self.tool_choice if self.tool_choice != "auto" else None,
                # Un goal parcheggiato in attesa dell'utente resta ``active`` ma
                # non va spronato: finché aspetta, nessun turno (nemmeno interno)
                # spende una chiamata per ripetergli «continua».
                goal_active_predicate=lambda: (
                    session is not None
                    and sustained_goal_active(session.metadata)
                    and not goal_awaiting_input(session.metadata)
                ),
                goal_continue_message=_goal_continue,
                finalize_on_max_iterations=turn_continuation.should_finalize_on_max_iterations(
                    pending_queue_available=pending_queue is not None and session is not None,
                    session_metadata=session_metadata,
                    message_metadata=metadata,
                ),
                on_context_overflow=_on_context_overflow,
            ))
        finally:
            reset_workspace_scope(workspace_token)
            reset_request_context(request_token)
            reset_file_states(file_state_token)
        self._last_usage = result.usage
        if result.goal_stalled and session is not None:
            # Il runner ha rifiutato di spronare il goal: sta aspettando una
            # risposta. Marcarlo tiene il goal vivo e onesto — l'alternativa che
            # il modello trovava da solo era chiuderlo con un recap falso. La
            # scrittura su disco arriva dal salvataggio di fine turno
            # (``_finalize_turn_save``), come per ``note_goal_turn``.
            if mark_goal_awaiting_input(session.metadata) is not None:
                logger.info(
                    "Sustained goal parked waiting for the user ({})",
                    session.key,
                )
        if result.images_stripped and result.final_content:
            # Il fallback in providers/base.py ha tolto le immagini in silenzio
            # dopo un rifiuto non transitorio del provider (modello senza
            # supporto vision) e ha ritentato solo testo: avvisa in chat invece
            # di lasciare che sembri che l'allegato sia stato ignorato.
            notice = (
                "\n\n⚠️ Le immagini allegate non sono state elaborate: "
                "il modello attivo non supporta input visivi."
            )
            result.final_content += notice
            if result.messages and result.messages[-1].get("role") == "assistant":
                last = result.messages[-1]
                if isinstance(last.get("content"), str):
                    last["content"] = (last["content"] or "") + notice
            if on_stream:
                await on_stream(notice)
        if result.stop_reason == "max_iterations":
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            should_stream = turn_continuation.should_stream_budget_response(
                stop_reason=result.stop_reason,
                pending_queue_available=pending_queue is not None and session is not None,
                session_metadata=session_metadata,
                message_metadata=metadata,
            )
            # Push final content through stream so streaming channels
            # update the card instead of leaving it empty.
            if on_stream and on_stream_end and should_stream:
                await on_stream(result.final_content or "")
                await on_stream_end(resuming=False)
        elif result.stop_reason == "error":
            logger.error("LLM returned error: {}", (result.final_content or "")[:200])
        return result.final_content, result.tools_used, result.messages, result.stop_reason, result.had_injections

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                now = time.monotonic()
                if now - self._last_ttl_check >= self._TTL_CHECK_INTERVAL_S:
                    self._last_ttl_check = now
                    self.auto_compact.check_expired(
                        self._schedule_background,
                        active_session_keys=self._pending_queues.keys(),
                    )
                continue
            except asyncio.CancelledError:
                # Preserve real task cancellation so shutdown can complete cleanly.
                # Only ignore non-task CancelledError signals that may leak from integrations.
                if not self._running or asyncio.current_task().cancelling():
                    raise
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            raw = msg.content.strip()
            effective_key = self._effective_session_key(msg)
            if self.commands.is_priority(raw):
                await self._dispatch_command_inline(
                    msg, effective_key, raw,
                    self.commands.dispatch_priority,
                )
                continue
            if self._cron_turns.defer_if_active(
                msg,
                session_key=effective_key,
                active_session_keys=self._pending_queues.keys(),
            ):
                logger.info(
                    "Deferred cron turn for active session {}",
                    effective_key,
                )
                continue
            # If this session already has an active pending queue (i.e. a task
            # is processing this session), route the message there for mid-turn
            # injection instead of creating a competing task.
            if effective_key in self._pending_queues:
                # Non-priority commands must not be queued for injection;
                # dispatch them directly (same pattern as priority commands).
                if self.commands.is_dispatchable_command(raw):
                    await self._dispatch_command_inline(
                        msg, effective_key, raw,
                        self.commands.dispatch,
                    )
                    continue
                pending_msg = msg
                if effective_key != msg.session_key:
                    pending_msg = dataclasses.replace(
                        msg,
                        session_key_override=effective_key,
                    )
                try:
                    self._pending_queues[effective_key].put_nowait(pending_msg)
                except asyncio.QueueFull:
                    logger.warning(
                        "Pending queue full for session {}, falling back to queued task",
                        effective_key,
                    )
                else:
                    logger.info(
                        "Routed follow-up message to pending queue for session {}",
                        effective_key,
                    )
                    continue
            # Compute the effective session key before dispatching
            # This ensures /stop command can find tasks correctly when unified session is enabled
            # Registra la pending-queue di injection PRIMA di create_task (fix
            # race di injection): run() è l'unico consumer, quindi il check
            # "effective_key in self._pending_queues" (sopra) e questa
            # registrazione sono atomici (nessun await in mezzo) → un follow-up
            # viene iniettato nel turno invece di generare un task competitivo.
            # Nel fallback QueueFull (già presente) passiamo None e lascia che
            # _dispatch gestisca come prima.
            injection_queue: asyncio.Queue | None = None
            if effective_key not in self._pending_queues:
                injection_queue = asyncio.Queue(maxsize=20)
                self._pending_queues[effective_key] = injection_queue
            task = asyncio.create_task(self._dispatch(msg, injection_queue))
            self._active_tasks.setdefault(effective_key, []).append(task)
            task.add_done_callback(
                lambda t, k=effective_key: self._active_tasks.get(k, [])
                and self._active_tasks[k].remove(t)
                if t in self._active_tasks.get(k, [])
                else None
            )

    async def _dispatch(
        self, msg: InboundMessage, pending: "asyncio.Queue | None" = None
    ) -> None:
        """Process a message: per-session serial, cross-session concurrent.

        ``pending`` è la coda di injection mid-turn. Il consumer (``run()``) la
        crea e registra PRIMA di ``create_task`` per chiudere la race di
        injection; i chiamanti legacy (es. cron) passano ``None`` e la coda
        viene creata e registrata qui, sotto il lock, come in origine."""
        session_key = self._effective_session_key(msg)
        if session_key != msg.session_key:
            msg = dataclasses.replace(msg, session_key_override=session_key)
        # Token di epoch del turno: se /stop o /new bumpano l'epoch mentre
        # questo turno è in volo, il turno è "ripudiato" e i suoi effetti
        # vengono scartati ai punti di rientro qui sotto.
        turn_token = self._turn_epochs.issue(session_key)
        current_task = asyncio.current_task()
        if current_task is not None:
            self._turn_tokens_by_task[current_task] = turn_token
        lock = self._session_locks.get(session_key)
        gate = self._concurrency_gate or nullcontext()
        # Identita del turno visibile ai tool (``RequestContext.turn_id``).
        # Legata qui perche _dispatch e il punto da cui passa *ogni* turno che
        # arriva dal bus — WebUI, Telegram, cron, annunci di subagent — mentre
        # ``process_direct`` e l'altro ingresso e la lega per conto suo. Le
        # guardie per-turno dei tool non possono dipendere dal ``message_id``:
        # il canale WebSocket non lo manda (vedi bind_turn_id).
        turn_id_token = bind_turn_id(_new_turn_id(session_key))

        try:
            # `keep_awake` DOPO lock e gate, non prima: l'attesa in coda dietro
            # un altro turno puo' durare minuti, e tenere sveglia la CPU per
            # aspettare sarebbe esattamente lo spreco che la modalita' "turns"
            # esiste per evitare. Da qui in giu' invece si lavora davvero — LLM,
            # tool, persistenza, pubblicazione dell'outbound — e se la CPU si
            # sospende il turno resta congelato a meta'. Il tag e' refcontato:
            # un turno annidato (subagent, tool che rientra) non prende un
            # secondo lock e non lo rilascia sotto il turno esterno.
            async with lock, gate, keep_awake("turn", timeout_s=_TURN_WAKELOCK_TIMEOUT_S):
                # Only the task that owns the session lock may publish the
                # active mid-turn injection queue for this session.
                if pending is None:
                    pending = asyncio.Queue(maxsize=20)
                    self._pending_queues[session_key] = pending
                try:
                    on_stream = on_stream_end = None
                    # Un turno silenzioso non streamma: i delta sono pubblicati
                    # sul canale d'origine e comparirebbero in chat per un turno
                    # che poi non consegna nulla — il silenzio promesso, rotto a
                    # metà. Il gate sta qui, all'unico posto che costruisce i
                    # callback di stream, e non nei call site che li ereditano.
                    if msg.metadata.get("_wants_stream") and not self._is_silent_turn(
                        msg, session_key
                    ):
                        # Split one answer into distinct stream segments.
                        stream_base_id = f"{msg.session_key}:{time.time_ns()}"
                        stream_segment = 0

                        def _current_stream_id() -> str:
                            return f"{stream_base_id}:{stream_segment}"

                        async def on_stream(delta: str) -> None:
                            meta = dict(msg.metadata or {})
                            meta["_stream_delta"] = True
                            meta["_stream_id"] = _current_stream_id()
                            # Transient live-preview: non bloccante, scartabile
                            # sotto backpressure (la risposta finale autoritativa
                            # è pubblicata a parte più sotto).
                            self.bus.try_publish_outbound(OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content=delta,
                                metadata=meta,
                            ))

                        async def on_stream_end(*, resuming: bool = False) -> None:
                            nonlocal stream_segment
                            meta = dict(msg.metadata or {})
                            meta["_stream_end"] = True
                            meta["_resuming"] = resuming
                            meta["_stream_id"] = _current_stream_id()
                            self.bus.try_publish_outbound(OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content="",
                                metadata=meta,
                            ))
                            stream_segment += 1

                    outcome = await self._process_message(
                        msg, on_stream=on_stream, on_stream_end=on_stream_end,
                        pending_queue=pending, turn_token=turn_token,
                    )
                    if self._turn_epochs.is_current(turn_token):
                        completed_channel = msg.channel
                        completed_chat_id = msg.chat_id
                        # UNICO punto di consegna implicita di un turno. La
                        # decisione e' una funzione dell'ESITO, non della
                        # provenienza del messaggio: un turno silenzioso non
                        # produce ``DELIVERED``, quindi qui non c'e' nulla da
                        # pubblicare e non serve un secondo controllo.
                        if outcome.message is not None:
                            await self.bus.publish_outbound(outcome.message)
                            completed_channel = outcome.message.channel
                            completed_chat_id = outcome.message.chat_id
                        elif msg.channel == INTERNAL_CHANNEL:
                            await self.bus.publish_outbound(OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content="", metadata=msg.metadata or {},
                            ))
                        continuing = turn_continuation.internal_continuation_pending(msg.metadata)
                        if not continuing:
                            await self._runtime_events().turn_completed(
                                channel=completed_channel,
                                chat_id=completed_chat_id,
                                session_key=session_key,
                                metadata=msg.metadata,
                            )
                    self._cron_turns.complete(msg, outcome=outcome)
                except asyncio.CancelledError:
                    self._cron_turns.complete(
                        msg,
                        error=asyncio.CancelledError(),
                    )
                    logger.info("Task cancelled for session {}", session_key)
                    # Preserve partial context from the interrupted turn so
                    # the user does not lose tool results and assistant
                    # messages accumulated before /stop.  The checkpoint was
                    # already persisted to session metadata by
                    # _emit_checkpoint during tool execution; materializing
                    # it into session history now makes it visible in the
                    # next conversation turn.
                    # Un turno RIPUDIATO (epoch bumpato da /stop o /new) salta
                    # il ripristino: lo ha già fatto il comando in modo
                    # sincrono, e questo handler può girare molto più tardi
                    # (task abbandonato) sovrascrivendo stato più recente.
                    if self._turn_epochs.is_current(turn_token):
                        try:
                            key = self._effective_session_key(msg)
                            session = self.sessions.get_or_create(key)
                            if self._restore_runtime_checkpoint(session):
                                self._clear_pending_user_turn(session)
                                self.sessions.save(session)
                                logger.info(
                                    "Restored partial context for cancelled session {}",
                                    key,
                                )
                        except Exception:
                            logger.debug(
                                "Could not restore checkpoint for cancelled session {}",
                                session_key,
                                exc_info=True,
                            )
                    raise
                except Exception as exc:
                    logger.exception("Error processing message for session {}", session_key)
                    if self._turn_epochs.is_current(turn_token):
                        # Un turno silenzioso non consegna nemmeno i propri
                        # errori: l'utente non ha chiesto questo lavoro e una
                        # bolla "Sorry, I encountered an error." in chat sarebbe
                        # rumore per un fallimento che appartiene alla run record
                        # del job. L'eccezione risale comunque al chiamante.
                        if not self._is_silent_turn(msg, session_key):
                            await self.bus.publish_outbound(OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content="Sorry, I encountered an error.",
                            ))
                        if not turn_continuation.internal_continuation_pending(msg.metadata):
                            await self._runtime_events().turn_completed(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                session_key=session_key,
                                metadata=msg.metadata,
                            )
                    self._cron_turns.complete(msg, error=exc)
                finally:
                    # Drain any messages still in the pending queue and re-publish
                    # them to the bus so they are processed as fresh inbound messages
                    # rather than silently lost.  Only remove our own queue; a
                    # later task waiting on the lock must not be able to steal
                    # cleanup ownership.
                    queue = None
                    if self._pending_queues.get(session_key) is pending:
                        queue = self._pending_queues.pop(session_key, None)
                    else:
                        queue = pending
                    if queue is not None:
                        leftover = 0
                        while True:
                            try:
                                item = queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                            await self.bus.publish_inbound(item)
                            leftover += 1
                        if leftover:
                            logger.info(
                                "Re-published {} leftover message(s) to bus for session {}",
                                leftover, session_key,
                            )
                    # Un turno ripudiato non deve segnalare "idle" né toccare lo
                    # stato del turno corrente: un turno più nuovo potrebbe già
                    # essere in esecuzione (/stop compensa via _emit_stop_turn_end).
                    if self._turn_epochs.is_current(turn_token):
                        if not turn_continuation.internal_continuation_pending(msg.metadata):
                            await self._runtime_events().run_status_changed(
                                msg, session_key, "idle"
                            )
                            self._runtime_events().clear_turn(session_key)
                        await self._cron_turns.publish_next_deferred(session_key)
        finally:
            reset_turn_id(turn_id_token)
            if current_task is not None:
                self._turn_tokens_by_task.pop(current_task, None)
            if pending is None and self._turn_epochs.is_current(turn_token):
                await self._runtime_events().run_status_changed(
                    msg, session_key, "idle"
                )
                self._runtime_events().clear_turn(session_key)
                await self._cron_turns.publish_next_deferred(session_key)


    async def _process_system_message(
        self,
        msg: InboundMessage,
        pending_queue: asyncio.Queue | None = None,
        turn_token: TurnToken | None = None,
    ) -> TurnOutcome:
        """Process a system inbound message (e.g. subagent announce)."""
        channel, chat_id = (
            msg.chat_id.split(":", 1) if ":" in msg.chat_id else (INTERNAL_CHANNEL, msg.chat_id)
        )
        logger.info("Processing system message from {}", msg.sender_id)
        key = msg.session_key_override or f"{channel}:{chat_id}"
        # Il turno di annuncio EREDITA la visibilita dell'origine. E' il punto in
        # cui il difetto si manifestava: un subagent lanciato dentro l'heartbeat
        # termina molto dopo la fine del turno che lo ha lanciato, e il suo
        # annuncio apriva un turno nuovo che consegnava in chat senza passare da
        # nessun gate. Qui la ``session_key`` d'origine e' quella interna, quindi
        # il resolver dice SILENT senza bisogno di un caso speciale.
        silent = self._is_silent_turn(msg, key)
        if silent:
            mark_silent_turn(msg.metadata)
        session = self.sessions.get_or_create(key)
        if self._restore_runtime_checkpoint(session):
            self.sessions.save(session)
        if self._restore_pending_user_turn(session):
            self.sessions.save(session)

        session, pending = self.auto_compact.prepare_session(session, key)
        if pending:
            logger.info("Memory compact triggered for session {}", key)

        await self.consolidator.maybe_consolidate_by_tokens(
            session,
            replay_max_messages=self._max_messages,
        )
        is_subagent = msg.sender_id == "subagent"
        if is_subagent and self._persist_subagent_followup(session, msg):
            logger.debug("Subagent result persisted for session {}", key)
            self.sessions.save(session)
        # Prelude di tooling condiviso con lo stato BUILD della FSM: sincronizza i
        # tool delle app, imposta il contesto tool e azzera lo stato per-turno del
        # MessageTool. Prima veniva fatto solo _set_tool_context, divergendo dalla
        # FSM (nessun sync app, nessun start_turn).
        await self._begin_turn_tooling(
            channel, chat_id, msg.metadata.get("message_id"), msg.metadata, key,
        )
        current_role = "assistant" if is_subagent else "user"
        _hist_kwargs: dict[str, Any] = {
            "max_messages": self._max_messages,
            "max_tokens": self._replay_token_budget(),
            "include_timestamps": True,
            "extend_to_user": is_subagent,
        }
        history = session.get_history(**_hist_kwargs)
        workspace_scope = self.workspace_scopes.for_message(msg, session.metadata)

        messages = self.context.build_messages(
            history=history,
            current_message="" if is_subagent else msg.content,
            channel=channel,
            chat_id=chat_id,
            current_role=current_role,
            sender_id=msg.sender_id,
            session_summary=pending,
            session_metadata=session.metadata,
            workspace=workspace_scope.project_path,
            session_key=key,
            current_runtime_lines=self._location_runtime_lines(channel, chat_id),
        )
        # Un controllo dell'heartbeat delegato con ``spawn`` non ha un esito nel
        # turno che lo delega: `spawn` ritorna subito. QUESTO turno è l'unico che
        # il risultato ce l'ha, ed è già quello a cui il preambolo dell'heartbeat
        # affida la decisione di parlare; qui riceve anche il modo di registrare
        # l'esito. Il blocco è vuoto per ogni altra sessione e per un heartbeat
        # che non ha delegato niente, quindi nessun altro annuncio cambia di un
        # byte. Aggiunto DOPO ``build_messages`` e fuori dal salvataggio
        # (``save_skip``): è un'istruzione per questo turno, non un messaggio
        # della conversazione.
        followup = self._heartbeat_followup() if is_subagent else None
        followup_block = ""
        if followup is not None:
            try:
                followup_block = followup.prompt_block(key)
            except Exception:
                logger.exception("Heartbeat follow-up: could not build the prompt block")
        if followup_block:
            messages.append({"role": "user", "content": followup_block})
        save_skip = 1 + len(history) + (1 if followup_block else 0)
        t_wall = time.time()
        # Differenza deliberata dallo stato RUN della FSM: il path di sistema NON
        # emette run_status_changed("running"). Un turno subagent/announce è di
        # background e non deve accendere il banner "in esecuzione" nella WebUI; la
        # transizione a "idle" a fine turno resta gestita a livello di dispatch.
        final_content, _, all_msgs, stop_reason, _ = await self._run_agent_loop(
            messages, session=session, channel=channel, chat_id=chat_id,
            message_id=msg.metadata.get("message_id"),
            metadata=msg.metadata,
            session_key=key,
            pending_queue=pending_queue,
            turn_token=turn_token,
        )
        # Re-sync dei tool delle app dopo l'esecuzione (mirror dello stato RUN):
        # il turno può aver creato/eliminato app durante il run.
        await self._sync_apps_and_notify()
        wall_done = time.time()
        latency_ms = max(0, int((wall_done - t_wall) * 1000))
        # I turni di sistema non passano dalla FSM: guardia di ripudio
        # esplicita prima di scrivere la history.
        if not self._turn_epochs.is_current(turn_token):
            raise asyncio.CancelledError()
        # Persistenza di fine turno condivisa con lo stato SAVE della FSM.
        # ``clear_pending=False`` è una differenza deliberata: il path di sistema
        # non azzera il pending user turn (a differenza del path utente), per non
        # scartare un messaggio utente arrivato durante un turno di background.
        self._finalize_turn_save(
            session,
            all_msgs,
            save_skip,
            turn_latency_ms=latency_ms,
            session_key=key,
            ephemeral=False,
            clear_pending=False,
        )
        message_tool = self.tools.get("message")
        spoke_via_tool = bool(
            isinstance(message_tool, MessageTool) and message_tool._sent_in_turn
        )
        if silent:
            # Un turno di sistema silenzioso non ha un outbound: ne il contenuto
            # ne il fallback. Il vecchio contratto ("restituisce SEMPRE una
            # risposta") e' esattamente cio che consegnava all'utente il
            # riempitivo di un lavoro che non aveva chiesto; l'unico modo di
            # parlare resta il tool ``message``.
            # ``final_text`` viaggia anche qui: un turno silenzioso non consegna
            # nulla, ma la sua risposta finale resta l'unico posto in cui il
            # modello puo' dichiarare un esito su di se' senza parlare.
            text = final_content or ""
            if followup is not None:
                # L'esito di un controllo delegato si scrive qui e in nessun
                # altro posto: questo turno non torna al dispatcher cron — è
                # nato dal bus — e con lui finirebbe l'unica occasione di
                # registrarlo. Isolato: un registratore rotto non deve poter
                # far fallire un turno di background.
                try:
                    followup.record(key, final_text=text, spoke=spoke_via_tool)
                except Exception:
                    logger.exception("Heartbeat follow-up: could not record the outcome")
            return (
                TurnOutcome.spoke_via_tool(final_text=text)
                if spoke_via_tool
                else TurnOutcome.silent(final_text=text)
            )
        # Differenza deliberata dal path utente: nessuna soppressione MessageTool
        # (_assemble_outbound). Un turno di sistema VISIBILE ha un contratto di
        # outbound proprio e restituisce sempre una risposta (contenuto o fallback).
        content = final_content or "Background task completed."
        outbound_metadata: dict[str, Any] = {}
        if origin_message_id := msg.metadata.get("origin_message_id"):
            outbound_metadata["origin_message_id"] = origin_message_id
        return TurnOutcome.delivered(
            OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=content,
                metadata=outbound_metadata,
            )
        )

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
        ephemeral: bool = False,
        tools: ToolRegistry | None = None,
        turn_token: TurnToken | None = None,
    ) -> TurnOutcome:
        """Process a single inbound message and return its outcome."""
        if msg.channel == "system":
            return await self._process_system_message(
                msg,
                pending_queue=pending_queue,
                turn_token=turn_token,
            )

        key = session_key or msg.session_key
        t0 = time.time()
        # Visibilita risolta QUI, una volta, al confine del turno; se e' SILENT
        # il fatto viene marchiato nei metadata cosi i consumatori a valle che
        # hanno solo il messaggio (tool ``message``, ramo d'errore, annuncio di
        # un subagent) non devono ridedurlo dalla session key. Marchio solo il
        # caso SILENT: i metadata inbound finiscono nell'outbound, e un turno
        # visibile non deve trascinare un flag fino al client.
        silent = resolve_turn_visibility(
            msg.metadata, channel=msg.channel, session_key=key
        ).silent
        if silent:
            mark_silent_turn(msg.metadata)
        ctx = TurnContext(
            msg=msg,
            session=None,
            session_key=key,
            state=TurnState.RESTORE,
            # Stessa identita che i tool vedono in ``RequestContext.turn_id``:
            # il turno nei log e il turno su cui si delimitano le guardie sono
            # la stessa cosa, non due numerazioni parallele. Il fallback copre i
            # chiamanti diretti (test), che non passano da _dispatch.
            turn_id=current_turn_id() or _new_turn_id(key),
            turn_wall_started_at=t0,
            visible_run_started_at=turn_continuation.internal_continuation_run_started_at(
                msg.metadata,
            ),
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            pending_queue=pending_queue,
            ephemeral=ephemeral,
            # Un turno silenzioso (cron monitor, heartbeat, annuncio nato da
            # lavoro interno) ha l'outbound finale soppresso SEMPRE, e l'unico
            # modo che il modello ha di parlare e chiamare il tool ``message``
            # durante il turno. Non c'e un token sentinella da parsare ne una
            # chiamata LLM in piu. ``silent`` e ``suppress_response`` partono
            # uguali e poi divergono: una goal continuation accende il secondo a
            # meta turno restando comunque un turno visibile.
            silent=silent,
            suppress_response=silent,
            # Risolto QUI, una volta, non piu in basso: il registry di questo
            # turno decide due cose che devono coincidere — cosa il modello puo
            # chiamare e cosa il prompt gli dichiara di avere. Finche la
            # risoluzione stava solo davanti al runner, chi costruiva il prompt
            # rispondeva da solo alla stessa domanda, e con un registry
            # sostituito (Dream, Atlas) rispondeva diverso.
            tools=tools or self.tools,
            turn_token=turn_token,
        )

        while ctx.state is not TurnState.DONE:
            # Guardia unica di ripudio per la FSM: uno zombie che si risveglia
            # da un RUN bloccato la incontra prima di SAVE/RESPOND e confluisce
            # nel path CancelledError di _dispatch (già guardato dall'epoch).
            if not self._turn_epochs.is_current(ctx.turn_token):
                raise asyncio.CancelledError()
            handler_name = f"_state_{ctx.state.name.lower()}"
            handler = getattr(self, handler_name, None)
            if handler is None:
                raise RuntimeError(f"Missing state handler for {ctx.state}")

            t0 = time.perf_counter()
            try:
                event = await handler(ctx)
            except Exception:
                duration = (time.perf_counter() - t0) * 1000
                ctx.trace.append(
                    StateTraceEntry(
                        state=ctx.state,
                        started_at=t0,
                        duration_ms=duration,
                        event="",
                        error="exception",
                    )
                )
                raise

            duration = (time.perf_counter() - t0) * 1000
            ctx.trace.append(
                StateTraceEntry(
                    state=ctx.state,
                    started_at=t0,
                    duration_ms=duration,
                    event=event,
                )
            )
            logger.debug(
                "[turn {}] State {} took {:.1f}ms -> event {}",
                ctx.turn_id,
                ctx.state.name,
                duration,
                event,
            )

            next_state = self._TRANSITIONS.get((ctx.state, event))
            if next_state is None:
                raise RuntimeError(
                    f"[turn {ctx.turn_id}] No transition from {ctx.state} "
                    f"on event {event!r}"
                )
            ctx.state = next_state

        logger.debug(
            "[turn {}] Turn completed after {} states",
            ctx.turn_id,
            len(ctx.trace),
        )
        return TurnOutcome.of(
            ctx.outbound,
            spoke_via_tool=ctx.spoke_via_tool,
            final_text=ctx.final_content or "",
        )

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
    ) -> OutboundMessage | None:
        """Assemble the final outbound message from turn results."""
        # MessageTool suppression
        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            if not had_injections or stop_reason == "empty_final_response":
                return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)

        meta = dict(msg.metadata or {})
        if on_stream is not None and stop_reason not in {"error", "tool_error"}:
            meta["_streamed"] = True
        if turn_latency_ms is not None:
            meta["latency_ms"] = int(turn_latency_ms)

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=meta,
        )


    def _append_channel_delivery(
        self, session_key: str, content: str, media: list[str] | None
    ) -> None:
        """Append the proactive delivery to *session_key* (caller holds the lock)."""
        session = self.sessions.get_or_create(session_key)
        extra: dict[str, Any] = {"_channel_delivery": True}
        if media:
            extra["media"] = list(media)
        session.add_message("assistant", content, **extra)
        self.sessions.save(session)

    async def _record_channel_delivery_locked(
        self, session_key: str, content: str, media: list[str] | None
    ) -> None:
        async with self._session_locks.get(session_key):
            self._append_channel_delivery(session_key, content, media)

    async def record_channel_delivery(
        self,
        *,
        session_key: str,
        content: str,
        media: list[str] | None = None,
    ) -> None:
        """Register a proactive delivery as an ``assistant`` message in the session.

        Chiamato dal ``ChannelDeliverer`` quando il tool ``message`` consegna un
        avviso proattivo: gira su una sessione interna (heartbeat, cron, Dream)
        ma l'utente lo legge nella conversazione unificata, quindi la riga deve
        finire *lì* o il turno successivo non ne ha traccia.

        La scrittura passa dal lock di sessione condiviso (``_session_locks``),
        che è l'invariante per mutare ``session.messages``: un turno lo tiene per
        tutta la sua durata e ``_save_turn`` appende il proprio blocco in coda,
        quindi un append concorrente da un altro task infilerebbe un messaggio
        assistant tra lo user persistito early e la coppia
        ``assistant``/``tool_calls`` + ``tool`` del turno — richiesta illegale al
        provider — oltre a correre col ``sessions.save``. Anche una
        consolidation detached muta la stessa lista sotto lo stesso lock.

        Due percorsi, per non bloccare mai il tool che sta consegnando:

        - lock libero (il caso normale: l'avviso arriva a sessione utente ferma)
          → scrittura inline, così l'avviso è durabile quando il tool ritorna;
        - lock occupato (turno utente in volo, o consolidation) → task in
          background che attende il lock. ``asyncio.Lock`` è FIFO, quindi la
          riga atterra dopo il blocco del turno in corso e prima che il turno
          successivo acquisisca il lock per costruire il proprio contesto.

        Compromesso accettato: un avviso consegnato *durante* un turno utente si
        colloca dopo il blocco di quel turno, non nell'istante esatto della
        consegna. È l'unica posizione legale senza rimaneggiare i messaggi, e
        l'ordine resta monotono.
        """
        if not content.strip():
            return
        lock = self._session_locks.get(session_key)
        if not lock.locked():
            # ``Lock.acquire()`` su un lock libero non cede il controllo, quindi
            # qui non c'è finestra in cui un turno possa infilarsi: e anche se
            # cedesse, l'append avverrebbe comunque sotto lock.
            await self._record_channel_delivery_locked(session_key, content, media)
            return
        logger.debug(
            "Channel delivery for session {} deferred: session busy",
            session_key,
        )
        self._schedule_background(
            self._record_channel_delivery_locked(session_key, content, media)
        )

    async def process_direct(
        self,
        content: str,
        session_key: str = "internal:direct",
        channel: str = INTERNAL_CHANNEL,
        chat_id: str = "direct",
        media: list[str] | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        ephemeral: bool = False,
        tools: ToolRegistry | None = None,
        persist_user_message: bool = True,
        visibility: TurnVisibility | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> OutboundMessage | None:
        """Process a message directly and return the outbound payload.

        Il valore di ritorno resta il *payload*, non il ``TurnOutcome``: qui
        dentro passano Dream e Atlas, che leggono l'outbound come risultato
        interno del proprio run e non come consegna all'utente. Il tipo di esito
        vive dove si prende la decisione di consegna, cioe' in ``_dispatch``.

        Chi l'esito lo vuole davvero (l'heartbeat: gli serve ``final_text``, in
        cui il modello dichiara quali task non ha potuto eseguire) chiama
        :meth:`process_direct_outcome`, che e' lo stesso turno senza la perdita
        di informazione. Un fratello additivo invece di un tipo di ritorno piu'
        largo: questa firma e' condivisa da Dream, Atlas e dai comandi, e
        cambiarla per un solo chiamante li toccherebbe tutti.

        ``visibility`` dichiara esplicitamente se il turno puo' raggiungere
        l'utente: serve a chi gira lavoro interno su un canale *utente* (e' il
        caso dell'heartbeat, che tiene ``websocket:default`` come target cosi il
        tool ``message`` ha dove consegnare quando la condizione scatta).
        """
        outcome = await self.process_direct_outcome(
            content,
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            media=media,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            ephemeral=ephemeral,
            tools=tools,
            persist_user_message=persist_user_message,
            visibility=visibility,
            metadata=metadata,
        )
        return outcome.message

    async def process_direct_outcome(
        self,
        content: str,
        session_key: str = "internal:direct",
        channel: str = INTERNAL_CHANNEL,
        chat_id: str = "direct",
        media: list[str] | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        ephemeral: bool = False,
        tools: ToolRegistry | None = None,
        persist_user_message: bool = True,
        visibility: TurnVisibility | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TurnOutcome:
        """Come :meth:`process_direct`, ma restituisce l'esito intero del turno.

        Serve a un solo tipo di chiamante: quello che gira un turno *silenzioso*
        e ha comunque bisogno di sapere cosa e' successo dentro. Su un turno
        silenzioso il payload e' ``None`` per costruzione, quindi
        ``process_direct`` non puo' dire ne se l'agente ha parlato col tool
        ``message`` ne cosa ha scritto come risposta finale — ed e' li che
        l'heartbeat dichiara i task che non ha potuto eseguire.
        """
        metadata = dict(metadata or {})
        if not persist_user_message:
            metadata[turn_continuation.SKIP_USER_PERSIST_META] = True
        if visibility is TurnVisibility.SILENT:
            mark_silent_turn(metadata)
        msg = InboundMessage(
            channel=channel, sender_id="user", chat_id=chat_id,
            content=content, media=media or [], metadata=metadata,
        )
        # Share the dispatch lock so direct calls serialize with bus turns.
        lock = self._session_locks.get(session_key)
        # Secondo (e ultimo) ingresso di turno: cron e i comandi che rilanciano
        # l'agente passano da qui, non dal bus. Anche questi turni devono avere
        # un'identita, altrimenti le guardie per-turno dei tool si troverebbero
        # disarmate proprio nei turni interni.
        turn_id_token = bind_turn_id(_new_turn_id(session_key))
        try:
            async with lock:
                kwargs: dict[str, Any] = {
                    "session_key": session_key,
                    "on_progress": on_progress,
                    "on_stream": on_stream,
                    "on_stream_end": on_stream_end,
                    "ephemeral": ephemeral,
                }
                if tools is not None:
                    kwargs["tools"] = tools
                return await self._process_message(
                    msg,
                    **kwargs,
                )
        finally:
            reset_turn_id(turn_id_token)
            await self._runtime_events().run_status_changed(msg, session_key, "idle")
            self._runtime_events().clear_turn(session_key)
