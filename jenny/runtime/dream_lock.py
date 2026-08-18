"""Lock di mutua esclusione per i run Dream (cron + ``/dream`` manuale).

Due run Dream concorrenti sono un bug reale, non un fastidio estetico: partono
dallo stesso cursore di ``history.jsonl`` e scrivono sugli stessi tre file di
memoria. Il primo che finisce avanza il cursore; il secondo, basato sul
contenuto *pre-edit* (vecchio), vede fallire ogni ``edit_file``/``apply_patch``
(``old_text`` introvabile) e brucia un intero passaggio LLM per concludere con
"attempts blocked/refused" e cursore non avanzato.

Questo modulo è l'unico posto in cui il lock vive, così entrambi i chiamanti
(``CronDispatcher._run_dream`` e ``cmd_dream`` in ``jenny/command/builtin.py``)
condividono la stessa primitiva. La mutua esclusione basta: chi arriva secondo
rilegge il prompt DENTRO il lock e scopre che l'altro run ha già avanzato il
cursore (``build_dream_prompt`` ritorna ``None``), quindi non serve nemmeno un
claim esplicito del cursore.

La primitiva è in una globale di modulo, quindi segue la regola dell'entry
point: ``reset_dream_state`` è chiamata dal blocco di reset di
``android_entry.run_gateway`` e iscritta in ``ALLOWED`` di
``tests/runtime/test_loop_bound_globals.py``. Un gateway che riparte nello
stesso processo non può ereditare un lock legato al loop morto del tentativo
precedente.
"""

from __future__ import annotations

import asyncio

_dream_lock = asyncio.Lock()


def get_dream_lock() -> asyncio.Lock:
    """Ritorna il lock condiviso dei run Dream."""
    return _dream_lock


def dream_lock_locked() -> bool:
    """True se un run Dream è già in corso (guardia non-bloccante)."""
    return _dream_lock.locked()


async def try_acquire_dream_lock() -> bool:
    """Acquisisce il lock Dream senza attendere.

    Ritorna ``True`` se il lock era libero (e ora è nostro), ``False`` se un
    altro run Dream è in corso. La sequenza check-then-acquire è atomica: tra
    ``locked()`` e ``acquire()`` non c'è nessun ``await``, e su un lock libero
    ``acquire`` completa senza sospendersi — nessun altro task può inserirsi
    in mezzo.
    """
    if _dream_lock.locked():
        return False
    await _dream_lock.acquire()
    return True


def release_dream_lock() -> None:
    """Rilascia il lock Dream (da chiamare solo se si è ottenuto con
    :func:`try_acquire_dream_lock`)."""
    _dream_lock.release()


def reset_dream_state() -> None:
    """Rimette a nuovo il lock Dream per un nuovo event loop."""
    global _dream_lock
    _dream_lock = asyncio.Lock()
