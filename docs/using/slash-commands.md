# Slash commands

Typing a message that starts with `/` in the chat input can trigger a built-in command instead of a normal turn — here is the full list, exactly what each one prints, and the one client-side command that isn't in that list at all.

## How commands work

Commands are matched on the whole message (case-insensitive), either as an exact word (`/new`) or as a prefix followed by an argument (`/model fast`). If what you type doesn't match any known command, it is **not rejected** — it's forwarded to the agent as an ordinary chat message, exactly as if it had no leading slash.

The WebUI has a **command palette**: typing `/` in the message box opens a small list of the slash commands that have no dedicated button or tab of their own (`/help`, `/status`, `/goal`, `/dream`, `/atlas`). Keep typing to filter by prefix; navigate with the arrow keys and confirm with Enter, or just tap an entry. `/stop`, `/new`, `/model`, `/skill` and `/history` are deliberately left out of the palette because they already have their own controls — the Stop button, the new-chat button, and the Model/Skills/transcript surfaces. Commands with an argument (`/goal`) insert their prefix into the composer for you to finish; the rest send immediately.

Two commands — `/stop` and `/status` — are handled on a "priority" fast path that runs even while a turn is actively streaming or a tool is executing. The rest wait for the current dispatch to be free, which in practice is rarely noticeable.

All server-side command responses below are **hardcoded in English**, regardless of whether the WebUI is set to Italian or English. This is true for the confirmation text, the usage/error messages, and the `/status`/`/model` output.

## The 10 server commands

| Command | Arguments | What it does |
|---|---|---|
| `/new` | none | Stops the active task (if any) and clears the model's context for a fresh conversation |
| `/stop` | none | Cancels the active agent turn for this chat |
| `/status` | none | Shows a runtime snapshot: version, model, token usage, context budget, session size, uptime, active tasks |
| `/model` | `[preset]` | Shows the current model/preset, or switches the active preset |
| `/history` | `[n]` | Prints the last `n` persisted user/assistant messages (default 10, max 50) |
| `/goal` | `<description>` | Tells the agent to treat the request as a long-running goal |
| `/dream` | none | Manually triggers a memory consolidation (Dream) run in the background |
| `/atlas` | `[force]` | Rebuilds the wiki directory (`memory/WIKI.md`) from your wikis, in the background |
| `/skill` | none | Lists the currently enabled skills with their descriptions |
| `/help` | none | Lists all of the above (not `/clear` — see below) |

Full details and exact output text for each command follow.

### `/new` — start a fresh conversation

Cancels any active task first, then clears the model's context (the LLM stops remembering everything before this point) and archives the discarded messages into long-term memory for later Dream processing. The response is rendered as a separator line in the chat, not a bubble:

```text
New session started.
```

**This does not erase anything from the screen.** Everything above the separator stays fully visible and scrollable — see [`/new` vs `/clear`](#new-vs-clear-the-crucial-distinction) below for why that matters.

### `/stop` — cancel the active turn

The chat shows a **Stop** button (a filled square next to the send button) while a turn is streaming; tapping it sends `/stop` for you. This command is also the way to interrupt a turn from Telegram, and it works specifically because it's on the priority fast path (it's processed even mid-turn, before the normal dispatch lock). Response:

```text
Stopped N task(s).
```

or, if nothing was running:

```text
No active task to stop.
```

`/stop` also cancels an active `/goal` and discards any subagent working in the background for this chat — a subagent that finishes after being stopped has its result silently thrown away.

### `/status` — runtime snapshot

No arguments. Output is a fixed-format block (rendered as plain text, not markdown), for example:

```text
🐈 jenny v0.6.6
🧠 Model: gpt-4o
📊 Tokens: 1234 in / 567 out (40% cached)
📚 Context: 12k/65k (22% of input budget)
💬 Session: 48 messages
⏱ Uptime: 2h 14m
⚡ Tasks: 0 active
```

The cached-percentage part of the token line only appears when the last turn actually used cached tokens. "% of input budget" is the context estimate divided by (context window − max output tokens − a small safety margin), not divided by the raw context window. `/status` and `/stop` are the two commands that work even while a turn is running.

### `/model [preset]` — show or switch model preset

Without an argument, shows the current state:

```text
## Model
- Current model: `gpt-4o`
- Current preset: `default`
- Available presets: `default`, `fast`, `deep`
```

`default` is always available and reflects the plain `agents.defaults.*` model fields; named presets come from `modelPresets` in `config.json` — there is no UI for creating presets, they exist only in the config file. See [Configuration reference](../reference/configuration.md#modelpresets).

With one argument, it switches presets for future turns and confirms:

```text
Switched model preset to `fast`.
- Model: `gpt-4o-mini`
- Context window: 65536
- Max output tokens: 4096
```

If the name doesn't match a configured preset:

```text
Could not switch model preset: <error detail>

Available presets: `default`, `fast`, `deep`
```

If you pass more than one word:

```text
Usage: `/model [preset]`
```

Switching is **runtime-only**: it does not rewrite `config.json`, and a turn that is already in progress keeps using the model it started with.

### `/history [n]` — print recent messages

Shows the last `n` persisted user/assistant messages from the current session. Default `n` is 10, maximum is 50 (a value above 50 is silently capped, not rejected). Each message is truncated to 200 characters with a trailing `…`.

```text
Last 10 message(s):
👤 You: what's the weather like today?
🤖 Bot: It's sunny and 22°C where you are right now.
```

If there's nothing to show:

```text
No conversation history yet.
```

If the argument isn't a number:

```text
Usage: /history [count] — e.g. /history 5 (default: 10, max: 50)
```

This reads the persisted session history, not the on-screen transcript — see [`/new` vs `/clear`](#new-vs-clear-the-crucial-distinction) for the difference.

### `/goal <description>` — start a long-running goal

Rewrites your message into a normal agent turn that instructs the model to register a sustained objective (via the internal `long_task` tool) instead of answering as a one-shot request. There's no separate "goal view" — it's the same chat, with normal turns, but the objective stays pinned in the agent's context (so it survives compaction) and the per-turn timeout is disabled until it's done. A banner with a running timer appears while a turn is in progress.

Without a description:

```text
Usage: /goal <long-running task description>
```

If a task is already running in this chat:

```text
A task is already running for this chat. Use `/stop` first, then send `/goal <long-running task description>` again.
```

Only one goal can be active per chat at a time. The agent closes it itself (or you can cancel with `/stop`); an inactive goal also expires automatically after 12 hours.

### `/dream` — run memory consolidation now

Triggers Dream (the long-term memory consolidation job) in the background. It replies immediately:

```text
Dreaming...
```

and later, once the run finishes, with a separate message giving the outcome — one of:

```text
Dream completed in 4.2s.
```
```text
Dream completed in 4.2s but wrote nothing (attempts blocked/refused); memory cursor was not advanced.
```
```text
Dream did not complete after 4.2s; memory cursor was not advanced.
```
```text
Dream failed after 4.2s: <error>
```

If there's no new history to process yet (common on a fresh or short chat, since Dream only reads from `memory/history.jsonl`, which is only populated after compaction), you get a longer explanation instead, ending with suggestions like enabling `idleCompactAfterMinutes`. See [Memory, Dream and Atlas](./memory.md) for the full model.

### `/atlas` — rebuild the wiki directory now

Triggers Atlas, the job that compiles your wikis into `memory/WIKI.md`. Like `/dream` it acknowledges immediately:

```text
Mapping the wiki...
```

and follows up with the outcome. The interesting cases are the ones where it deliberately does nothing:

```text
Atlas updated `memory/WIKI.md` in 6.4s.
```
```text
The wiki hasn't changed since the last Atlas run, so `memory/WIKI.md` is already current — no tokens spent. Use `/atlas force` to rebuild it anyway.
```
```text
Atlas found no wikis to map.
```

`/atlas force` skips the change check and rebuilds regardless. It does not skip the "do you have any wikis" check — with no wikis there is nothing to compile. See [Atlas](./memory.md#atlas-the-wiki-side-of-memory).

### `/skill` — list enabled skills

```text
Available skills (3):

- **weather** — Look up current weather and forecasts.
- **app-creator** — Guide the user through building a new Jenny App.
- **llm-wiki** — Maintain the workspace wiki (scaffold, ingest, compile).
```

or, if none are enabled:

```text
No skills available.
```

### `/help` — list available commands

```text
✿ jenny commands:
/new — Stop the current task and start a fresh conversation.
/stop — Cancel the active agent turn for this chat.
/status — Display runtime, provider, and channel status.
/model [preset] — Show or switch the active model preset.
/history [n] — Print the last N persisted conversation messages.
/goal <goal> — Tell the agent to treat the request as a long-running goal.
/dream — Manually trigger memory consolidation.
/atlas [force] — Rebuild the wiki directory in memory/WIKI.md. Add 'force' to skip the change check.
/skill — List enabled skills and their descriptions.
/help — List available slash commands.
```

`/clear` is deliberately **not** in this list — see below.

## `/clear` — the hidden, client-side command

`/clear` exists but does not appear in `/help`, is not registered as a server command, and is handled entirely inside the WebUI before your message is even sent. It:

- Wipes the rendered message list from the screen.
- Prints a local system line: `Chat cleared.`
- Disarms the WebUI's own history pagination, so the cleared screen stays cleared: scrolling up does not pull the older messages back. They reappear the next time the chat view loads its history from scratch — reopening the app, or reloading the WebUI.

It never reaches the gateway. The server-side session, the model's context, and the persisted transcript are all completely untouched.

## `/new` vs `/clear`: the crucial distinction

These two commands are easy to confuse and do genuinely different things — and **neither one deletes anything on the server**:

| | `/new` | `/clear` |
|---|---|---|
| Where it runs | Server (gateway) | Client (WebUI only, never sent) |
| What it clears | The model's context (what the LLM remembers) | Only what's drawn on your screen right now |
| Effect on the visible chat | Nothing is erased; adds a "New session started." separator | Wipes the screen; stays wiped until the app is reopened |
| Effect on the persisted transcript | Untouched — it's a separate, permanent log | Untouched |
| In `/help`? | Yes | No |

In short: **`/new` changes what the model remembers but leaves everything on screen; `/clear` changes what you see but leaves everything the model remembers and everything stored on the server untouched.** If you want a genuinely blank-looking chat that the model has also forgotten, you need `/new` — `/clear` alone hides nothing permanently, and everything comes back the next time you reopen the app. If you want to actually forget content, `/new` is still not permanent deletion: the discarded messages are archived for Dream, not destroyed, and the full transcript with everything before the separator is still visible on screen.

## Periodic tasks (HEARTBEAT.md)

This is unrelated to slash commands but shares the same "plain files, no terminal" spirit: Jenny also runs a periodic check every 30 minutes (`gateway.heartbeat.intervalS`, default 1800) driven by `workspace/HEARTBEAT.md`. It only acts on lines under a `## Active Tasks` heading; everything else in the file is ignored. You can edit that file directly from the Workspace tab, or just ask Jenny in chat to "add a periodic task" and she'll update it for you. This job shows up in the agent's internal job list as `heartbeat`, but it's system-managed and can't be removed the way a normal reminder can; to disable it you'd set `gateway.heartbeat.enabled` to `false` in `config.json` and restart the app (there is no in-app toggle for it). See [Scheduling and proactivity](./scheduling.md) for the full picture, including the cost-per-cycle caveat and reliability limits.

## See also

- [Chat basics](./chat.md) for the message composer, streaming, and the "Agent running" banner referenced above.
- [Memory, Dream and Atlas](./memory.md) for what `/dream` and `/atlas` actually process, and why either can say there's nothing to do.
- [Scheduling and proactivity](./scheduling.md) for `/goal`, reminders, and the heartbeat.
- [Configuration reference](../reference/configuration.md) for `modelPresets` and other config-only settings.
- [Troubleshooting](./troubleshooting.md) if a command's response looks wrong or the chat seems unresponsive.
