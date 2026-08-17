# Security Boundaries

The agent operates with significant power (file system, code execution, web). The following guards must not be bypassed when modifying related code.

## Workspace Restriction

Filesystem tools (`read_file`, `write_file`, `edit_file`, `list_dir`, `apply_patch`) resolve paths through the workspace path resolver (`agent/tools/filesystem.py` / `agent/tools/path_utils.py`), which enforces that the resolved path must lie under the active workspace when workspace restriction is enabled. The media upload directory is always an internal extra read root while restricted.

Additional filesystem roots must be capability-specific. `extra_allowed_dirs` is a legacy read-only alias. Use `extra_read_allowed_dirs` for read-only roots, `extra_write_allowed_dirs` only when a write-capable tool is intentionally allowed to modify an extra directory, and exact file allowlists when a tool may modify only specific files.

**Rule**: Any new path-handling logic must go through the workspace path resolver or perform an equivalent containment check with explicit read/write capability semantics.

## SSRF Protection

All outbound HTTP requests from agent tools must pass through `validate_url_target` (`security/network.py`). By default it blocks loopback, RFC1918 private addresses, CGNAT ranges, link-local ranges, and cloud metadata endpoints (including `169.254.169.254`).

The only escape hatch is `configure_ssrf_whitelist(cidrs)`, which reads from `config.tools.ssrf_whitelist` at load time.

**Rule**: Do not add direct `httpx.get` / `requests.get` calls in tools. Route through the existing web fetch utilities or replicate the `validate_url_target` check.

### Jenny Apps server SSRF policy (intentionally more permissive)

Jenny App `http` actions use a **distinct, deliberately more permissive** policy, `validate_app_server_target` (`security/network.py`), backed by `_APP_SERVER_BLOCKED_NETWORKS`. Unlike `validate_url_target`, it **allows RFC1918 private ranges** (`10/8`, `172.16/12`, `192.168/16`) and IPv6 ULA (`fc00::/7`) **by design**: an app server is a user-declared LAN device, reachable at a `server.baseUrl` that the user sees and approves in the manifest. Loopback (`127.0.0.0/8`, `::1`), link-local / cloud metadata (`169.254.0.0/16`), `0.0.0.0/8`, and CGNAT (`100.64.0.0/10`) remain blocked — so an app manifest cannot use the proxy as an authenticated bridge to the gateway's own API. Redirects are never followed, so a server cannot bounce the proxy to a blocked address.

As a related safeguard, `server.auth` is **fail-closed**: when an app declares it needs authentication but no credential store exists yet, the action is refused with a 501 rather than being sent unauthenticated (`apps/http.py`).

**Rule**: Keep the two policies separate. Widening the app-server allowlist further (or letting it follow redirects) requires re-evaluating the LAN-device threat model; do not route general agent web fetches through `validate_app_server_target`.

### SSH target policy (a third one, wider still)

`validate_ssh_target` (`security/network.py`), backed by `_SSH_BLOCKED_NETWORKS`, allows RFC1918, IPv6 ULA **and** CGNAT (`100.64.0.0/10`), blocking only `0.0.0.0/8`, loopback and link-local/metadata. CGNAT is allowed here rather than through `configure_ssrf_whitelist` on purpose: the whitelist is global, so opening it for Tailscale would also open CGNAT to `web_fetch` and to Jenny Apps — a narrow permission in one policy beats a wide one across all three. What backs the extra room is that an SSH host is user-typed in Settings and host-key pinned before any connection, not that SSH is inherently safer.

**Rule**: Loopback stays blocked in all four policies — it is the phone itself, and the gateway's own API lives there.

### MCP server policy (a fourth one, same shape as SSH)

`validate_mcp_target` (`security/network.py`), backed by the same `_SSH_BLOCKED_NETWORKS`, allows RFC1918, IPv6 ULA **and** CGNAT — an MCP server on a LAN device or over Tailscale is the normal use case, exactly like an SSH host. Blocked: `0.0.0.0/8`, loopback and link-local/metadata.

The loopback check runs **before** the SSRF whitelist, for the same reason as SSH: `ssrfWhitelist` must not be able to reopen the phone. The check runs twice on purpose — at save time in Settings (immediate error to the user) and again at discovery/connection time (covers a name that starts resolving to a blocked address later, DNS rebinding).

**Rule**: MCP targets are user-typed in Settings (never model-supplied — the agent can only call tools of servers the user declared), and the policy must stay as strict as SSH: no loopback, no link-local/metadata, no `0.0.0.0/8`.

## Telegram pairing oracle

The Telegram bot follows a **no-oracle rule** (`channels/telegram.py`): outside a pairing
window — no `pairing_code` set, or already paired — the bot NEVER replies to non-owner
chats, so it does not reveal that it exists or its pairing state.

**Accepted trade-off** for onboarding: while a `pairing_code` is active (from token save
until a successful pairing), the bot answers service replies (`/start` prompt, wrong-code
feedback) up to `_MAX_PAIR_ATTEMPTS` per chat, with the attempt table bounded fail-closed
at `_MAX_TRACKED_CHATS` (no eviction). A chat at/over the cap — or a new chat when the
table is full — becomes **ineligible to pair even with the correct code**: the cap is a
brute-force defence on the 6-digit code (total guess budget ≈ cap × bound out of 10^6 per
channel lifetime), not just a reply throttle.

Known limits, accepted by design: the pairing window is not time-bounded (a code persists
after unpair until re-paired), and in-memory counters reset on channel reload/gateway
restart with the same persisted code — the reload paths that matter (token save, unpair)
regenerate the code anyway. Owner lockout recovers via WebUI "Unpair"/"Change token".

**Rule**: any new reply on the unpaired path MUST go through the attempt counter and MUST
NOT fire when `pairing_code` is unset. Never reply to non-owner chats once paired.

## Code Execution

`PythonExecTool` (`agent/tools/python_exec.py`) is the current execution surface. It runs arbitrary Python **in-process** on the single Chaquopy interpreter (in the executor threadpool / dedicated session threads) — **not** in a subprocess, and it is **not a security sandbox**. This matches the honest trust-boundary docstring in `python_exec.py`. The real containment comes from three layers outside the interpreter:

- the **Android app sandbox** (the app's own uid / permissions);
- the **workspace path policy** for filesystem writes — now enforced for the builtin `open` / `io.open` / `pathlib` I/O paths too, not just the registered helpers and `os.open` (`security/workspace_policy.py`);
- the **SSRF policy** for outbound network.

The module allow/block lists are a **usability guardrail** (they stop the model from accidentally reaching for e.g. `subprocess`), **not** a containment control. In particular, `httpx` is **no longer in the default allowlist** (`config/tool_schemas.py`): outbound network is available only through the `http_get` / `http_post` builtins, which validate targets via the SSRF policy. Raw `httpx` can be re-added explicitly in config, accepting the risk.

Deployments that do not trust the model must disable the tool via `tools.python_exec.enable = false` — that is the real answer to "no sandbox", not in-process hardening.

**Rule**: Do not describe `python_exec` as a sandbox, and do not introduce shell execution or command wrappers. Any new path-handling or network path added to the execution surface must go through the workspace path policy and the SSRF policy, since those layers (not the interpreter) are the containment boundary.
