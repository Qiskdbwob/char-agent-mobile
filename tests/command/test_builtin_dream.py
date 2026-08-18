"""Lo slash command ``/dream``.

La regressione sotto test: un ``/dream`` manuale mentre il cron Dream è in
volo non deve partire — due run concorrenti partono dallo stesso cursore e
scrivono sugli stessi file, e il secondo fallisce su tutte le edit dopo aver
bruciato un intero turno LLM (il messaggio \"attempts blocked/refused\" visto
dall'utente). La guardia condivide il lock con ``CronDispatcher``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from jenny.agent.memory import MemoryStore
from jenny.bus.events import InboundMessage
from jenny.command.builtin import _DREAM_BUSY_MESSAGE, register_builtin_commands
from jenny.command.router import CommandContext, CommandRouter
from jenny.runtime import dream_lock


def _snapshot_tasks() -> set:
    """Task asyncio vivi PRIMA del dispatch: per distinguere quello che il
    comando crea in background da tutto il resto."""
    return set(asyncio.all_tasks())


async def _drain(before: set) -> None:
    """Attende il task fire-and-forget creato dal comando (usa ``to_thread``,
    quindi i soli ``sleep(0)`` non bastano: serve attendere davvero)."""
    spawned = [
        t for t in asyncio.all_tasks()
        if t not in before and t is not asyncio.current_task()
    ]
    if spawned:
        await asyncio.gather(*spawned, return_exceptions=True)
        # Un tick in più per gli effetti collaterali post-task (publish finale).
        await asyncio.sleep(0)


@pytest.fixture()
def router() -> CommandRouter:
    r = CommandRouter()
    register_builtin_commands(r)
    return r


def _completed_resp():
    return SimpleNamespace(
        metadata={"_stop_reason": "completed"},
        usage={},
    )


def _make_loop(tmp_path, store: MemoryStore, published: list):
    """Loop finto: bus che colleziona, memory reale, process_direct finto."""
    async def _process_direct(*_args, **_kwargs):
        return _completed_resp()

    return SimpleNamespace(
        bus=SimpleNamespace(publish_outbound=_collect(published)),
        context=SimpleNamespace(memory=store, timezone=None),
        process_direct=_process_direct,
        sessions=SimpleNamespace(sessions_dir=tmp_path),
        evict_pruned_sessions=lambda keys: None,
    )


def _dispatch(router, loop, msg):
    ctx = CommandContext(
        msg=msg, session=None, key="k", raw=msg.content, args="", loop=loop,
    )
    return router.dispatch(ctx)


class TestBusyGuard:
    @pytest.mark.asyncio
    async def test_replies_busy_immediately_when_dream_is_running(
        self, router, tmp_path
    ) -> None:
        """Lock Dream occupato (cron in volo): risposta sincrona \"already
        running\", nessun task in background, nessun turno LLM."""
        store = MemoryStore(tmp_path)
        store.soul_file.write_text("# Soul", encoding="utf-8")
        store.memory_file.write_text("# Memory", encoding="utf-8")
        published: list = []
        loop = _make_loop(tmp_path, store, published)

        assert await dream_lock.try_acquire_dream_lock() is True
        try:
            msg = InboundMessage(
                channel="websocket", sender_id="u", chat_id="default",
                content="/dream",
            )
            ack = await _dispatch(router, loop, msg)
            await _drain(_snapshot_tasks())

            assert _DREAM_BUSY_MESSAGE in ack.content
            assert published == []
        finally:
            dream_lock.release_dream_lock()

    @pytest.mark.asyncio
    async def test_background_task_also_guards_the_race(
        self, router, tmp_path
    ) -> None:
        """La finestra fra la risposta sincrona e l'acquisizione del lock è
        chiusa dal task in background: se nel frattempo un altro run è partito,
        il task pubblica \"already running\" invece di eseguire il turno."""
        store = MemoryStore(tmp_path)
        store.soul_file.write_text("# Soul", encoding="utf-8")
        store.memory_file.write_text("# Memory", encoding="utf-8")
        published: list = []
        loop = _make_loop(tmp_path, store, published)

        # Lock libero al momento del dispatch: il comando risponde \"Dreaming...\"
        # e spara il task in background, che però trova il lock occupato.
        before = _snapshot_tasks()
        msg = InboundMessage(
            channel="websocket", sender_id="u", chat_id="default",
            content="/dream",
        )
        ack = await _dispatch(router, loop, msg)
        assert "Dreaming" in ack.content

        assert await dream_lock.try_acquire_dream_lock() is True
        try:
            await _drain(before)
            assert published and _DREAM_BUSY_MESSAGE in published[0].content
        finally:
            dream_lock.release_dream_lock()


class TestNormalRun:
    @pytest.mark.asyncio
    async def test_ack_then_background_completion(self, router, tmp_path) -> None:
        """Lock libero: ack \"Dreaming...\", poi il task consolida e pubblica
        l'esito (cursore avanzato: nessuna scrittura tentata)."""
        store = MemoryStore(tmp_path)
        store.soul_file.write_text("# Soul", encoding="utf-8")
        store.memory_file.write_text("# Memory", encoding="utf-8")
        store.append_history("fact da consolidare")
        published: list = []
        loop = _make_loop(tmp_path, store, published)

        before = _snapshot_tasks()
        msg = InboundMessage(
            channel="websocket", sender_id="u", chat_id="default",
            content="/dream",
        )
        ack = await _dispatch(router, loop, msg)
        assert "Dreaming" in ack.content

        await _drain(before)

        assert published and "Dream completed in" in published[0].content
        # Il cursore è avanzato: un secondo /dream non ha più niente da fare.
        assert store.build_dream_prompt() is None

    @pytest.mark.asyncio
    async def test_no_input_message_when_nothing_to_process(
        self, router, tmp_path
    ) -> None:
        """Nessuna history oltre il cursore: il task pubblica il messaggio
        \"no conversation history\" senza chiamare process_direct."""
        store = MemoryStore(tmp_path)
        store.soul_file.write_text("# Soul", encoding="utf-8")
        store.memory_file.write_text("# Memory", encoding="utf-8")
        published: list = []
        loop = _make_loop(tmp_path, store, published)

        before = _snapshot_tasks()
        msg = InboundMessage(
            channel="websocket", sender_id="u", chat_id="default",
            content="/dream",
        )
        await _dispatch(router, loop, msg)
        await _drain(before)

        assert published and "no conversation history" in published[0].content


def _collect(sink: list):
    async def _publish(message):
        sink.append(message)

    return _publish
