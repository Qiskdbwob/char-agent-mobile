"""Auto compact: proactive compression of idle sessions to reduce token cost and latency.

Struttura dei riassunti generati:
    SESSION STATE
    - Main goal: ...
    - Current task: ...
    - Important decisions: ...
    - Key facts: ...
    - Pending tasks: ...
    - Unresolved questions: ...
    - Important references: ...
    - Recent direction: ...
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Coroutine

from loguru import logger

from jenny.session.keys import UNIFIED_SESSION_KEY
from jenny.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from jenny.agent.memory import Consolidator


# ── Structured summary template ────────────────────────────────────────

STRUCTURED_SUMMARY_TEMPLATE = """\
SESSION STATE
- Main goal: {goal}
- Current task: {current_task}
- Important decisions: {decisions}
- Key facts: {key_facts}
- Pending tasks: {pending}
- Unresolved questions: {unresolved}
- Important references: {references}
- Recent direction: {recent}"""


def build_structured_summary(
    raw_summary: str,
    *,
    goal: str = "not specified",
    current_task: str = "not specified",
    decisions: str = "none identified",
    key_facts: str = "none identified",
    pending: str = "none",
    unresolved: str = "none",
    references: str = "none",
    recent: str = "conversation flow not captured",
) -> str:
    """Genera un riassunto strutturato della sessione.

    Quando il raw_summary contiene già un blocco SESSION STATE, lo lascia
    intatto. Altrimenti lo incapsula nel template strutturato.
    """
    if "SESSION STATE" in raw_summary:
        return raw_summary
    return STRUCTURED_SUMMARY_TEMPLATE.format(
        goal=goal,
        current_task=current_task,
        decisions=decisions,
        key_facts=key_facts,
        pending=pending,
        unresolved=unresolved,
        references=references,
        recent=recent,
    )


def extract_summary_fields(raw_text: str) -> dict[str, str]:
    """Estrae i campi dal testo grezzo del riassunto per decomporli in categorie strutturate.

    Usa euristiche semplici (keyword matching) per classificare le frasi.
    """
    lines = [line.strip() for line in raw_text.split("\n") if line.strip()]
    fields: dict[str, list[str]] = {
        "goal": [],
        "current_task": [],
        "decisions": [],
        "key_facts": [],
        "pending": [],
        "unresolved": [],
        "references": [],
        "recent": [],
    }
    keyword_map = {
        "goal": "goal",
        "task": "current_task",
        "decision": "decisions",
        "decided": "decisions",
        "chose": "decisions",
        "fact": "key_facts",
        "important": "key_facts",
        "pending": "pending",
        "todo": "pending",
        "question": "unresolved",
        "unknown": "unresolved",
        "unclear": "unresolved",
        "reference": "references",
        "file": "references",
        "path": "references",
        "url": "references",
    }
    for line in lines:
        classified = False
        lower = line.lower()
        for keyword, bucket in keyword_map.items():
            if keyword in lower:
                fields[bucket].append(line)
                classified = True
                break
        if not classified:
            fields["recent"].append(line)

    result = {}
    for key, items in fields.items():
        result[key] = "; ".join(items) if items else "none"
    return result


class AutoCompact:
    _RECENT_SUFFIX_MESSAGES = 8
    _INTERNAL_SESSION_PREFIXES = ("dream:", "atlas:")

    def __init__(
        self,
        sessions: SessionManager,
        consolidator: Consolidator,
        session_ttl_minutes: int = 0,
    ):
        self.sessions = sessions
        self.consolidator = consolidator
        self._ttl = session_ttl_minutes
        self._archiving: set[str] = set()
        self._summaries: dict[str, tuple[str, datetime]] = {}
        # Lazy compaction: flag per session che indica compaction pending
        self._compaction_pending: set[str] = set()
        # Token usage tracking per session
        self._session_token_usage: dict[str, dict[str, int]] = {}

    def _is_expired(
        self,
        ts: datetime | str | None,
        now: datetime | None = None,
    ) -> bool:
        if self._ttl <= 0 or not ts:
            return False
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        return ((now or datetime.now()) - ts).total_seconds() >= self._ttl * 60

    @staticmethod
    def _format_summary(text: str, last_active: datetime) -> str:
        return (
            f"Previous conversation summary "
            f"(last active {last_active.isoformat()}):\n{text}"
        )

    @classmethod
    def _is_internal_session(cls, key: str) -> bool:
        return key.startswith(cls._INTERNAL_SESSION_PREFIXES)

    def check_expired(
        self,
        schedule_background: Callable[[Coroutine], None],
        active_session_keys: Collection[str] = (),
    ) -> None:
        """Schedule archival of idle sessions (unified or multi-session).

        Rispetto all'implementazione originale, questa versione controlla
        TUTTE le sessioni utente, non solo ``unified:default``.
        """
        # Prima: controlla la sessione unificata legacy
        self._check_single_key(UNIFIED_SESSION_KEY, schedule_background, active_session_keys)
        # Poi: controlla tutte le sessioni webui/chat
        try:
            for info in self.sessions.list_user_sessions():
                key = info.get("key", "")
                if key and key != UNIFIED_SESSION_KEY:
                    self._check_single_key(key, schedule_background, active_session_keys)
        except Exception:
            logger.debug("AutoCompact: failed to enumerate user sessions", exc_info=True)

    def _check_single_key(
        self,
        key: str,
        schedule_background: Callable[[Coroutine], None],
        active_session_keys: Collection[str] = (),
    ) -> None:
        """Controlla se una singola sessione deve essere archiviata."""
        if key in self._archiving or key in active_session_keys:
            return
        if self._is_internal_session(key):
            return
        info = self.sessions.read_session_metadata(key)
        if info is None:
            return
        if self._is_expired(info.get("updated_at")):
            self._archiving.add(key)
            schedule_background(self._archive(key))

    async def _archive(self, key: str) -> None:
        if self._is_internal_session(key):
            self._archiving.discard(key)
            return
        try:
            summary = await self.consolidator.compact_idle_session(
                key,
                self._RECENT_SUFFIX_MESSAGES,
            )
            if summary and summary != "(nothing)":
                session = self.sessions.get_or_create(key)
                meta = session.metadata.get("_last_summary")
                if isinstance(meta, dict):
                    self._summaries[key] = (
                        meta["text"],
                        datetime.fromisoformat(meta["last_active"]),
                    )
        except Exception:
            logger.exception("Auto-compact: failed for {}", key)
        finally:
            self._archiving.discard(key)
            self._compaction_pending.discard(key)

    # ── Lazy compaction ────────────────────────────────────────────────

    def mark_compaction_pending(self, key: str) -> None:
        """Segna una sessione come 'compaction pending'.

        Il compaction effettivo viene deferito al prossimo ``prepare_session``
        quando la sessione torna attiva.  Evita di interrompere risposte
        in corso che hanno appena superato la soglia.
        """
        self._compaction_pending.add(key)

    def has_pending_compaction(self, key: str) -> bool:
        """Restituisce True se la sessione ha un compaction in attesa."""
        return key in self._compaction_pending

    # ── Token usage tracking ───────────────────────────────────────────

    def record_token_usage(self, key: str, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        """Registra l'uso token per una sessione specifica."""
        if key not in self._session_token_usage:
            self._session_token_usage[key] = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_turns": 0,
            }
        usage = self._session_token_usage[key]
        usage["prompt_tokens"] += prompt_tokens
        usage["completion_tokens"] += completion_tokens
        usage["total_turns"] += 1

    def get_session_token_usage(self, key: str) -> dict[str, int]:
        """Restituisce le statistiche token per una sessione."""
        return self._session_token_usage.get(key, {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_turns": 0,
        })

    def get_all_token_usage(self) -> dict[str, dict[str, int]]:
        """Restituisce le statistiche token per tutte le sessioni."""
        return dict(self._session_token_usage)

    # ── Session preparation ────────────────────────────────────────────

    def prepare_session(
        self, session: Session, key: str
    ) -> tuple[Session, str | None]:
        """Prepara una sessione per l'uso: ricarica se necessario, applica summary."""
        if self._is_internal_session(key):
            self._archiving.discard(key)
            self._summaries.pop(key, None)
            return session, None

        # Se c'è un compaction pending, eseguilo prima di procedere
        if key in self._compaction_pending:
            logger.info(
                "Auto-compact: executing deferred compaction for {}", key
            )
            self._compaction_pending.discard(key)
            # Il riarchivio avviene nel background; qui forziamo il reload
            session = self.sessions.get_or_create(key)

        if key in self._archiving or self._is_expired(session.updated_at):
            logger.info(
                "Auto-compact: reloading session {} (archiving={})",
                key,
                key in self._archiving,
            )
            session = self.sessions.get_or_create(key)

        # Hot path: summary from in-memory dict (process hasn't restarted).
        entry = self._summaries.pop(key, None)
        if entry:
            return session, self._format_summary(entry[0], entry[1])

        # Cold path: summary persisted in session metadata (process restarted).
        meta = session.metadata.get("_last_summary")
        if isinstance(meta, dict):
            return session, self._format_summary(
                meta["text"],
                datetime.fromisoformat(meta["last_active"]),
            )
        return session, None
