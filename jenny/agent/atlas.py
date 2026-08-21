"""Atlas — il gemello di Dream sul lato wiki.

Dream legge ``memory/history.jsonl`` e mantiene ``SOUL.md`` / ``USER.md`` /
``memory/MEMORY.md``. Atlas legge ``workspace/wikis/`` e mantiene un solo file,
``memory/WIKI.md``: la rubrica delle entità che contano operativamente (persone,
progetti, sistemi) più l'elenco delle wiki disponibili. Quel file viene iniettato
in ogni system prompt accanto alla memoria a lungo termine, così l'agente sa cosa
c'è nella wiki senza doverla attraversare a ogni turno.

Tre scelte che distinguono Atlas da Dream, tutte per lo stesso motivo — gira su
un telefono:

1. **La scansione è deterministica.** L'inventario (quali wiki, quali pagine,
   quali titoli) è costruito qui in Python e messo nel prompt. Al modello resta
   il giudizio — cosa è rilevante, come classificarlo — non l'esplorazione.
2. **Il fingerprint decide se partire.** Se nessun file rilevante è cambiato
   dall'ultimo run, non c'è nessuna chiamata al provider.
3. **Nessuno snapshot pre-run.** ``WIKI.md`` è derivato: se va perso lo
   ricostruisce il run successivo. Dream invece riscrive memoria irrecuperabile
   e lo snapshot lì è giustificato.

Il runner è uno solo (:func:`run_atlas`), condiviso dal cron e dallo slash
command, per non ripetere la divergenza che Dream ha oggi fra
``runtime/cron_dispatch.py`` e ``command/builtin.py``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from jenny.utils.path import atomic_write
from jenny.utils.prompt_templates import render_template
from jenny.utils.wiki_paths import (
    discover_wiki_roots,
    extract_title,
    read_wiki_scope,
    wiki_fingerprint,
)

# Tetto sulle righe di pagina messe nell'inventario. Una wiki da 300 pagine è
# già oltre quello che una rubrica può riassumere utilmente; oltre questa soglia
# l'elenco viene troncato con una nota esplicita, perché un inventario tagliato
# in silenzio si legge come "la wiki è tutta qui".
_MAX_INVENTORY_ENTRIES = 300

# Gruppi di pagine da cui si compila la rubrica, in ordine di rilevanza.
# ``summaries/`` resta fuori: è il livello di citazione delle fonti, non
# materiale da cui nascono voci di rubrica.
_INVENTORY_GROUPS = ("entities", "concepts")

_STATE_VERSION = 1


@dataclass(frozen=True)
class AtlasOutcome:
    """Esito di un run, in forma leggibile sia dal log sia dalla chat."""

    status: str
    elapsed: float = 0.0
    detail: str = ""

    @property
    def ran(self) -> bool:
        """True se il run ha effettivamente chiamato il provider."""
        return self.status in {"written", "no_write", "incomplete", "failed"}


class AtlasStore:
    """File I/O, inventario e sandbox di un run Atlas.

    Speculare alla sezione Dream di :class:`~jenny.agent.memory.MemoryStore`, ma
    tenuta in un modulo suo: Dream e Atlas condividono la forma, non lo stato.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        wikis_dir_name: str = "wikis",
        default_wiki: str = "main",
        max_entries: int = _MAX_INVENTORY_ENTRIES,
    ) -> None:
        self.workspace = workspace
        self.wikis_dir = workspace / wikis_dir_name
        self.default_wiki = default_wiki
        self.max_entries = max_entries
        memory_dir = workspace / "memory"
        self.wiki_file = memory_dir / "WIKI.md"
        self.policy_file = memory_dir / "WIKI_POLICY.md"
        self.state_file = memory_dir / ".atlas_state.json"

    @classmethod
    def from_config(cls, workspace: Path, config: Any) -> "AtlasStore":
        """Costruisce lo store dalla config runtime (``config.wiki``)."""
        wiki_cfg = getattr(config, "wiki", None)
        return cls(
            workspace,
            wikis_dir_name=getattr(wiki_cfg, "wikis_dir", "wikis") or "wikis",
            default_wiki=getattr(wiki_cfg, "default_wiki", "main") or "main",
        )

    # -- stato ---------------------------------------------------------------

    def has_wikis(self) -> bool:
        return bool(discover_wiki_roots(self.wikis_dir))

    def fingerprint(self) -> str:
        """Impronta corrente delle sorgenti che determinano la rubrica."""
        return wiki_fingerprint(self.wikis_dir, extra_paths=(self.policy_file,))

    def read_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def last_fingerprint(self) -> str:
        value = self.read_state().get("fingerprint")
        return value if isinstance(value, str) else ""

    def write_state(self, fingerprint: str) -> None:
        # atomic_write e non write_text: uno stato troncato a metà si rilegge
        # come JSON invalido, cioè fingerprint vuoto, cioè un run inutile al
        # prossimo tick. Costa niente evitarlo.
        payload = {
            "version": _STATE_VERSION,
            "fingerprint": fingerprint,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        atomic_write(self.state_file, json.dumps(payload, ensure_ascii=False, indent=2))

    def is_stale(self) -> bool:
        """True se la wiki è cambiata dall'ultimo run registrato."""
        return self.fingerprint() != self.last_fingerprint()

    # -- inventario ----------------------------------------------------------

    def build_inventory(self) -> str:
        """Elenco deterministico di wiki e pagine, pronto da mettere nel prompt."""
        roots = discover_wiki_roots(self.wikis_dir)
        if not roots:
            return "(no wikis found)"

        lines: list[str] = []
        lines.append("### Wikis")
        for name, root in roots.items():
            pages = root / "wiki"
            counts = ", ".join(
                f"{group}: {_count_pages(pages / group)}" for group in _INVENTORY_GROUPS
            )
            total = _count_pages(pages)
            lines.append(
                f"- **{name}** — {read_wiki_scope(root)} "
                f"({counts}, total pages: {total}) → wikis/{name}/wiki/index.md"
            )

        target = self.default_wiki if self.default_wiki in roots else next(iter(roots))
        pages_dir = roots[target] / "wiki"
        lines.append("")
        lines.append(f"### Pages in `{target}` (directory scope)")

        emitted = 0
        truncated = False
        for group in _INVENTORY_GROUPS:
            group_dir = pages_dir / group
            entries = _page_entries(group_dir)
            if not entries:
                continue
            lines.append("")
            lines.append(f"#### {group}/ ({len(entries)})")
            for rel, title in entries:
                if emitted >= self.max_entries:
                    truncated = True
                    break
                lines.append(f"- `{group}/{rel}` — {title}")
                emitted += 1
            if truncated:
                break

        if truncated:
            lines.append("")
            lines.append(
                f"_(inventory truncated at {self.max_entries} pages — the wiki has more; "
                "treat the directory as partial and say so in the file header.)_"
            )
        if emitted == 0:
            lines.append("")
            lines.append("_(no entity or concept pages yet)_")
        return "\n".join(lines)

    # -- prompt --------------------------------------------------------------

    def read_policy(self) -> str:
        try:
            return self.policy_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def read_directory(self) -> str:
        try:
            return self.wiki_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def build_prompt(self) -> str:
        """Prompt completo del run: meccanismo, policy utente, inventario, stato."""
        parts = [
            render_template(
                "agent/atlas.md",
                strip=True,
                # Relativo al workspace, come fa Dream con `memory/MEMORY.md`, e
                # non assoluto. Un assoluto qui era un divieto travestito da
                # istruzione: la allowlist di scrittura tiene la forma
                # *risolta* del path (vedi `build_tools`), mentre questa era la
                # forma logica, e su Android le due divergono — `/data/user/0`
                # è un symlink a `/data/data`. Il modello riceveva l'unico path
                # su cui può scrivere in una forma che la guardia rifiuta, e
                # ogni run finiva senza scrivere niente: nessun errore visibile,
                # solo un fingerprint che non avanza e una chiamata al provider
                # bruciata ogni dodici ore. I test non lo vedevano perché
                # scrivono tutti in relativo.
                wiki_file=self.wiki_file.relative_to(self.workspace).as_posix(),
                default_wiki=self.default_wiki,
            )
        ]
        policy = self.read_policy()
        if policy:
            # La policy dell'utente viene dopo il meccanismo e prima dei dati:
            # è l'unica parte del prompt che può restringere o allargare i
            # criteri, e deve poter contraddire i default del template.
            parts.append(
                "## User Policy (authoritative — overrides the generic criteria above)\n\n"
                + policy
            )
        parts.append("## Wiki Inventory\n\n" + self.build_inventory())
        current = self.read_directory()
        parts.append(
            "## Current `memory/WIKI.md`\n\n" + (current if current else "_(empty — first run)_")
        )
        return "\n\n---\n\n".join(parts)

    # -- sandbox -------------------------------------------------------------

    def build_tools(self):
        """Registry ristretto del run: lettura ovunque nel workspace, scrittura solo su WIKI.md.

        Il ``FileStates`` del run è esposto come attributo ``file_states`` del
        registry, come fa Dream: è per-run e traccia scritture tentate e
        riuscite, così :func:`run_atlas` può decidere se registrare il
        fingerprint.
        """
        from jenny.agent.tools.apply_patch import ApplyPatchTool
        from jenny.agent.tools.file_state import FileStates
        from jenny.agent.tools.filesystem import (
            EditFileTool,
            ListDirTool,
            ReadFileTool,
            WriteFileTool,
        )
        from jenny.agent.tools.registry import ToolRegistry
        from jenny.agent.tools.search import FindFilesTool, GrepTool

        tools = ToolRegistry()
        file_states = FileStates()
        # Stessa canonicalizzazione di ``MemoryStore.build_dream_tools``: su
        # Android la dir dati è esposta come ``/data/user/0/<pkg>`` ma
        # ``.resolve()`` la riscrive in ``/data/data/<pkg>``. Se la base di
        # risoluzione e la allowlist di file esatti restano in forme diverse, il
        # guard anti-symlink scatta e il run non riesce a scrivere niente.
        workspace = self.workspace.resolve()
        writable = [self.wiki_file.resolve()]

        for read_only_tool in (ReadFileTool, ListDirTool, FindFilesTool, GrepTool):
            tools.register(read_only_tool(
                workspace=workspace,
                allowed_dir=workspace,
                file_states=file_states,
            ))
        # ``write_files_only``: nessuna directory è scrivibile, solo la
        # allowlist di file esatti — cioè memory/WIKI.md e basta. MEMORY.md,
        # SOUL.md e USER.md restano fuori portata (appartengono a Dream, e due
        # processi che riscrivono lo stesso file a cadenze diverse si cancellano
        # a vicenda) e così anche la wiki, che è la fonte da cui Atlas legge.
        for write_tool in (WriteFileTool, EditFileTool, ApplyPatchTool):
            tools.register(write_tool(
                workspace=workspace,
                allowed_dir=workspace,
                extra_write_allowed_files=writable,
                file_states=file_states,
                restrict_to_workspace=True,
                write_files_only=True,
            ))
        tools.file_states = file_states
        return tools

    # -- session -------------------------------------------------------------

    @staticmethod
    def session_key() -> str:
        """Session key di un run, es. ``atlas:20260806-100000``."""
        return f"atlas:{datetime.now():%Y%m%d-%H%M%S}"


def _count_pages(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    return sum(1 for p in directory.rglob("*.md") if not p.name.startswith("."))


def _page_entries(group_dir: Path) -> list[tuple[str, str]]:
    """``(path relativo al gruppo, titolo)`` per ogni pagina, in ordine stabile."""
    if not group_dir.is_dir():
        return []
    entries: list[tuple[str, str]] = []
    for path in sorted(group_dir.rglob("*.md")):
        if path.name.startswith("."):
            continue
        stem = path.stem
        try:
            title = extract_title(path.read_text(encoding="utf-8")) or stem
        except OSError:
            title = stem
        entries.append((path.relative_to(group_dir).as_posix(), title))
    return entries


async def _silent(*_args: Any, **_kwargs: Any) -> None:
    pass


async def run_atlas(
    agent: Any,
    *,
    store: AtlasStore | None = None,
    force: bool = False,
    snapshot_callback: Callable[[], Awaitable[None]] | None = None,
) -> AtlasOutcome:
    """Esegue un run Atlas e restituisce l'esito.

    Unico punto di ingresso: lo usano sia il cron (``runtime/cron_dispatch.py``)
    sia lo slash command (``command/builtin.py``). *force* salta il controllo
    del fingerprint ma non quello sull'esistenza delle wiki.

    ``snapshot_callback``, quando fornito, viene invocato *prima* che il modello
    modifichi ``WIKI.md``. Il pattern è lo stesso di Dream: uno snapshot
    pre-scrittura rende ogni modifica reversibile. Fail-open: un checkpoint
    fallito non blocca il run.
    """
    from jenny.agent.memory import MemoryStore
    from jenny.agent.token_usage import record_response_token_usage

    if store is None:
        from jenny.config.loader import load_config

        config = load_config()
        store = AtlasStore.from_config(config.workspace_path, config)

    if not store.has_wikis():
        logger.debug("Atlas: no wikis under {}", store.wikis_dir)
        return AtlasOutcome(status="skipped_no_wikis")

    fingerprint = store.fingerprint()
    if not force and fingerprint == store.last_fingerprint():
        logger.debug("Atlas: wiki unchanged since last run")
        return AtlasOutcome(status="skipped_unchanged")

    t0 = time.monotonic()
    resp = None
    tools = store.build_tools()
    # Checkpoint pre-Atlas: uno snapshot prima rende ogni modifica a WIKI.md
    # reversibile. Fail-open: un checkpoint fallito non blocca il run.
    if snapshot_callback is not None:
        try:
            await snapshot_callback()
        except Exception:
            logger.exception("Pre-atlas snapshot failed")
    try:
        resp = await agent.process_direct(
            store.build_prompt(),
            session_key=AtlasStore.session_key(),
            ephemeral=True,
            tools=tools,
            on_progress=_silent,
        )
    except Exception as exc:  # noqa: BLE001 — l'esito viaggia nell'outcome
        logger.exception("Atlas run failed")
        return AtlasOutcome(
            status="failed", elapsed=time.monotonic() - t0, detail=str(exc)
        )
    finally:
        record_response_token_usage(
            resp,
            source="atlas",
            timezone_name=_timezone_of(agent),
        )

    elapsed = time.monotonic() - t0
    file_states = getattr(tools, "file_states", None)
    if MemoryStore.internal_run_should_commit(resp, file_states):
        store.write_state(fingerprint)
        logger.info("Atlas: directory updated in {:.1f}s", elapsed)
        outcome = AtlasOutcome(status="written", elapsed=elapsed)
    elif MemoryStore.internal_run_completed(resp):
        # Completato ma con tutte le scritture bloccate o fallite: non
        # registrare il fingerprint, altrimenti la wiki risulterebbe digerita
        # e il prossimo tick salterebbe un aggiornamento mai avvenuto.
        logger.warning("Atlas: run completed without writing; fingerprint not advanced")
        outcome = AtlasOutcome(status="no_write", elapsed=elapsed)
    else:
        logger.warning("Atlas: run did not complete; fingerprint not advanced")
        outcome = AtlasOutcome(status="incomplete", elapsed=elapsed)

    _prune_sessions(agent)
    return outcome


def _timezone_of(agent: Any) -> str | None:
    context = getattr(agent, "context", None)
    return getattr(context, "timezone", None)


def _prune_sessions(agent: Any) -> None:
    from jenny.agent.memory import MemoryStore

    sessions = getattr(agent, "sessions", None)
    sessions_dir = getattr(sessions, "sessions_dir", None)
    if sessions_dir is None:
        return
    pruned = MemoryStore.prune_internal_sessions(sessions_dir, "atlas")
    if pruned and hasattr(agent, "evict_pruned_sessions"):
        agent.evict_pruned_sessions(pruned)
