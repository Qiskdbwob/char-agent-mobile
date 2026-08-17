"""Config dataclasses for the built-in tools.

Queste classi vivevano accanto alle implementazioni dei tool, costringendo
``ToolsConfig`` a forward-ref + risoluzione lazy (``_lazy_default`` +
``model_rebuild`` + ``try/except ImportError``) per evitare cicli d'import.

Qui stanno in un modulo LEGGERO che importa solo ``Base`` + ``Field`` (nessuna
dipendenza dai moduli tool, pesanti). Così sia ``config/schema.py`` sia i moduli
tool importano da qui *verso il basso* — niente cicli, niente rebuild a runtime,
e un fallimento d'import è un errore rumoroso allo startup invece di essere
silenziato.

I moduli tool re-esportano la loro classe (``from jenny.config.tool_schemas
import PythonExecConfig``) così gli import storici continuano a funzionare.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from loguru import logger

from jenny.config_base import Base
from jenny.pydantic_compat import Field, model_validator


class PythonExecConfig(Base):
    """Python exec tool configuration."""

    enable: bool = True
    timeout: int = Field(default=60, ge=0)  # 0 = no limit
    max_output_chars: int = Field(default=10_000, ge=1000, le=50_000)
    allowed_modules: list[str] = Field(
        default_factory=lambda: [
            "os", "sys", "pathlib", "json", "re", "math", "datetime",
            "collections", "itertools", "functools", "typing",
            "io", "shutil", "glob", "hashlib",
            # Raw URL/HTTP clients (httpx, urllib) intentionally NOT allowlisted:
            # `import httpx` or `from urllib.request import urlopen` would let
            # guarded code hit loopback/link-local/RFC1918 targets (SSRF) or read
            # local files via file:// (LFI), bypassing both the SSRF policy and
            # the workspace policy. Outbound HTTP stays available only via the
            # http_get/http_post helpers (which call validate_url_target).
            # Re-add "httpx"/"urllib" here explicitly to opt back into raw access.
            "base64", "asyncio", "csv",
            "platform", "time", "struct", "textwrap", "unicodedata",
            "html", "xml", "dataclasses", "enum", "uuid",
        ]
    )
    blocked_modules: list[str] = Field(
        default_factory=lambda: [
            "subprocess", "pty", "shlex",
            "multiprocessing", "ctypes", "socket", "signal",
            "termios", "tty", "grp", "pwd", "resource",
            "syslog", "curses", "readline", "_thread", "fcntl",
        ]
    )


class FileToolsConfig(Base):
    """Filesystem tools configuration."""

    enable: bool = True  # built-in file tools on by default
    # Grant read-only access to jenny's own source so the agent can
    # inspect the framework it runs on (never writable).
    expose_package_source: bool = True


class MyToolConfig(Base):
    """Self-inspection tool configuration."""

    enable: bool = True
    allow_set: bool = False


# Unici motori supportati dal bridge Android. Il valore di ``search_engine``
# è una stringa libera nello schema perché la route di settings la valida già
# con 400 (vedi ``settings_api.update_web_search_settings``); qui il validator
# copre la strada che la route non vede — una config.json modificata a mano —
# con lo stesso pattern di ``PowerConfig._coerce_keep_awake``: un valore
# scritto male è un refuso, non un motivo per cui ``web_search`` fallisca su
# ogni chiamata con un ValueError scoperto solo a runtime.
ANDROID_WEB_SEARCH_ENGINES = ("bing",)
DEFAULT_ANDROID_WEB_SEARCH_ENGINE = "bing"


class AndroidWebSearchConfig(Base):
    """Android WebView-backed search configuration."""

    search_engine: str = DEFAULT_ANDROID_WEB_SEARCH_ENGINE
    max_results: int = 5
    timeout: int = 30

    @model_validator(mode="before")
    @classmethod
    def _coerce_search_engine(cls, data: Any) -> Any:
        """Normalizza ``search_engine`` e ricade sul default se non riconosciuto.

        ``mode="before"``: il valore è ancora quello grezzo del file, quindi si
        intercetta anche un tipo sbagliato (numero, null) che la validazione del
        campo boccerebbe con un'eccezione. Se l'engine non è tra quelli
        implementati dal bridge (``android_web._bridge_search`` ne accetta uno
        solo), si logga e si ricade su ``"bing"`` invece di far fallire il
        gateway o ogni chiamata a ``web_search``.
        """
        if not isinstance(data, dict):
            return data
        # Entrambe le grafie: il file può usare camelCase (alias di Base) e il
        # validator gira sul dict grezzo, prima della popolazione degli alias.
        for key in ("search_engine", "searchEngine"):
            if key not in data:
                continue
            raw = data[key]
            engine = raw.strip().lower() if isinstance(raw, str) else ""
            if engine not in ANDROID_WEB_SEARCH_ENGINES:
                logger.warning(
                    "Invalid androidWeb.search.searchEngine value {!r}; falling back to {!r}",
                    raw,
                    DEFAULT_ANDROID_WEB_SEARCH_ENGINE,
                )
                engine = DEFAULT_ANDROID_WEB_SEARCH_ENGINE
            if engine != raw:
                data = {**data, key: engine}
            break
        return data


class AndroidWebFetchConfig(Base):
    """Android WebView-backed fetch configuration."""

    max_chars: int = 50000


class AndroidWebToolsConfig(Base):
    """Android-only WebView web tools configuration."""

    enable: bool = True
    search: AndroidWebSearchConfig = Field(default_factory=AndroidWebSearchConfig)
    fetch: AndroidWebFetchConfig = Field(default_factory=AndroidWebFetchConfig)


class LocationConfig(Base):
    """Configurazione della posizione del dispositivo (solo Android).

    Sorgente primaria: GPS del telefono via ``LocationBridge`` nativo (fix
    last-known, gratis, iniettato nel contesto a ogni turno). Il tool
    ``get_location`` on-demand forza un fix fresco solo quando l'agente passa
    ``precise=true``. La posizione condivisa via Telegram fa da override
    per-canale con validità ``telegram_ttl_s`` (poi anche Telegram ricade sul
    GPS live).

    ``enable`` è il toggle utente (default ON), comunque gattato dal permesso
    runtime Android ``ACCESS_FINE_LOCATION``: se il permesso manca il bridge
    ritorna sempre ``None`` e non viene iniettato nulla.
    """

    enable: bool = True
    # Validità di una posizione condivisa via Telegram prima del fallback a GPS.
    telegram_ttl_s: int = Field(default=3600, ge=60)  # 1 h
    # Timeout del fix fresco on-demand (precise=true).
    fresh_timeout_s: int = Field(default=15, ge=1, le=60)


class IntrospectToolConfig(Base):
    """Source introspection tool configuration."""

    enable: bool = True


class DiagnosticsToolConfig(Base):
    """Diagnostics tool configuration."""

    enable: bool = True


class SshHostConfig(Base):
    """Un host SSH registrato a mano dall'utente in Settings.

    ``alias`` è **l'unica cosa che il modello passa** ai tool SSH, e da lì viene
    la garanzia che conta: l'agente non può raggiungere un indirizzo arbitrario
    della rete, può solo nominare un alias che un umano ha già dichiarato qui.
    Nessuna credenziale entra mai negli argomenti o nei risultati dei tool.

    Host e username invece il modello li *vede*, elencati da ``ssh_hosts``:
    senza non potrebbe scegliere fra due alias né dire all'utente su quale
    macchina ha agito. Non sono segreti — i segreti sono la chiave privata, che
    vive fuori dal workspace, e la ``password`` qui sotto.

    Su quella password serve una precisazione, perché questo commento diceva il
    falso: nessun tool *SSH* la legge e nessun risultato di tool la contiene, ma
    sta in chiaro in ``config.json``, che è *dentro* il workspace. Qualunque tipo
    di agente con ``read_file`` può quindi leggerla — ``researcher`` compreso,
    che è l'unico che ingerisce pagine non fidate e ha anche ``web_fetch``. È la
    stessa esposizione delle chiavi API dei provider e del token Telegram, ed è
    esattamente ciò che la chiave privata evita stando fuori dal workspace.

    ``host_key_fingerprint`` è **solo per display** nella UI. L'enforcement vero
    è il file ``known_hosts`` accanto alla chiave (vedi
    ``jenny.config.paths.get_ssh_dir``): è quello che il backend legge, e senza
    una riga corrispondente la connessione viene rifiutata. Vale per entrambi i
    modi di autenticazione, e con ``auth="password"`` conta di più: senza
    impronta verificata la password andrebbe a chiunque risponda a quell'indirizzo.
    """

    alias: str
    host: str
    port: int = Field(default=22, ge=1, le=65535)
    username: str
    # Mostrata al modello da ``ssh_hosts``: serve a fargli scegliere l'alias
    # giusto quando ce n'è più di uno ("il NAS di casa", "il VPS del sito").
    description: str = ""
    host_key_fingerprint: str | None = None
    # Come si autentica questo host. Default ``key``: è il modo che non lascia
    # un segreto riutilizzabile nella config, quindi resta quello di partenza
    # anche ora che la password esiste.
    auth: Literal["key", "password"] = "key"
    # ``repr=False`` è la convenzione con cui questo repo tiene i segreti fuori
    # dai log (come ``api_key`` e ``bot_token`` in ``config/schema.py``): un
    # ``repr`` di questo oggetto finisce facilmente in una riga di log o in un
    # messaggio d'errore, e la password non deve poterci arrivare.
    password: str | None = Field(default=None, repr=False)
    # Dove vivono i log dei job lunghi lato server (vedi il tool ``ssh_job``).
    job_log_dir: str = "/tmp/jenny-jobs"


class SshConfig(Base):
    """Accesso SSH a macchine remote.

    Spento di default e senza host: sono due gate distinti e volutamente
    entrambi necessari, perché questa è la sola capacità di Jenny che agisce su
    una macchina che non è il telefono.

    ``command_timeout_s`` è basso di proposito, e il wakelock introdotto in
    0.6.6 (``jenny/runtime/power.py``, tag ``ssh``) non è un motivo per alzarlo:
    tiene accesa la CPU, non la connessione. Un comando lungo atteso su un canale
    SSH aperto muore comunque al primo passaggio wifi→dati mobili, o se il
    gateway viene riavviato. I comandi lunghi vanno passati a ``ssh_job``, che li
    stacca dalla connessione e li segue a delta.
    """

    enable: bool = False
    hosts: list[SshHostConfig] = Field(default_factory=list)
    connect_timeout_s: float = Field(default=15.0, ge=1.0, le=60.0)
    command_timeout_s: int = Field(default=60, ge=1, le=300)
    max_output_chars: int = Field(default=10_000, ge=1_000, le=50_000)
    # 0 = keepalive disattivato.
    keepalive_interval_s: int = Field(default=30, ge=0, le=300)
    idle_close_s: int = Field(default=300, ge=30)
    max_transfer_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)


# --- MCP (Model Context Protocol) -------------------------------------------
# Server MCP configurati a mano dall'utente in Settings → Tools. Stesso modello
# di fiducia di ``SshConfig``: l'agente può raggiungere solo server che un umano
# ha dichiarato qui — mai indirizzi arbitrari, mai discovery automatica. Le
# header (es. ``Authorization``) possono contenere segreti e stanno in
# ``config.json`` con la stessa esposizione delle chiavi API dei provider
# (``chmod 600`` garantito da ``store.mutate``); ``repr=False`` le tiene fuori
# dai log, come ``password`` per SSH.

MCP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MCP_DEFAULT_TIMEOUT_S = 30
MCP_MIN_TIMEOUT_S = 1
MCP_MAX_TIMEOUT_S = 600


class MCPServerConfig(Base):
    """Un server MCP Streamable HTTP.

    ``name`` è l'unica cosa che il modello passa ai tool (prefisso
    ``mcp__<name>__<tool>``) e da lì viene la garanzia che conta: l'agente non
    può indovinare un endpoint, può solo nominare un server già dichiarato.
    ``headers`` vengono aggiunte a ogni richiesta JSON-RPC (auth Bearer, ecc.).
    """

    name: str = ""
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict, repr=False)
    enabled: bool = True
    timeout: int = Field(default=MCP_DEFAULT_TIMEOUT_S, ge=MCP_MIN_TIMEOUT_S, le=MCP_MAX_TIMEOUT_S)

    @model_validator(mode="before")
    @classmethod
    def _coerce_headers(cls, data: Any) -> Any:
        """Normalizza ``headers`` (e i campi scalari) da un file scritto a mano.

        Un valore non-dict per headers non deve far fallire l'intero gateway:
        si ricade su {}. I valori vengono forzati a str (un header deve essere
        testo). ``timeout`` viene forzato a int e riportato nei limiti del
        campo: un refuso come 999 non deve trasformare l'intera config in
        "recuperata dai default" — la route di settings valida già con 400, qui
        si copre la strada che la route non vede. Il controllo vero di
        ``name``/``url`` lo fa il validator di ``MCPConfig`` a monte, che
        scarta l'intero server se non validi.
        """
        if not isinstance(data, dict):
            return data
        headers = data.get("headers") or {}
        if not isinstance(headers, dict):
            logger.warning("MCP server headers must be a dict; ignoring")
            headers = {}
        headers = {str(k): str(v) for k, v in headers.items()}
        data = {**data, "headers": headers}
        for key in ("name", "url"):
            value = data.get(key)
            if value is not None and not isinstance(value, str):
                data = {**data, key: str(value)}
        if "enabled" in data and not isinstance(data["enabled"], bool):
            data = {**data, "enabled": bool(data["enabled"])}
        if "timeout" in data:
            raw_timeout = data["timeout"]
            try:
                timeout = int(raw_timeout)
            except (TypeError, ValueError):
                timeout = MCP_DEFAULT_TIMEOUT_S
            if timeout < MCP_MIN_TIMEOUT_S or timeout > MCP_MAX_TIMEOUT_S:
                logger.warning(
                    "MCP server timeout {!r} out of range; clamping to {}..{}",
                    raw_timeout, MCP_MIN_TIMEOUT_S, MCP_MAX_TIMEOUT_S,
                )
                timeout = max(MCP_MIN_TIMEOUT_S, min(MCP_MAX_TIMEOUT_S, timeout))
            if timeout != raw_timeout:
                data = {**data, "timeout": timeout}
        return data


class MCPConfig(Base):
    """Configurazione dei server MCP."""

    servers: list[MCPServerConfig] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _drop_invalid_servers(cls, data: Any) -> Any:
        """Scarta (con warning) i server non validi di un config scritto a mano.

        La route di settings valida già con 400 (name slug unico, URL http(s)
        passato al guard SSRF); qui si copre la strada che la route non vede —
        una ``config.json`` modificata a mano — senza far fallire l'avvio del
        gateway o il turno: un server senza nome o con URL non-http è un
        refuso, non un motivo per buttare giù tutto. Stesso pattern di
        ``AndroidWebSearchConfig._coerce_search_engine``.
        """
        if not isinstance(data, dict):
            return data
        raw_servers = data.get("servers") or []
        if not isinstance(raw_servers, list):
            return data
        kept: list[Any] = []
        for entry in raw_servers:
            if not isinstance(entry, dict):
                logger.warning("MCP server entry is not an object; skipping")
                continue
            name = entry.get("name")
            url = entry.get("url")
            valid_name = isinstance(name, str) and bool(MCP_NAME_RE.match(name.strip()))
            valid_url = isinstance(url, str) and url.strip().startswith(("http://", "https://"))
            if not valid_name or not valid_url:
                logger.warning(
                    "MCP server {!r} ({!r}) is invalid (name must be a slug, url must be http(s)); skipping",
                    name, url,
                )
                continue
            kept.append(entry)
        if len(kept) != len(raw_servers):
            data = {**data, "servers": kept}
        return data
