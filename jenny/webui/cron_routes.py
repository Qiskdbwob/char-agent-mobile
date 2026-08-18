"""Route HTTP ``/api/cron`` — elenco e rimozione dei job schedulati.

Il cron vive nel ``CronService`` del container: l'agente ci crea job (tool
``cron``), e finora la WebUI non aveva nessuna superficie per vederli o
rimuoverli. Questa famiglia di route espone esattamente le due operazioni
sicure: **leggere** i job (inclusi quelli di sistema, protetti) e **rimuovere**
solo i job d'utente (``agent_turn``). I ``system_event`` (dream, atlas,
heartbeat, update_check) restano protetti: il servizio li rifiuta già in
``remove_job``, qui ci si limita a mostrarlo.

Pattern identico alle altre famiglie di route: il costruttore riceve solo
dipendenze esplicite (getter late-binding del servizio + helper di risposta),
e ``dispatch`` decide per path.
"""

from __future__ import annotations

from typing import Any, Callable

from jenny.channels.http_utils import QueryParams

#: Lunghezza massima del messaggio del job nel payload: la lista serve a
#: riconoscere il job, non a leggere il promemoria intero.
_MESSAGE_MAX_CHARS = 160


def _humanize_ms(ms: int) -> str:
    """``7200000`` -> ``2h`` — compatto, per le righe della lista."""
    seconds = max(1, int(ms / 1000))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            value = seconds // size
            return f"{value}{unit}"
    return f"{seconds}s"


def _schedule_label(schedule: Any) -> str:
    """Rappresentazione leggibile della pianificazione di un job."""
    kind = getattr(schedule, "kind", None)
    if kind == "every":
        every_ms = getattr(schedule, "every_ms", None)
        label = f"every {_humanize_ms(every_ms)}" if every_ms else "every"
    elif kind == "at":
        at_ms = getattr(schedule, "at_ms", None)
        label = f"at {at_ms}" if at_ms else "at"
    elif kind == "cron":
        label = getattr(schedule, "expr", None) or "cron"
    else:
        label = str(kind or "?")
    tz = getattr(schedule, "tz", None)
    return f"{label} ({tz})" if tz else label


def _job_payload(job: Any) -> dict[str, Any]:
    """Serie il job per la WebUI: campi stabili, nessun path del filesystem."""
    state = job.state
    payload = job.payload
    message = (payload.message or "").strip()
    if len(message) > _MESSAGE_MAX_CHARS:
        message = message[:_MESSAGE_MAX_CHARS] + "…"
    return {
        "id": job.id,
        "name": job.name,
        "enabled": job.enabled,
        "protected": payload.kind == "system_event",
        "schedule_label": _schedule_label(job.schedule),
        "mode": getattr(payload, "mode", "reminder"),
        "message": message,
        "delete_after_run": job.delete_after_run,
        "next_run_at_ms": state.next_run_at_ms,
        "last_run_at_ms": state.last_run_at_ms,
        "last_status": state.last_status,
        "last_error": state.last_error,
        "consecutive_could_not_check": state.consecutive_could_not_check,
    }


class CronRoutes:
    """Handler delle route ``/api/cron`` per la WebUI mobile."""

    def __init__(
        self,
        *,
        get_cron_service: Callable[[], Any | None],
        check_api_token: Callable[[Any], bool],
        json_response: Callable[[Any], Any],
        error_response: Callable[[int, str], Any],
        parse_query: Callable[[str], QueryParams],
        query_first: Callable[[QueryParams, str], str | None],
    ) -> None:
        self._get_cron_service = get_cron_service
        self._check_api_token = check_api_token
        self._json_response = json_response
        self._error_response = error_response
        self._parse_query = parse_query
        self._query_first = query_first

    def dispatch(self, request: Any, path: str) -> Any:
        """Risponde se il path è di questa famiglia, altrimenti ``None``."""
        if path == "/api/cron":
            return self._list(request)
        if path == "/api/cron/remove":
            return self._remove(request)
        return None

    def _list(self, request: Any) -> Any:
        if not self._check_api_token(request):
            return self._error_response(401, "Unauthorized")
        cron = self._get_cron_service()
        if cron is None:
            return self._json_response({"jobs": []})
        jobs = [_job_payload(j) for j in cron.list_jobs(include_disabled=True)]
        return self._json_response({"jobs": jobs})

    def _remove(self, request: Any) -> Any:
        if not self._check_api_token(request):
            return self._error_response(401, "Unauthorized")
        cron = self._get_cron_service()
        if cron is None:
            return self._error_response(503, "Cron service unavailable")
        params = self._parse_query(request.path)
        job_id = self._query_first(params, "job_id")
        if not job_id:
            return self._error_response(400, "job_id is required")
        result = cron.remove_job(job_id)
        if result == "removed":
            return self._json_response({"removed": True, "protected": False})
        if result == "protected":
            return self._json_response({"removed": False, "protected": True})
        return self._json_response({"removed": False, "protected": False, "not_found": True})
