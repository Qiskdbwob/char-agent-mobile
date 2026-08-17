# Security model

Jenny's containment is four independent layers stacked on top of each other, and the outermost one — the Android app sandbox — is the only one that is a real, OS-enforced boundary; the other three are application-level guards that a sufficiently motivated adversary (or a misbehaving model) can defeat from inside the app's own process.

## The four levels

### Level 1 — Android app sandbox (the real fence)

This is the only level enforced by the operating system, not by Jenny's own code. Jenny runs as an ordinary Android app under its own UID, in its own private storage (`<filesDir>/workspace`), with no shared storage permission and no way to touch another app's data.

Concretely, this means the agent:

- **cannot** read or write any other app's files or data,
- **cannot** access the camera directly (photo capture is delegated to the system camera app via an intent; Jenny never holds the `CAMERA` permission),
- **cannot** read contacts, SMS, or call logs (no permission is ever requested for them),
- **cannot** access shared/external storage except through user-initiated actions (`share`, `save to Downloads`) that go through Android's own share sheet or Storage Access Framework.

Location and notifications *are* requested at runtime (`ACCESS_FINE_LOCATION`/`ACCESS_COARSE_LOCATION`, `POST_NOTIFICATIONS`), but both are optional, user-facing toggles — see [Location](../using/location.md).

Everything below this line is Jenny's own code running *inside* that sandbox. None of it can widen the sandbox; it can only narrow what the agent is allowed to do within it.

### Level 2 — Workspace policy (fail-closed)

File tools (`read_file`, `write_file`, `edit_file`, `list_dir`, `find_files`, `grep`, `apply_patch`) are constrained to the workspace directory when `security.restrictToWorkspace` is `true` (the default). The enforcement point is `resolve_allowed_path()` in `jenny/security/workspace_policy.py`, and it is deliberately **fail-closed**: if a caller doesn't pass an explicit allowed root or file allowlist, the path is rejected outright rather than defaulting to permissive. Symlinks are resolved before the containment check, so a symlink pointing outside the workspace does not bypass the boundary.

When a path falls outside the boundary, the tool raises an error whose message says, verbatim:

> *(this is a hard policy boundary, not a transient failure; do not retry with alternative tools, and ask the user how to proceed if the resource is genuinely required)*

That phrasing is intentional — it's written to stop the model from treating a workspace-boundary rejection as a fluke worth retrying with a different tool.

That roster of seven is the full set across all agents; no single agent holds it. With `agents.defaults.orchestratorMode` at its default of `true`, the agent you talk to gets `read_file`, `list_dir`, `grep` and the web tools (`web_search`, `web_fetch`, `browser_*`) — and its `grep` is an **index**: it returns matching file paths or per-file match counts, never the matching lines, capped at 60 files. `find_files` is not in the orchestrator's scope at all. Everything that writes goes to a subagent. This is a context-budget decision rather than a security one — a subagent's tool output doesn't stay in your conversation — but it does mean the write path and the boundary above apply to a different agent than the one you're typing to.

One deliberate exception: Jenny's own Python source (`jenny/`) is exposed **read-only** to the agent when `tools.file.exposePackageSource` is `true` (default), so it can inspect the framework it runs on. It is never writable through this path.

The boundary also works in the other direction, to keep things *out*. The SSH private keys and `known_hosts` live in `<filesDir>/ssh`, **beside** the workspace rather than inside it, precisely so that no file tool can reach them — unlike `config.json`, which sits in the workspace and which the agent can therefore already read. The cost is paid by the user, not by the model: snapshots and encrypted backups only walk the workspace root, so a restore brings back the host list without the keys (see [SSH access](../using/ssh.md)).

**That protection is exactly as conditional as this level is.** Living outside the workspace only helps while `security.restrictToWorkspace` is `true`, which is what makes the path policy reject anything above the workspace root in the first place. Turn it off and `<filesDir>/ssh` becomes an ordinary readable directory: `read_file` can print the private key, and `python_exec`'s file helpers can too. Nothing else in the SSH design compensates for that — the key is protected by a path check, not by encryption or by an OS boundary. If you turn `restrictToWorkspace` off, assume the SSH private key is readable by the model.

Note the framing: this is an **application-level guard**, not a sandbox. If `restrict_to_workspace` is turned off, the agent is *not* granted access to the rest of the phone — Android's own sandbox (Level 1) still applies — but it loses the extra safety net that keeps it inside `workspace/` even when it shouldn't need to leave it.

### Level 3 — Network / SSRF filter

Outbound network tools (`web_fetch`, `download_file`, and the `http_get`/`http_post` helpers available inside `python_exec`) validate every target address before connecting. Requests to private, loopback, link-local, and carrier-grade-NAT ranges are blocked by default:

| Blocked range | Why |
|---|---|
| `0.0.0.0/8` | unspecified |
| `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` | RFC1918 private |
| `100.64.0.0/10` | carrier-grade NAT |
| `127.0.0.0/8`, `::1/128` | loopback |
| `169.254.0.0/16`, `fe80::/10` | link-local (includes cloud metadata endpoints) |
| `fc00::/7` | IPv6 unique local |

`security.ssrfWhitelist` (default `[]`) lists CIDR ranges that are exempted from this block — the documented use case is a Tailscale range like `100.64.0.0/10` so the agent's tools can reach a self-hosted service over your own VPN.

**This filter does not cover calls to your configured LLM provider.** Provider requests (the actual chat completions) go out through the HTTP client used by the provider integration, not through the tool-layer SSRF check. If you point a provider's `apiBase` at a LAN or VPN address, that call is not subject to the SSRF whitelist at all — reachability and the HTTPS-outside-localhost constraint (below) are what actually gate it. See [Local models](../reference/local-models.md).

Jenny Apps get a separate, slightly looser blocklist for their own outbound `http` actions (private/RFC1918 ranges are allowed there, since app servers are expected to be LAN devices the user pointed the app at — loopback, link-local, and CGNAT stay blocked so an app manifest can't use the proxy as a back door into the gateway's own API).

**SSH targets get a third policy, looser still**: RFC1918, IPv6 ULA *and* the CGNAT range `100.64.0.0/10` are allowed, because reaching a home server over Tailscale from a phone on mobile data is the case the feature exists for, and the alternative — listing that range in `security.ssrfWhitelist` — is global, so it would have opened CGNAT to `web_fetch` and to Jenny Apps as well, that is, to the targets the model chooses. What justifies the extra room is not that SSH is safer but that its targets are named differently: a person types the host into Settings and accepts its fingerprint by hand before anything is sent. Loopback and link-local/metadata stay blocked in every policy — those resolve to the phone itself, so the agent cannot SSH into its own device or use an SSH session as a bridge back to the gateway's own API. The check runs twice — once when the host is saved in Settings, and again at connection time, so a hostname that only later starts resolving to a blocked address (DNS rebinding) is still caught.

### Level 4 — `python_exec` guardrails (explicitly not a sandbox)

`python_exec` runs arbitrary Python **in-process**, on the same Chaquopy interpreter as the rest of the gateway. The source file itself states this bluntly, and this documentation states it just as bluntly: **it is not a security sandbox.**

The allow/block module lists (`os`, `sys`, `pathlib`, `shutil`, and a few dozen others are allowed; `subprocess`, `ctypes`, `socket`, `pty`, and similar are blocked) are a *usability guardrail* — they stop the agent from casually reaching for `subprocess` — not a containment control. With `os`/`sys`/`shutil` allowed, there is no in-process boundary that resists a motivated adversary: guarded code can still reach arbitrary modules through `sys.modules` or `os` internals. `httpx` and `urllib` are deliberately excluded from the allowlist specifically so that raw HTTP clients can't bypass the SSRF and workspace policies — outbound HTTP from `python_exec` is only available through the `http_get`/`http_post` helpers, which do go through `validate_url_target()`.

What actually contains `python_exec`, in order, is:

1. the Android app sandbox (Level 1) — the code runs as this app's UID, nothing more,
2. the workspace path policy (Level 2), to the extent guarded code goes through Jenny's own file helpers,
3. the SSRF policy (Level 3), to the extent guarded code goes through Jenny's own HTTP helpers.

If code inside `python_exec` calls `os` or `shutil` directly, it can do anything the app's own UID can do on disk — which in practice is still confined to the app's private storage, because that's all the UID has access to.

Defaults: `tools.pythonExec.enable` = `true`, `timeout` = 60 seconds (`0` = no limit), `maxOutputChars` = 10,000.

**If you don't trust the model to run arbitrary code responsibly, the only real mitigation is to turn the tool off:** set `tools.pythonExec.enable` to `false` in `workspace/config.json`. There is no "sandboxed mode" to fall back to.

## Remote machines: a fifth boundary that isn't a level

SSH is the only capability that acts outside the phone, so the four-level stack above doesn't describe it — nothing Android enforces protects a server on the other side of the network. What contains it instead is four independent gates, none of which the model can open:

**1. Targeting is by alias.** Every SSH tool takes a `host` argument that must be the alias of a machine a person registered in Settings → SSH. There is no parameter for an address, a port, a username or a credential anywhere in the four tool schemas, so no prompt injection can redirect the agent at a machine the user never declared. Aliases are resolved against live config on every call, and the address behind one is re-validated against the network policy each time.

**2. Host keys are pinned, with no trust-on-first-use.** A connection to a host whose key has not been accepted by a human is refused outright, and the error tells the model to ask the user rather than retry. The enforcement is the `known_hosts` file next to the private key — the fingerprint stored in `config.json` is for display only. A registered host presenting a *different* key raises rather than overwriting: it is a possible man-in-the-middle, and the only acceptable response is a person looking at both fingerprints and deciding, which is a second explicit confirmation in the UI.

This gate is unconditional in both authentication modes, and password authentication is the case that needs it most. A key presented to an impostor costs a signature the impostor cannot reuse; a password presented to an impostor *is* the credential. The pinned fingerprint is what decides which machine receives it, so the settings UI states that in the acceptance dialog itself rather than leaving it to the docs.

**3. The credential is unreachable from the agent — completely for a key, partially for a password.** The key is generated on-device (ed25519, one pair per alias), the private half is never returned by any API — the settings payload carries a boolean, not the key — and it lives outside the workspace, so — **while `security.restrictToWorkspace` is `true`, the default** — no file tool and no `python_exec` file helper can read it. Turning that setting off removes this gate and nothing replaces it (see Level 2). Nothing in the tool layer needs the key material; the backend opens the file by a path derived from the alias, never from a configurable field.

A password gets the same treatment at every layer the tools touch — never in a tool argument, never in a tool result, never in a settings payload (a `has_password` boolean stands in for it, and there is no masked-hint variant of the kind `_mask_api_key` produces for provider keys, because four real characters of a password are four characters given away), kept out of `repr()` so it can't fall into a log line, and named `password` on the wire so `redact_query_secrets` masks it in the request-path log. What it does **not** get is the fourth layer: it is stored in clear text in `config.json`, which sits *inside* the workspace and which the agent's file tools can already read — the same exposure as `telegram.botToken` and the provider API keys. That is the whole reason the SSH private key was put outside the workspace in the first place, so the honest statement is that password authentication trades this specific protection for convenience. `auth` defaults to `"key"`, and Settings refuses to save a password host with an empty password rather than leaving a half-configured host that only fails mid-turn.

**4. The capability is compartmentalized.** The four SSH tools live in a tool scope of their own (`remote`) that no agent loads by default. The main agent — the one you talk to — has no SSH at all: it delegates to a **`sysadmin` subagent**, the only type that requests that scope, and that type has neither the web tools nor `download_file` nor `python_exec`. This is the same rule the researcher/coder split follows, applied to a shorter and worse chain: whoever reads untrusted pages must not be whoever holds a shell on a production machine. Keeping the SSH tools out of the `subagent` scope is what stops the catch-all `operator` type — defined as "everything in that scope" — from inheriting a remote shell by accident.

Two things this does **not** protect against, stated plainly: a command the agent runs on the server has whatever rights the account you gave it has, and Jenny's snapshots do not cover a remote machine. There is no undo on the other end.

## What the agent can and cannot do on the phone

**Can:** (read this list as "Jenny, as a whole" — the agent you talk to is an **orchestrator** and does several of these only by delegating)

- Read files inside `workspace/` (and read Jenny's own source, read-only), and locate them with an index-only `grep`.
- Write, edit, and patch files inside `workspace/` — but with `agents.defaults.orchestratorMode` at its default of `true`, only through a subagent; the main agent has no write tool at all.
- Download files from the web into `workspace/downloads/` — only through a subagent, for the same reason.
- Search the web, fetch/read pages and drive the interactive browser (through the hidden WebView — see [Tool reference](../reference/tools.md)) — directly, with the same tools a `researcher` subagent gets; fetched content is treated as untrusted data, never as instructions.
- Send you notifications and schedule reminders (`cron`).
- Read your last-known device location, or request a fresh GPS fix, if the location toggle and the Android permission are both granted.
- Send and receive messages over Telegram, if you've paired a bot.
- Run Python code in-process (unless you disable `python_exec`) — only through a subagent, typically a `coder` or an `analyst`.
- Spawn subagents, steer and stop them, and run long-running background tasks.
- Run commands on a remote machine over SSH — but only on an alias you registered, only once you have accepted its host key (in both authentication modes), only through a `sysadmin` subagent, and only if you enabled `tools.ssh` (off by default).

**Cannot:**
- Read or write any other app's data — the Android sandbox stops this regardless of any Jenny-level toggle.
- Use the camera directly, or read contacts, SMS, or call logs — these permissions are never requested.
- Reach private/loopback/link-local network addresses with its web tools, unless you've added them to `security.ssrfWhitelist`.
- Meaningfully resist a compromised or adversarial model once inside `python_exec` — that boundary is the Android sandbox, not Jenny's own guardrails.
- Escape the workspace boundary through file tools when `security.restrictToWorkspace` is `true` — the fail-closed policy rejects paths outside it rather than silently widening scope.
- SSH to a machine you didn't register, or accept a host key on your behalf — the alias is the only target it can name.
- Read its own SSH private key, **while `security.restrictToWorkspace` is `true`** (the default): the key lives outside every path its tools can then resolve. Turn that setting off and `read_file` reaches it like any other file. (A host configured with `auth: "password"` is a gap that exists either way: the password is in `config.json`, inside the workspace, so the file tools can read it just as they can read the Telegram token and the API keys.)

## Signed media URLs

Media previews served by the WebUI (images, attachments) use URLs of the form `/api/media/<signature>/<path>`, where `<signature>` is an HMAC-SHA256 digest (truncated to 16 bytes) over the path, keyed by a per-install secret. This means another app on the phone can't guess a valid media URL for a file it doesn't already have a link to.

Two things worth knowing:

- **The signature never expires.** Responses are served with `Cache-Control: private, max-age=31536000, immutable` — a year. Anyone who obtains a signed URL (through a screenshot, a shared link, a proxy log) can reuse it for as long as the underlying file exists and the signing secret hasn't changed. There's no revocation or expiry mechanism.
- Path traversal outside the media root returns a plain 404, and an invalid signature returns 401 — the check is namespaced to the media directory, so a valid signature for one file can't be replayed against an arbitrary path.

## WebUI authentication

Every WebUI API call and the WebSocket handshake require a per-install secret (`websocket.tokenIssueSecret`, generated once at first boot and stored in `config.json` with `chmod 600`). It's checked as an `Authorization: Bearer <secret>` or `X-Jenny-Auth: <secret>` header on HTTP requests, and as a `?token=` query parameter on the WebSocket handshake.

Android hands this secret to the WebView as a **URL fragment** (`#bs=<secret>`), never as a query parameter — fragments aren't sent to the server and aren't logged the way query strings can be. The WebView's JavaScript reads the fragment locally and exchanges it once, over `/webui/bootstrap`, for the actual WebSocket URL.

Operations that carry content — saving a workspace file, closing an audit with a note — run as
**commands over the WebSocket** (`rpc` frames, see the [WebSocket protocol](../reference/websocket.md#commands-rpc)),
because the HTTP surface cannot carry a request body at all. Their authorization is the
handshake's verdict, recorded per connection: with a secret configured, only a connection that
presented it may run a command, even when `websocket_requires_token` is off. Without that rule
a file write would sit behind a weaker gate than an HTTP call, which fails closed when no
secret is set.

The gateway listens on `127.0.0.1:18790` by default (WebSocket and HTTP share the same host/port). If you were to reconfigure `websocket.host` to `0.0.0.0` (all interfaces) without setting `tokenIssueSecret`, the config itself refuses to validate — this is rejected before the gateway can even start, specifically to prevent an unauthenticated gateway from being exposed to the rest of your network.

## Related pages

- [Privacy](./privacy.md) — what leaves the device and when.
- [SSH access](../using/ssh.md) — setting up a remote host, and why a restore does not restore access.
- [Configuration reference](../reference/configuration.md) — the `security.*` and `tools.*` keys in full.
- [Tool reference](../reference/tools.md) — per-tool limits and behavior.
- [Local models](../reference/local-models.md) — the HTTPS-outside-localhost constraint for self-hosted providers.
