"""Tests for the gateway entry point."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from jenny.android_entry import MAX_RETRIES, run_gateway
from jenny.config.bootstrap import ensure_minimal_config
from jenny.runtime.context import get_runtime_context


def test_run_gateway_prepares_workspace_and_passes_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """run_gateway should create workspace, sync templates, ensure config,
    and forward host/port/ws_port=port to _run_gateway."""
    mock_run = AsyncMock()

    # Il workspace vive nel RuntimeContext; monkeypatch ripristina la sessione.
    monkeypatch.setattr(get_runtime_context(), "workspace_dir", None)

    with patch("jenny.gateway_runtime._run_gateway", new=mock_run):
        run_gateway(
            str(tmp_path),
            host="127.0.0.1",
            port=18000,
        )

    workspace = tmp_path / "workspace"
    assert workspace.exists()
    assert (workspace / "config.json").exists()
    assert (workspace / "SOUL.md").exists()

    mock_run.assert_awaited_once_with(
        config=None,
        host="127.0.0.1",
        port=18000,
        ws_port=18000,
    )


def test_run_gateway_retries_after_a_system_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """B2: SystemExit non è una Exception. Con il vecchio `except Exception`
    i tre tentativi venivano saltati e run_gateway tornava a Kotlin lasciando
    il servizio in piedi senza agente dietro."""
    monkeypatch.setattr(get_runtime_context(), "workspace_dir", None)
    monkeypatch.setattr("jenny.android_entry.RETRY_DELAY_S", 0)

    calls: list[int] = []

    async def _fake_run(**_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise SystemExit(2)

    with patch("jenny.gateway_runtime._run_gateway", new=_fake_run):
        run_gateway(str(tmp_path), host="127.0.0.1", port=18001)

    assert len(calls) == 2


def test_run_gateway_resets_bridge_state_on_every_retry_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Il blocco di reset vive DENTRO il loop di retry: ogni tentativo riparte
    con primitive asyncio nuove, non con quelle legate al loop morto del
    tentativo precedente (classe di bug R6, vedi
    tests/runtime/test_loop_bound_globals.py). Un reset eseguito una sola volta
    all'ingresso lascerebbe l'attempt 2 su un loop nuovo con i lock
    dell'attempt 1: alla prima contesa "bound to a different event loop" per
    power/android_web/notifier, dove un await sta dentro la sezione critica."""
    monkeypatch.setattr(get_runtime_context(), "workspace_dir", None)
    monkeypatch.setattr("jenny.android_entry.RETRY_DELAY_S", 0)

    events: list[str] = []
    runs: list[int] = []

    def _fake_reset() -> None:
        events.append("reset")

    monkeypatch.setattr(
        "jenny.agent.tools.android_web.reset_android_web_state", _fake_reset
    )

    async def _fake_run(**_kwargs):
        runs.append(1)
        events.append("run")
        if len(runs) == 1:
            raise SystemExit(2)

    with patch("jenny.gateway_runtime._run_gateway", new=_fake_run):
        run_gateway(str(tmp_path), host="127.0.0.1", port=18004)

    assert len(runs) == 2
    # reset prima di OGNI tentativo, non solo del primo.
    assert events == ["reset", "run", "reset", "run"]


def test_run_gateway_reraises_after_max_retries_of_base_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Esaurititi i tentativi la BaseException risale, come per Exception."""
    monkeypatch.setattr(get_runtime_context(), "workspace_dir", None)
    monkeypatch.setattr("jenny.android_entry.RETRY_DELAY_S", 0)

    calls: list[int] = []

    async def _always_exit(**_kwargs):
        calls.append(1)
        raise SystemExit(9)

    with patch("jenny.gateway_runtime._run_gateway", new=_always_exit):
        with pytest.raises(SystemExit):
            run_gateway(str(tmp_path), host="127.0.0.1", port=18002)

    assert len(calls) == MAX_RETRIES


def test_run_gateway_does_not_retry_a_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Ctrl-C è volontario: un solo tentativo, poi risale."""
    monkeypatch.setattr(get_runtime_context(), "workspace_dir", None)
    monkeypatch.setattr("jenny.android_entry.RETRY_DELAY_S", 0)

    calls: list[int] = []

    async def _interrupted(**_kwargs):
        calls.append(1)
        raise KeyboardInterrupt

    with patch("jenny.gateway_runtime._run_gateway", new=_interrupted):
        with pytest.raises(KeyboardInterrupt):
            run_gateway(str(tmp_path), host="127.0.0.1", port=18003)

    assert len(calls) == 1


def test_ensure_minimal_config_writes_minimal_json(tmp_path: Path):
    """ensure_minimal_config should write a minimal config when missing."""
    ensure_minimal_config(tmp_path)

    path = tmp_path / "config.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["gateway"]["host"] == "127.0.0.1"
    ws = data["websocket"]
    assert ws["enabled"] is True
    assert "channels" not in data


def test_ensure_minimal_config_uses_existing_workspace(tmp_path: Path):
    """The config should land inside the provided workspace directory."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_minimal_config(workspace)
    path = workspace / "config.json"
    assert path.exists()


def test_ensure_minimal_config_is_idempotent(tmp_path: Path):
    """ensure_minimal_config should not overwrite an existing config."""
    ensure_minimal_config(tmp_path)
    path = tmp_path / "config.json"
    original = path.read_text(encoding="utf-8")

    ensure_minimal_config(tmp_path)
    assert path.read_text(encoding="utf-8") == original


