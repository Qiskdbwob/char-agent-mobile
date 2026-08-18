"""Guardia sull'avanzamento del cursore Dream nel ``CronDispatcher``.

Il contratto: il cursore avanza solo se il turno Dream è completato pulito
**e** ha davvero scritto (o non ha mai provato a scrivere). Se ogni tentativo
di scrittura è stato bloccato dalla policy, avanzare perderebbe per sempre
quelle voci di ``history.jsonl``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from jenny.agent.tools.file_state import FileStates
from jenny.config.schema import Config
from jenny.runtime.cron_dispatch import CronDispatcher

_DREAM_JOB = SimpleNamespace(name="dream", id="job-dream")


class _FakeMemory:
    """Memory store minimale: espone il registry Dream con i suoi ``file_states``."""

    def __init__(self, file_states: FileStates | None) -> None:
        self.cursor: int | None = None
        self.tools = SimpleNamespace(file_states=file_states)
        self.prompt_builds = 0

    def build_dream_prompt(self, **_kwargs):
        self.prompt_builds += 1
        return ("prompt di consolidamento", 42)

    def build_dream_tools(self):
        return self.tools

    def set_last_dream_cursor(self, cursor: int) -> None:
        self.cursor = cursor

    def get_last_dream_cursor(self) -> int:
        return 7

    def compact_history(self) -> None:
        pass


class _FakeAgent:
    def __init__(self, sessions_dir: Path, memory: _FakeMemory, stop_reason: str) -> None:
        self.context = SimpleNamespace(memory=memory)
        self.sessions = SimpleNamespace(sessions_dir=sessions_dir)
        self._stop_reason = stop_reason

    async def process_direct(self, prompt: str, **_kwargs):
        return SimpleNamespace(metadata={"_stop_reason": self._stop_reason}, usage={})

    def evict_pruned_sessions(self, keys) -> None:
        pass


def _dispatch(
    tmp_path: Path, file_states: FileStates | None, stop_reason: str
) -> tuple[_FakeMemory, CronDispatcher]:
    memory = _FakeMemory(file_states)
    dispatcher = CronDispatcher(
        get_agent=lambda: _FakeAgent(tmp_path, memory, stop_reason),
        config=Config(),
        cron=MagicMock(),
        heartbeat_cfg=SimpleNamespace(),
    )
    return memory, dispatcher


async def test_cursor_advances_when_dream_wrote(tmp_path: Path) -> None:
    file_states = FileStates()
    file_states.record_write_attempt()
    file_states.record_write(tmp_path / "written.md")
    memory, dispatcher = _dispatch(tmp_path, file_states, "completed")

    await dispatcher.dispatch(_DREAM_JOB)

    assert memory.cursor == 42


async def test_cursor_advances_when_nothing_was_attempted(tmp_path: Path) -> None:
    """Nulla da consolidare: nessun tentativo di scrittura, si avanza."""
    memory, dispatcher = _dispatch(tmp_path, FileStates(), "completed")

    await dispatcher.dispatch(_DREAM_JOB)

    assert memory.cursor == 42


async def test_cursor_held_when_every_write_was_blocked(tmp_path: Path) -> None:
    """Regressione: turno "completed" ma scritture tutte bloccate → non avanzare."""
    file_states = FileStates()
    file_states.record_write_attempt()
    memory, dispatcher = _dispatch(tmp_path, file_states, "completed")

    await dispatcher.dispatch(_DREAM_JOB)

    assert memory.cursor is None


async def test_cursor_held_when_turn_did_not_complete(tmp_path: Path) -> None:
    file_states = FileStates()
    file_states.record_write_attempt()
    file_states.record_write(tmp_path / "written.md")
    memory, dispatcher = _dispatch(tmp_path, file_states, "error")

    await dispatcher.dispatch(_DREAM_JOB)

    assert memory.cursor is None


async def test_dream_cron_skips_when_another_run_is_in_progress(tmp_path: Path) -> None:
    """Due Dream concorrenti (cron + manuale): se un run è già in volo il job
    cron NON parte — nessun prompt costruito, nessun turno, nessun avanzamento.

    Senza la guardia il secondo run partirebbe dallo stesso cursore del primo
    e scriverebbe sugli stessi file basandosi sul contenuto pre-edit: tutte le
    edit fallirebbero e un intero turno LLM andrebbe sprecato.
    """
    from jenny.runtime.dream_lock import (
        release_dream_lock,
        try_acquire_dream_lock,
    )

    assert await try_acquire_dream_lock() is True
    try:
        memory, dispatcher = _dispatch(tmp_path, FileStates(), "completed")

        await dispatcher.dispatch(_DREAM_JOB)

        assert memory.prompt_builds == 0
        assert memory.cursor is None
    finally:
        release_dream_lock()


async def test_dream_cron_runs_when_lock_is_free(tmp_path: Path) -> None:
    """Lock libero: il job cron procede normalmente (il prompt viene costruito)."""
    from jenny.runtime.dream_lock import release_dream_lock, try_acquire_dream_lock

    # Lock sicuramente libero (i test precedenti rilasciano sempre):
    # verifichiamo solo che la guardia non blocchi il caso felice.
    assert await try_acquire_dream_lock() is True
    release_dream_lock()

    file_states = FileStates()
    file_states.record_write_attempt()
    file_states.record_write(tmp_path / "written.md")
    memory, dispatcher = _dispatch(tmp_path, file_states, "completed")

    await dispatcher.dispatch(_DREAM_JOB)

    assert memory.prompt_builds == 1
    assert memory.cursor == 42
