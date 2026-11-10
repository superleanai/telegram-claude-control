# Telegram Claude Controller

A private, single-user Telegram bot for controlling Claude Code on a machine
you own. Ported from [monperrus/telegram-tmux-controller](https://github.com/monperrus/telegram-tmux-controller):
same tmux bridge, same pairing/security model, but the Codex app-server JSONL
client is replaced with Claude Code's own documented non-interactive mode
(`claude -p --output-format json`, continued across turns with `--resume`)
instead of a reverse-engineered protocol. See
[`claude-code-remote-control.md`](claude-code-remote-control.md) for why this
repo does *not* talk to Anthropic's own Remote Control feature (claude.ai/code
↔ local CLI bridge) — that's undocumented and gated behind claude.ai OAuth in
a way its ToS reserves for Anthropic's own clients. This bot instead drives
the `claude` binary directly, the same way you would from a terminal.

## What it does

| Telegram input | Result |
| --- | --- |
| Normal text or `/ask <prompt>` | Runs `claude -p` headless (`--output-format stream-json`), continuing the same Claude Code session via `--resume` across turns. |
| `/newsession` | Forgets the headless session ID for this conversation; the next message starts a fresh `claude -p` session. |
| `/model` | Shows the model for this conversation and offers `sonnet`/`opus`/`haiku` as buttons. |
| `/model <name>` | Sets `--model <name>` for this conversation's `claude -p` calls (`/model default` clears it). |
| `/projects` | Shows/picks the project (a git repo with a remote, found directly under `TELEGRAM_CLAUDE_PROJECTS_DIR`) used as this conversation's working directory, as buttons. |
| `/projects <name>` | Sets project `<name>` as this conversation's cwd, and clears its session (`/projects default` resets it to `TELEGRAM_CLAUDE_WORKSPACE`). |
| `/usage` | Current session/weekly usage against your plan's limits — free, instant, uses no tokens. `/help` also offers this as a button. |
| `/tmux <text>` | Types text and Enter into an interactive `claude` session running in the configured tmux window, then returns its recent output. |
| `/sh <command>` | Runs a shell command directly (not through tmux) and returns its combined stdout/stderr. |
| `/task <prompt>` | Dispatches the prompt as a `claude --bg` background agent: no timeout, and hosted by Claude Code's own daemon rather than by this controller, so it keeps running — and keeps reporting here — across a `/restart` or a crash. A message with the result (or failure) lands here whenever the job actually finishes, even hours later. Replies with a 3-letter job id (e.g. `abc`), short enough to type back for `/tasks <job_id>` or `/cancel <job_id>`. |
| `/tasks` | One message listing the 5 most recent background jobs (icon, id, live state, duration, prompt), each also a tappable button into its detail view — history survives a controller restart. |
| `/tasks <job_id>` | Detail view: status, live state, duration, thread, the `claude attach`/`claude logs` id, full prompt, and last message/error — what tapping a `/tasks` button does. |
| `/cancel <job_id>` | Stops a running background job (`claude stop`). Its conversation is kept, so `/ask` continues from where it got to. |
| `/allow <id>`, `/deny <id>` | Answers a background job's tool-confirmation request (`TELEGRAM_CLAUDE_CONFIRM_TOOLS`). Never typed — these are the Allow/Deny buttons on the 🔐 message the job sends. |
| `/restart` | Restarts the controller's systemd `--user` service (see Install). |
| `/screen` | One message listing every pane across the whole tmux server as a button (even if there's only one) — tap to capture and return its content. |
| `/screen_show <target>` | Captures and returns a specific tmux target directly (e.g. `web:1`, `web:1.0`) — what the `/screen` buttons call under the hood. |
| `/status` | Reports whether the tmux session exists. |
| `/interrupt` | Sends Ctrl-C to the tmux session. |

While a headless turn runs, the bot streams progress instead of going quiet
until it's done: every completed `Write`/`Edit`/`MultiEdit`/`NotebookEdit`
tool call sends its own Telegram message, e.g.

```
✏️ Edited src/app.py (+4 -2)
+ def handler(request):
+ return respond(request)
- def handler():
- return respond()
```

Diffs come straight from the tool call's own before/after strings (no extra
file reads), with each changed line's indentation and trailing whitespace
stripped to stay readable on a phone. When a change touches
`TELEGRAM_CLAUDE_DIFF_PREVIEW_LINES` (default 10) lines or more, only the
`(+added -removed)` counts are shown, not the full diff. A 🫡 reaction on
your message acknowledges receipt immediately, before the turn finishes. A
"typing…" indicator also stays up for the whole `/ask` turn, refreshed on
each completed tool call, so a slow reply doesn't look like a dropped
message. Common `claude -p` failures (rate limits, an overloaded API) are
translated into a short, plain-English message instead of a raw error dump.

Commands are tappable two ways: `/help`'s reply carries inline buttons for
every command that needs no argument (`usage`, `status`, `screen`, `tasks`,
`model`, `interrupt`, `newsession`, `restart`), and the controller registers
the full command list with Telegram (`setMyCommands`) at startup, so typing
`/` in any client brings up the native autocomplete menu with descriptions
— tap one to insert `/command ` and just type the argument for commands
like `/ask`, `/task`, `/sh`, `/tmux`, `/cancel`, `/model <name>`.

One-letter shortcuts save typing on a phone: `h`=`/help` `s`=`/status`
`v`=`/screen` `i`=`/interrupt` `r`=`/restart` `t`=`/tasks` (bare) or `/task`
(`t <prompt>`) `a`=`/model` (bare) or `/model <name>` (`a <name>`)
`p`=`/projects` (bare) or `/projects <name>` (`p <name>`) `u`=`/usage`
`m <text>`=`/tmux <text>` `x <cmd>`=`/sh <cmd>` `c <prompt>`=`/ask <prompt>`.
Only an exact `<letter>` or `<letter> <rest>` triggers a shortcut —
anything else (including sentences that happen to start with one of these
letters, e.g. "im on my way") falls through to the normal `/ask` prompt path
unchanged. The one exception is `x`/`m`/`t`/`c`/`a`/`p`: a message that
genuinely starts with one of those letters followed by a space (e.g. "x-ray"
typed as "x ray gun") will be misread as a shortcut — say it another way, or
use the full `/sh`/`/tmux`/`/task`/`/ask`/`/model`/`/projects` command instead.

`/usage` runs Claude Code's own built-in `/usage` command (aliased
`/cost`/`/stats`) directly via `claude -p "/usage" --output-format json` —
not through the tmux bridge, and not tied to any conversation's `--resume`
session. It's a free, local, near-instant query (no API tokens spent)
reporting current session and weekly usage against your plan's limits, plus
what's contributing to it.

The per-conversation model choice (`/model`) is persisted the same way the
`--resume` session id is — it survives a controller restart and is
independent of `/newsession`, which only clears the conversation history,
not the model.

`/projects` lets one controller (and one Telegram bot) work across several
repos instead of being pinned to `TELEGRAM_CLAUDE_WORKSPACE`: a "project" is
any direct subdirectory of `TELEGRAM_CLAUDE_PROJECTS_DIR` (default `$HOME`)
that's a git repo with at least one remote configured — discovered fresh
each time you run `/projects`, not configured by hand. Selecting one sets
it as this conversation's `cwd` for every subsequent `/ask`, `/task`, etc.,
and also clears the conversation's `--resume` session id, since Claude Code
scopes a session's history to the cwd it was started in — resuming it under
a different project's directory wouldn't work anyway. Like `/model`, the
selection is per-conversation and persists across a controller restart.

The first chat must pair using a secret pairing code. Once paired, messages
from all other chats are silently ignored. In Telegram forum groups, each
topic (`message_thread_id`) gets its own independent `claude -p` session and
replies land back in that same topic, so parallel topics never share or
clobber each other's Claude conversation.

**In a group, the bot only answers when addressed.** A group is a conversation
between people, so treating every message as a prompt would make the bot reply
to humans talking to each other. Plain text becomes a prompt only when it opens
with the bot's name (`claude what's the load?`, `Claude: status?`,
`hey claude - deploy it`), @mentions the bot anywhere, or replies to one of
the bot's own messages — the name or mention is stripped before Claude sees the
prompt. An explicit `/command` always counts as addressing it. The name is the
bot's own Telegram first name by default; override or disable it with
`TELEGRAM_CLAUDE_ADDRESS_NAME`. One-to-one chats are never gated, since there
is nobody else there to address.

**In a group, turn off the bot's privacy mode or only `/commands` reach it.**
Telegram enables *group privacy mode* on every new bot, and a bot in that mode
receives only messages that start with `/`, @mention it, or reply to one of its
own messages. Everything else is never delivered to the bot at all — so the
plain-text `/ask` path above, and the one-letter shortcuts, silently do nothing
in a group: no reply, no error, nothing in the log, because the controller
never sees an update. Fix it once in
[BotFather](https://t.me/BotFather): `/mybots` → your bot → *Bot Settings* →
*Group Privacy* → *Turn off*, then remove and re-add the bot to the group for
the change to take effect. Verify with
`curl -s https://api.telegram.org/bot<token>/getMe` — you want
`"can_read_all_group_messages": true`. In a one-to-one chat with the bot none
of this applies; privacy mode only governs groups.

Only one request (foreground `/ask` or background `/task`) runs per topic at a
time, since two processes can't safely share the same `--resume` session —
a second request on the same topic is rejected with a "already running"
reply rather than queued. Different topics run fully in parallel, so a
long `/task` job in one topic never blocks `/ask` in another.

### Background jobs (`/task`) and how they survive a restart

A `/task` job is not a child of this controller. It is dispatched with
`claude --bg`, which hands the session to Claude Code's own background
supervisor (`claude daemon`), and the controller only keeps a row about it in
a small SQLite database (`TELEGRAM_CLAUDE_JOBS_DB`) mapping the session to
the Telegram chat and topic that started it.

Everything a job has to say comes back through hooks that the job's own
process fires, configured in a generated settings file
(`TELEGRAM_CLAUDE_HOOK_SETTINGS`) passed to it with `--settings`, and posted
with `curl` to a unix socket the controller listens on
(`TELEGRAM_CLAUDE_HOOK_SOCKET`):

| Hook | Telegram |
| --- | --- |
| `PostToolUse` on the file-editing tools | ✏️ one message per edited file, with the diff |
| `Stop` | ✅ the finished job's answer (`last_assistant_message`) |
| `SessionEnd` without a finished turn | ❌ the job died |
| `PermissionRequest` | 🔐 the tool and its input, saying the job is blocked on a prompt Claude Code raised itself — a report only, see below |
| `PreToolUse`, only for `TELEGRAM_CLAUDE_CONFIRM_TOOLS` | 🔐 the tool and its input, with Allow/Deny buttons — the job stays blocked, inside the hook, until a button is tapped or `TELEGRAM_CLAUDE_PERMISSION_ASK_TIMEOUT` passes |

Approving from Telegram works through `PreToolUse`, not `PermissionRequest`:
in Claude Code 2.1.x a `PermissionRequest` hook does fire in a background
session, but its `permissionDecision` is ignored there (verified — its own
bundled reference scopes `permissionDecision` to `PreToolUse`), so a prompt
Claude Code raises by itself can only be reported here and has to be
answered on the server with `claude attach <id>`. Setting
`TELEGRAM_CLAUDE_CONFIRM_TOOLS` (e.g. `Bash`, or `Bash|Write|Edit`) puts the
gate one step earlier instead, on `PreToolUse`, whose allow/deny *is*
honoured — so those tools ask Telegram first and never reach a prompt.

Because the socket path is fixed, this survives the controller being
restarted mid-job: the session keeps working while the controller is down,
and its next hook finds the socket again and lands in the right chat. At
startup every row still marked "running" is reconciled against `claude
agents --json` — still working or blocked, it is simply re-adopted; finished
while the controller was down, it is closed out with a message; unknown to
Claude Code, it is flagged "interrupted". A job's session also becomes the
topic's session, so a later `/ask` continues from the work the job did.

Nothing about a job is lost when the controller is not around: on the server
`claude agents` lists them, `claude logs <id>` prints output, and `claude
attach <id>` opens one in a terminal.

`/screen` walks the *entire* tmux server, not just `TELEGRAM_CLAUDE_SESSION`
— every pane in every window of every session gets its own button, always,
even when there's only one pane total: tapping a screen is always exactly
one tap away from its content, with no auto-skipped levels to cause
surprises. Button taps are Telegram `callback_query` updates: the bot
acknowledges them (so the client stops showing a spinner) and replays the
button's payload through the same command dispatcher a typed message would
use — a `/screen` button is, under the hood, just `/screen_show <target>`
sent on your behalf.

## Agent inbox

Other agents on the host (Claude sessions, other bots, crons, including other
Unix users) reach the bot through a topic of agent-fleet's `agent-inbox`
(a maildir spool in `~/.agent-inbox/<topic>`, opened to the peer accounts by POSIX ACLs
the bot's account sets on its own files: no root).
Set `AGENT_INBOX=<topic>` and `AGENT_INBOX_PEERS=<account,...>` in the config file
(or `TELEGRAM_CLAUDE_AGENT_INBOX`, `TELEGRAM_CLAUDE_AGENT_INBOX_PEERS`); at start the bot opens the topic to those accounts, then
consumes it: each message runs as a headless `claude -p`
turn, in one conversation per sending agent (never the operator's), with a
prompt that states the sending agent and its Unix user (the message file's
owner). The paired chat gets the request and the answer; the answer also goes
to the sender's `--reply-to` topic:

    agent-inbox post remote-tmux:claude_pellow "reply with the word pong" --sender crystal-clear --reply-to martin:crystal-clear

The bot loads `agent_inbox.py` from `TELEGRAM_CLAUDE_AGENT_INBOX_MODULE`
(default `~/bin/agent_inbox.py`). Telegram bots cannot message other
bots, so this inbox, not Telegram, is the agent-to-agent channel.

## Requirements

- Python 3.9+; standard library only, no dependencies to install.
- `tmux` on `PATH` (only needed for the `/tmux`, `/screen`, `/status`,
  `/interrupt` commands).
- `claude` (Claude Code CLI) installed and authenticated on this host.
- A Telegram bot token from [BotFather](https://t.me/BotFather).
- `systemd --user` (only needed for `install.sh` and `/restart`; everything
  else works fine run by hand or under another supervisor).

## Install

Quickest path — installs a `systemd --user` service running straight out of
this checkout (no copy elsewhere):

```sh
./install.sh
# or non-interactively:
BOT_TOKEN='your-token' ./install.sh
```

`install.sh` prompts for (or accepts via `--bot-token`/`BOT_TOKEN`) the bot
token, generates a `PAIR_CODE` if you don't pass one, writes
`~/.config/telegram-claude-control.env` at mode 600, and installs+enables
`systemd/telegram-claude-controller.user.service` under
`~/.config/systemd/user/`. Options: `--force` (regenerate the config),
`--no-start` (install/enable without starting). This is also what `/restart`
targets, so pairing and restarting from Telegram only work once the service
is installed this way (or an equivalent unit named `telegram-claude-controller.service`
exists — override the name via `TELEGRAM_CLAUDE_UNIT`).

Validate an install without entering the polling loop:

```sh
./telegram-claude-control.py --check
```

Checks config file permissions, `tmux` on `PATH`, the `claude` binary,
the workspace directory, tmux session availability (warning only), and
Telegram API connectivity.

For the bot to keep running after you log out of this machine:

```sh
loginctl enable-linger "$USER"
```

If you want the `/tmux` bridge, start an interactive Claude Code session in
a named tmux session (default name `claude`) before pairing:

```sh
tmux new-session -d -s claude "claude"
```

Then in Telegram, send `/pair <your pairing code>` from the one chat that
should control the bot. Use `/help` to confirm it's working.

### Manual install

Prefer to run it by hand (e.g. under your own process supervisor) instead of
`install.sh`:

1. Create a config file with restricted permissions:

   ```sh
   mkdir -p ~/.config ~/.local/state
   cp telegram-claude-control.env.example ~/.config/telegram-claude-control.env
   chmod 600 ~/.config/telegram-claude-control.env
   ```

2. Set `BOT_TOKEN` and a long, random `PAIR_CODE` in that file.
3. Run the controller manually:

   ```sh
   ./telegram-claude-control.py
   ```

For a system-wide (not `--user`) service, adapt
[systemd/telegram-claude-controller.service.example](systemd/telegram-claude-controller.service.example).

## Configuration

The config file (`~/.config/telegram-claude-control.env` by default) accepts
`KEY=value` lines and must contain:

- `BOT_TOKEN`: Telegram HTTP API token. Treat it like a password.
- `PAIR_CODE`: one-time pairing secret. Use a unique, high-entropy value.

Optional environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `TELEGRAM_CLAUDE_CONFIG` | `~/.config/telegram-claude-control.env` | Secret config file. |
| `TELEGRAM_CLAUDE_STATE` | `~/.local/state/telegram-claude-control.json` | Pairing state and Telegram update offset. |
| `TELEGRAM_CLAUDE_JOBS_DB` | `~/.local/state/telegram-claude-control-jobs.db` | SQLite registry of `/task` jobs: `/tasks` history, and the mapping from a background session to its Telegram chat. |
| `TELEGRAM_CLAUDE_HOOK_SOCKET` | `~/.local/state/telegram-claude-control-hooks.sock` | Unix socket the hooks of `/task` jobs post to. |
| `TELEGRAM_CLAUDE_HOOK_SETTINGS` | `~/.local/state/telegram-claude-control-hooks.json` | Generated `--settings` file handed to every `/task` job; rewritten at startup. |
| `TELEGRAM_CLAUDE_SESSION` | `claude` | tmux target session for the `/tmux` bridge. |
| `TELEGRAM_CLAUDE_WORKSPACE` | `$HOME` | Default working directory for headless `claude -p` calls, used when a conversation has no `/projects` selection. |
| `TELEGRAM_CLAUDE_PROJECTS_DIR` | `$HOME` | Directory `/projects` scans for selectable project subdirectories. |
| `TELEGRAM_CLAUDE_BIN` | `~/.local/bin/claude` | Path to the `claude` executable. |
| `TELEGRAM_CLAUDE_PERMISSION_MODE` | *(unset)* | Passed as `--permission-mode` to headless calls and `/task` jobs. Leave unset to inherit whatever `permissions.defaultMode` is configured in `~/.claude/settings.json`. A mode that actually prompts turns every prompt in a `/task` job into 🔐 Allow/Deny buttons in Telegram; `bypassPermissions` means it never asks. |
| `TELEGRAM_CLAUDE_ASK_TIMEOUT` | `600` | Seconds to wait for a headless `claude -p` turn before giving up. |
| `TELEGRAM_CLAUDE_SH_TIMEOUT` | `60` | Seconds to wait for a `/sh` command before giving up. |
| `TELEGRAM_CLAUDE_CONFIRM_TOOLS` | *(unset)* | Hook matcher (e.g. `Bash`, `Bash\|Write\|Edit`, `*`) for tools a `/task` job must get an explicit Telegram Allow/Deny tap for. Unset means no extra gate. |
| `TELEGRAM_CLAUDE_PERMISSION_ASK_TIMEOUT` | `1800` | Seconds a `/task` job stays blocked on its 🔐 Allow/Deny buttons. After that the tool call goes back to Claude Code's normal permission flow. |
| `TELEGRAM_CLAUDE_USAGE_TIMEOUT` | `30` | Seconds to wait for `/usage` before giving up. |
| `TELEGRAM_CLAUDE_UNIT` | `telegram-claude-controller.service` | systemd `--user` unit name that `/restart` restarts. |
| `TELEGRAM_CLAUDE_DIFF_PREVIEW_LINES` | `10` | Inline the diff in an edited-file notification when total changed lines (added + removed) is below this; otherwise show only the counts. |
| `TELEGRAM_CLAUDE_ADDRESS_NAME` | the bot's own Telegram first name | In a **group**, the name plain text must open with for the bot to treat it as a prompt. Set to the empty string to answer every message instead. One-to-one chats are never gated. |

## Operational notes

- **Headless calls execute with real tool access.** `claude -p` cannot show
  interactive permission prompts (there's no TTY), so it needs either
  `permissions.defaultMode` set to something non-interactive (e.g.
  `bypassPermissions` or `acceptEdits`) in `~/.claude/settings.json`, or
  `TELEGRAM_CLAUDE_PERMISSION_MODE` set explicitly — otherwise turns that
  need a tool permission will fail rather than hang. Whatever mode is active,
  anyone who can message the paired chat can make Claude Code read, edit, and
  run commands on this host. Pair only a Telegram account you fully trust,
  and keep `BOT_TOKEN`/`PAIR_CODE` out of source control and logs.
- The bot uses long polling; do not run two controller instances with the
  same bot token, or they may consume each other's updates.
- The sender connection is warmed at startup and kept alive to minimize the
  delay before the acknowledgement reaction. Long polling uses a separate
  connection so it never blocks an acknowledgement or final reply.
- The state file is written with mode `0600` and records the paired chat ID,
  each conversation's `--resume` session id, and each conversation's
  `/model` choice — all of which survive a controller restart. Delete the
  state file to allow pairing a different chat (this also forgets every
  conversation's session and model).
- Outgoing Telegram text is capped at the API's 4096-character limit; tmux
  output is trimmed to recent lines for the same reason.

## Troubleshooting

- **Bot does not respond:** check process logs, the bot token, network
  access, and make sure no second polling process shares the same bot token.
- **`tmux session is unavailable`:** create the configured session (see step
  3 above) or set `TELEGRAM_CLAUDE_SESSION` to the actual session name.
- **`claude -p request failed`:** run the same `claude -p "..." --output-format json`
  command manually as the service user to see the raw error — usually a
  missing/expired login, an unreachable `TELEGRAM_CLAUDE_BIN` path, or a
  permission-mode mismatch (see the note above).
- **`/restart` replies "Restart failed: Unit ... not found":** the
  controller isn't running under the systemd unit `/restart` expects — run
  `./install.sh` (or set `TELEGRAM_CLAUDE_UNIT` to the actual unit name).

## Files

- `telegram-claude-control.py` — controller program (also: `--check`).
- `install.sh` — installs config + the systemd `--user` service.
- `telegram-claude-control.env.example` — safe secret-config template.
- `systemd/` — service templates (`.user.service` installed by `install.sh`;
  `.service.example` is a system-wide template to adapt by hand).
- `claude-code-remote-control.md` — research notes on Anthropic's own Remote
  Control feature and why this bot doesn't use it.

## License

[MIT](LICENSE).
