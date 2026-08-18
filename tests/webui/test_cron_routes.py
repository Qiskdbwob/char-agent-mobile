"""Test delle route /api/cron (elenco job + rimozione job d'utente)."""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from websockets.http11 import Headers
from websockets.http11 import Request as WsRequest

from jenny.cron.service import CronService
from jenny.webui.ws_http import GatewayHTTPHandler

_AUTH_SECRET = "test-secret"
_LONG_MESSAGE = "Ricordami di " + ("aggiornare il dashboard " * 20)  # > 160 chars


def _store_json() -> dict:
    """Store cron con un job di sistema (protetto) e uno d'utente."""
    return {
        "version": 1,
        "jobs": [
            {
                "id": "dream",
                "name": "dream",
                "enabled": True,
                "schedule": {"kind": "every", "atMs": None, "everyMs": 43_200_000, "expr": None, "tz": "UTC"},
                "payload": {"kind": "system_event", "mode": "reminder", "message": "", "sessionKey": None, "originChannel": None, "originChatId": None, "originMetadata": {}},
                "state": {"nextRunAtMs": 1_780_000_000_000, "lastRunAtMs": 1_770_000_000_000, "lastStatus": "ok", "lastError": None, "consecutiveCouldNotCheck": 0, "couldNotCheckSinceMs": None, "couldNotCheckEscalated": False, "taskChecks": {}, "runHistory": []},
                "createdAtMs": 1_700_000_000_000, "updatedAtMs": 1_700_000_000_000, "deleteAfterRun": False,
            },
            {
                "id": "abc12345",
                "name": "daily standup",
                "enabled": True,
                "schedule": {"kind": "cron", "atMs": None, "everyMs": None, "expr": "0 9 * * *", "tz": "Europe/Rome"},
                "payload": {"kind": "agent_turn", "mode": "monitor", "message": _LONG_MESSAGE, "sessionKey": "unified:default", "originChannel": "websocket", "originChatId": "default", "originMetadata": {}},
                "state": {"nextRunAtMs": 1_780_000_000_000, "lastRunAtMs": None, "lastStatus": None, "lastError": None, "consecutiveCouldNotCheck": 0, "couldNotCheckSinceMs": None, "couldNotCheckEscalated": False, "taskChecks": {}, "runHistory": []},
                "createdAtMs": 1_700_000_000_000, "updatedAtMs": 1_700_000_000_000, "deleteAfterRun": False,
            },
        ],
    }


def _make_request(path: str, token: str | None = _AUTH_SECRET):
    if token is not None and "token=" not in path:
        sep = "&" if "?" in path else "?"
        path = f"{path}{sep}token={urllib.parse.quote(token)}"
    return WsRequest(path=path, headers=Headers())


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    """Workspace + CronService reali su tmp_path, handler HTTP completo."""
    runtime_root = tmp_path / "data"
    workspace = runtime_root / "workspace"
    (workspace / "memory").mkdir(parents=True)
    (workspace / "SOUL.md").write_text("anima", encoding="utf-8")

    from jenny.config import paths as paths_mod

    monkeypatch.setattr(paths_mod, "get_workspace_path", lambda: workspace)

    store_path = runtime_root / "cron" / "jobs.json"
    store_path.parent.mkdir(parents=True)
    store_path.write_text(json.dumps(_store_json()), encoding="utf-8")

    cron = CronService(store_path)

    config = SimpleNamespace(
        workspace=SimpleNamespace(enabled=True),
        wiki=SimpleNamespace(enabled=True, wikis_dir="wikis"),
        token_issue_secret=_AUTH_SECRET,
        verbose=False,
    )
    handler = GatewayHTTPHandler(
        config=config,
        session_manager=None,
        runtime_model_name=lambda: "test-model",
        bus=MagicMock(),
        media=MagicMock(),
        workspaces=MagicMock(),
        skills_workspace_path=workspace / "skills",
        get_cron_service=lambda: cron,
    )
    return SimpleNamespace(handler=handler, cron=cron)


def _json(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


async def test_unauthorized_without_token(env) -> None:
    response = env.handler.cron_routes.dispatch(
        _make_request("/api/cron", token=None), "/api/cron"
    )
    assert response.status_code == 401


async def test_list_jobs(env) -> None:
    response = env.handler.cron_routes.dispatch(
        _make_request("/api/cron"), "/api/cron"
    )
    assert response.status_code == 200
    jobs = _json(response)["jobs"]
    assert len(jobs) == 2

    by_id = {j["id"]: j for j in jobs}
    system = by_id["dream"]
    assert system["protected"] is True
    assert system["schedule_label"] == "every 12h (UTC)"
    assert system["last_status"] == "ok"

    user = by_id["abc12345"]
    assert user["protected"] is False
    assert user["schedule_label"] == "0 9 * * * (Europe/Rome)"
    assert user["mode"] == "monitor"
    assert user["name"] == "daily standup"
    # Il messaggio lungo viene troncato per la lista.
    assert user["message"].endswith("…")
    assert len(user["message"]) <= 161
    assert user["next_run_at_ms"] == 1_780_000_000_000


async def test_remove_user_job(env) -> None:
    response = env.handler.cron_routes.dispatch(
        _make_request("/api/cron/remove?job_id=abc12345"), "/api/cron/remove"
    )
    assert response.status_code == 200
    assert _json(response) == {"removed": True, "protected": False}

    listing = env.handler.cron_routes.dispatch(
        _make_request("/api/cron"), "/api/cron"
    )
    ids = [j["id"] for j in _json(listing)["jobs"]]
    assert ids == ["dream"]


async def test_remove_protected_job_refused(env) -> None:
    response = env.handler.cron_routes.dispatch(
        _make_request("/api/cron/remove?job_id=dream"), "/api/cron/remove"
    )
    assert response.status_code == 200
    assert _json(response) == {"removed": False, "protected": True}

    listing = env.handler.cron_routes.dispatch(
        _make_request("/api/cron"), "/api/cron"
    )
    assert len(_json(listing)["jobs"]) == 2


async def test_remove_unknown_job(env) -> None:
    response = env.handler.cron_routes.dispatch(
        _make_request("/api/cron/remove?job_id=nope"), "/api/cron/remove"
    )
    assert response.status_code == 200
    assert _json(response) == {"removed": False, "protected": False, "not_found": True}


async def test_remove_requires_job_id(env) -> None:
    response = env.handler.cron_routes.dispatch(
        _make_request("/api/cron/remove"), "/api/cron/remove"
    )
    assert response.status_code == 400


async def test_list_without_service(tmp_path: Path, monkeypatch) -> None:
    from jenny.config import paths as paths_mod

    monkeypatch.setattr(paths_mod, "get_workspace_path", lambda: tmp_path)
    handler = GatewayHTTPHandler(
        config=SimpleNamespace(
            workspace=SimpleNamespace(enabled=True),
            wiki=SimpleNamespace(enabled=True, wikis_dir="wikis"),
            token_issue_secret=_AUTH_SECRET,
            verbose=False,
        ),
        session_manager=None,
        runtime_model_name=lambda: None,
        bus=MagicMock(),
        media=MagicMock(),
        workspaces=MagicMock(),
        skills_workspace_path=tmp_path / "skills",
    )
    response = handler.cron_routes.dispatch(
        _make_request("/api/cron"), "/api/cron"
    )
    assert response.status_code == 200
    assert _json(response) == {"jobs": []}
