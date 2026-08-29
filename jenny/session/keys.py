"""Shared session key constants and helpers."""

from __future__ import annotations

import secrets
import time

UNIFIED_SESSION_KEY = "unified:default"

# Prefisso delle sessioni WebUI multiplo (``webui:<session_id>``).
# Ogni sessione WebUI usa una chiave unica con questo prefisso.
WEBUI_SESSION_PREFIX = "webui:"

# Prefisso delle sessioni Tier-2 dei subagent (``subagent:<lineage_id>``).
# Sono storia di lavoro interno, non conversazioni: non devono comparire in
# nessun elenco user-facing ne essere leggibili dalle route HTTP della WebUI.
SUBAGENT_SESSION_PREFIX = "subagent:"

# Sessione dell'heartbeat: chiave *nuda*, senza suffisso, perche ce n'e una sola
# (``cron_dispatch._run_heartbeat``). Sta qui e non inline nel dispatcher perche
# e' anche il discriminante di :func:`is_internal_session_key`.
HEARTBEAT_SESSION_KEY = "heartbeat"

# Prefissi delle sessioni interne (lavoro del sistema, non conversazione con
# l'utente). Elencare le sessioni e per definizione un'operazione user-facing:
# chi lo fa deve filtrare con :func:`is_internal_session_key`.
#
# Ogni run coniato con un suffisso compare qui col separatore (``dream:<data>``,
# ``atlas:<data>``, ``cron:<job_id>``, ``subagent:<lineage>``, ``internal:direct``).
_INTERNAL_SESSION_PREFIXES = (
    SUBAGENT_SESSION_PREFIX,
    "cron:",
    "dream:",
    "atlas:",
    "internal:",
)

# Chiavi interne senza suffisso: vanno confrontate per uguaglianza, non per
# prefisso, altrimenti non matchano (era il caso di ``heartbeat``, che il
# prefisso ``"heartbeat:"`` non ha mai intercettato).
_INTERNAL_SESSION_KEYS = frozenset({HEARTBEAT_SESSION_KEY})


def is_internal_session_key(key: str) -> bool:
    """True se la session key appartiene a lavoro interno, non all'utente.

    Usata come filtro unico per gli elenchi di sessioni e come default della
    visibilita di un turno (:mod:`jenny.session.turn_visibility`): il confine
    sta qui e non replicato in ogni chiamante, cosi aggiungere una sessione
    interna non richiede di ricordarsi di aggiornare N punti.
    """
    if key in _INTERNAL_SESSION_KEYS:
        return True
    return any(key.startswith(prefix) for prefix in _INTERNAL_SESSION_PREFIXES)


def subagent_session_key(lineage_id: str) -> str:
    """Session key della storia Tier-2 di un lineage."""
    return f"{SUBAGENT_SESSION_PREFIX}{lineage_id}"


def new_session_key() -> str:
    """Generate a unique session key for a new WebUI session.

    Returns a key like ``webui:<timestamp>-<random>`` that is guaranteed
    to be unique across all sessions.
    """
    ts = int(time.time() * 1000)
    rand = secrets.token_hex(4)
    return f"{WEBUI_SESSION_PREFIX}{ts}-{rand}"


def is_webui_session_key(key: str) -> bool:
    """True se la session key e' una sessione WebUI utente (non interna).

    Include sia il formato legacy ``unified:default`` sia le sessioni
    multipli ``webui:<id>``.
    """
    return key == UNIFIED_SESSION_KEY or key.startswith(WEBUI_SESSION_PREFIX)


def session_key_for_channel(channel: str, chat_id: str) -> str:
    """Return the session key for a channel/chat pair.

    Every channel/chat maps onto the single unified conversation; explicit
    ``session_key_override`` values (internal keys) bypass this helper.
    """
    return UNIFIED_SESSION_KEY
