"""Layer unico per i knob operativi da variabile d'ambiente (``JENNY_*``).

Questi non sono config utente (non stanno in ``config.json``): sono override
operativi di tuning (timeout, concorrenza) letti dall'ambiente. Prima erano
``os.environ.get(...)`` sparsi; qui sono centralizzati con nome/parsing/default
documentati in un solo posto (Fase 3.2 — "un solo layer env").

Precedenza: default del codice → override ``JENNY_*`` dell'ambiente.
"""

from __future__ import annotations

import os

from loguru import logger


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid {}={!r}; using {}", name, raw, default)
        return default


def _positive_float_env(name: str, default: float) -> float:
    """Come ``_float_env``, ma per i knob dove zero non vuol dire "disabilitato".

    La differenza è di significato, non di stile. Alcuni knob usano ``<= 0`` come
    "nessun limite" e lo interpretano nel chiamante (``llm_timeout_s``,
    ``tool_timeout_s``, ``max_concurrent_requests``): lì lo zero è una scelta.
    Per tutti gli altri viene passato tale e quale a chi lo consuma, che lo
    prende alla lettera — un ``asyncio.wait_for(timeout=0)`` scade sempre, un TTL
    a zero fa scadere tutto al primo giro — e allora è un valore malformato.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    value = _float_env(name, default)
    if value <= 0:
        logger.warning("Ignoring non-positive {}={!r}; using {}", name, raw, default)
        return default
    return value


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid {}={!r}; using {}", name, raw, default)
        return default


def max_concurrent_requests(default: int = 3) -> int:
    """Cap sui turni LLM concorrenti. ``<= 0`` = illimitato (deciso dal chiamante).

    Env: ``JENNY_MAX_CONCURRENT_REQUESTS`` (default 3).
    """
    return _int_env("JENNY_MAX_CONCURRENT_REQUESTS", default)


def llm_timeout_s(default: float = 300.0) -> float:
    """Timeout hard per singola richiesta LLM (evita starvation del lock di
    sessione se la rete si blocca). ``<= 0`` = disabilitato (deciso dal chiamante).

    Env: ``JENNY_LLM_TIMEOUT_S`` (default 300).
    """
    return _float_env("JENNY_LLM_TIMEOUT_S", default)


def llm_http_timeout_s(default: float) -> float:
    """Timeout della singola richiesta HTTP verso il provider del modello.

    Distinto da ``llm_timeout_s``, che limita il *turno*: questo è il budget del
    trasporto, e su uno stream si applica fra un chunk e il successivo. Il
    default arriva dal chiamante, che sa se l'endpoint è in loopback (vedi
    ``providers/endpoint_budget.py``).

    Env: ``JENNY_LLM_HTTP_TIMEOUT_S``, oppure il nome storico
    ``JENNY_OPENAI_COMPAT_TIMEOUT_S`` — che vale per entrambi i provider, e non
    solo per quello che ha nel nome.
    """
    for name in ("JENNY_LLM_HTTP_TIMEOUT_S", "JENNY_OPENAI_COMPAT_TIMEOUT_S"):
        if os.environ.get(name, "").strip():
            return _positive_float_env(name, default)
    return default


def ws_send_timeout_s(default: float = 12.0) -> float:
    """Timeout wall-clock per singolo ``connection.send()`` sul canale WebSocket.

    La libreria ``websockets`` applica backpressure: se il buffer TCP di un client
    è saturo (rete mobile lenta, app in background) ``send`` si blocca a tempo
    indeterminato. Il dispatcher outbound è seriale, quindi un client zombie
    stallerebbe la consegna a *tutti*. Allo scadere, quella connessione viene
    chiusa e scartata (mai riusata: un send cancellato lascia un frame parziale
    sul filo). Default generoso per non disconnettere client sani su reti lente.

    Un valore ``<= 0`` non disabilita il timeout: finirebbe in
    ``asyncio.wait_for``, che lo prende alla lettera e fa fallire *ogni* send —
    cioè scollega tutti i client invece di essere permissivo. Vedi
    ``_positive_float_env``.

    Env: ``JENNY_WS_SEND_TIMEOUT_S`` (default 12).
    """
    return _positive_float_env("JENNY_WS_SEND_TIMEOUT_S", default)


def goal_inactivity_ttl_h(default: float = 12.0) -> float:
    """TTL (ore) di inattività oltre il quale un goal sostenuto ``active`` viene
    fatto scadere in modo lazy all'inizio del turno.

    Su Android il kill abrupto del processo è lo scenario normale: un goal lasciato
    ``active`` da un crash resterebbe zombie per sempre, disabilitando in modo
    permanente il wall-timeout LLM della sessione (vedi
    ``jenny.session.goal_state.runner_wall_llm_timeout_s``). L'inattività si misura
    sul più recente tra ``started_at`` e ``last_turn_at``: un goal che avanza davvero
    aggiorna ``last_turn_at`` a ogni turno e non scade mai.

    Un TTL ``<= 0`` non significa "non scadere mai": la soglia in
    ``expire_stale_goal`` diventa vera per qualunque attività passata, quindi ogni
    goal sostenuto morirebbe al primo turno utile. Vedi ``_positive_float_env``.

    Env: ``JENNY_GOAL_INACTIVITY_TTL_H`` (default 12).
    """
    return _positive_float_env("JENNY_GOAL_INACTIVITY_TTL_H", default)


def tool_timeout_s(default: float = 300.0) -> float:
    """Timeout wall-clock per singola esecuzione di un tool.

    Rete di sicurezza contro i tool che non ritornano mai (il caso più comune di
    turno bloccato con UI ferma su "Agent running"). Allo scadere, l'esecuzione
    viene interrotta e l'errore è restituito al modello come normale tool-error,
    così il turno prosegue e si chiude. ``<= 0`` = disabilitato.

    Env: ``JENNY_TOOL_TIMEOUT_S`` (default 300).
    """
    return _float_env("JENNY_TOOL_TIMEOUT_S", default)


def dream_turn_threshold(default: int = 15) -> int:
    """Soglia di turni per il Dream trigger event-based.

    Quando il numero di turni completati dall'ultimo Dream raggiunge questa
    soglia, viene pianificato un run Dream event-driven (in aggiunta al
    trigger wall-clock ogni 2h). Questo è特别 utile per sessioni brevi su
    Android dove il timer di 2 ore potrebbe non scattare mai.

    ``<= 0`` disabilita il trigger event-based (solo wall-clock).

    Env: ``JENNY_DREAM_TURN_THRESHOLD`` (default 15).
    """
    return _int_env("JENNY_DREAM_TURN_THRESHOLD", default)
