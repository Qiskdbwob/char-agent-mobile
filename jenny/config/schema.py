"""Configuration schema using Pydantic."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from loguru import logger

from jenny.config.tool_schemas import (
    AndroidWebToolsConfig,
    DiagnosticsToolConfig,
    FileToolsConfig,
    IntrospectToolConfig,
    LocationConfig,
    MCPConfig,
    MyToolConfig,
    PythonExecConfig,
    SshConfig,
)
from jenny.config_base import Base
from jenny.cron.types import CronSchedule
from jenny.pydantic_compat import (
    AliasChoices,
    BaseSettings,
    Field,
    model_validator,
)
from jenny.runtime.update_manifest import DEFAULT_MANIFEST_URL
from jenny.snapshot.engine import DEFAULT_EXCLUDE_GLOBS


class DreamConfig(Base):
    """Dream memory consolidation configuration."""

    _HOUR_MS = 3_600_000

    enabled: bool = True  # Register the periodic Dream consolidation job on startup
    interval_h: int = Field(default=2, ge=1)  # Every 2 hours by default

    def build_schedule(self) -> CronSchedule:
        """Build the runtime schedule from the configured interval."""
        return CronSchedule(kind="every", every_ms=self.interval_h * self._HOUR_MS)

    def describe_schedule(self) -> str:
        """Return a human-readable summary for logs and startup output."""
        hours = self.interval_h
        return f"every {hours}h"


class AtlasConfig(Base):
    """Atlas wiki-directory configuration.

    Atlas è il gemello di Dream sul lato wiki: compila ``memory/WIKI.md``
    leggendo ``workspace/wikis/``. Il default è ``enabled`` perché senza wiki
    il job esce prima di qualunque chiamata al provider — a workspace vuoto
    costa zero token.
    """

    _HOUR_MS = 3_600_000

    enabled: bool = True  # Register the periodic Atlas job on startup
    # Sei ore, non due come Dream: una wiki cambia con la cadenza con cui
    # l'utente ci fa ingest, non con quella delle conversazioni. Il fingerprint
    # rende comunque gratuiti i tick a wiki ferma.
    # Non dodici, però: su Android il doze allunga i tick (misurato, un job da
    # 30 minuti scattava fino a 83) e il processo non sopravvive sempre mezza
    # giornata. Una scadenza a sei ore cade dentro una sessione plausibile
    # dell'app; una a dodici rischiava di non arrivare mai.
    interval_h: int = Field(default=6, ge=1)
    # Tetto del blocco iniettato in *ogni* system prompt: la rubrica è utile
    # perché è corta. Oltre questa soglia viene troncata a valle, così un run
    # generoso non si porta dietro il costo su tutti i turni successivi.
    max_context_tokens: int = Field(
        default=1200,
        ge=100,
        validation_alias=AliasChoices("maxContextTokens", "max_context_tokens"),
        serialization_alias="maxContextTokens",
    )

    def build_schedule(self) -> CronSchedule:
        """Build the runtime schedule from the configured interval."""
        return CronSchedule(kind="every", every_ms=self.interval_h * self._HOUR_MS)

    def describe_schedule(self) -> str:
        """Return a human-readable summary for logs and startup output."""
        hours = self.interval_h
        return f"every {hours}h"


class AgentDefaults(Base):
    """Default agent configuration."""

    model: str = ""
    # Tetto per singola risposta. Sui reasoning model il thinking pesa su questo
    # stesso budget: con 8192 un turno che pianifica a lungo lo consumava tutto
    # prima di dire qualcosa. 16384 lascia margine restando dentro la finestra
    # anche coi prompt più grossi osservati (~38k su 65536).
    max_tokens: int = 16384
    context_window_tokens: int = 65536
    context_block_limit: int | None = None
    temperature: float = 0.1
    # Esplicito invece di lasciare il default del provider: un reasoning model a
    # briglia sciolta consuma tutto il budget di output in ragionamento su un
    # compito aperto. "medium" limita il thinking senza appiattirlo.
    reasoning_effort: str | None = "medium"
    max_tool_iterations: int = 200
    # L'agente principale delega: tre slot reggono due lavori lunghi piu il
    # lavoro breve. Uno slot resta sempre riservato ai job quick (vedi
    # ``SubagentManager._check_capacity``), altrimenti i long-running saturano il
    # pool e non c'e piu modo di rispondere all'utente. Tre e non cinque perche
    # ogni slot e una richiesta LLM in volo da un telefono: oltre non e la CPU a
    # cedere ma il rate limit del provider e la batteria.
    #
    # Alzare questo default NON basta per chi aggiorna: ``loader.py`` serializza
    # il config *includendo i default*, quindi ogni installazione esistente porta
    # il vecchio valore scritto nel file. Se lo cambi, aggiungi una migrazione in
    # ``Config._migrate_by_version`` e alza ``CURRENT_CONFIG_VERSION``.
    max_concurrent_subagents: int = Field(default=3, ge=1)
    # Modalita orchestratore: l'agente principale carica il registry con lo scope
    # "orchestrator" invece di "core" — delega il lavoro pesante ai subagent e
    # perde i tool che gonfiano la sessione dell'utente (python_exec, scrittura,
    # patch, download, exec_session, search), tenendo lettura, controllo e i tool
    # web (web_search/web_fetch/browser_*). Acceso di default perche e il
    # comportamento voluto; resta un flag perche cambia in modo sostanziale cio
    # che Jenny puo fare da sola e l'utente gira su un solo telefono, senza
    # altro modo per tornare indietro.
    orchestrator_mode: bool = Field(
        default=True,
        validation_alias=AliasChoices("orchestratorMode", "orchestrator_mode"),
        serialization_alias="orchestratorMode",
    )
    # Watchdog di stallo: oltre questa soglia senza progresso il subagent viene
    # marcato ``stalled``. Marcatura sola, mai cancellazione: rilanciare e una
    # decisione dell'utente o dell'orchestratore.
    subagent_stall_threshold_seconds: int = Field(default=180, ge=10)
    # Errori tool recuperabili che un subagent puo commettere prima di arrendersi.
    # Zero = il vecchio comportamento, in cui il primo risultato che iniziava per
    # "Error" uccideva il subagent: un ``offset`` indovinato male buttava via un
    # lavoro finito. La contabilita (consecutivi, totali, boundary di sicurezza)
    # sta in ``jenny/agent/tool_execution.py::ToolErrorBudget``.
    subagent_tool_error_budget: int = Field(default=3, ge=0)
    max_tool_result_chars: int = 16000
    provider_retry_mode: Literal["standard", "persistent"] = "standard"
    tool_hint_max_length: int = Field(default=40, ge=20, le=500)
    # Stringa vuota = auto: timezone del dispositivo su Android, altrimenti
    # UTC. Risolta una volta per load in ``loader._resolve_default_timezone``.
    timezone: str = ""
    bot_name: str = "Jenny"
    bot_icon: str = "✿"
    language: str = "it"
    tool_choice: Literal["auto", "any", "none", "required"] = Field(
        default="auto",
        validation_alias=AliasChoices("toolChoice", "tool_choice"),
    )
    disabled_skills: list[str] = Field(default_factory=list)
    session_ttl_minutes: int = Field(
        default=15,
        ge=0,
        validation_alias=AliasChoices("idleCompactAfterMinutes"),
        serialization_alias="idleCompactAfterMinutes",
    )
    max_messages: int = Field(default=120, ge=0)
    consolidation_ratio: float = Field(default=0.5, ge=0.1, le=0.95)
    dream: DreamConfig = Field(default_factory=DreamConfig)
    atlas: AtlasConfig = Field(default_factory=AtlasConfig)
    model_preset: str | None = Field(
        default=None,
        validation_alias=AliasChoices("modelPreset", "model_preset"),
    )


class AgentsConfig(Base):
    """Agent configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


class ProviderConfig(Base):
    """LLM provider configured by the user."""

    name: str
    format: Literal["openai_compat", "anthropic"]
    api_key: str | None = Field(default=None, repr=False)
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None
    extra_body: dict[str, Any] | None = None
    extra_query: dict[str, str] | None = None
    api_type: Literal["auto", "chat_completions", "responses"] = "auto"


class ProvidersConfig(Base):
    """User-defined LLM providers."""

    providers: list[ProviderConfig] = Field(default_factory=list)
    default: str | None = None


class HeartbeatConfig(Base):
    """Heartbeat service configuration (now backed by cron)."""

    enabled: bool = True
    interval_s: int = Field(default=30 * 60, ge=1)  # 30 minutes
    keep_recent_messages: int = 8


class GatewayConfig(Base):
    """Gateway/server configuration."""

    host: str = "127.0.0.1"  # Safer default: local-only bind.
    port: int = 18790
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)


class ToolsConfig(Base):
    """Tools configuration.

    I tipi dei sub-config dei tool sono importati direttamente da
    ``config.tool_schemas`` (modulo leggero, nessun ciclo): niente più
    forward-ref / ``model_rebuild`` / risoluzione lazy.
    """

    android_web: AndroidWebToolsConfig = Field(
        default_factory=AndroidWebToolsConfig,
        validation_alias=AliasChoices("androidWeb", "android_web"),
    )
    python_exec: PythonExecConfig = Field(
        default_factory=PythonExecConfig,
        validation_alias=AliasChoices("pythonExec", "python_exec"),
    )
    file: FileToolsConfig = Field(default_factory=FileToolsConfig)
    location: LocationConfig = Field(default_factory=LocationConfig)
    my: MyToolConfig = Field(default_factory=MyToolConfig)
    introspect: IntrospectToolConfig = Field(default_factory=IntrospectToolConfig)
    diagnostics: DiagnosticsToolConfig = Field(default_factory=DiagnosticsToolConfig)
    ssh: SshConfig = Field(default_factory=SshConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    # NB: canonical home = ``Config.security`` (SecurityConfig). Questo campo
    # resta su ToolsConfig come **mirror** sincronizzato (il tool-layer lo legge
    # via ``ctx.config.restrict_to_workspace``); il validator di ``Config`` lo
    # tiene allineato a ``security``. Non impostarlo a mano: usare ``security``.
    restrict_to_workspace: bool = True


class SecurityConfig(Base):
    """Policy di sicurezza a livello top-level (fonte canonica).

    ``restrict_to_workspace``: mantiene l'accesso dei tool dentro il workspace.
    ``ssrf_whitelist``: CIDR esentati dal blocco SSRF (es. ``["100.64.0.0/10"]``
    per Tailscale). Retro-compat: se un vecchio ``config.json`` porta questi campi
    sotto ``tools`` e non c'è ``security``, il validator di ``Config`` li migra qui.
    """

    restrict_to_workspace: bool = True
    ssrf_whitelist: list[str] = Field(default_factory=list)


# Modalità ammesse per ``PowerConfig.keep_awake``. Fuori da queste tre si
# ricade su ``DEFAULT_KEEP_AWAKE``: un valore scritto male non deve impedire
# l'avvio del gateway.
KEEP_AWAKE_MODES = ("off", "turns", "always")
DEFAULT_KEEP_AWAKE = "turns"


class PowerConfig(Base):
    """Gestione dell'alimentazione: wakelock e risvegli programmati (anti-doze).

    Perché esiste: un foreground service **non** tiene un wakelock sulla CPU.
    Tiene vivo il processo, non il processore. A schermo spento il device entra
    in suspend e i timer asyncio non scattano: il loop del gateway resta fermo
    ovunque si trovi, i cron slittano di minuti o ore e da fuori sembra che
    Jenny si sia piantata. Solo un ``PARTIAL_WAKE_LOCK`` impedisce la sospensione
    della CPU, e i risvegli puntuali richiedono un alarm dell'OS.

    ``keep_awake`` sceglie quanto in là spingersi:

    * ``"turns"`` (default) — il wakelock viene preso **solo** attorno al lavoro
      vero (un turno dell'agente, un job cron, una sessione SSH) e rilasciato
      subito dopo. È il compromesso: la CPU resta sveglia quando serve, il
      telefono dorme il resto del tempo.
    * ``"always"`` — wakelock tenuto per tutta la vita del servizio. Da usare a
      telefono in carica: la batteria non regge un lock permanente.
    * ``"off"`` — comportamento pre-0.6.6, nessun wakelock. Resta disponibile
      come via di fuga se il lock dovesse creare problemi su un device.

    ``wakelock_rotate_min`` ruota il lock (release + acquire) per non farlo
    invecchiare indefinitamente; 0 disattiva la rotazione. Il watchdog misura il
    ritardo reale del loop e ``gap_warning_min`` è la soglia oltre la quale un
    buco di attività va segnalato invece di passare inosservato.
    """

    keep_awake: str = Field(
        default=DEFAULT_KEEP_AWAKE,
        validation_alias=AliasChoices("keepAwake", "keep_awake"),
        serialization_alias="keepAwake",
    )
    # 0 = nessuna rotazione. Il tetto a 4 ore evita che una config assurda
    # trasformi la "rotazione" in "mai".
    wakelock_rotate_min: int = Field(
        default=50,
        ge=0,
        le=240,
        validation_alias=AliasChoices("wakelockRotateMin", "wakelock_rotate_min"),
        serialization_alias="wakelockRotateMin",
    )
    watchdog_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("watchdogEnabled", "watchdog_enabled"),
        serialization_alias="watchdogEnabled",
    )
    watchdog_interval_min: int = Field(
        default=15,
        ge=5,
        le=120,
        validation_alias=AliasChoices("watchdogIntervalMin", "watchdog_interval_min"),
        serialization_alias="watchdogIntervalMin",
    )
    alarm_driven_cron: bool = Field(
        default=True,
        validation_alias=AliasChoices("alarmDrivenCron", "alarm_driven_cron"),
        serialization_alias="alarmDrivenCron",
    )
    alarm_clock_fallback: bool = Field(
        default=True,
        validation_alias=AliasChoices("alarmClockFallback", "alarm_clock_fallback"),
        serialization_alias="alarmClockFallback",
    )
    gap_warning_min: int = Field(
        default=60,
        ge=5,
        validation_alias=AliasChoices("gapWarningMin", "gap_warning_min"),
        serialization_alias="gapWarningMin",
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_keep_awake(cls, data: Any) -> Any:
        """Normalizza ``keep_awake`` e ricade su ``"turns"`` se non riconosciuto.

        Deliberatamente in ``mode="before"`` e non un ``field_validator``: qui il
        valore è ancora quello grezzo del file, quindi si intercetta anche un
        tipo sbagliato (``true``, ``null``, un numero) che la validazione del
        campo boccerebbe con un'eccezione. Un ``keep_awake`` scritto male è un
        refuso, non un motivo per non far partire il gateway.
        """
        if not isinstance(data, dict):
            return data
        for key in ("keepAwake", "keep_awake"):
            if key not in data:
                continue
            raw = data[key]
            mode = raw.strip().lower() if isinstance(raw, str) else ""
            if mode not in KEEP_AWAKE_MODES:
                logger.warning(
                    "Invalid power.keepAwake value {!r}; falling back to {!r}",
                    raw,
                    DEFAULT_KEEP_AWAKE,
                )
                mode = DEFAULT_KEEP_AWAKE
            if mode != raw:
                data = {**data, key: mode}
            break
        return data


class WikiConfig(Base):
    """Wiki configuration."""

    enabled: bool = True
    wikis_dir: str = "wikis"  # Relativo a workspace
    default_wiki: str = "main"
    extensions: list[str] = Field(default_factory=lambda: [
        "fenced_code",
        "tables",
        "toc",
        "wikilinks",
        "mermaid",
    ])


class WorkspaceConfig(Base):
    """Workspace file management configuration."""

    enabled: bool = True
    max_file_size: int = 1_000_000  # 1MB
    allow_delete: bool = True
    allow_write: bool = True


class AppsConfig(Base):
    """Jenny Apps configuration (workspace apps with typed actions)."""

    enabled: bool = True
    http_timeout_s: float = Field(default=20.0, ge=1.0, le=120.0)
    max_collection_bytes: int = 5_000_000


class SnapshotConfig(Base):
    """Configurazione del versioning locale del workspace (snapshot + backup).

    Gli snapshot sono creati automaticamente dal runtime (debounce su quiete,
    checkpoint pre-Dream, shutdown, safety giornaliero) senza coinvolgere
    l'LLM. ``pbkdf2_iterations`` governa la derivazione chiave del backup
    cifrato esportato.
    """

    enabled: bool = True
    scan_interval_minutes: int = Field(default=5, ge=1)
    quiet_minutes: int = Field(default=10, ge=1)
    daily_safety_snapshot: bool = True
    retention_recent: int = Field(default=20, ge=1)
    retention_thin_after_days: int = Field(default=30, ge=1)
    # Orizzonte massimo della storia in giorni (0 = per sempre). Gli ultimi
    # ``retention_recent`` snapshot restano comunque protetti dall'orizzonte.
    retention_max_age_days: int = Field(default=0, ge=0)
    # Il tetto rispecchia MAX_KDF_ITERATIONS del formato container (crypto.py).
    pbkdf2_iterations: int = Field(default=600_000, ge=100_000, le=10_000_000)
    # Unica fonte di verità: la costante del motore di snapshot (engine.py).
    exclude_globs: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDE_GLOBS)
    )


class UpdatesConfig(Base):
    """Controllo degli aggiornamenti in-app (manifest remoto + notifica in chat).

    ``enabled`` decide se il job periodico ``update_check`` viene registrato
    all'avvio **e** se ogni sua esecuzione fa qualcosa: il job registrato da un
    avvio precedente resta nello store del cron, quindi a spegnere davvero la
    rete è il controllo in ``CronDispatcher._run_update_check``.
    ``notify_in_chat`` decide invece se una versione nuova apre un messaggio in
    chat oppure resta solo visibile dove l'utente va a cercarla.

    Le ventiquattro ore di default non sono un compromesso di rete: sono la
    cadenza con cui ha senso *disturbare*. Il controllo costa una richiesta HTTP
    da qualche centinaio di byte, ma ogni suo esito positivo è un'interruzione.
    """

    enabled: bool = True
    # Unica fonte di verità: la costante di ``runtime/update_manifest.py``, un
    # modulo senza dipendenze proprio perché questo schema viene caricato da
    # ``config/bootstrap.py`` prima dell'event loop.
    manifest_url: str = DEFAULT_MANIFEST_URL
    check_interval_h: int = Field(default=24, ge=1, le=168)
    notify_in_chat: bool = True


class TelegramConfig(Base):
    """Configurazione del canale Telegram (bot personale).

    Stato derivato, nessuna enum persistita:
    disabled → token presente ma unpaired (``pairing_code`` attivo) → paired.
    Il ``pairing_code`` è persistito così il pairing sopravvive ai riavvii del
    processo (frequenti su Android) e viene azzerato al pairing riuscito.
    """

    enabled: bool = False
    bot_token: str | None = Field(default=None, repr=False)
    bot_username: str | None = None
    paired_chat_id: str | None = None
    paired_username: str | None = None
    pairing_code: str | None = Field(default=None, repr=False)
    poll_timeout_s: int = Field(default=50, ge=1, le=300)


class ModelPresetConfig(Base):
    """Named model preset configuration."""
    label: str | None = None
    provider: str | None = Field(default=None, validation_alias=AliasChoices("provider"))
    model: str | None = None
    max_tokens: int | None = None
    context_window_tokens: int | None = None
    temperature: float | None = None
    reasoning_effort: str | None = None


# Versione corrente dello schema del config. Alzala di uno ogni volta che
# aggiungi un ramo a ``Config._migrate_by_version``, mai altrimenti.
CURRENT_CONFIG_VERSION = 1

# Migrazioni gia annunciate in questo processo. Solo per il log: la migrazione
# resta idempotente e rigira a ogni parse finche il file non viene riscritto (lo
# fa ``store.persist_schema_migrations`` all'avvio), ma il config viene letto piu
# volte per boot e una riga per lettura e rumore, non informazione.
_ANNOUNCED_MIGRATIONS: set[int] = set()


class Config(BaseSettings):
    """Root configuration for jenny."""

    # Versione dello *schema* del file, non della app. Serve a una sola cosa:
    # distinguere "questo valore e una scelta dell'utente" da "questo valore e un
    # vecchio default rimasto scritto nel file". Senza il contatore la differenza
    # e indecidibile, perche ``loader.py`` serializza includendo i default: un
    # config scritto quando il default era X porta X per sempre, e alzare il
    # default nello schema non raggiunge nessuno di quelli che aggiornano.
    #
    # Assente (installazioni pre-versioning) => 0, cosi le migrazioni girano.
    # Dopo il parse viene sempre riportata a ``CURRENT_CONFIG_VERSION``, e la
    # prima scrittura ordinaria del config la persiste: da quel momento i valori
    # nel file *sono* scelte, e nessuna migrazione li tocca piu.
    config_version: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("configVersion", "config_version"),
        serialization_alias="configVersion",
    )
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    # Allegati non-immagine: di default vengono solo referenziati per path
    # (salvati in ``uploads/``) e letti on-demand dall'agente coi suoi tool,
    # senza iniettarne il testo nel contesto a ogni turno. Impostare a ``True``
    # per estrarre e inlinare subito il testo di PDF/documenti.
    extract_document_text: bool = False
    websocket: dict[str, Any] = Field(default_factory=dict)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    power: PowerConfig = Field(default_factory=PowerConfig)
    wiki: WikiConfig = Field(default_factory=WikiConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    apps: AppsConfig = Field(default_factory=AppsConfig)
    snapshots: SnapshotConfig = Field(default_factory=SnapshotConfig)
    updates: UpdatesConfig = Field(default_factory=UpdatesConfig)
    model_presets: dict[str, ModelPresetConfig] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("modelPresets", "model_presets"),
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_by_version(cls, data: Any) -> Any:
        """Migrazioni una-tantum sui valori, guidate da ``config_version``.

        Ogni migrazione e condizionata *anche* sul valore vecchio esatto: chi ha
        gia il valore nuovo non viene toccato, e la migrazione resta idempotente
        se il file non fa in tempo a essere riscritto prima del boot successivo.

        Costo accettato consapevolmente: un utente che avesse scelto a mano
        esattamente il vecchio default viene comunque spostato, una volta sola e
        con un log a WARNING. Il contrario — lasciare spenta la concorrenza a
        tutti quelli che aggiornano — e peggio e silenzioso.
        """
        if not isinstance(data, dict):
            return data
        raw_version = data.get("configVersion", data.get("config_version", 0))
        try:
            version = int(raw_version)
        except (TypeError, ValueError):
            # Versione illeggibile (file toccato a mano): la trattiamo come 0 e la
            # riscriviamo sanificata, invece di far fallire la validazione del
            # campo e mandare in quarantena un config per il resto valido.
            version = 0
            data = {k: v for k, v in data.items() if k != "config_version"}
            data["configVersion"] = 0
        if version >= CURRENT_CONFIG_VERSION:
            return data

        # v1: ``maxConcurrentSubagents`` passa da 1 a 3. L'1 era il default di
        # quando i subagent erano fire-and-forget uno alla volta; con
        # l'orchestratore che delega tutto, un solo slot serializza il fan-out e
        # un job lungo blocca ogni altra richiesta dell'utente.
        if version < 1:
            agents = data.get("agents")
            if isinstance(agents, dict):
                defaults = agents.get("defaults")
                if isinstance(defaults, dict):
                    for key in ("maxConcurrentSubagents", "max_concurrent_subagents"):
                        if defaults.get(key) == 1:
                            new_value = AgentDefaults.model_fields[
                                "max_concurrent_subagents"
                            ].default
                            if 1 not in _ANNOUNCED_MIGRATIONS:
                                _ANNOUNCED_MIGRATIONS.add(1)
                                logger.warning(
                                    "Config migration v1: maxConcurrentSubagents 1 -> {} "
                                    "(the old default blocked subagent fan-out; set it back "
                                    "explicitly if you really want one at a time)",
                                    new_value,
                                )
                            defaults = {**defaults, key: new_value}
                            agents = {**agents, "defaults": defaults}
                            data = {**data, "agents": agents}
                            break
        return data

    @model_validator(mode="after")
    def _stamp_config_version(self) -> "Config":
        """Porta la versione a quella corrente: le migrazioni sono state applicate.

        Non scrive nulla — la prima ``store.mutate()`` ordinaria persiste lo
        stamp insieme al resto, perche il dump include tutti i campi.
        """
        if self.config_version != CURRENT_CONFIG_VERSION:
            self.config_version = CURRENT_CONFIG_VERSION
        return self

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_security_fields(cls, data: Any) -> Any:
        """Retro-compat: sposta ``tools.{restrict_to_workspace,ssrf_whitelist}``
        legacy sotto ``security`` quando ``security`` non è dato esplicitamente."""
        if not isinstance(data, dict):
            return data
        tools = data.get("tools")
        if not isinstance(tools, dict):
            return data
        if "security" not in data:
            # Accetta sia snake_case sia l'alias camelCase (Base) presenti nei
            # config legacy: es. ``ssrf_whitelist`` o ``ssrfWhitelist``.
            aliases = {
                "restrict_to_workspace": ("restrict_to_workspace", "restrictToWorkspace"),
                "ssrf_whitelist": ("ssrf_whitelist", "ssrfWhitelist"),
            }
            migrated: dict[str, Any] = {}
            for field, keys in aliases.items():
                for key in keys:
                    if key in tools:
                        migrated[field] = tools[key]
                        break
            if migrated:
                data = {**data, "security": migrated}
        return data

    @model_validator(mode="after")
    def _sync_security_mirror(self) -> Config:
        """``security`` è canonico; ``tools`` ne è il mirror letto dal tool-layer."""
        self.tools.restrict_to_workspace = self.security.restrict_to_workspace
        return self

    @property
    def workspace_path(self) -> Path:
        """Get the fixed workspace path."""
        from jenny.config.paths import get_workspace_path
        return get_workspace_path()

    def get_active_provider(self) -> ProviderConfig:
        """Return the active provider config.

        Uses ``providers.default`` if set, otherwise the first provider in
        the list.  Raises ValueError if no provider is configured.
        """
        if self.providers.default:
            for p in self.providers.providers:
                if p.name == self.providers.default:
                    return p
        if self.providers.providers:
            return self.providers.providers[0]
        raise ValueError("No provider configured. Add one in settings or config.json.")
