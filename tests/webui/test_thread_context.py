"""Test del campo ``context`` nel thread WebUI (stato contesto per la popover).

Il backend arricchisce la risposta del thread con stima token vs finestra e
conteggio messaggi quando l'``AgentLoop`` è disponibile. Qui si verifica che
il blocco compaia con i valori giusti, e che senza loop non ci sia (best-effort,
niente che rompa il thread).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from websockets.http11 import Headers
from websockets.http11 import Request as WsRequest

from jenny.webui.transcript_recorder import WebUITranscriptRecorder
from jenny.webui.ws_http import GatewayHTTPHandler

_AUTH_SECRET = "test-secret"


class _FakeSession:
    def __init__(self, n: int) -> None:
        self._n = n

    def get_history(self, max_messages: int = 0) -> list[dict]:
        return [{"role": "user", "content": "x"} for _ in range(self._n)]


class _FakeConsolidator:
    @staticmethod
    def estimate_session_prompt_tokens(session) -> tuple[int, None]:
        return (12345, None)


class _FakeLoop:
    context_window_tokens = 65536
    sessions = SimpleNamespace(get_or_create=lambda key: _FakeSession(37))
    consolidator = _FakeConsolidator()


def _make_request(path: str, token: str | None = _AUTH_SECRET):
    if token is not None and "token=" not in path:
        sep = "&" if "?" in path else "?"
        path = f"{path}{sep}token={token}"
    return WsRequest(path=path, headers=Headers())


def _build_handler(
    tmp_path: Path, monkeypatch, *, get_loop_status=None
) -> GatewayHTTPHandler:
    from jenny.config import paths as paths_mod

    monkeypatch.setattr(paths_mod, "get_data_dir", lambda: tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "memory").mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "get_workspace_path", lambda: workspace)

    return GatewayHTTPHandler(
        config=SimpleNamespace(
            workspace=SimpleNamespace(enabled=True),
            wiki=SimpleNamespace(enabled=True, wikis_dir="wikis"),
            token_issue_secret=_AUTH_SECRET,
            verbose=False,
        ),
        session_manager=None,
        runtime_model_name=lambda: None,
        bus=MagicMock(),
        media=SimpleNamespace(
            augment_transcript_media=lambda paths: paths,
            rewrite_local_markdown_images=lambda text, workspace_path=None: text,
        ),
        workspaces=SimpleNamespace(
            scope_for_session_key=lambda key: SimpleNamespace(
                project_path=str(workspace),
                payload=lambda: {"project_path": str(workspace), "access_mode": "restricted"},
            )
        ),
        skills_workspace_path=workspace / "skills",
        get_loop_status=get_loop_status,
    )


@pytest.fixture()
def transcript(tmp_path: Path, monkeypatch):
    """Transcript WebUI minimale per la sessione default."""
    monkeypatch.setattr("jenny.config.paths.get_data_dir", lambda: tmp_path)
    recorder = WebUITranscriptRecorder()
    recorder.append("default", {"event": "user", "chat_id": "default", "text": "ciao"})
    recorder.append("default", {"event": "message", "chat_id": "default", "text": "ciao!"})
    recorder.append("default", {"event": "turn_end", "chat_id": "default", "text": ""})


def _json(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def test_thread_includes_context_when_loop_available(tmp_path, monkeypatch, transcript) -> None:
    handler = _build_handler(tmp_path, monkeypatch, get_loop_status=lambda: _FakeLoop())
    response = handler._handle_webui_thread_get(
        _make_request("/api/sessions/websocket%3Adefault/webui-thread?limit=160"),
        "websocket:default",
    )
    assert response.status_code == 200
    data = _json(response)
    assert data["context"] == {
        "tokens_estimate": 12345,
        "context_window_tokens": 65536,
        "message_count": 37,
    }


def test_thread_without_loop_has_no_context_key(tmp_path, monkeypatch, transcript) -> None:
    handler = _build_handler(tmp_path, monkeypatch)
    response = handler._handle_webui_thread_get(
        _make_request("/api/sessions/websocket%3Adefault/webui-thread?limit=160"),
        "websocket:default",
    )
    assert response.status_code == 200
    assert "context" not in _json(response)


def test_thread_loop_errors_do_not_break_response(tmp_path, monkeypatch, transcript) -> None:
    class _BrokenLoop:
        context_window_tokens = 65536

        @property
        def sessions(self):
            raise RuntimeError("boom")

        @property
        def consolidator(self):
            raise RuntimeError("boom")

    handler = _build_handler(tmp_path, monkeypatch, get_loop_status=lambda: _BrokenLoop())
    response = handler._handle_webui_thread_get(
        _make_request("/api/sessions/websocket%3Adefault/webui-thread?limit=160"),
        "websocket:default",
    )
    assert response.status_code == 200
    assert _json(response)["context"] is None
