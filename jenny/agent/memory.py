"""Memory system: pure file I/O store and lightweight Consolidator."""

from __future__ import annotations

import json
import os
import threading
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from loguru import logger

from jenny.utils.helpers import (
    ensure_dir,
    strip_think,
    truncate_text,
    truncate_text_to_tokens,
)
from jenny.utils.path import atomic_write
from jenny.utils.prompt_templates import render_template

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# MemoryStore — pure file I/O layer
# ---------------------------------------------------------------------------

class MemoryStore:
    """Pure file I/O for memory files: MEMORY.md, history.jsonl, SOUL.md, USER.md."""

    _DEFAULT_MAX_HISTORY = 1000
    _INTERNAL_HISTORY_SESSION_PREFIXES = ("cron:", "dream:", "atlas:")
    _INTERNAL_HISTORY_SESSION_KEYS = {"heartbeat"}

    def __init__(self, workspace: Path, max_history_entries: int = _DEFAULT_MAX_HISTORY):
        self.workspace = workspace
        self.max_history_entries = max_history_entries
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        # Rubrica compilata da Atlas a partire da workspace/wikis/. Vive qui
        # accanto a MEMORY.md perché è memoria a tutti gli effetti, ma è un file
        # distinto con un proprietario distinto: Dream non ha il permesso di
        # scriverlo e Atlas non ha il permesso di scrivere MEMORY.md.
        self.wiki_file = self.memory_dir / "WIKI.md"
        self.history_file = self.memory_dir / "history.jsonl"
        self.soul_file = workspace / "SOUL.md"
        self.user_file = workspace / "USER.md"
        self._cursor_file = self.memory_dir / ".cursor"
        self._dream_cursor_file = self.memory_dir / ".dream_cursor"
        self._turn_counter_file = self.memory_dir / ".turn_counter"
        self._corruption_logged = False  # rate-limit non-int cursor warning
        self._malformed_entry_logged = False  # rate-limit bad history shape warning
        self._oversize_logged = False  # rate-limit oversized-entry warning
        self._append_lock = threading.Lock()  # serialize cursor allocation + append

    # -- generic helpers -----------------------------------------------------

    @staticmethod
    def read_file(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    # -- MEMORY.md (long-term facts) -----------------------------------------

    def read_memory(self) -> str:
        return self.read_file(self.memory_file)

    # -- WIKI.md (wiki directory, managed by Atlas) --------------------------

    def read_wiki_memory(self) -> str:
        return self.read_file(self.wiki_file)

    # -- context injection (used by context.py) ------------------------------

    def get_memory_context(self) -> str:
        long_term = self.read_memory()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    def get_wiki_memory_context(self, max_tokens: int | None = None) -> str:
        """Blocco rubrica per il system prompt, troncato al tetto configurato.

        Il troncamento sta qui e non nel prompt di Atlas perché è l'ultima
        linea di difesa: un run che produce un file lungo il doppio del dovuto
        peserebbe altrimenti su ogni turno fino al run successivo.
        """
        content = self.read_wiki_memory().strip()
        if not content:
            return ""
        if max_tokens is not None and max_tokens > 0:
            content = truncate_text_to_tokens(content, max_tokens)
        return f"## Wiki Directory\n{content}"

    # -- history.jsonl — append-only, JSONL format ---------------------------

    def append_history(
        self,
        entry: str,
        *,
        max_chars: int | None = None,
        session_key: str | None = None,
    ) -> int:
        """Append *entry* to history.jsonl and return its auto-incrementing cursor.

        Entries are passed through `strip_think` to drop template-level leaks
        (e.g. unclosed `<think` prefixes, `<channel|>` markers) before being
        persisted. If the cleaned content is empty but the raw entry wasn't,
        the record is persisted with an empty string rather than falling back
        to the raw leak — otherwise `strip_think`'s guarantees would be
        undone by history replay / consolidation downstream.

        A defensive cap (*max_chars*, default ``_HISTORY_ENTRY_HARD_CAP``) is
        applied as a final safety net: individual callers should cap their own
        content more tightly; this default only exists to catch unintentional
        large writes (e.g. an LLM echoing its input back as a "summary").
        """
        limit = max_chars if max_chars is not None else _HISTORY_ENTRY_HARD_CAP
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        raw = entry.rstrip()
        if len(raw) > limit:
            if not self._oversize_logged:
                self._oversize_logged = True
                logger.warning(
                    "history entry exceeds {} chars ({}); truncating. "
                    "Usually means a caller forgot its own cap; "
                    "further occurrences suppressed.",
                    limit, len(raw),
                )
            raw = truncate_text(raw, limit)
        content = strip_think(raw)
        # Cursor allocation and the append must be atomic: concurrent writers
        # could otherwise read the same current cursor and emit duplicates.
        with self._append_lock:
            cursor = self._next_cursor()
            if raw and not content:
                logger.debug(
                    "history entry {} stripped to empty (likely template leak); "
                    "persisting empty content to avoid re-polluting context",
                    cursor,
                )
            record = {"cursor": cursor, "timestamp": ts, "content": content}
            if session_key:
                record["session_key"] = session_key
            with open(self.history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            # Small full-file replacement each append: use the same atomic
            # temp-file+fsync+rename helper as every other on-disk cursor/state
            # file in this codebase (cron store, session manager, sidebar
            # state, ...), rather than a bare write_text with no durability.
            atomic_write(self._cursor_file, str(cursor))
        return cursor

    @staticmethod
    def _valid_cursor(value: Any) -> int | None:
        """Int cursors only — reject bool (``isinstance(True, int)`` is True)."""
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    def _iter_valid_entries(self) -> Iterator[tuple[dict[str, Any], int]]:
        """Yield ``(entry, cursor)`` for well-formed entries; warn once on corruption."""
        poisoned: Any = None
        malformed_cursor: int | None = None
        for entry in self._read_entries():
            raw = entry.get("cursor")
            if raw is None:
                continue
            cursor = self._valid_cursor(raw)
            if cursor is None:
                poisoned = raw
                continue
            if not self._valid_history_payload(entry):
                malformed_cursor = cursor
                continue
            yield entry, cursor
        if poisoned is not None and not self._corruption_logged:
            self._corruption_logged = True
            logger.warning(
                "history.jsonl contains a non-int cursor ({!r}); dropping it. "
                "Usually caused by an external writer; further occurrences suppressed.",
                poisoned,
            )
        if malformed_cursor is not None and not self._malformed_entry_logged:
            self._malformed_entry_logged = True
            logger.warning(
                "history.jsonl contains a malformed entry at cursor {}; dropping it. "
                "Usually caused by an external writer; further occurrences suppressed.",
                malformed_cursor,
            )

    @staticmethod
    def _valid_history_payload(entry: dict[str, Any]) -> bool:
        if not isinstance(entry.get("timestamp"), str):
            return False
        if not isinstance(entry.get("content"), str):
            return False
        session_key = entry.get("session_key")
        return session_key is None or isinstance(session_key, str)

    def _next_cursor(self) -> int:
        """Return the next cursor value, robust to a crash between the history
        append and the ``.cursor`` write.

        ``append_history`` fsyncs the new entry *before* atomically rewriting
        ``.cursor``; a process kill in that window (frequent on Android) leaves
        ``.cursor`` one behind the last persisted entry.  Trusting ``.cursor``
        alone would then re-allocate a cursor already on disk, producing
        duplicates that break ``read_unprocessed_history`` and the Dream
        cursor.  So we always consider *both* sources — the ``.cursor`` file
        and the last persisted entry — and take the maximum.  ``max`` also
        preserves monotonicity in the inverse case (history externally
        truncated below a higher ``.cursor``): a cursor is never reused.
        """
        candidates: list[int] = []
        if self._cursor_file.exists():
            with suppress(ValueError, OSError):
                file_cursor = self._valid_cursor(
                    int(self._cursor_file.read_text(encoding="utf-8").strip())
                )
                if file_cursor is not None:
                    candidates.append(file_cursor)
        last = self._read_last_entry() or {}
        entry_cursor = self._valid_cursor(last.get("cursor"))
        if entry_cursor is not None:
            candidates.append(entry_cursor)
        if candidates:
            return max(candidates) + 1
        # Both fast paths unusable — scan the whole file and take ``max``,
        # which stays correct even if the monotonic invariant was broken by
        # external writes.
        return max((c for _, c in self._iter_valid_entries()), default=0) + 1

    def read_unprocessed_history(self, since_cursor: int) -> list[dict[str, Any]]:
        """Return history entries with a valid cursor > *since_cursor*."""
        return [e for e, c in self._iter_valid_entries() if c > since_cursor]

    @classmethod
    def _is_internal_history_session(cls, session_key: str | None) -> bool:
        if not session_key:
            return False
        return (
            session_key in cls._INTERNAL_HISTORY_SESSION_KEYS
            or session_key.startswith(cls._INTERNAL_HISTORY_SESSION_PREFIXES)
        )

    def read_recent_history_for_prompt(
        self,
        since_cursor: int,
        *,
        session_key: str | None,
    ) -> list[dict[str, Any]]:
        """Return unprocessed history entries safe to inject into a turn prompt."""
        entries = self.read_unprocessed_history(since_cursor=since_cursor)
        if session_key is None:
            return entries
        return [
            entry
            for entry in entries
            if (entry_session := entry.get("session_key")) == session_key
            or not self._is_internal_history_session(entry_session)
        ]

    def compact_history(self) -> None:
        """Drop oldest entries if the file exceeds *max_history_entries*.

        The read→rewrite must hold ``_append_lock``: ``append_history`` runs on
        real threads (Consolidator via ``asyncio.to_thread``) and fsyncs a new
        entry before returning. Without the lock, an append landing between our
        ``_read_entries`` and the atomic ``_write_entries`` rewrite would be
        silently dropped by the rename. Holding the lock serializes the two:
        a concurrent append blocks until the rewrite completes, then appends on
        top of the compacted file.

        No self-deadlock: ``_append_lock`` is a non-reentrant ``threading.Lock``
        and nothing under it re-enters ``append_history`` or ``compact_history``
        (``_read_entries`` / ``_write_entries`` are pure file I/O). No caller
        holds the lock when invoking this method.
        """
        if self.max_history_entries <= 0:
            return
        with self._append_lock:
            entries = self._read_entries()
            if len(entries) <= self.max_history_entries:
                return
            kept = entries[-self.max_history_entries:]
            self._write_entries(kept)

    # -- JSONL helpers -------------------------------------------------------

    def _read_entries(self) -> list[dict[str, Any]]:
        """Read all entries from history.jsonl."""
        entries: list[dict[str, Any]] = []
        with suppress(FileNotFoundError):
            with open(self.history_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue

        return entries

    def _read_last_entry(self) -> dict[str, Any] | None:
        """Read the last entry from the JSONL file efficiently."""
        try:
            with open(self.history_file, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if size == 0:
                    return None
                read_size = min(size, 4096)
                f.seek(size - read_size)
                data = f.read().decode("utf-8")
                lines = [line for line in data.split("\n") if line.strip()]
                if not lines:
                    return None
                return json.loads(lines[-1])
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _write_entries(self, entries: list[dict[str, Any]]) -> None:
        """Overwrite history.jsonl with the given entries (atomic write)."""
        content = "".join(
            json.dumps(entry, ensure_ascii=False) + "\n"
            for entry in entries
        )
        atomic_write(self.history_file, content)

    # -- dream cursor --------------------------------------------------------

    def get_last_dream_cursor(self) -> int:
        if self._dream_cursor_file.exists():
            with suppress(ValueError, OSError):
                return int(self._dream_cursor_file.read_text(encoding="utf-8").strip())
        return 0

    def set_last_dream_cursor(self, cursor: int) -> None:
        # Stesso helper del cursore di history (vedi ``append``): un
        # write_text nudo qui lascerebbe, se il processo muore a metà, un file
        # vuoto o parziale — cioè un cursore che ``get_last_dream_cursor``
        # legge come 0 e Dream ricomincia da capo su tutta la storia.
        atomic_write(self._dream_cursor_file, str(cursor))

    # -- turn counter (event-based Dream trigger) ----------------------------

    def get_turn_counter(self) -> int:
        """Ritorna il conteggio dei turni completati dall'ultimo Dream."""
        if self._turn_counter_file.exists():
            with suppress(ValueError, OSError):
                return int(self._turn_counter_file.read_text(encoding="utf-8").strip())
        return 0

    def increment_turn_counter(self) -> int:
        """Incrementa il contatore e ritorna il nuovo valore."""
        current = self.get_turn_counter()
        new_val = current + 1
        atomic_write(self._turn_counter_file, str(new_val))
        return new_val

    def reset_turn_counter(self) -> None:
        """Resetta il contatore a zero (chiamato dopo un Dream completato)."""
        atomic_write(self._turn_counter_file, "0")

    def build_dream_prompt(self, *, max_entries: int = 20) -> tuple[str, int] | None:
        """Build the Dream prompt with unprocessed history context.

        Returns ``(prompt, last_cursor)`` or ``None`` if nothing to process.
        """
        last_cursor = self.get_last_dream_cursor()
        entries = self.read_unprocessed_history(since_cursor=last_cursor)
        if not entries:
            return None

        batch = entries[:max_entries]
        history_text = "\n".join(
            f"[{e['timestamp']}] {truncate_text(e['content'], 500)}"
            for e in batch
        )
        skill_creator_path = str(self.workspace / "skills" / "skill-creator" / "SKILL.md")
        template = render_template(
            "agent/dream.md", strip=True, skill_creator_path=skill_creator_path,
        )
        prompt = f"{template}\n\n## Conversation History\n{history_text}"
        return (prompt, batch[-1]["cursor"])

    def build_dream_tools(self):
        """Build the restricted tool registry used by Dream runs.

        Il ``FileStates`` creato per il run viene esposto come attributo
        ``file_states`` del registry restituito: è per-run (nessuna condivisione
        tra Dream concorrenti) e traccia scritture tentate/riuscite, così il
        chiamante può decidere via :meth:`dream_should_advance_cursor` se
        avanzare il cursore.
        """
        from jenny.agent.tools.apply_patch import ApplyPatchTool
        from jenny.agent.tools.file_state import FileStates
        from jenny.agent.tools.filesystem import EditFileTool, ReadFileTool, WriteFileTool
        from jenny.agent.tools.registry import ToolRegistry

        tools = ToolRegistry()
        file_states = FileStates()
        # Canonicalizza la radice del workspace e i file editabili. Su Android il
        # filesystem espone la dir dati come ``/data/user/0/<pkg>`` ma ``.resolve()``
        # la canonicalizza in ``/data/data/<pkg>``: se la base di risoluzione dei
        # path e la allowlist di file esatti (``extra_write_allowed_files``) restano
        # in forme diverse, il guard anti-symlink di ``_is_path_exactly_allowed``
        # (logico via ``abspath`` vs risolto via ``.resolve()``) scatta e Dream non
        # riesce a scrivere MEMORY/SOUL/USER. Risolvendo entrambi i lati qui le due
        # forme coincidono, senza indebolire la protezione contro gli escape via
        # symlink *interni* al workspace (lì logico e risolto continuano a divergere).
        workspace = self.workspace.resolve()
        skills_dir = workspace / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)

        extra_read = [skills_dir] if skills_dir.exists() else None
        editable_files = [
            self.memory_file.resolve(),
            self.soul_file.resolve(),
            self.user_file.resolve(),
        ]

        tools.register(ReadFileTool(
            workspace=workspace,
            allowed_dir=workspace,
            extra_read_allowed_dirs=extra_read,
            file_states=file_states,
        ))
        tools.register(EditFileTool(
            workspace=workspace,
            allowed_dir=skills_dir,
            extra_write_allowed_files=editable_files,
            file_states=file_states,
        ))
        tools.register(ApplyPatchTool(
            workspace=workspace,
            allowed_dir=skills_dir,
            extra_write_allowed_files=editable_files,
            file_states=file_states,
        ))
        tools.register(WriteFileTool(
            workspace=workspace,
            allowed_dir=skills_dir,
            file_states=file_states,
        ))
        # Skill management tool: permette a Dream di creare/aggiornare/cancellare
        # skill in base a pattern osservati nella history.
        from jenny.agent.tools.skill_manage import SkillManageTool
        tools.register(SkillManageTool(
            workspace=workspace,
            file_states=file_states,
        ))
        # Esposto per ``dream_should_advance_cursor``: stesso oggetto usato da
        # tutti i tool sopra (passato esplicitamente ai costruttori), quindi
        # riflette le scritture del run.
        tools.file_states = file_states
        return tools

    @staticmethod
    def internal_run_completed(resp: object | None) -> bool:
        """Return True only when an ephemeral internal agent turn completed cleanly."""
        metadata = getattr(resp, "metadata", None)
        return isinstance(metadata, dict) and metadata.get("_stop_reason") == "completed"

    @staticmethod
    def dream_run_completed(resp: object | None) -> bool:
        """Return True only when an ephemeral Dream agent turn completed cleanly."""
        return MemoryStore.internal_run_completed(resp)

    @staticmethod
    def internal_run_should_commit(
        resp: object | None,
        file_states: object | None,
    ) -> bool:
        """Return True quando un run interno può registrare il proprio progresso.

        Regola condivisa da Dream (avanzamento del cursore su ``history.jsonl``)
        e da Atlas (avanzamento del fingerprint della wiki). In entrambi i casi
        il progresso è un'affermazione — "questo input è stato digerito" — e
        farla dopo un run che non ha prodotto nulla per un blocco di policy
        significa perdere quell'input per sempre. Si registra quindi solo se il
        run:

        - è completato pulito (``internal_run_completed``), **e**
        - ha scritto almeno un file (``writes_ok > 0``), **oppure** non ha mai
          tentato una scrittura (``writes_attempted == 0``) — il caso legittimo
          "non c'era niente da cambiare".

        Se ha tentato scritture e nessuna è riuscita NON si registra: l'input va
        riprocessato al run seguente.

        ``file_states`` è tollerante a ``None`` / oggetti senza i contatori
        (fallback conservativo: nessun avanzamento) per non far esplodere il
        chiamante se il registry non è quello costruito qui.
        """
        if not MemoryStore.internal_run_completed(resp):
            return False
        writes_ok = getattr(file_states, "writes_ok", None)
        writes_attempted = getattr(file_states, "writes_attempted", None)
        if not isinstance(writes_ok, int) or not isinstance(writes_attempted, int):
            return False
        if writes_ok > 0:
            return True
        return writes_attempted == 0

    @staticmethod
    def dream_should_advance_cursor(
        resp: object | None,
        file_states: object | None,
    ) -> bool:
        """Return True only when the Dream cursor may safely advance.

        Un turno che completa pulito non basta: se Dream non produce alcuna
        scrittura perché è stato bloccato (policy) o ha rifiutato, avanzare il
        cursore perderebbe per sempre quelle voci di history (consolidamento
        silenziosamente saltato). Perciò si avanza solo quando il run:

        - è completato pulito (``dream_run_completed``), **e**
        - ha scritto almeno un file (``writes_ok > 0``), **oppure** non ha mai
          tentato una scrittura (``writes_attempted == 0``) — il caso legittimo
          "nulla da consolidare".

        Se invece ha tentato scritture ma nessuna è riuscita (tutte bloccate o
        fallite) NON si avanza: quelle voci vanno riprocessate al run seguente.

        ``file_states`` è tollerante a ``None`` / oggetti senza i contatori
        (fallback conservativo: nessun avanzamento) per non far esplodere il
        chiamante se il registry non è quello di :meth:`build_dream_tools`.
        """
        return MemoryStore.internal_run_should_commit(resp, file_states)

    # -- message formatting utility ------------------------------------------

    @staticmethod
    def _format_messages(messages: list[dict]) -> str:
        lines = []
        for message in messages:
            if not message.get("content"):
                continue
            tools = f" [tools: {', '.join(message['tools_used'])}]" if message.get("tools_used") else ""
            lines.append(
                f"[{message.get('timestamp', '?')[:16]}] {message['role'].upper()}{tools}: {message['content']}"
            )
        return "\n".join(lines)

    def raw_archive(
        self,
        messages: list[dict],
        *,
        max_chars: int | None = None,
        session_key: str | None = None,
    ) -> None:
        """Fallback: dump raw messages to history.jsonl without LLM summarization."""
        limit = max_chars if max_chars is not None else _RAW_ARCHIVE_MAX_CHARS
        formatted = truncate_text(self._format_messages(messages), limit)
        self.append_history(
            f"[RAW] {len(messages)} messages\n"
            f"{formatted}",
            session_key=session_key,
        )
        logger.warning(
            "Memory consolidation degraded: raw-archived {} messages", len(messages)
        )

    # ------------------------------------------------------------------
    # Dream helpers
    # ------------------------------------------------------------------

    @staticmethod
    def dream_session_key() -> str:
        """Return a unique session key for a Dream run, e.g. ``dream:20260528-100000``."""
        return f"dream:{datetime.now():%Y%m%d-%H%M%S}"

    @staticmethod
    def prune_internal_sessions(
        sessions_dir: Path, prefix: str, *, keep: int = 10
    ) -> list[str]:
        """Remove the oldest ``<prefix>_*.jsonl`` session files, keeping N.

        Only files matching the prefix are considered; sessions belonging to
        anything else are never touched.

        Returns the original ``<prefix>:...`` session keys of the files that
        were actually removed, so callers can also evict any in-memory
        bookkeeping (``SessionManager`` cache, active tasks, session locks)
        keyed by the same value — deleting the on-disk file alone leaves those
        caches growing forever.
        """
        files = sorted(
            sessions_dir.glob(f"{prefix}_*.jsonl"), key=lambda p: p.stat().st_mtime,
        )
        if len(files) <= keep:
            return []

        to_remove = files[: len(files) - keep]
        removed_keys: list[str] = []
        for path in to_remove:
            try:
                path.unlink()
                logger.debug("Pruned old {} session: {}", prefix, path.stem)
                removed_keys.append(path.stem.replace("_", ":", 1))
            except OSError:
                logger.warning("Failed to prune {} session {}", prefix, path)
        return removed_keys

    @classmethod
    def prune_dream_sessions(cls, sessions_dir: Path, *, keep: int = 10) -> list[str]:
        """Remove the oldest Dream session files, keeping only the N most recent."""
        return cls.prune_internal_sessions(sessions_dir, "dream", keep=keep)


# ---------------------------------------------------------------------------
# Consolidator — lightweight token-budget triggered consolidation
# ---------------------------------------------------------------------------

# Individual history.jsonl writers cap their own payloads tightly; the
# _HISTORY_ENTRY_HARD_CAP at append_history() is a belt-and-suspenders default
# that catches any new caller that forgot to set its own cap.
_RAW_ARCHIVE_MAX_CHARS = 16_000       # fallback dump (LLM failed)
_ARCHIVE_SUMMARY_MAX_CHARS = 8_000    # LLM-produced consolidation summary
_HISTORY_ENTRY_HARD_CAP = 64_000      # emergency cap in append_history


# Consolidator vive in ``consolidator.py`` (importa MemoryStore qui sopra).
# Re-export in coda per preservare l'API storica; a questo punto MemoryStore
# e le costanti sono già definite, quindi l'import di ritorno non trova un
# modulo a metà inizializzazione.
from jenny.agent.consolidator import Consolidator as Consolidator  # noqa: E402
