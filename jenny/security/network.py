"""Network security utilities — SSRF protection and internal URL detection."""

from __future__ import annotations

import ipaddress
import re
import socket
from contextlib import suppress
from urllib.parse import urlparse

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),   # carrier-grade NAT
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),          # unique local
    ipaddress.ip_network("fe80::/10"),         # link-local v6
]

# Blocklist for Jenny App `http` actions: app servers are user-declared LAN
# devices, so private ranges (RFC1918, IPv6 ULA) are allowed. Loopback stays
# blocked so an app manifest can't use the proxy as an authenticated bridge to
# the gateway's own API; link-local/metadata and CGNAT stay blocked too
# (CGNAT is exemptable via the existing ssrf whitelist, e.g. for Tailscale).
_APP_SERVER_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),   # carrier-grade NAT
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / cloud metadata
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),         # link-local v6
]

# Blocklist for SSH targets. Come quella degli app server, ma senza CGNAT:
# 100.64.0.0/10 è la rete che Tailscale assegna ai propri nodi, ed è il modo
# normale di raggiungere il proprio server da un telefono in 4G. Bloccarla qui
# lasciava una sola via d'uscita — mettere 100.64.0.0/10 nella ssrf_whitelist —
# che però è globale: per aprire l'SSH verso Tailscale si sarebbe aperto il
# CGNAT anche a `web_fetch` e alle Jenny App, cioè proprio ai target che sceglie
# il modello. Meglio un permesso stretto qui che uno largo altrove.
#
# Quel che il CGNAT proteggeva resta coperto da vincoli più forti: un host SSH
# lo dichiara l'utente in Settings, e la sua host key va accettata a mano prima
# che parta una connessione. Loopback e link-local restano bloccati — quelli
# puntano al telefono stesso, non a un server dell'utente.
_SSH_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / cloud metadata
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),         # link-local v6
]

_URL_RE = re.compile(r"https?://[^\s\"'`;|<>]+", re.IGNORECASE)

_allowed_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []


def configure_ssrf_whitelist(cidrs: list[str]) -> None:
    """Allow specific CIDR ranges to bypass SSRF blocking (e.g. Tailscale's 100.64.0.0/10)."""
    global _allowed_networks
    nets = []
    for cidr in cidrs:
        with suppress(ValueError):
            nets.append(ipaddress.ip_network(cidr, strict=False))
    _allowed_networks = nets


def _normalize_addr(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Normalize IPv6-mapped IPv4 addresses to their IPv4 form.

    ``::ffff:127.0.0.1`` is semantically identical to ``127.0.0.1`` but
    Python's ipaddress treats it as an IPv6Address that matches neither
    ``127.0.0.0/8`` nor ``::1/128``.  Converting it to IPv4 ensures
    blocklist/allowlist checks work correctly.
    """
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _is_blocked(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
    blocked_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    normalized = _normalize_addr(addr)
    if _allowed_networks and any(normalized in net for net in _allowed_networks):
        return False
    return any(normalized in net for net in blocked_networks)


def _validate_target(
    url: str,
    blocked_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
    *,
    allow_loopback: bool = False,
) -> tuple[bool, str]:
    try:
        p = urlparse(url)
    except Exception as e:
        return False, str(e)

    if p.scheme not in ("http", "https"):
        return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
    if not p.netloc:
        return False, "Missing domain"

    hostname = p.hostname
    if not hostname:
        return False, "Missing hostname"

    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return False, f"Cannot resolve hostname: {hostname}"

    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        addrs.append(addr)
    if allow_loopback and _is_allowed_loopback_target(hostname, addrs):
        return True, ""
    for addr in addrs:
        if _is_blocked(addr, blocked_networks):
            return False, f"Blocked: {hostname} resolves to private/internal address {addr}"

    return True, ""


def validate_url_target(url: str, *, allow_loopback: bool = False) -> tuple[bool, str]:
    """Validate a URL is safe to fetch: scheme, hostname, and resolved IPs.

    ``allow_loopback`` is intentionally narrow: it only permits literal
    loopback hosts (localhost, 127.0.0.0/8, ::1) when every resolved address is
    loopback. It does not allow RFC1918, link-local, metadata, or public DNS
    names that happen to resolve to loopback.

    Returns (ok, error_message).  When ok is True, error_message is empty.
    """
    return _validate_target(url, _BLOCKED_NETWORKS, allow_loopback=allow_loopback)


def validate_app_server_target(url: str) -> tuple[bool, str]:
    """Validate a Jenny App server URL (``server.baseUrl`` targets only).

    Unlike :func:`validate_url_target`, RFC1918 and IPv6 ULA ranges are
    allowed — app servers are user-declared LAN devices. Loopback, link-local
    metadata, 0.0.0.0/8 and CGNAT stay blocked (see
    ``_APP_SERVER_BLOCKED_NETWORKS``).

    Returns (ok, error_message).  When ok is True, error_message is empty.
    """
    return _validate_target(url, _APP_SERVER_BLOCKED_NETWORKS)


def validate_ssh_target(host: str) -> tuple[bool, str]:
    """Validate an SSH target host (declared by the user, never model-supplied).

    RFC1918, IPv6 ULA and CGNAT are allowed: a home server reached over the LAN
    or over Tailscale is the main use case, and both are named by the user in
    Settings and host-key pinned before a single byte is sent. Loopback stays
    blocked — the agent must not be able to SSH into the phone itself, nor use
    the SSH tool as a bridge back to the gateway's own API — and so do
    link-local/metadata and 0.0.0.0/8 (see ``_SSH_BLOCKED_NETWORKS``).

    The loopback check is deliberately made **before** the blocklist, because
    the blocklist yields to ``security.ssrfWhitelist`` and this must not: a
    range opened for ``web_fetch`` would otherwise open the phone to SSH too.

    Unlike the URL validators this takes a bare hostname: SSH has no scheme to
    parse, so ``_validate_target`` (which requires http/https) does not apply.

    Called twice on purpose: once when the host is saved in Settings, and again
    at connection time so a name that later starts resolving to a blocked
    address (DNS rebinding) is caught.

    Returns (ok, error_message).  When ok is True, error_message is empty.
    """
    hostname = (host or "").strip().rstrip(".")
    if not hostname:
        return False, "Missing host"

    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return False, f"Cannot resolve hostname: {hostname}"

    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        try:
            addrs.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not addrs:
        return False, f"Cannot resolve hostname: {hostname}"

    for addr in addrs:
        # Il pavimento, controllato PRIMA della whitelist. ``_is_blocked``
        # consulta ``_allowed_networks`` per primo, quindi chi apre una fascia
        # per ``web_fetch`` la aprirebbe anche qui: bastava mettere in whitelist
        # un intervallo che copre 127.0.0.0/8 perche l'agente potesse aprire una
        # sessione SSH verso il telefono stesso, e con essa raggiungere l'API del
        # gateway dall'interno. Il loopback e la sola cosa che questa policy
        # promette senza condizioni: qui non si negozia.
        if _normalize_addr(addr).is_loopback:
            return False, f"Blocked: {hostname} resolves to the phone itself ({addr})"
        if _is_blocked(addr, _SSH_BLOCKED_NETWORKS):
            return False, f"Blocked: {hostname} resolves to {addr}"

    return True, ""


def validate_mcp_target(url: str) -> tuple[bool, str]:
    """Validate an MCP server URL (declared by the user, never model-supplied).

    I server MCP li dichiara l'utente in Settings → Tools, come gli host SSH:
    sono target scelti da un umano, quindi RFC1918, ULA e CGNAT sono ammessi
    (un server MCP sul proprio NAS o via Tailscale è il caso d'uso normale).
    Loopback resta bloccato — l'agente non deve poter aprire una sessione MCP
    verso il telefono stesso né usare il server dichiarato come ponte verso
    l'API del gateway — e così link-local/metadata e 0.0.0.0/8 (stesso set di
    ``_SSH_BLOCKED_NETWORKS``).

    Il controllo loopback è volutamente PRIMA della whitelist: la
    ``ssrfWhitelist`` non deve poter riaprire il telefono, per le stesse
    ragioni di ``validate_ssh_target``.

    Chiamata due volte di proposito: al salvataggio in Settings (errore subito
    all'utente) e alla discovery/connessione (copre il nome che comincia a
    risolvere a un indirizzo vietato più tardi, DNS rebinding).

    Returns (ok, error_message).  When ok is True, error_message is empty.
    """
    try:
        p = urlparse(url)
    except Exception as e:
        return False, str(e)
    if p.scheme not in ("http", "https"):
        return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
    hostname = (p.hostname or "").strip().rstrip(".")
    if not hostname:
        return False, "Missing hostname"

    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return False, f"Cannot resolve hostname: {hostname}"

    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        try:
            addrs.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not addrs:
        return False, f"Cannot resolve hostname: {hostname}"

    for addr in addrs:
        if _normalize_addr(addr).is_loopback:
            return False, f"Blocked: {hostname} resolves to the phone itself ({addr})"
        if _is_blocked(addr, _SSH_BLOCKED_NETWORKS):
            return False, f"Blocked: {hostname} resolves to {addr}"

    return True, ""


def _is_allowed_loopback_target(
    hostname: str,
    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address],
) -> bool:
    if not addrs or not all(_normalize_addr(addr).is_loopback for addr in addrs):
        return False
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost":
        return True
    with suppress(ValueError):
        return ipaddress.ip_address(hostname).is_loopback
    return False
