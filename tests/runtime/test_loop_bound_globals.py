"""Guardia: ogni primitiva asyncio in una globale di modulo deve avere un reset.

Il gateway riparte nello stesso processo (retry di ``run_gateway`` + restart
lato Kotlin), quindi apre più di un ``asyncio.run``. Una primitiva asyncio
tenuta in una globale di modulo sopravvive al loop che l'ha adottata, e da lì
in poi rifiuta ogni accodamento: la classe di bug che il blocco di reset in
``android_entry.run_gateway`` esiste per chiudere.

Il blocco è però una lista che si può dimenticare di allungare — è esattamente
così che ``config.store._LOCK`` è rimasto fuori. Questo test rende quella
dimenticanza rumorosa: chi aggiunge una primitiva la deve o resettare, o
iscrivere qui con la ragione per cui non serve.

**Cosa il test non vede**, e va quindi controllato a mano: le primitive create
dentro ``__init__`` di una classe di cui esiste un singleton di modulo (es.
``ssh_jobs._store``, la cui ``SshJobStore._lock`` è la ragione di
``reset_job_store``). Riconoscerle via AST richiederebbe inseguire i tipi, e il
prezzo sarebbe un test che fallisce a caso; meglio un perimetro netto.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "jenny"

# Le primitive di ``asyncio`` che si legano al loop, più i riferimenti a un loop
# vivo: tutte cose che un secondo ``asyncio.run`` non può ereditare.
_LOOP_BOUND_FACTORIES = {
    "Lock",
    "Event",
    "Condition",
    "Semaphore",
    "BoundedSemaphore",
    "Barrier",
    "Queue",
    "LifoQueue",
    "PriorityQueue",
    "Future",
    "get_event_loop",
    "get_running_loop",
    "new_event_loop",
}

# ``modulo:nome`` -> funzione di reset che la rimette a nuovo. Ogni voce viene
# verificata due volte: che esista ancora, e che il reset sia davvero chiamato
# da ``android_entry.run_gateway``.
ALLOWED: dict[str, str] = {
    "jenny/agent/tools/android_web.py:_BRIDGE_LOCK": "reset_android_web_state",
    "jenny/config/store.py:_LOCK": "reset_config_store_state",
    "jenny/runtime/location.py:_BRIDGE_LOCK": "reset_location_state",
    "jenny/runtime/dream_lock.py:_dream_lock": "reset_dream_state",
    "jenny/runtime/notifier.py:_BRIDGE_LOCK": "reset_notifier_state",
    "jenny/runtime/power.py:_BRIDGE_LOCK": "reset_power_state",
    "jenny/runtime/power.py:_STATE_LOCK": "reset_power_state",
    "jenny/runtime/power.py:_WAKE_EVENT": "reset_power_state",
    "jenny/runtime/power.py:_WAKE_LOOP": "reset_power_state",
    "jenny/webui/android_apps_api.py:_BRIDGE_LOCK": "reset_installed_apps_state",
    "jenny/webui/settings_api.py:_update_check_lock": "reset_update_check_state",
}


def _factory_name(node: ast.expr | None) -> str | None:
    """``asyncio.Lock()`` -> ``"Lock"``; qualunque altra cosa -> ``None``."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id == "asyncio" and func.attr in _LOOP_BOUND_FACTORIES:
            return func.attr
    return None


def _assigned_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return [t.id for t in targets if isinstance(t, ast.Name)]


def _module_level_hits(tree: ast.Module, rel: str) -> list[str]:
    """Assegnamenti a livello di modulo, anche dentro ``if``/``try`` di modulo."""
    hits: list[str] = []

    def walk(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and _factory_name(node.value):
                hits.extend(f"{rel}:{name}" for name in _assigned_names(node))
            for attr in ("body", "orelse", "finalbody"):
                nested = getattr(node, attr, None)
                if isinstance(nested, list):
                    walk([s for s in nested if isinstance(s, ast.stmt)])

    walk(tree.body)
    return hits


def _cached_global_hits(tree: ast.Module, rel: str) -> list[str]:
    """Globali riempite pigramente dentro una funzione (il pattern cache).

    Le funzioni ``reset_*`` sono la cura, non la malattia: quello che assegnano
    non conta come nuova primitiva da censire.
    """
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("reset_"):
            continue
        declared: set[str] = {
            name
            for sub in ast.walk(node)
            if isinstance(sub, ast.Global)
            for name in sub.names
        }
        if not declared:
            continue
        for sub in ast.walk(node):
            if isinstance(sub, (ast.Assign, ast.AnnAssign)) and _factory_name(sub.value):
                hits.extend(f"{rel}:{n}" for n in _assigned_names(sub) if n in declared)
    return hits


def _discover() -> set[str]:
    found: set[str] = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found.update(_module_level_hits(tree, rel))
        found.update(_cached_global_hits(tree, rel))
    return found


def test_every_module_level_asyncio_primitive_is_accounted_for():
    found = _discover()
    allowed = set(ALLOWED)

    unlisted = sorted(found - allowed)
    assert not unlisted, (
        "Nuova primitiva asyncio in una globale di modulo: "
        f"{unlisted}. Un secondo ``asyncio.run`` non può ereditarla. "
        "Aggiungi una funzione ``reset_*``, chiamala dal blocco di reset in "
        "``android_entry.run_gateway``, e iscrivila in ALLOWED qui sopra."
    )

    stale = sorted(allowed - found)
    assert not stale, (
        f"Voci di ALLOWED che non esistono più nel sorgente: {stale}. "
        "Rimuovile (e togli il reset ormai inutile dall'entry point)."
    )


def _entry_point_calls() -> set[str]:
    tree = ast.parse((PACKAGE / "android_entry.py").read_text(encoding="utf-8"))
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def test_every_allowlisted_reset_is_wired_into_the_entry_point():
    """Iscrivere il reset in ALLOWED senza chiamarlo lascerebbe il bug in piedi."""
    called = _entry_point_calls()

    missing = sorted({reset for reset in ALLOWED.values() if reset not in called})
    assert not missing, (
        f"Reset dichiarati in ALLOWED ma mai chiamati da run_gateway: {missing}"
    )


def test_the_ssh_job_store_singleton_is_reset_too():
    """Il caso che l'AST non vede, tenuto fermo a mano.

    ``ssh_jobs._store`` è un singleton di modulo e ``SshJobStore._lock`` resta
    preso *durante* l'exec SSH: due poll concorrenti si accodano sul serio, e
    quello lega il lock al loop. È la stessa classe di bug, solo nascosta
    dentro un ``__init__``.
    """
    assert "reset_job_store" in _entry_point_calls()
