"""Guardia sul lock di mutua esclusione dei run Dream (cron + ``/dream``).

Due run Dream concorrenti partono dallo stesso cursore e scrivono sugli stessi
file di memoria: quello che arriva secondo fallisce su tutte le edit dopo aver
bruciato un intero turno LLM. Il lock condiviso in ``jenny/runtime/dream_lock``
è l'unica primitiva usata da entrambi i chiamanti (``CronDispatcher`` e
``cmd_dream``): qui si verifica che escluda davvero, che la guardia non
bloccante sia atomica, e che il reset la rimetta a nuovo per un nuovo loop.
"""

from __future__ import annotations

import asyncio

from jenny.runtime import dream_lock


async def test_try_acquire_succeeds_when_free() -> None:
    """Lock libero: acquisito subito, ``locked()`` vero, rilascio pulito."""
    assert await dream_lock.try_acquire_dream_lock() is True
    assert dream_lock.dream_lock_locked() is True
    dream_lock.release_dream_lock()
    assert dream_lock.dream_lock_locked() is False


async def test_second_acquire_fails_while_held() -> None:
    """Un run in corso: il secondo chiamante non attende e fallisce subito."""
    assert await dream_lock.try_acquire_dream_lock() is True
    try:
        assert await dream_lock.try_acquire_dream_lock() is False
        assert dream_lock.dream_lock_locked() is True
    finally:
        dream_lock.release_dream_lock()
    # Dopo il rilascio si può riacquisire.
    assert await dream_lock.try_acquire_dream_lock() is True
    dream_lock.release_dream_lock()


async def test_acquire_is_atomic_no_await_between_check_and_acquire() -> None:
    """La sequenza check-then-acquire non cede mai il loop: nessun altro task
    può infilarsi fra ``locked()`` e ``acquire()`` e rubare il lock."""
    results: list[bool] = []

    async def contender() -> None:
        ok = await dream_lock.try_acquire_dream_lock()
        results.append(ok)
        if ok:
            dream_lock.release_dream_lock()

    # Lock già nostro: i due contender devono essere entrambi respinti.
    assert await dream_lock.try_acquire_dream_lock() is True
    try:
        await asyncio.gather(contender(), contender())
        assert results == [False, False]
        assert dream_lock.dream_lock_locked() is True
    finally:
        dream_lock.release_dream_lock()


async def test_reset_replaces_the_lock() -> None:
    """``reset_dream_state`` rimette a nuovo il lock: un gateway che riparte
    nello stesso processo non eredita un lock legato al loop morto (né uno
    lasciato ``locked`` da un run interrotto)."""
    first = dream_lock.get_dream_lock()
    dream_lock.reset_dream_state()
    try:
        second = dream_lock.get_dream_lock()
        assert first is not second
        assert dream_lock.dream_lock_locked() is False
        assert await dream_lock.try_acquire_dream_lock() is True
    finally:
        dream_lock.release_dream_lock()
        dream_lock.reset_dream_state()
