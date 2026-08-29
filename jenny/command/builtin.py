"""Built-in slash command handlers."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jenny import __version__
from jenny.bus.events import OutboundMessage
from jenny.command.router import CommandContext, CommandRouter
from jenny.utils.helpers import build_status_content

if TYPE_CHECKING:
    from jenny.agent.atlas import AtlasOutcome


@dataclass(frozen=True)
class BuiltinCommandSpec:
    command: str
    title: str
    description: str
    icon: str
    arg_hint: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "command": self.command,
            "title": self.title,
            "description": self.description,
            "icon": self.icon,
            "arg_hint": self.arg_hint,
        }


BUILTIN_COMMAND_SPECS: tuple[BuiltinCommandSpec, ...] = (
    BuiltinCommandSpec(
        "/new",
        "New chat",
        "Stop the current task and start a fresh conversation.",
        "square-pen",
    ),
    BuiltinCommandSpec(
        "/stop",
        "Stop current task",
        "Cancel the active agent turn for this chat.",
        "square",
    ),
    BuiltinCommandSpec(
        "/status",
        "Show status",
        "Display runtime, provider, and channel status.",
        "activity",
    ),
    BuiltinCommandSpec(
        "/model",
        "Switch model preset",
        "Show or switch the active model preset.",
        "brain",
        "[preset]",
    ),
    BuiltinCommandSpec(
        "/history",
        "Show conversation history",
        "Print the last N persisted conversation messages.",
        "history",
        "[n]",
    ),
    BuiltinCommandSpec(
        "/goal",
        "Start long-running goal",
        "Tell the agent to treat the request as a long-running goal.",
        "activity",
        "<goal>",
    ),
    BuiltinCommandSpec(
        "/dream",
        "Run Dream",
        "Manually trigger memory consolidation.",
        "sparkles",
    ),
    BuiltinCommandSpec(
        "/atlas",
        "Run Atlas",
        "Rebuild the wiki directory in memory/WIKI.md. Add 'force' to skip the change check.",
        "map",
        "[force]",
    ),
    BuiltinCommandSpec(
        "/skill",
        "List skills",
        "List enabled skills and their descriptions.",
        "puzzle",
    ),
    BuiltinCommandSpec(
        "/sessions",
        "List sessions",
        "Show all chat sessions with titles and message counts.",
        "messages",
    ),
    BuiltinCommandSpec(
        "/help",
        "Show help",
        "List available slash commands.",
        "circle-help",
    ),
)


async def cmd_stop(ctx: CommandContext) -> OutboundMessage:
    """Cancel all active tasks and subagents for the session."""
    loop = ctx.loop
    msg = ctx.msg
    total = await loop._cancel_active_tasks(ctx.key)
    if total:
        # Il turno ripudiato salta il proprio restore/turn_end: li emette /stop,
        # in modo sincrono e deterministico (la UI riceve sempre turn_end).
        loop._restore_cancelled_turn(ctx.key)
        await loop._emit_stop_turn_end(msg, ctx.key)
    content = f"Stopped {total} task(s)." if total else "No active task to stop."
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content=content,
        metadata=dict(msg.metadata or {})
    )


async def cmd_status(ctx: CommandContext) -> OutboundMessage:
    """Build an outbound status message for a session."""
    loop = ctx.loop
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    ctx_est = 0
    with suppress(Exception):
        ctx_est, _ = loop.consolidator.estimate_session_prompt_tokens(session)
    if ctx_est <= 0:
        ctx_est = loop._last_usage.get("prompt_tokens", 0)

    active_tasks = loop._active_tasks.get(ctx.key, [])
    task_count = sum(1 for t in active_tasks if not t.done())
    with suppress(Exception):
        task_count += loop.subagents.get_running_count_by_session(ctx.key)
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=build_status_content(
            version=__version__, model=loop.model,
            start_time=loop._start_time, last_usage=loop._last_usage,
            context_window_tokens=loop.context_window_tokens,
            session_msg_count=len(session.get_history(max_messages=0)),
            context_tokens_estimate=ctx_est,
            active_task_count=task_count,
            max_completion_tokens=getattr(
                getattr(loop.provider, "generation", None), "max_tokens", 8192
            ),
        ),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_new(ctx: CommandContext) -> OutboundMessage:
    """Stop active task and archive the current session, then create a new one.

    The old session is preserved on disk with all its messages. A new session
    with a unique key is created, and the client is instructed to switch to it
    via the ``_session_switch`` metadata event.
    """
    from jenny.session.keys import new_session_key

    loop = ctx.loop
    cancelled = await loop._cancel_active_tasks(ctx.key)
    if cancelled:
        # Materializza il lavoro parziale del turno fermato PRIMA dello
        # snapshot, così finisce nell'archivio invece di andare perso.
        loop._restore_cancelled_turn(ctx.key)
        await loop._emit_stop_turn_end(ctx.msg, ctx.key)
    # Archive the old session's unconsolidated messages to history.
    old_session = ctx.session or loop.sessions.get_or_create(ctx.key)
    snapshot = old_session.messages[old_session.last_consolidated:]
    if snapshot:
        loop._schedule_background(
            loop.consolidator.archive(snapshot, session_key=ctx.key)
        )
    # Create a genuinely new session with a unique key.
    new_key = new_session_key()
    new_session = loop.sessions.get_or_create(new_key)
    new_session.metadata["webui"] = True
    loop.sessions.save(new_session)
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content="New session started.",
        metadata={
            **dict(ctx.msg.metadata or {}),
            "_session_boundary": True,
            # _session_switch tells the WebUI client to switch to the new
            # session key. The client stores this mapping and includes the
            # session_key_override in subsequent messages.
            "_session_switch": True,
            "_new_session_key": new_key,
        },
    )


def _format_preset_names(names: list[str]) -> str:
    return ", ".join(f"`{name}`" for name in names) if names else "(none configured)"


def _model_preset_names(loop) -> list[str]:
    return sorted(loop.model_presets)


def _active_model_preset_name(loop) -> str:
    return loop.model_preset or "(none)"


def _command_error_message(exc: Exception) -> str:
    return str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)


def _model_command_status(loop) -> str:
    names = _model_preset_names(loop)
    active = _active_model_preset_name(loop)
    return "\n".join([
        "## Model",
        f"- Current model: `{loop.model}`",
        f"- Current preset: `{active}`",
        f"- Available presets: {_format_preset_names(names)}",
    ])


async def cmd_model(ctx: CommandContext) -> OutboundMessage:
    """Show or switch model presets."""
    loop = ctx.loop
    args = ctx.args.strip()
    metadata = {**dict(ctx.msg.metadata or {}), "render_as": "text"}

    if not args:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=_model_command_status(loop),
            metadata=metadata,
        )

    parts = args.split()
    if len(parts) != 1:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Usage: `/model [preset]`",
            metadata=metadata,
        )

    name = parts[0]
    try:
        await loop.set_model_preset(name)
    except (KeyError, ValueError) as exc:
        names = _model_preset_names(loop)
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=(
                f"Could not switch model preset: {_command_error_message(exc)}\n\n"
                f"Available presets: {_format_preset_names(names)}"
            ),
            metadata=metadata,
        )

    max_tokens = getattr(getattr(loop.provider, "generation", None), "max_tokens", None)
    lines = [
        f"Switched model preset to `{loop.model_preset}`.",
        f"- Model: `{loop.model}`",
        f"- Context window: {loop.context_window_tokens}",
    ]
    if max_tokens is not None:
        lines.append(f"- Max output tokens: {max_tokens}")
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content="\n".join(lines),
        metadata=metadata,
    )


_DREAM_BUSY_MESSAGE = (
    "Dream is already running (scheduled consolidation); try again in a few minutes."
)


async def cmd_dream(ctx: CommandContext) -> OutboundMessage:
    """Manually trigger a Dream consolidation run."""
    from jenny.runtime.dream_lock import (
        dream_lock_locked,
        release_dream_lock,
        try_acquire_dream_lock,
    )

    loop = ctx.loop
    msg = ctx.msg

    # Guardia sincrona: se il cron Dream è in volo, rispondiamo subito invece
    # di far partire un secondo run che colliderebbe (stesso cursore, stessi
    # file) e brucerebbe un intero passaggio LLM. Il task in background ripete
    # la guardia con ``try_acquire_dream_lock`` per chiudere la finestra di
    # race fra questo check e l'effettiva acquisizione.
    if dream_lock_locked():
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content=_DREAM_BUSY_MESSAGE,
            metadata={"render_as": "text"},
        )

    async def _run_dream():
        async def _silent(*_args, **_kwargs):
            pass

        from jenny.agent.memory import MemoryStore

        dream_session_key = MemoryStore.dream_session_key
        prune_dream_sessions = MemoryStore.prune_dream_sessions

        store = loop.context.memory
        content = ""
        resp = None
        t0 = time.monotonic()
        acquired = False
        try:
            if not await try_acquire_dream_lock():
                await loop.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content=_DREAM_BUSY_MESSAGE,
                    metadata={"render_as": "text"},
                ))
                return
            acquired = True
            # Lettura di history.jsonl (potenzialmente grande) FUORI dal loop:
            # ``build_dream_prompt`` legge e parsifica l'intero file in modo
            # sincrono, e farlo qui congelerebbe WebSocket/HTTP — e con loro
            # l'input utente — per tutta la durata della lettura. Rileggere il
            # prompt DENTRO il lock copre anche il caso "un altro run ha già
            # consolidato": ``build_dream_prompt`` ritorna ``None``.
            result = await asyncio.to_thread(store.build_dream_prompt)
            if result is None:
                await loop.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content=_format_dream_no_input_message(),
                    metadata={"render_as": "text"},
                ))
                return
            prompt, last_cursor = result
            key = dream_session_key()
            dream_tools = store.build_dream_tools()
            resp = await loop.process_direct(
                prompt,
                session_key=key,
                ephemeral=True,
                tools=dream_tools,
                on_progress=_silent,
            )
            elapsed = time.monotonic() - t0
            # ``getattr``: il registry Dream espone ``file_states``, ma il
            # contratto resta tollerante verso registry di altra provenienza.
            dream_file_states = getattr(dream_tools, "file_states", None)
            if MemoryStore.dream_should_advance_cursor(resp, dream_file_states):
                store.set_last_dream_cursor(last_cursor)
                content = f"Dream completed in {elapsed:.1f}s."
            elif MemoryStore.dream_run_completed(resp):
                content = (
                    f"Dream completed in {elapsed:.1f}s but wrote nothing "
                    "(attempts blocked/refused); memory cursor was not advanced."
                )
            else:
                content = (
                    f"Dream did not complete after {elapsed:.1f}s; "
                    "memory cursor was not advanced."
                )
        except Exception as e:
            elapsed = time.monotonic() - t0
            content = f"Dream failed after {elapsed:.1f}s: {e}"
        finally:
            if acquired:
                release_dream_lock()
            from jenny.agent.token_usage import record_response_token_usage

            record_response_token_usage(
                resp,
                source="dream",
                timezone_name=getattr(loop.context, "timezone", None),
            )
            await asyncio.to_thread(store.compact_history)
            pruned_keys = prune_dream_sessions(loop.sessions.sessions_dir)
            if pruned_keys:
                loop.evict_pruned_sessions(pruned_keys)
        await loop.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
        ))

    asyncio.create_task(_run_dream())
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content="Dreaming...",
    )


def _format_dream_no_input_message() -> str:
    return "\n".join([
        "Dream has no conversation history to process yet.",
        "",
        "Dream reads new entries from `memory/history.jsonl` after the current Dream cursor.",
        (
            "Short chats only reach that file after token compaction or idle auto-compact, "
            "so a fresh or short WebUI chat may leave Dream with no input."
        ),
        "",
        "Next steps:",
        "- Enable `agents.defaults.idleCompactAfterMinutes` so completed chats become Dream input automatically.",
        "- Compact the current chat into memory once that manual action is available.",
        "- If you expected history to exist, check whether `memory/history.jsonl` has new entries after the Dream cursor.",
    ])


async def cmd_atlas(ctx: CommandContext) -> OutboundMessage:
    """Manually trigger an Atlas run (rebuild the wiki directory)."""
    loop = ctx.loop
    msg = ctx.msg
    force = ctx.args.strip().lower() == "force"

    async def _run():
        from jenny.agent.atlas import AtlasStore, run_atlas
        from jenny.config.loader import load_config

        try:
            config = load_config()
            store = AtlasStore.from_config(config.workspace_path, config)
            outcome = await run_atlas(loop, store=store, force=force)
            content = _format_atlas_outcome(outcome)
        except Exception as e:
            content = f"Atlas failed: {e}"
        await loop.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
            metadata={"render_as": "text"},
        ))

    asyncio.create_task(_run())
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content="Mapping the wiki...",
    )


def _format_atlas_outcome(outcome: "AtlasOutcome") -> str:
    """Messaggio utente per un run Atlas.

    Gli esiti "non ho fatto niente" hanno messaggi distinti apposta: un comando
    che risponde "fatto" senza aver fatto nulla è peggio di uno che dice perché.
    """
    elapsed = f"{outcome.elapsed:.1f}s"
    if outcome.status == "skipped_no_wikis":
        return (
            "Atlas found no wikis to map.\n\n"
            "It reads `workspace/wikis/<name>/wiki/`. Ask me to create a wiki first, "
            "then run `/atlas` again."
        )
    if outcome.status == "skipped_unchanged":
        return (
            "The wiki hasn't changed since the last Atlas run, so `memory/WIKI.md` is "
            "already current — no tokens spent. Use `/atlas force` to rebuild it anyway."
        )
    if outcome.status == "written":
        return f"Atlas updated `memory/WIKI.md` in {elapsed}."
    if outcome.status == "no_write":
        return (
            f"Atlas finished in {elapsed} without writing (attempts blocked or refused); "
            "the wiki fingerprint was not advanced, so the next run will retry."
        )
    if outcome.status == "incomplete":
        return f"Atlas did not complete after {elapsed}; the directory was left untouched."
    return f"Atlas failed after {elapsed}: {outcome.detail}"


_HISTORY_DEFAULT_COUNT = 10
_HISTORY_MAX_COUNT = 50
_HISTORY_MAX_CONTENT_CHARS = 200


def _format_history_message(msg: dict) -> str | None:
    """Format a single history message for display. Returns None to skip."""
    role = msg.get("role")
    if role not in ("user", "assistant"):
        return None
    content = msg.get("content") or ""
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        content = " ".join(parts)
    content = str(content).strip()
    if not content:
        return None
    if len(content) > _HISTORY_MAX_CONTENT_CHARS:
        content = content[:_HISTORY_MAX_CONTENT_CHARS] + "…"
    label = "👤 You" if role == "user" else "🤖 Bot"
    return f"{label}: {content}"


async def cmd_history(ctx: CommandContext) -> OutboundMessage:
    """Show the last N messages of the current session (default 10, max 50).

    Usage: /history [count]
    """
    count = _HISTORY_DEFAULT_COUNT
    if ctx.args.strip():
        try:
            count = max(1, min(int(ctx.args.strip()), _HISTORY_MAX_COUNT))
        except ValueError:
            return OutboundMessage(
                channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
                content="Usage: /history [count] — e.g. /history 5 (default: 10, max: 50)",
                metadata=dict(ctx.msg.metadata or {}),
            )

    session = ctx.session or ctx.loop.sessions.get_or_create(ctx.key)
    history = session.get_history(max_messages=0)
    visible = [_format_history_message(m) for m in history]
    visible = [m for m in visible if m is not None]
    recent = visible[-count:]

    if not recent:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="No conversation history yet.",
            metadata=dict(ctx.msg.metadata or {}),
        )

    header = f"Last {len(recent)} message(s):\n"
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=header + "\n".join(recent),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


_GOAL_PROMPT_TEMPLATE = """The user declared a sustained objective for this thread.

Inspect or clarify if needed, then call `long_task` with the refined objective (and optional short ui_summary). Work proceeds as normal assistant turns using your usual tools. When the objective is fully done and verified, call `complete_goal` with a brief recap. If the user later cancels or changes direction, still call `complete_goal` with an honest recap (then `long_task` again only after there is no active goal). Do not use `long_task` / `complete_goal` for trivial one-shot answers.

Goal:
{goal}
"""


async def cmd_goal(ctx: CommandContext) -> OutboundMessage | None:
    """Rewrite /goal into a normal agent turn that nudges long_task use."""
    goal = ctx.args.strip()
    if not goal:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Usage: /goal <long-running task description>",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    current_task = asyncio.current_task()
    active_tasks = ctx.loop._active_tasks.get(ctx.key, [])
    running = sum(1 for t in active_tasks if t is not current_task and not t.done())
    running += ctx.loop.subagents.get_running_count_by_session(ctx.key)
    if running > 0:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=(
                "A task is already running for this chat. "
                "Use `/stop` first, then send `/goal <long-running task description>` again."
            ),
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )

    ctx.msg.metadata = {
        **dict(ctx.msg.metadata or {}),
        "original_command": "/goal",
        "goal_started_at": time.time(),
    }
    ctx.msg.content = _GOAL_PROMPT_TEMPLATE.format(goal=goal)
    return None


async def cmd_skill(ctx: CommandContext) -> OutboundMessage:
    """List all enabled skills (name and description only)."""
    loop = ctx.loop
    skills = loop.context.skills.list_skills(filter_unavailable=False)
    if not skills:
        content = "No skills available."
    else:
        lines = [f"Available skills ({len(skills)}):", ""]
        for entry in skills:
            desc = loop.context.skills._get_skill_description(entry["name"])
            lines.append(f"- **{entry['name']}** — {desc}")
        content = "\n".join(lines)
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata=dict(ctx.msg.metadata or {}),
    )

async def cmd_sessions(ctx: CommandContext) -> OutboundMessage:
    """List all user sessions with titles and metadata.

    Usage: /sessions
    """
    loop = ctx.loop
    sessions = loop.sessions.list_user_sessions()
    if not sessions:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="No sessions found.",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    current_key = ctx.key
    lines = [f"Sessions ({len(sessions)}):", ""]
    for s in sessions:
        key = s.get("key", "?")
        title = s.get("metadata", {}).get("title") or "(untitled)"
        updated = s.get("updated_at", "")
        msg_count = len(
            loop.sessions.get_or_create(key).get_history(max_messages=0)
        )
        marker = " →" if key == current_key else "  "
        lines.append(
            f"{marker} {title}  [{msg_count} msgs, updated {updated[:10]}]"
        )
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content="\n".join(lines),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_help(ctx: CommandContext) -> OutboundMessage:
    """Return available slash commands."""
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=build_help_text(),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


def build_help_text() -> str:
    """Build canonical help text shared across channels."""
    lines = ["✿ jenny commands:"]
    for spec in BUILTIN_COMMAND_SPECS:
        command = spec.command
        if spec.arg_hint:
            command = f"{command} {spec.arg_hint}"
        lines.append(f"{command} — {spec.description}")
    return "\n".join(lines)


def register_builtin_commands(router: CommandRouter) -> None:
    """Register the default set of slash commands."""
    router.priority("/stop", cmd_stop)
    router.priority("/status", cmd_status)
    router.exact("/new", cmd_new)
    router.exact("/model", cmd_model)
    router.prefix("/model ", cmd_model)
    router.exact("/history", cmd_history)
    router.prefix("/history ", cmd_history)
    router.exact("/goal", cmd_goal)
    router.prefix("/goal ", cmd_goal)
    router.exact("/dream", cmd_dream)
    router.exact("/atlas", cmd_atlas)
    router.prefix("/atlas ", cmd_atlas)
    router.exact("/sessions", cmd_sessions)
    router.exact("/skill", cmd_skill)
    router.exact("/help", cmd_help)
