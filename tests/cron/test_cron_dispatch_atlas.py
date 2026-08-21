"""Instradamento del job ``atlas`` nel ``CronDispatcher``.

Il dispatcher non deve contenere logica Atlas: il suo unico compito è chiamare
``run_atlas``. Se un giorno qualcuno reimplementa il run qui dentro — come è
successo a Dream, che oggi vive in due copie — questo test resta verde ma il
prossimo cambiamento andrà fatto in due posti. Meglio fissarlo adesso.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jenny.config.schema import Config
from jenny.cron.bound_runner import CRON_WAKELOCK_TIMEOUT_S
from jenny.cron.service import CronJobSkippedError
from jenny.runtime.cron_dispatch import CronDispatcher

_ATLAS_JOB = SimpleNamespace(name="atlas", id="job-atlas")


class _FakeAgent:
    def __init__(self, sessions_dir: Path) -> None:
        self.context = SimpleNamespace(memory=None, timezone=None)
        self.sessions = SimpleNamespace(sessions_dir=sessions_dir)
        self.prompts: list[str] = []

    async def process_direct(self, prompt: str, **_kwargs):
        self.prompts.append(prompt)
        return SimpleNamespace(metadata={"_stop_reason": "completed"}, usage={})

    def evict_pruned_sessions(self, keys) -> None:
        pass


def _dispatcher(agent) -> CronDispatcher:
    return CronDispatcher(
        get_agent=lambda: agent,
        config=Config(),
        cron=MagicMock(),
        heartbeat_cfg=SimpleNamespace(),
    )


@pytest.mark.asyncio
async def test_atlas_job_reaches_run_atlas(tmp_path, monkeypatch):
    seen: dict[str, object] = {}

    async def _fake_run_atlas(agent, *, store=None, force=False, snapshot_callback=None):
        from jenny.agent.atlas import AtlasOutcome

        seen["agent"] = agent
        seen["store"] = store
        seen["force"] = force
        seen["snapshot_callback"] = snapshot_callback
        return AtlasOutcome(status="written", elapsed=0.1)

    monkeypatch.setattr("jenny.agent.atlas.run_atlas", _fake_run_atlas)
    agent = _FakeAgent(tmp_path)

    result = await _dispatcher(agent).dispatch(_ATLAS_JOB)

    assert result is None  # Atlas non consegna niente all'utente
    assert seen["agent"] is agent
    assert seen["force"] is False
    assert seen["store"] is not None


@pytest.mark.asyncio
async def test_atlas_store_is_built_from_the_dispatcher_config(tmp_path, monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_run_atlas(agent, *, store=None, force=False, snapshot_callback=None):
        from jenny.agent.atlas import AtlasOutcome

        captured["wikis_dir"] = store.wikis_dir
        captured["default_wiki"] = store.default_wiki
        return AtlasOutcome(status="skipped_no_wikis")

    monkeypatch.setattr("jenny.agent.atlas.run_atlas", _fake_run_atlas)
    config = Config()

    await _dispatcher(_FakeAgent(tmp_path)).dispatch(_ATLAS_JOB)

    assert captured["wikis_dir"] == config.workspace_path / config.wiki.wikis_dir
    assert captured["default_wiki"] == config.wiki.default_wiki


@pytest.mark.asyncio
async def test_a_run_without_wikis_is_not_an_error(tmp_path):
    """Nessuna wiki è lo stato normale di un workspace nuovo, non un guasto."""
    agent = _FakeAgent(tmp_path)

    result = await _dispatcher(agent).dispatch(_ATLAS_JOB)

    assert result is None
    assert agent.prompts == []


@pytest.mark.asyncio
async def test_atlas_runs_inside_the_cron_wakelock(tmp_path, monkeypatch):
    """Dream, atlas e heartbeat non passano da ``bound_runner``.

    Entrano da ``process_direct``, che non è il percorso di turno protetto in
    ``AgentLoop._dispatch``: senza il wakelock su ``dispatch`` resterebbero
    scoperti proprio i tre job che girano sempre a schermo spento. Atlas fa da
    campione per tutti e tre — il blocco è unico e li avvolge insieme.
    """
    events: list[tuple[str, str, float]] = []

    @asynccontextmanager
    async def fake_keep_awake(tag: str, *, timeout_s: float = 0.0):
        events.append(("enter", tag, timeout_s))
        try:
            yield True
        finally:
            events.append(("exit", tag, timeout_s))

    async def _fake_run_atlas(agent, *, store=None, force=False, snapshot_callback=None):
        from jenny.agent.atlas import AtlasOutcome

        # Il lock deve essere già preso mentre il job gira, non dopo.
        assert events == [("enter", "cron", CRON_WAKELOCK_TIMEOUT_S)]
        return AtlasOutcome(status="written", elapsed=0.1)

    monkeypatch.setattr("jenny.runtime.cron_dispatch.keep_awake", fake_keep_awake)
    monkeypatch.setattr("jenny.agent.atlas.run_atlas", _fake_run_atlas)

    await _dispatcher(_FakeAgent(tmp_path)).dispatch(_ATLAS_JOB)

    assert [e[0] for e in events] == ["enter", "exit"]


@pytest.mark.asyncio
async def test_a_skipped_job_still_leaves_the_wakelock_block(monkeypatch):
    # Senza provider ``dispatch`` solleva: il rilascio non può dipendere dal
    # fatto che ci fosse un agente da far girare.
    events: list[str] = []

    @asynccontextmanager
    async def fake_keep_awake(tag: str, *, timeout_s: float = 0.0):
        events.append("enter")
        try:
            yield True
        finally:
            events.append("exit")

    monkeypatch.setattr("jenny.runtime.cron_dispatch.keep_awake", fake_keep_awake)

    with pytest.raises(CronJobSkippedError):
        await _dispatcher(None).dispatch(_ATLAS_JOB)

    assert events == ["enter", "exit"]
