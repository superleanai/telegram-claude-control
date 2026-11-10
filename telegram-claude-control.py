#!/usr/bin/env python3
"""Private Telegram controller for a tmux-hosted Claude Code session and the
local `claude -p` headless CLI. Modeled on
https://github.com/monperrus/telegram-tmux-controller, swapping that
project's Codex app-server JSONL client for Claude Code's documented
non-interactive mode (`claude -p --output-format stream-json --verbose
[--resume <id>]`), parsing the JSONL event stream so each completed
file-editing tool call reaches Telegram as its own message.

/task jobs instead go through `claude --bg`, Claude Code's own background
agents: they are hosted by its daemon rather than by this process, so they
survive a controller restart, and they report progress, permission prompts
and their final answer back through hooks posting to a unix socket here."""
import difflib
import http.client
import http.server
import json
import os
import random
import re
import shlex
import shutil
import socket
import socketserver
import sqlite3
import string
import subprocess
import sys
import threading
import time
import urllib.parse

CONFIG_PATH = os.environ.get("TELEGRAM_CLAUDE_CONFIG", os.path.expanduser("~/.config/telegram-claude-control.env"))
STATE_PATH = os.environ.get("TELEGRAM_CLAUDE_STATE", os.path.expanduser("~/.local/state/telegram-claude-control.json"))
JOBS_DB_PATH = os.environ.get("TELEGRAM_CLAUDE_JOBS_DB", os.path.expanduser("~/.local/state/telegram-claude-control-jobs.db"))
SESSION = os.environ.get("TELEGRAM_CLAUDE_SESSION", "claude")
WORKSPACE = os.environ.get("TELEGRAM_CLAUDE_WORKSPACE", os.path.expanduser("~"))
CLAUDE_BIN = os.environ.get("TELEGRAM_CLAUDE_BIN", os.path.expanduser("~/.local/bin/claude"))
# Root directory /projects discovers "projects" under: any direct
# subdirectory that's a git repo with at least one remote.
PROJECTS_DIR = os.environ.get("TELEGRAM_CLAUDE_PROJECTS_DIR", os.path.expanduser("~"))
# Left unset by default: headless calls then inherit whatever permission mode
# is configured in ~/.claude/settings.json on this host. Set this to
# constrain headless requests (e.g. "plan" or "acceptEdits") independent of
# that global setting.
PERMISSION_MODE = os.environ.get("TELEGRAM_CLAUDE_PERMISSION_MODE", "")
ASK_TIMEOUT = int(os.environ.get("TELEGRAM_CLAUDE_ASK_TIMEOUT", "600"))
SH_TIMEOUT = int(os.environ.get("TELEGRAM_CLAUDE_SH_TIMEOUT", "60"))
# /task jobs are hosted by Claude Code's own background daemon (claude --bg),
# not by this process, so they have no timeout at all -- they outlive the
# controller. They report back through hooks posting to HOOK_SOCKET_PATH; see
# start_job and handle_hook. TELEGRAM_CLAUDE_TASK_TIMEOUT (a cap this process
# used to enforce) is therefore no longer read.
HOOK_SOCKET_PATH = os.environ.get("TELEGRAM_CLAUDE_HOOK_SOCKET", os.path.expanduser("~/.local/state/telegram-claude-control-hooks.sock"))
HOOK_SETTINGS_PATH = os.environ.get("TELEGRAM_CLAUDE_HOOK_SETTINGS", os.path.expanduser("~/.local/state/telegram-claude-control-hooks.json"))
# Tools a /task job must get an explicit Telegram Allow/Deny tap for, as a
# hook matcher ("Bash", "Bash|Write|Edit", "*", ...). Empty (the default)
# means no extra gate: jobs run under whatever PERMISSION_MODE gives them.
# This is enforced with a PreToolUse hook, which is the one hook event whose
# allow/deny decision Claude Code honours in a background session --
# PermissionRequest fires there too, but its decision is ignored (2.1.x), so
# a prompt Claude Code raises by itself can only be reported, not answered.
CONFIRM_TOOLS = os.environ.get("TELEGRAM_CLAUDE_CONFIRM_TOOLS", "")
# How long a background job may sit blocked waiting for that tap. On expiry
# the hook declines to decide and the tool call goes back to Claude Code's
# normal permission flow.
PERMISSION_ASK_TIMEOUT = int(os.environ.get("TELEGRAM_CLAUDE_PERMISSION_ASK_TIMEOUT", "1800"))
# Inline the diff in the edited-file notification when total changed lines
# (added + removed) is below this. Above it, only the +/- counts are shown.
DIFF_PREVIEW_MAX_LINES = int(os.environ.get("TELEGRAM_CLAUDE_DIFF_PREVIEW_LINES", "10"))
# /usage runs claude -p "/usage" directly: a free, instant, local-only
# meta-command (no API tokens, no --resume needed), so it gets its own short
# timeout instead of reusing ASK_TIMEOUT.
USAGE_TIMEOUT = int(os.environ.get("TELEGRAM_CLAUDE_USAGE_TIMEOUT", "30"))
# systemd --user unit installed by install.sh; /restart targets this.
UNIT_NAME = os.environ.get("TELEGRAM_CLAUDE_UNIT", "telegram-claude-controller.service")
# In a group, plain text is only taken as a prompt when it addresses the bot by
# name -- otherwise the bot answers every message two humans send each other.
# Defaults to the bot's own Telegram first name, discovered via getMe at
# startup. Set it explicitly to use a different name, or to the empty string to
# answer every message in a group. One-to-one chats are never gated: there is
# no one else there to address.
ADDRESS_NAME = os.environ.get("TELEGRAM_CLAUDE_ADDRESS_NAME")
# Agent inbox: other agents on the host (other Unix users included) post to a
# topic of agent-fleet's agent_inbox.py; each message runs as a headless turn
# (one conversation per sending agent) and its answer is posted to the
# sender's reply topic. AGENT_INBOX (config file, or TELEGRAM_CLAUDE_AGENT_INBOX)
# names the topic; unset disables it.
AGENT_INBOX_MODULE = os.environ.get("TELEGRAM_CLAUDE_AGENT_INBOX_MODULE", os.path.expanduser("~/bin/agent_inbox.py"))
AGENT_INBOX_POLL = float(os.environ.get("TELEGRAM_CLAUDE_AGENT_INBOX_POLL", "5"))
# Filled in at startup from getMe, for @username mentions and reply detection.
BOT_IDENTITY = {"username": "", "id": None, "first_name": ""}
# /model button shortcuts; /model <any other id> is also accepted verbatim.
MODEL_PRESETS = ["sonnet", "opus", "haiku"]

# Registered with Telegram via setMyCommands so typing "/" brings up a
# tappable, autocompleted menu (with descriptions) in any Telegram client --
# in addition to the /help message's own inline buttons for the
# no-argument commands. /screen_show, driven exclusively by /screen's own
# buttons, is left out, same as it's left out of /help's text.
BOT_COMMANDS = [
    ("help", "Show available commands"),
    ("ask", "Headless claude -p with a prompt (or just send plain text)"),
    ("newsession", "Forget this conversation and start fresh"),
    ("model", "Show/pick the Claude model for this conversation"),
    ("projects", "Show/pick the project (folder) used as this conversation's cwd"),
    ("usage", "Current session/weekly usage against your plan limits"),
    ("task", "Run a claude --bg background agent, no timeout"),
    ("tasks", "List recent background jobs"),
    ("cancel", "Cancel a running background job"),
    ("sh", "Run a shell command directly"),
    ("tmux", "Type text into the interactive tmux session"),
    ("screen", "Capture tmux session/window/pane output"),
    ("status", "Report tmux session status"),
    ("interrupt", "Send Ctrl-C to the tmux session"),
    ("restart", "Restart the controller service"),
]


def config():
    values = {}
    with open(CONFIG_PATH, encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
    for required in ("BOT_TOKEN", "PAIR_CODE"):
        if not values.get(required):
            raise RuntimeError(f"Missing {required} in {CONFIG_PATH}")
    return values


CFG = config()
API_HOST = "api.telegram.org"
API_PREFIX = f"/bot{CFG['BOT_TOKEN']}/"


# Tool names whose completed tool_result should trigger one Telegram message
# per invocation (default: file-editing tools, one message per edited file).
FILE_EDIT_TOOLS = {
    "Write": "Wrote",
    "Edit": "Edited",
    "MultiEdit": "Edited (multi-part)",
    "NotebookEdit": "Edited notebook",
}


def _tool_target_path(name, tool_input):
    if name == "NotebookEdit":
        return tool_input.get("notebook_path", "?")
    return tool_input.get("file_path", "?")


def _compact_diff_line(line):
    """A raw unified-diff +/- line, with its content's indentation and
    trailing whitespace stripped so it fits a phone screen."""
    marker, content = line[0], line[1:]
    return f"{marker} {content.strip()}"


def _text_diff(old_text, new_text):
    """(added, removed, compact +/- lines) between two text blobs, context
    lines omitted so only actually-changed lines are counted and shown."""
    diff = difflib.unified_diff(old_text.splitlines(), new_text.splitlines(), n=0, lineterm="")
    changes = [line for line in diff if line[:1] in ("+", "-") and not line.startswith(("+++", "---"))]
    added = sum(1 for line in changes if line.startswith("+"))
    removed = sum(1 for line in changes if line.startswith("-"))
    return added, removed, [_compact_diff_line(line) for line in changes]


def _edit_stats(name, tool_input):
    """(added, removed, preview lines) for a completed file-editing tool
    call. Edit/MultiEdit get a real before/after diff since both strings are
    right there in the tool call. Write/NotebookEdit only ever see the new
    content, so they're reported as pure additions."""
    if name == "Edit":
        return _text_diff(tool_input.get("old_string", ""), tool_input.get("new_string", ""))
    if name == "MultiEdit":
        added = removed = 0
        preview = []
        for edit in tool_input.get("edits", []):
            a, r, lines = _text_diff(edit.get("old_string", ""), edit.get("new_string", ""))
            added += a
            removed += r
            preview += lines
        return added, removed, preview
    if name == "NotebookEdit" and tool_input.get("edit_mode") == "delete":
        return 0, 0, []
    content = tool_input.get("content") if name == "Write" else tool_input.get("new_source", "")
    lines = (content or "").splitlines()
    return len(lines), 0, [_compact_diff_line("+" + line) for line in lines]


class ClaudeHeadless:
    """Runs prompts through `claude -p --output-format stream-json`, one
    process per turn, parsing the JSONL event stream so callers can get a
    Telegram message per completed tool call (e.g. per edited file) instead
    of waiting for the whole turn. Conversation continuity across turns comes
    from Claude Code's own --resume <session_id>, not from a long-lived
    subprocess.

    Session ids are kept per conversation_id (Telegram's message_thread_id,
    or None outside of forum topics) so parallel Telegram threads don't
    share -- or clobber -- each other's Claude context. The mapping is
    persisted (via on_change) so a thread's context survives a controller
    restart; only an explicit /newsession clears it early.

    Locking is per conversation_id, not global: a claude -p session cannot
    safely be driven by two concurrent processes sharing the same --resume
    id, but unrelated Telegram topics must still be able to run at the same
    time -- otherwise one /task job would freeze every other topic for as
    long as it runs."""

    def __init__(self, sessions=None, models=None, projects=None, on_change=None):
        self.sessions = sessions if sessions is not None else {}
        self.models = models if models is not None else {}
        self.projects = projects if projects is not None else {}
        self.on_change = on_change or (lambda: None)
        self._locks_guard = threading.Lock()
        self._conversation_locks = {}

    def set_model(self, conversation_id, model):
        """model=None resets the conversation to the CLI's own default."""
        with STATE_LOCK:
            if model:
                self.models[str(conversation_id)] = model
            else:
                self.models.pop(str(conversation_id), None)
        self.on_change()

    def get_model(self, conversation_id):
        return self.models.get(str(conversation_id))

    def select_project(self, conversation_id, project):
        """project=None resets the conversation to the default WORKSPACE.
        Also clears the conversation's --resume session id: Claude Code
        scopes a session's history to the cwd it was started in, so
        resuming it under a different project's cwd wouldn't work anyway."""
        with STATE_LOCK:
            if project:
                self.projects[str(conversation_id)] = project
            else:
                self.projects.pop(str(conversation_id), None)
            self.sessions.pop(str(conversation_id), None)
        self.on_change()

    def get_project(self, conversation_id):
        return self.projects.get(str(conversation_id))

    def _conversation_lock(self, conversation_id):
        key = str(conversation_id)
        with self._locks_guard:
            return self._conversation_locks.setdefault(key, threading.Lock())

    def _execute(self, prompt, conversation_id, notify, timeout):
        """One `claude -p` turn: spawns the process, parses its JSONL event
        stream (calling notify(text) per completed file-editing tool call),
        and returns the final assistant result text. This is the foreground
        /ask path; /task jobs are dispatched with `claude --bg` instead (see
        start_job) and never come through here."""
        prefix, cwd = self.command_prefix(conversation_id)
        args = [CLAUDE_BIN, "-p", prompt, "--output-format", "stream-json", "--verbose"] + prefix
        process = subprocess.Popen(
            args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        timed_out = threading.Event()

        def on_timeout():
            timed_out.set()
            process.kill()

        timer = threading.Timer(timeout, on_timeout)
        timer.start()
        pending_tools = {}
        final_result = None
        try:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                event_type = event.get("type")
                if event_type == "assistant":
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") == "tool_use":
                            pending_tools[block["id"]] = (block.get("name"), block.get("input", {}))
                elif event_type == "user":
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") != "tool_result":
                            continue
                        name, tool_input = pending_tools.pop(block.get("tool_use_id"), (None, {}))
                        if name not in FILE_EDIT_TOOLS:
                            continue
                        path = _tool_target_path(name, tool_input)
                        if block.get("is_error"):
                            error_text = str(block.get("content", ""))[:300]
                            notify(f"❌ {name} failed on {path}: {error_text}")
                            continue
                        added, removed, preview = _edit_stats(name, tool_input)
                        header = f"✏️ {FILE_EDIT_TOOLS[name]} {path} (+{added} -{removed})"
                        if preview and added + removed < DIFF_PREVIEW_MAX_LINES:
                            notify(header + "\n" + "\n".join(preview))
                        else:
                            notify(header)
                elif event_type == "result":
                    final_result = event
            process.wait(timeout=10)
        finally:
            timer.cancel()
        stderr_text = process.stderr.read()
        if final_result is None:
            if timed_out.is_set():
                raise RuntimeError(f"claude timed out after {timeout}s")
            raise RuntimeError((stderr_text or "claude exited without a result event").strip()[:1500])
        if final_result.get("session_id"):
            with STATE_LOCK:
                self.sessions[str(conversation_id)] = final_result["session_id"]
            self.on_change()
        if final_result.get("is_error"):
            raise RuntimeError(str(final_result.get("result") or "claude reported an error"))
        return str(final_result.get("result") or "(empty response)")

    def run(self, prompt, conversation_id=None, notify=None):
        """Runs one foreground turn, calling notify(text) for each completed
        file-editing tool call as it happens, and returning the final
        assistant result text once the turn completes. Raises immediately
        (no blocking) if a /task job already owns this conversation."""
        notify = notify or (lambda text: None)
        lock = self._conversation_lock(conversation_id)
        if not lock.acquire(blocking=False):
            raise RuntimeError("A request is already running for this conversation. Check /tasks, or wait for it to finish.")
        try:
            return self._execute(prompt, conversation_id, notify, ASK_TIMEOUT)
        finally:
            lock.release()

    def command_prefix(self, conversation_id):
        """The per-conversation `claude` invocation bits shared by the
        foreground (`-p`) path and background (`--bg`) jobs: resume id,
        permission mode, model, and working directory. Returns
        (args, cwd)."""
        args = []
        session_id = self.sessions.get(str(conversation_id))
        if session_id:
            args += ["--resume", session_id]
        if PERMISSION_MODE:
            args += ["--permission-mode", PERMISSION_MODE]
        model = self.models.get(str(conversation_id))
        if model:
            args += ["--model", model]
        project = self.projects.get(str(conversation_id))
        return args, os.path.join(PROJECTS_DIR, project) if project else WORKSPACE

    def adopt_session(self, conversation_id, session_id):
        """Point a conversation at a session id produced elsewhere (a /task
        job's `claude --bg` session), so a later /ask continues from the
        work that job did."""
        if not session_id:
            return
        with STATE_LOCK:
            if self.sessions.get(str(conversation_id)) == session_id:
                return
            self.sessions[str(conversation_id)] = session_id
        self.on_change()

    def reset(self, conversation_id=None):
        with STATE_LOCK:
            removed = self.sessions.pop(str(conversation_id), None) is not None
        if removed:
            self.on_change()


CLAUDE = ClaudeHeadless()


class JobStore:
    """Durable registry of /task jobs (SQLite). Since a job is now a `claude
    --bg` session hosted by Claude Code's own background daemon -- not a
    child of this process -- this table is the *only* link between a live
    session and the Telegram chat that started it: the hooks those sessions
    fire (see HookServer) arrive with nothing but a session id, and are
    routed back to a chat by looking the id up here. That indirection is
    what makes a job survive a controller restart end to end: the session
    keeps working while the controller is down, and its hooks find the row
    again the moment the socket is back.

    Rows still marked "running" at startup are not blindly declared
    interrupted any more; main() reconciles them against `claude agents
    --json` first (see reconcile_jobs)."""

    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._local = threading.local()
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    chat_id TEXT,
                    thread_id TEXT,
                    prompt TEXT,
                    status TEXT,
                    created_at REAL,
                    finished_at REAL,
                    result TEXT
                )"""
            )
            # Added after the switch to `claude --bg`; ALTER rather than a
            # new schema so an existing jobs.db keeps its history.
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            for column in ("agent_id", "session_id"):
                if column not in columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")

    def _connect(self):
        # One connection per thread: sqlite3 connections aren't safe to share
        # across threads, and start_task/hook/handle() calls all come from
        # different ones.
        if not hasattr(self._local, "db"):
            self._local.db = sqlite3.connect(self.path, timeout=30)
        return self._local.db

    def start(self, job_id, chat_id, thread_id, prompt, agent_id):
        with self._connect() as db:
            db.execute(
                "INSERT INTO jobs (job_id, chat_id, thread_id, prompt, status, created_at, agent_id) VALUES (?, ?, ?, ?, 'running', ?, ?)",
                (job_id, str(chat_id), str(thread_id), prompt, time.time(), agent_id),
            )

    def finish(self, job_id, status, result):
        with self._connect() as db:
            db.execute(
                "UPDATE jobs SET status = ?, result = ?, finished_at = ? WHERE job_id = ?",
                (status, (result or "")[:1500], time.time(), job_id),
            )

    def set_session(self, job_id, session_id):
        with self._connect() as db:
            db.execute("UPDATE jobs SET session_id = ? WHERE job_id = ?", (session_id, job_id))

    def recent(self, limit=8):
        with self._connect() as db:
            return db.execute(
                "SELECT job_id, status, prompt, created_at, finished_at, agent_id FROM jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def get(self, job_id):
        with self._connect() as db:
            return db.execute(
                "SELECT job_id, chat_id, thread_id, prompt, status, created_at, finished_at, result, agent_id, session_id FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()

    def running(self):
        with self._connect() as db:
            return db.execute(
                "SELECT job_id, chat_id, thread_id, prompt, agent_id FROM jobs WHERE status = 'running' ORDER BY created_at"
            ).fetchall()

    def running_for(self, thread_id):
        """The job_id of the /task job currently holding this conversation,
        if any. Checked instead of an in-process lock so the constraint (one
        live claude session per conversation) still holds across a
        controller restart, when no lock object exists any more."""
        with self._connect() as db:
            row = db.execute(
                "SELECT job_id FROM jobs WHERE status = 'running' AND thread_id = ? LIMIT 1",
                (str(thread_id),),
            ).fetchone()
        return row[0] if row else None

    def by_agent(self, agent_id):
        """Route a hook payload back to its job. Claude Code's short agent id
        is the first 8 characters of the session uuid the hooks report, so a
        job is found either way round. Several rows can share a session id --
        a /task job resumes the conversation's session, which an earlier job
        may already own -- so the live one wins, then the most recent."""
        with self._connect() as db:
            return db.execute(
                """SELECT job_id, chat_id, thread_id, prompt, status FROM jobs
                   WHERE agent_id = ? OR session_id = ?
                   ORDER BY status = 'running' DESC, created_at DESC LIMIT 1""",
                (agent_id[:8], agent_id),
            ).fetchone()


JOBS = JobStore(JOBS_DB_PATH)


def hook_command(timeout):
    """The shell command Claude Code runs for one hook event: POST the hook's
    JSON payload (stdin) to this controller's unix socket, and write the
    controller's JSON answer back to stdout, which is exactly the contract
    a "command" hook has. A unix socket rather than a TCP port so the path
    baked into the settings file stays valid across controller restarts and
    nothing is ever reachable from the network."""
    return (
        f"curl -sS --max-time {timeout} --unix-socket {shlex.quote(HOOK_SOCKET_PATH)} "
        "-H 'Content-Type: application/json' --data-binary @- http://localhost/hook"
    )


def write_hook_settings():
    """(Re)write the settings file handed to every `claude --bg` job with
    --settings. It is additive: the host's own ~/.claude/settings.json hooks
    still apply, these are appended for background jobs only -- the
    foreground /ask path never loads it."""
    quick = [{"type": "command", "command": hook_command(30), "timeout": 45}]
    document = {
        "hooks": {
            # One Telegram message per edited file, the same notification the
            # foreground path builds from its stream-json events.
            "PostToolUse": [{"matcher": "|".join(FILE_EDIT_TOOLS), "hooks": quick}],
            # The completion callback: fires in the job's own process when the
            # turn ends, and carries last_assistant_message -- the answer.
            "Stop": [{"hooks": quick}],
            # Backstop for a session that dies without finishing a turn.
            "SessionEnd": [{"hooks": [{"type": "command", "command": hook_command(15), "timeout": 20}]}],
            # Reports (but cannot answer) a prompt Claude Code raises itself.
            "PermissionRequest": [{"hooks": quick}],
        }
    }
    if CONFIRM_TOOLS:
        # Blocks the job while the Allow/Deny buttons sit in Telegram.
        document["hooks"]["PreToolUse"] = [{"matcher": CONFIRM_TOOLS, "hooks": [{
            "type": "command",
            "command": hook_command(PERMISSION_ASK_TIMEOUT),
            "timeout": PERMISSION_ASK_TIMEOUT + 30,
        }]}]
    os.makedirs(os.path.dirname(HOOK_SETTINGS_PATH), exist_ok=True)
    with open(HOOK_SETTINGS_PATH, "w", encoding="utf-8") as file:
        json.dump(document, file, indent=2)


# request_id -> {"event": Event, "decision": "allow"/"deny"}. One entry per
# permission prompt currently parked in Telegram, waiting for a tap.
PERMISSIONS = {}
PERMISSIONS_LOCK = threading.Lock()
# Held while a /task job is being spawned, and by every hook lookup, so a job
# that finishes in under a second cannot fire its Stop hook before the row
# that routes it back to Telegram exists.
JOB_START_LOCK = threading.Lock()


def _random_id(exists):
    """3 random lowercase letters -- short and easy to type on a phone.
    exists(candidate) rejects ids already taken (26**3 = 17,576 combinations
    makes collisions rare for a single-user bot, but not impossible)."""
    for _ in range(100):
        candidate = "".join(random.choices(string.ascii_lowercase, k=3))
        if not exists(candidate):
            return candidate
    raise RuntimeError("could not allocate a unique id")


def _thread_of(thread_id):
    """Telegram thread ids come back out of SQLite as strings, including the
    literal "None" used outside forum topics."""
    return None if thread_id in (None, "None", "") else int(thread_id)


def _tool_summary(payload):
    tool = payload.get("tool_name") or "a tool"
    tool_input = payload.get("tool_input") or {}
    detail = tool_input.get("command") or _tool_target_path(tool, tool_input)
    if detail == "?":
        detail = json.dumps(tool_input)[:300]
    return tool, str(detail)[:800]


def ask_permission(job_id, chat_id, thread_id, payload):
    """Gate one tool call (a PreToolUse hook) on an Allow/Deny tap in
    Telegram, blocking the job until it comes. Returning no decision on
    timeout is deliberate: the call then goes back to Claude Code's normal
    permission flow rather than being silently denied."""
    tool, detail = _tool_summary(payload)
    event = threading.Event()
    with PERMISSIONS_LOCK:
        request_id = _random_id(lambda candidate: candidate in PERMISSIONS)
        PERMISSIONS[request_id] = {"event": event, "decision": None}
    reply(
        chat_id,
        f"🔐 Job {job_id} wants to run {tool}:\n{detail}",
        thread_id,
        buttons=[("✅ allow", f"/allow {request_id}"), ("⛔ deny", f"/deny {request_id}")],
        columns=2,
    )
    event.wait(PERMISSION_ASK_TIMEOUT)
    with PERMISSIONS_LOCK:
        decision = (PERMISSIONS.pop(request_id, None) or {}).get("decision")
    if not decision:
        reply(chat_id, f"⌛ Job {job_id}: no answer, handing the {tool} call back to Claude Code's own permission flow.", thread_id)
        return {}
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": f"{decision} from Telegram",
    }}


def answer_permission(request_id, decision):
    with PERMISSIONS_LOCK:
        pending = PERMISSIONS.get(request_id)
        if pending is None:
            return False
        pending["decision"] = decision
    pending["event"].set()
    return True


def handle_hook(payload):
    """Dispatch one hook POST from a background job. Everything is routed by
    session id: the job's row in JOBS is what says which Telegram chat and
    thread it belongs to, so this keeps working for jobs started before the
    last controller restart."""
    event = payload.get("hook_event_name")
    session_id = payload.get("session_id") or ""
    if not session_id:
        return {}
    with JOB_START_LOCK:
        row = JOBS.by_agent(session_id)
    if row is None:
        return {}
    job_id, chat_id, thread_id, prompt, status = row
    thread_id = _thread_of(thread_id)
    JOBS.set_session(job_id, session_id)
    # A /task job's session is the conversation's session from now on, so a
    # later /ask continues from the work the job just did.
    CLAUDE.adopt_session(thread_id, session_id)
    if event == "PostToolUse":
        name = payload.get("tool_name")
        if name not in FILE_EDIT_TOOLS:
            return {}
        tool_input = payload.get("tool_input") or {}
        path = _tool_target_path(name, tool_input)
        added, removed, preview = _edit_stats(name, tool_input)
        header = f"✏️ {FILE_EDIT_TOOLS[name]} {path} (+{added} -{removed})"
        if preview and added + removed < DIFF_PREVIEW_MAX_LINES:
            reply(chat_id, header + "\n" + "\n".join(preview), thread_id)
        else:
            reply(chat_id, header, thread_id)
        return {}
    if event == "Stop":
        if status != "running":
            return {}
        result = str(payload.get("last_assistant_message") or "(empty response)")
        JOBS.finish(job_id, "completed", result)
        reply(chat_id, f"✅ Background job {job_id} finished:\n{result}", thread_id)
        return {}
    if event == "SessionEnd":
        # Only reached when the session died without completing a turn --
        # a normal finish has already flipped the row to "completed" above.
        if status != "running":
            return {}
        reason = str(payload.get("reason") or "session ended")
        JOBS.finish(job_id, "failed", reason)
        reply(chat_id, f"❌ Background job {job_id} ended without finishing: {reason}", thread_id)
        return {}
    if event == "PreToolUse":
        return ask_permission(job_id, chat_id, thread_id, payload)
    if event == "PermissionRequest":
        # Claude Code decided this call needs a human, and ignores whatever a
        # PermissionRequest hook answers (2.1.x), so all this can do is say
        # where the job got stuck. TELEGRAM_CLAUDE_CONFIRM_TOOLS is the way
        # to get a decision that actually lands (see ask_permission).
        tool, detail = _tool_summary(payload)
        reply(chat_id, f"🔐 Job {job_id} is blocked: Claude Code wants your permission for {tool}:\n{detail}\nAnswer it on the server with `claude attach {(session_id or '')[:8]}`, or /cancel {job_id}.", thread_id)
        return {}
    return {}


class HookRequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            payload = {}
        try:
            answer = handle_hook(payload)
        except Exception as error:
            print(f"telegram-claude-control: hook failed: {error}", file=sys.stderr, flush=True)
            answer = {}
        body = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def address_string(self):
        return "hook"

    def log_message(self, *args):
        pass


class HookServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """HTTP over an AF_UNIX socket. Threaded because a confirmation hook parks
    its connection for as long as the Allow/Deny buttons go unanswered, and
    other jobs must keep reporting in meanwhile."""

    address_family = socket.AF_UNIX
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind() would try to resolve the "host" of the
        # bound address, which for AF_UNIX is a filesystem path.
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = 0


def start_hook_server(path=HOOK_SOCKET_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # A socket file left behind by a killed controller would make bind()
    # fail; the unit is single-instance, so the stale one is always ours.
    if os.path.exists(path):
        os.unlink(path)
    server = HookServer(path, HookRequestHandler)
    os.chmod(path, 0o600)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def agents_state():
    """{agent_id: session dict} from `claude agents --json --all`, Claude
    Code's own view of its background sessions (state, cwd, session id).
    Empty on any failure -- it is only ever used to enrich or reconcile what
    JOBS already knows."""
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "agents", "--json", "--all"],
            capture_output=True, text=True, timeout=60,
        )
        # Tolerate anything a wrapper script may print before the array.
        start = result.stdout.index("[")
        rows = json.loads(result.stdout[start:])
    except Exception as error:
        print(f"telegram-claude-control: claude agents failed: {error}", file=sys.stderr, flush=True)
        return {}
    return {row["id"]: row for row in rows if isinstance(row, dict) and row.get("id")}


def start_job(chat_id, thread_id, prompt):
    """Dispatch prompt as a `claude --bg` background session. Returns
    (job_id, agent_id); raises on failure. The session is owned by Claude
    Code's daemon, so it keeps running (and keeps reporting to Telegram
    through the hooks) across a restart of this controller."""
    prefix, cwd = CLAUDE.command_prefix(thread_id)
    with JOB_START_LOCK:
        job_id = _random_id(lambda candidate: JOBS.get(candidate) is not None)
        args = [CLAUDE_BIN, "--bg", prompt, "--settings", HOOK_SETTINGS_PATH, "--name", f"telegram {job_id}"] + prefix
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip()[:1500] or f"claude --bg exited {result.returncode}")
        # "backgrounded · 7c5dcf5d · <name>" -- the short id is also the
        # first 8 characters of the session uuid the hooks report.
        match = re.search(r"backgrounded\s+\S\s+([0-9a-f]{6,})", result.stdout)
        if match is None:
            raise RuntimeError((result.stdout or result.stderr).strip()[:1500] or "claude --bg printed no session id")
        agent_id = match.group(1)
        JOBS.start(job_id, chat_id, thread_id, prompt, agent_id)
    return job_id, agent_id


def reconcile_jobs():
    """Called at startup: settle every row still marked "running". A job whose
    session is alive is simply re-adopted -- its hooks will find it again
    now that the socket is back. One that finished while the controller was
    down is closed out (its Stop hook had nowhere to post), and one Claude
    Code no longer knows about is flagged interrupted, as before. Returns
    the messages to send to Telegram."""
    rows = JOBS.running()
    if not rows:
        return []
    agents = agents_state()
    messages = []
    for job_id, chat_id, thread_id, prompt, agent_id in rows:
        agent = agents.get(agent_id) if agent_id else None
        state = (agent or {}).get("state")
        if state in ("working", "blocked"):
            continue
        if state in ("done", "failed", "stopped"):
            status = "completed" if state == "done" else "failed"
            JOBS.finish(job_id, status, f"({state} while the controller was down)")
            icon = STATUS_ICONS.get(status, "?")
            messages.append((chat_id, _thread_of(thread_id), f"{icon} Background job {job_id} {state} while the controller was restarting.\nPrompt: {prompt[:800]}\nFull output: claude logs {agent_id}"))
            continue
        JOBS.finish(job_id, "interrupted", "(session no longer known to claude)")
        messages.append((chat_id, _thread_of(thread_id), f"⚠️ Background job {job_id} was lost (its background session is gone).\nPrompt: {prompt[:800]}"))
    return messages


class TelegramApi:
    """Small keep-alive Telegram client; one instance per request lane."""

    def __init__(self):
        self.connection = None
        self.lock = threading.Lock()

    def _connect(self):
        if self.connection is None:
            self.connection = http.client.HTTPSConnection(API_HOST, timeout=40)
        return self.connection

    def _discard_connection(self):
        if self.connection is not None:
            self.connection.close()
        self.connection = None

    def call(self, method, payload=None):
        data = urllib.parse.urlencode(payload or {}).encode()
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(data)),
            "Connection": "keep-alive",
        }
        # A server may close an idle keep-alive connection. Reconnect once so
        # that an idle bot does not fail its first reply. A 409 means another
        # getUpdates call briefly overlapped this one (e.g. right after a
        # restart) -- also worth one retry rather than surfacing as an error.
        with self.lock:
            for attempt in range(2):
                try:
                    connection = self._connect()
                    connection.request("POST", API_PREFIX + method, data, headers)
                    response = connection.getresponse()
                    body = response.read()
                    if response.status == 409 and not attempt:
                        self._discard_connection()
                        continue
                    if response.status >= 400:
                        raise RuntimeError(f"Telegram API returned HTTP {response.status}")
                    result = json.loads(body)
                    if not result.get("ok"):
                        raise RuntimeError(result.get("description", "Telegram API request failed"))
                    return result["result"]
                except (http.client.HTTPException, OSError):
                    self._discard_connection()
                    if attempt:
                        raise


# Keep long polling separate: an in-flight getUpdates request must never delay
# acknowledgements or final replies from worker threads.
POLL_API = TelegramApi()
SEND_API = TelegramApi()


def api(method, payload=None):
    client = POLL_API if method == "getUpdates" else SEND_API
    return client.call(method, payload)


def _friendly_claude_error(error):
    """Best-effort translation of a raw claude -p failure into short,
    Telegram-friendly text -- recognized failure classes (rate limits,
    overload) get a plain explanation instead of a raw stack/API dump."""
    text = str(error).strip()
    lowered = text.lower()
    if "429" in text or "rate limit" in lowered:
        return "Claude's rate limit was hit (HTTP 429). No reply was produced -- please try again later."
    if "usage limit" in lowered:
        return "Claude's usage limit was reached. Please try again later."
    if "529" in text or "overloaded" in lowered:
        return "Claude's API is overloaded right now. Please try again shortly."
    return text or "claude -p request failed."


class TypingIndicator:
    """Keeps a chat's "typing..." indicator up for the duration of a turn:
    Telegram only shows it for a few seconds per call, so this refreshes it
    on a timer (and on demand, e.g. per completed tool call) until stop()."""

    INTERVAL = 4

    def __init__(self, chat_id, thread_id):
        self.chat_id = chat_id
        self.thread_id = thread_id
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def ping(self):
        try:
            payload = {"chat_id": self.chat_id, "action": "typing"}
            if self.thread_id is not None:
                payload["message_thread_id"] = self.thread_id
            api("sendChatAction", payload)
        except Exception as error:
            print(f"telegram-claude-control: typing indicator failed: {error}", file=sys.stderr, flush=True)

    def _run(self):
        while not self._stop.wait(self.INTERVAL):
            self.ping()

    def __enter__(self):
        self.ping()
        self._thread.start()
        return self

    def __exit__(self, *exc_info):
        self._stop.set()
        self._thread.join(timeout=1)


# Guards both the on-disk state file and the in-memory `sessions` dict it
# mirrors, so a background `claude -p` turn finishing (mutating sessions) can
# never race the main loop's per-update write_state() into a corrupt file or
# a "dictionary changed size during iteration" error.
STATE_LOCK = threading.Lock()


def read_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return {"offset": 0}


def write_state(state):
    with STATE_LOCK:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        temporary = STATE_PATH + ".tmp"
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(state, file)
        os.chmod(temporary, 0o600)
        os.replace(temporary, STATE_PATH)


def tmux(*args, input_text=None):
    return subprocess.run(["tmux", *args], input=input_text, text=True, capture_output=True, timeout=15)


def screen(lines=120, target=None):
    result = tmux("capture-pane", "-p", "-J", "-S", f"-{lines}", "-t", target or SESSION)
    if result.returncode:
        return "tmux session is unavailable: " + result.stderr.strip()
    output = result.stdout.strip() or "(terminal is blank)"
    # Telegram messages are capped at 4096 characters.
    return output[-3800:]


def list_tmux_sessions():
    result = tmux("list-sessions", "-F", "#{session_name}")
    return [line for line in result.stdout.splitlines() if line] if result.returncode == 0 else []


def list_tmux_windows(session):
    """[(window_target, window_name), ...] for every window in session."""
    result = tmux("list-windows", "-t", session, "-F", "#{session_name}:#{window_index} #{window_name}")
    if result.returncode:
        return []
    pairs = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        target, _, name = line.partition(" ")
        pairs.append((target, name or target))
    return pairs


def list_tmux_panes(window_target):
    """[(pane_target, pane_title), ...] for every pane in window_target."""
    result = tmux("list-panes", "-t", window_target, "-F", "#{session_name}:#{window_index}.#{pane_index} #{pane_title}")
    if result.returncode:
        return []
    pairs = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        target, _, title = line.partition(" ")
        pairs.append((target, title or target))
    return pairs


def screen_entry():
    """(text, buttons) listing every pane across the whole tmux server as a
    button -- always, even if there's only one -- so tapping a screen is
    always exactly one tap away from its content, with no auto-skipped
    levels to cause surprises. callback_data is "/screen_show <target>"."""
    buttons = []
    for session in list_tmux_sessions():
        for window_target, _ in list_tmux_windows(session):
            for pane_target, pane_title in list_tmux_panes(window_target):
                label = f"{pane_target} — {pane_title}" if pane_title and pane_title != pane_target else pane_target
                buttons.append((label[:60], f"/screen_show {pane_target}"))
    if not buttons:
        return "tmux is unavailable: no panes found.", None
    return "Pick a screen:", buttons


def restart_controller():
    """Asks systemd to restart this service's unit and returns an error
    string, or None on success. --no-block makes systemctl return as soon as
    the job is queued instead of waiting for the stop+start cycle to finish
    -- which would otherwise mean waiting on systemd to SIGTERM this very
    process before the call could ever return."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "--no-block", "restart", UNIT_NAME],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as error:
        return str(error)
    if result.returncode != 0:
        return (result.stderr or result.stdout).strip() or f"systemctl exited {result.returncode}"
    return None


def delayed_screen(chat_id, thread_id):
    """Give an interactive Claude Code session time to respond, without
    pausing Telegram polling."""
    time.sleep(20)
    try:
        reply(chat_id, screen(lines=30), thread_id)
    except Exception as error:
        print(f"telegram-claude-control: delayed reply failed: {error}", file=sys.stderr, flush=True)


def run_ask(chat_id, thread_id, prompt):
    """Run a headless `claude -p` request on its own thread, relaying one
    Telegram message per edited file as the turn progresses. A "typing..."
    indicator stays up for the whole turn so a slow reply doesn't look like
    a dropped message, refreshed on each completed tool call."""
    with TypingIndicator(chat_id, thread_id) as typing:
        def notify(text):
            typing.ping()
            reply(chat_id, text, thread_id)

        try:
            answer = CLAUDE.run(prompt, conversation_id=thread_id, notify=notify)
            reply(chat_id, answer, thread_id)
        except Exception as error:
            print(f"telegram-claude-control: headless request failed: {error}", file=sys.stderr, flush=True)
            reply(chat_id, _friendly_claude_error(error), thread_id)


INBOX = None  # the agent_inbox module, loaded when a topic is configured
INBOX_TOPIC = ""


def load_agent_inbox(path=None):
    import importlib.machinery
    import importlib.util
    path = path or AGENT_INBOX_MODULE
    loader = importlib.machinery.SourceFileLoader("agent_inbox", path)
    spec = importlib.util.spec_from_loader("agent_inbox", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def inbox_prompt(topic, msg):
    """The turn prompt for one inbox message: who sent it, then the text."""
    sender = msg.get("sender") or "an unnamed agent"
    reply_to = msg.get("reply_to")
    where = (f"Your final answer is posted back to agent-inbox topic {reply_to}."
             if reply_to else "The sender gave no reply topic: only the operator sees your answer.")
    refs = msg.get("refs") or []
    body = str(msg["text"]).strip() + ("\n\nrefs:\n" + "\n".join(f"- {r}" for r in refs) if refs else "")
    return (f"[Message from agent {sender} (Unix user {msg.get('poster', '?')}) via agent-inbox topic "
            f"{topic}, posted {msg.get('posted_at', '?')}. It was not typed by the operator. {where}]\n\n{body}")


def inbox_conversation(msg):
    """One headless conversation per sending agent, apart from the operator's."""
    return f"inbox:{msg.get('sender') or msg.get('poster') or '?'}"


def handle_inbox_message(path, msg, state):
    """Run one inbox message as a headless turn, copy it to the paired chat,
    post the answer to the sender's reply topic, then ack it (a controller
    that dies mid-turn runs it again: at-least-once)."""
    chat_id = state.get("chat_id")
    sender, poster, reply_to = msg.get("sender") or "?", msg.get("poster", "?"), msg.get("reply_to")
    first = str(msg["text"]).strip().splitlines()[0][:200]
    if chat_id:
        reply(chat_id, f"📬 Inbox {INBOX_TOPIC}: {sender} (user {poster})"
                       + (f", answer to {reply_to}" if reply_to else "") + f"\n{first}")
    try:
        answer = CLAUDE.run(inbox_prompt(INBOX_TOPIC, msg), conversation_id=inbox_conversation(msg))
    except Exception as error:
        print(f"telegram-claude-control: inbox request failed: {error}", file=sys.stderr, flush=True)
        answer = f"Request failed: {_friendly_claude_error(error)}"
    if chat_id:
        reply(chat_id, f"📬 Answer to {sender}:\n{answer}")
    if reply_to:
        try:
            INBOX.post(reply_to, answer or "(empty answer)", sender=INBOX_TOPIC, kind="peer", reply_to=INBOX_TOPIC)
        except Exception as error:
            print(f"telegram-claude-control: inbox reply to {reply_to} failed: {error}", file=sys.stderr, flush=True)
    INBOX.ack(path)


def take_inbox(state):
    """Run every pending inbox message, oldest first."""
    for path in INBOX.pending(INBOX_TOPIC):
        try:
            msg = INBOX.read(path)
        except (OSError, ValueError) as error:
            print(f"telegram-claude-control: inbox: skipped {path.name}: {error}", file=sys.stderr, flush=True)
            INBOX.ack(path)
            continue
        handle_inbox_message(path, msg, state)


def start_inbox(topic, state, peers=()):
    """Claim the topic (one consumer per topic) and run it in a thread."""
    global INBOX, INBOX_TOPIC
    module = load_agent_inbox()
    for change in module.open_topic(topic, list(peers)):  # ~/.agent-inbox/<topic>, ACLs for the peers
        print(f"agent inbox: {change}", file=sys.stderr, flush=True)
    lock = module.ConsumerLock(topic, "auto")
    lock.acquire()
    INBOX, INBOX_TOPIC = module, topic

    def loop():
        while True:
            try:
                take_inbox(state)
            except Exception as error:
                print(f"telegram-claude-control: inbox: {error}", file=sys.stderr, flush=True)
            time.sleep(AGENT_INBOX_POLL)

    threading.Thread(target=loop, name="agent-inbox", daemon=True).start()
    return lock


def start_task(chat_id, message_id, thread_id, prompt):
    """Start a /task job as a `claude --bg` session. Unlike /ask this has no
    timeout at all and is not owned by this process: the job keeps running
    (and keeps reporting edits, its result, and any permission prompt to
    this chat, through the hooks in HOOK_SETTINGS_PATH) even across a
    /restart of the controller."""
    busy = JOBS.running_for(thread_id)
    if busy:
        reply(chat_id, f"Background job {busy} is already running for this conversation. Check /tasks, or /cancel {busy}.", thread_id)
        return
    try:
        job_id, agent_id = start_job(chat_id, thread_id, prompt)
    except Exception as error:
        print(f"telegram-claude-control: /task failed: {error}", file=sys.stderr, flush=True)
        reply(chat_id, _friendly_claude_error(error), thread_id)
        return
    try:
        acknowledge(chat_id, message_id)
    except Exception as error:
        print(f"telegram-claude-control: reaction failed: {error}", file=sys.stderr, flush=True)
    reply(chat_id, f"Started background job {job_id} (claude session {agent_id}). You'll get a message here as it edits files, when it needs a permission, and when it finishes.", thread_id)


STATUS_ICONS = {"running": "🏃", "completed": "✅", "failed": "❌", "cancelled": "🚫", "interrupted": "⚠️"}
# `claude agents --json` state -> what to show for a row still marked running.
AGENT_STATES = {"working": "🏃 working", "blocked": "🔐 needs input", "done": "✅ done", "failed": "❌ failed", "stopped": "🚫 stopped"}


def human_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s" if seconds else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def tasks_summary():
    """(text, buttons) for the 5 most recent /task jobs -- text shows all 5
    at a glance, and each also gets a button (callback_data "/tasks
    <job_id>") that drills into job_detail()'s duration/prompt/result view,
    the same as tapping through /screen's picker. Rows still running are
    annotated with Claude Code's own live view of the session (working, or
    blocked on a permission prompt)."""
    rows = JOBS.recent(5)
    if not rows:
        return "No background jobs yet.", None
    agents = agents_state() if any(row[1] == "running" for row in rows) else {}
    now = time.time()
    lines = []
    buttons = []
    for job_id, status, prompt, created_at, finished_at, agent_id in rows:
        icon = STATUS_ICONS.get(status, "?")
        label = status
        if status == "running":
            label = AGENT_STATES.get((agents.get(agent_id) or {}).get("state"), status)
        duration = human_duration((finished_at or now) - created_at)
        lines.append(f"{icon} {job_id} ({label}, {duration}): {prompt[:80]}")
        buttons.append((f"{icon} {job_id}", f"/tasks {job_id}"))
    return "\n".join(lines), buttons


def job_detail(job_id):
    row = JOBS.get(job_id)
    if row is None:
        return f"No job {job_id}."
    job_id, chat_id, thread_id, prompt, status, created_at, finished_at, result, agent_id, session_id = row
    now = time.time()
    lines = [
        f"{STATUS_ICONS.get(status, '?')} {job_id} — {status}",
        f"Duration: {human_duration((finished_at or now) - created_at)}",
        f"Thread: {thread_id}",
        f"Session: {agent_id or '?'} (claude attach {agent_id or ''}, claude logs {agent_id or ''})",
        f"Prompt: {prompt[:800]}",
    ]
    if status == "running":
        state = (agents_state().get(agent_id) or {}).get("state")
        lines.insert(1, f"Live state: {AGENT_STATES.get(state, state or 'unknown to claude')}")
    if result:
        lines.append(f"Last message: {result[:1500]}")
    return "\n".join(lines)


def cancel_job(job_id):
    """Stop the job's background session with `claude stop`. Its conversation
    is kept by Claude Code either way, so the work so far stays resumable
    with a fresh /ask or /task on this Telegram thread."""
    row = JOBS.get(job_id)
    if row is None or row[4] != "running":
        return f"No running background job {job_id}."
    agent_id = row[8]
    try:
        result = subprocess.run([CLAUDE_BIN, "stop", agent_id], capture_output=True, text=True, timeout=60)
    except Exception as error:
        return f"Could not stop {job_id}: {error}"
    JOBS.finish(job_id, "cancelled", (result.stdout or result.stderr).strip()[:300])
    if result.returncode != 0:
        return f"Marked {job_id} cancelled, but claude stop failed: {(result.stderr or result.stdout).strip()[:300]}"
    return f"Cancelled {job_id}."


def list_projects():
    """Every direct subdirectory of PROJECTS_DIR that's a git repo with at
    least one remote configured -- a "project" selectable via /projects."""
    try:
        entries = sorted(os.listdir(PROJECTS_DIR))
    except OSError:
        return []
    projects = []
    for name in entries:
        path = os.path.join(PROJECTS_DIR, name)
        if not os.path.isdir(os.path.join(path, ".git")):
            continue
        try:
            result = subprocess.run(["git", "-C", path, "remote"], capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            continue
        if result.returncode == 0 and result.stdout.strip():
            projects.append(name)
    return projects


def projects_entry(conversation_id):
    """(text, buttons) listing every discovered project as a button
    (callback_data "/projects <name>"), plus one to reset to WORKSPACE.
    Shows which project (if any) is currently selected."""
    current = CLAUDE.get_project(conversation_id) or WORKSPACE
    projects = list_projects()
    if not projects:
        return f"No git projects with a remote found under {PROJECTS_DIR}.\nCurrent: {current}", None
    buttons = [(("🟢 " if name == CLAUDE.get_project(conversation_id) else "") + name, f"/projects {name}") for name in projects]
    buttons.append((f"(default: {WORKSPACE})", "/projects default"))
    return f"Current project: {current}\nPick one:", buttons


def usage_report():
    """Runs claude -p "/usage" -- a built-in Claude Code command
    (supportsNonInteractive) that reports current session/weekly usage
    against the account's plan limits. It's a local, no-token query (no
    --resume, no conversation state), so it's called directly rather than
    through ClaudeHeadless."""
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", "/usage", "--output-format", "json"],
            cwd=WORKSPACE,
            capture_output=True,
            text=True,
            timeout=USAGE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"/usage timed out after {USAGE_TIMEOUT}s"
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        return (result.stderr or result.stdout).strip()[:1500] or f"/usage exited {result.returncode} with no output"
    if payload.get("is_error"):
        return str(payload.get("result") or "/usage reported an error")
    return str(payload.get("result") or "(empty /usage response)")


def run_shell(command):
    """Runs `command` directly (not through tmux) and returns its combined
    stdout/stderr, unlike /tmux which types into the long-lived interactive
    session."""
    try:
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=WORKSPACE,
            capture_output=True,
            text=True,
            timeout=SH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"Command timed out after {SH_TIMEOUT}s"
    output = (result.stdout + result.stderr).strip() or "(no output)"
    # Telegram messages are capped at 4096 characters.
    return f"$ {command} (exit {result.returncode})\n{output}"[:4096]


def run_sh(chat_id, thread_id, command):
    """Run a /sh shell command on its own thread so it can't block Telegram
    polling for up to SH_TIMEOUT seconds."""
    try:
        reply(chat_id, run_shell(command), thread_id)
    except Exception as error:
        print(f"telegram-claude-control: /sh failed: {error}", file=sys.stderr, flush=True)
        reply(chat_id, f"/sh failed: {error}", thread_id)


def send_terminal(text):
    typed = tmux("send-keys", "-t", SESSION, "-l", text)
    entered = tmux("send-keys", "-t", SESSION, "Enter")
    return typed.returncode == 0 and entered.returncode == 0


def reply(chat_id, text, thread_id=None, buttons=None, columns=1):
    """buttons, if given, is [(label, callback_data), ...], packed columns
    per row (default: one per row -- wide labels like tmux target names
    need the room; short ones, e.g. /help's action buttons, look better
    packed two or three per row)."""
    # Plain text avoids Telegram markup interpretation of terminal output.
    payload = {"chat_id": chat_id, "text": text[:4096]}
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    if buttons:
        rows = [buttons[i:i + columns] for i in range(0, len(buttons), columns)]
        keyboard = [[{"text": label, "callback_data": data} for label, data in row] for row in rows]
        payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
    api("sendMessage", payload)


def register_bot_commands():
    """Registers BOT_COMMANDS with Telegram (setMyCommands) so every client
    shows a tappable "/" menu with descriptions, letting the user pick a
    command and just type its argument -- complements, but doesn't replace,
    /help's own inline buttons for the no-argument commands."""
    api("setMyCommands", {"commands": json.dumps([{"command": name, "description": description} for name, description in BOT_COMMANDS])})


def acknowledge(chat_id, message_id):
    """Mark an accepted request without adding a separate chat message."""
    api("setMessageReaction", {
        "chat_id": chat_id,
        "message_id": message_id,
        "reaction": json.dumps([{"type": "emoji", "emoji": "\U0001fae1"}]),
    })


def start_ask(chat_id, message_id, thread_id, prompt):
    """Acknowledge promptly, then run the request asynchronously."""
    # A /task job owns this conversation's claude session for as long as it
    # runs, and two processes must not resume the same session at once.
    busy = JOBS.running_for(thread_id)
    if busy:
        reply(chat_id, f"Background job {busy} is running for this conversation. Wait for it, or /cancel {busy}.", thread_id)
        return
    try:
        acknowledge(chat_id, message_id)
    except Exception as error:
        print(f"telegram-claude-control: reaction failed: {error}", file=sys.stderr, flush=True)
    threading.Thread(target=run_ask, args=(chat_id, thread_id, prompt), daemon=True).start()


def start_sh(chat_id, message_id, thread_id, command):
    """Acknowledge promptly, then run the shell command asynchronously."""
    try:
        acknowledge(chat_id, message_id)
    except Exception as error:
        print(f"telegram-claude-control: reaction failed: {error}", file=sys.stderr, flush=True)
    threading.Thread(target=run_sh, args=(chat_id, thread_id, command), daemon=True).start()


def permitted(chat_id, state):
    return str(state.get("chat_id", "")) == str(chat_id)


def address_name():
    """The name the bot answers to in groups: ADDRESS_NAME if set (including
    empty, meaning "answer everything"), otherwise the bot's own first name."""
    if ADDRESS_NAME is not None:
        return ADDRESS_NAME.strip()
    return BOT_IDENTITY["first_name"].strip()


def addressed(message, text):
    """Is this group message aimed at the bot?

    True when it opens with the bot's name, @mentions the bot anywhere, or
    replies to one of the bot's own messages. Returns the prompt with a
    leading name or mention stripped, so Claude sees "status?" rather than
    "claude, status?".

    Returns (False, None) when the message is two humans talking to each other.
    """
    reply_to = message.get("reply_to_message") or {}
    if reply_to.get("from", {}).get("id") == BOT_IDENTITY["id"] and BOT_IDENTITY["id"]:
        return True, text

    username = BOT_IDENTITY["username"]
    if username:
        mention = re.compile(rf"@{re.escape(username)}\b", re.IGNORECASE)
        if mention.search(text):
            # Drop every mention of the bot, not just a leading one: "@bot ping"
            # and "ping @bot" should both arrive as "ping".
            return True, mention.sub("", text).strip()

    name = address_name()
    if not name:
        # Explicitly disabled: behave as before and treat all text as a prompt.
        return True, text

    # For a bot named "claude": "claude", "claude:", "claude,", "hey claude --
    # ..." all count, as long as the name is a whole word near the start.
    # Requiring it at the start (after an optional greeting) keeps "ask claude
    # about it later" from being read as an instruction to the bot.
    opener = re.compile(
        rf"^(?:hey|hi|hello|ok|okay|yo)?[\s,]*{re.escape(name)}\b[\s,:;\-–—]*",
        re.IGNORECASE,
    )
    stripped = opener.sub("", text, count=1)
    if stripped != text:
        return True, stripped.strip() or text.strip()
    return False, None


# Bare one-letter shortcuts (no argument).
ONE_LETTER_SHORTCUTS = {
    "h": "/help",
    "s": "/status",
    "v": "/screen",
    "i": "/interrupt",
    "r": "/restart",
    "t": "/tasks",
    "a": "/model",
    "u": "/usage",
    "p": "/projects",
}
# One letter plus " <argument>".
PREFIXED_SHORTCUTS = {
    "t": "/task",
    "m": "/tmux",
    "x": "/sh",
    "c": "/ask",
    "a": "/model",
    "p": "/projects",
}


def expand_shortcut(command):
    """Expands one-letter shortcuts (h/s/v/i/r/t/m/x/c) to their full command
    form. Only exact matches trigger: a message that happens to start with
    one of these letters but isn't "<letter>" or "<letter> <rest>" (e.g. an
    ordinary sentence) falls through unchanged to the normal /ask prompt
    path."""
    if command in ONE_LETTER_SHORTCUTS:
        return ONE_LETTER_SHORTCUTS[command]
    if len(command) > 2 and command[1] == " " and command[0] in PREFIXED_SHORTCUTS:
        return f"{PREFIXED_SHORTCUTS[command[0]]} {command[2:]}"
    return command


def handle(message, state):
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text")
    if chat_id is None or not text:
        return
    # Telegram forum topics each carry a message_thread_id; keying both the
    # Claude session and the reply's destination on it lets separate topics
    # hold separate, independently-resumed conversations instead of sharing
    # one Claude session and one reply thread across all of them.
    thread_id = message.get("message_thread_id")
    command = text.strip()
    if not state.get("chat_id"):
        if command.startswith("/pair ") and command[6:].strip() == CFG["PAIR_CODE"]:
            state["chat_id"] = chat_id
            reply(chat_id, "Paired. Send text for headless `claude -p`. Use /tmux <text> for the interactive terminal bridge. /help lists commands.", thread_id)
        else:
            reply(chat_id, "This private bot needs pairing. Send: /pair <your pairing code>", thread_id)
        return
    if not permitted(chat_id, state):
        # Do not reveal that a controller exists to unapproved chats.
        return
    command = expand_shortcut(command)
    if command in ("/start", "/help"):
        reply(
            chat_id,
            "Normal text or /ask <prompt>: headless `claude -p`, continued via --resume. "
            "You get one message per edited file as the turn runs, then a final summary. "
            "/newsession: forget the headless conversation and start fresh. "
            "/tmux <text>: terminal input into the interactive session, then last 30 lines after 20 seconds. "
            "/sh <command>: run a shell command directly and return its output. "
            "/task <prompt>: run it as a `claude --bg` background agent, with no timeout and hosted by "
            "Claude Code's own daemon, so it keeps running (and keeps reporting here) even across a /restart. "
            "You get a message per edited file, Allow/Deny buttons whenever it needs a permission, "
            "and its answer the moment it finishes (or fails). "
            "/tasks: the 5 most recent background jobs with their live state, each tappable for its "
            "duration/prompt/last message. /tasks <job_id>: same detail directly. "
            "/cancel <job_id>: stop a running one (its conversation is kept, /ask continues it). "
            "/restart: restart the controller's systemd service. "
            "/screen: every tmux pane on the server as a button, tap to see its content. "
            "/model: show/pick the Claude model for this conversation (sonnet/opus/haiku or any model id); "
            "/model default resets it. "
            "/projects: show/pick the project (a git repo with a remote, directly under "
            f"{PROJECTS_DIR}) used as this conversation's working directory; /projects default resets it. "
            "/usage: current session/weekly usage against your plan limits (free, instant, no tokens used). "
            "/status, /interrupt. "
            "One-letter shortcuts: h=help s=status v=screen i=interrupt r=restart t=tasks (`t <prompt>`=/task) "
            "a=model (`a <name>` selects) p=projects (`p <name>` selects) u=usage m <text>=/tmux x <cmd>=/sh c <prompt>=/ask. "
            "Tap a button below for the commands that need no arguments, or type / for Telegram's own "
            "command menu (with the rest, autocompleted so you can just add the argument).",
            thread_id,
            buttons=[
                ("usage", "/usage"), ("status", "/status"),
                ("screen", "/screen"), ("tasks", "/tasks"),
                ("model", "/model"), ("projects", "/projects"),
                ("interrupt", "/interrupt"), ("newsession", "/newsession"),
                ("restart", "/restart"),
            ],
            columns=2,
        )
    elif command == "/usage":
        reply(chat_id, usage_report(), thread_id)
    elif command == "/screen":
        text, buttons = screen_entry()
        reply(chat_id, text, thread_id, buttons=buttons)
    elif command.startswith("/screen_show "):
        reply(chat_id, screen(target=command[13:].strip()), thread_id)
    elif command == "/status":
        result = tmux("has-session", "-t", SESSION)
        reply(chat_id, f"tmux session '{SESSION}': " + ("available" if result.returncode == 0 else "unavailable"), thread_id)
    elif command == "/interrupt":
        result = tmux("send-keys", "-t", SESSION, "C-c")
        if result.returncode == 0:
            try:
                acknowledge(chat_id, message["message_id"])
            except Exception as error:
                print(f"telegram-claude-control: reaction failed: {error}", file=sys.stderr, flush=True)
        else:
            reply(chat_id, "Unable to reach tmux.", thread_id)
    elif command == "/tmux":
        reply(chat_id, "Usage: /tmux <text>", thread_id)
    elif command.startswith("/tmux "):
        if send_terminal(command[6:]):
            threading.Thread(target=delayed_screen, args=(chat_id, thread_id), daemon=True).start()
        else:
            reply(chat_id, "Unable to reach the tmux session.", thread_id)
    elif command == "/sh":
        reply(chat_id, "Usage: /sh <command>", thread_id)
    elif command.startswith("/sh "):
        shell_command = command[4:].strip()
        if shell_command:
            start_sh(chat_id, message["message_id"], thread_id, shell_command)
        else:
            reply(chat_id, "Usage: /sh <command>", thread_id)
    elif command == "/task":
        reply(chat_id, "Usage: /task <prompt>", thread_id)
    elif command.startswith("/task "):
        task_prompt = command[6:].strip()
        if task_prompt:
            start_task(chat_id, message["message_id"], thread_id, task_prompt)
        else:
            reply(chat_id, "Usage: /task <prompt>", thread_id)
    elif command == "/tasks":
        text, buttons = tasks_summary()
        reply(chat_id, text, thread_id, buttons=buttons)
    elif command.startswith("/tasks "):
        reply(chat_id, job_detail(command[7:].strip()), thread_id)
    elif command == "/cancel":
        reply(chat_id, "Usage: /cancel <job_id>", thread_id)
    elif command.startswith("/cancel "):
        reply(chat_id, cancel_job(command[8:].strip()), thread_id)
    elif command.startswith("/allow ") or command.startswith("/deny "):
        # Answers to the buttons on a background job's permission prompt;
        # the blocked hook is waiting on exactly this (see ask_permission).
        decision, _, request_id = command[1:].partition(" ")
        if answer_permission(request_id.strip(), "allow" if decision == "allow" else "deny"):
            reply(chat_id, f"{'✅ Allowed' if decision == 'allow' else '⛔ Denied'}, the job continues.", thread_id)
        else:
            reply(chat_id, "That permission request is no longer waiting (answered already, or timed out).", thread_id)
    elif command == "/restart":
        # Reply (a blocking network call) completes before systemd's SIGTERM
        # can land, so the acknowledgment always reaches Telegram -- even
        # though this same process is what's about to be killed. Flush the
        # already-advanced offset first too, otherwise a SIGTERM landing
        # before main()'s post-dispatch write_state() would leave this same
        # /restart update unconsumed on disk -- replayed on every boot into
        # an infinite self-restart loop (this is what tripped systemd's
        # start-limit before this fix).
        reply(chat_id, "Restarting controller…", thread_id)
        write_state(state)
        error = restart_controller()
        if error:
            reply(chat_id, f"Restart failed: {error}", thread_id)
    elif command == "/model":
        current = CLAUDE.get_model(thread_id) or "(CLI default)"
        buttons = [(name, f"/model {name}") for name in MODEL_PRESETS] + [("reset to default", "/model default")]
        reply(chat_id, f"Model for this conversation: {current}\nPick one, or send /model <any model id>:", thread_id, buttons=buttons)
    elif command.startswith("/model "):
        model_name = command[7:].strip()
        if model_name in ("default", "reset", "clear", ""):
            CLAUDE.set_model(thread_id, None)
            reply(chat_id, "Model reset to the CLI default for this conversation.", thread_id)
        else:
            CLAUDE.set_model(thread_id, model_name)
            reply(chat_id, f"Model set to {model_name} for this conversation (takes effect next turn).", thread_id)
    elif command == "/projects":
        text, buttons = projects_entry(thread_id)
        reply(chat_id, text, thread_id, buttons=buttons)
    elif command.startswith("/projects "):
        project_name = command[10:].strip()
        if project_name in ("default", "reset", "clear", ""):
            CLAUDE.select_project(thread_id, None)
            reply(chat_id, f"Project reset to {WORKSPACE}. Next message starts a new claude -p session there.", thread_id)
        elif project_name in list_projects():
            CLAUDE.select_project(thread_id, project_name)
            reply(chat_id, f"Project set to {os.path.join(PROJECTS_DIR, project_name)}. Next message starts a new claude -p session there.", thread_id)
        else:
            reply(chat_id, f"No project named {project_name}. Use /projects to list them.", thread_id)
    elif command == "/newsession":
        CLAUDE.reset(thread_id)
        reply(chat_id, "Headless conversation cleared. Next message starts a new claude -p session.", thread_id)
    elif command == "/ask":
        reply(chat_id, "Usage: /ask <prompt>", thread_id)
    elif command.startswith("/ask "):
        prompt = command[5:].strip()
        if prompt:
            start_ask(chat_id, message["message_id"], thread_id, prompt)
        else:
            reply(chat_id, "Usage: /ask <prompt>", thread_id)
    elif command.startswith("/"):
        reply(chat_id, "Unknown command. Use /help.", thread_id)
    elif message.get("chat", {}).get("type", "private") == "private":
        # Nobody else is in a one-to-one chat, so every message is for the bot.
        start_ask(chat_id, message["message_id"], thread_id, text)
    else:
        # In a group, stay quiet unless actually spoken to. Explicit /commands
        # above are always honoured -- typing one is addressing the bot.
        for_me, prompt = addressed(message, text)
        if for_me:
            start_ask(chat_id, message["message_id"], thread_id, prompt)


def handle_callback(callback_query, state):
    """An inline-button tap: acknowledge it (required so Telegram stops
    showing a loading spinner on the button), then replay its callback_data
    through the same handle() dispatcher a typed command would use -- e.g. a
    /screen picker button's callback_data is literally "/screen_show ..."."""
    try:
        api("answerCallbackQuery", {"callback_query_id": callback_query["id"]})
    except Exception as error:
        print(f"telegram-claude-control: answerCallbackQuery failed: {error}", file=sys.stderr, flush=True)
    message = callback_query.get("message") or {}
    if not message.get("chat"):
        return
    synthetic = {
        "chat": message["chat"],
        "message_id": message.get("message_id"),
        "message_thread_id": message.get("message_thread_id"),
        "text": callback_query.get("data", ""),
    }
    handle(synthetic, state)


def main():
    state = read_state()
    # Reuse the same dict object (not a copy) so mutations CLAUDE makes to it
    # are already reflected in the next write_state(state) call below.
    CLAUDE.sessions = state.setdefault("sessions", {})
    CLAUDE.models = state.setdefault("models", {})
    CLAUDE.projects = state.setdefault("projects", {})
    CLAUDE.on_change = lambda: write_state(state)
    try:
        # Establish the sender's TLS connection before the first user message,
        # making the immediate acknowledgement a reused connection. The reply
        # also carries the identity the group name-gate needs.
        me = api("getMe") or {}
        BOT_IDENTITY.update(
            username=me.get("username") or "",
            id=me.get("id"),
            first_name=me.get("first_name") or "",
        )
        if me.get("can_read_all_group_messages") is False:
            print(
                "telegram-claude-control: group privacy mode is ON -- in groups this bot "
                "only receives /commands, @mentions and replies to itself, so plain text "
                "will be silently ignored. Turn it off in BotFather (Bot Settings -> Group "
                "Privacy) and re-add the bot to the group.",
                file=sys.stderr,
                flush=True,
            )
    except Exception as error:
        print(f"telegram-claude-control: sender preflight failed: {error}", file=sys.stderr, flush=True)
    try:
        register_bot_commands()
    except Exception as error:
        print(f"telegram-claude-control: setMyCommands failed: {error}", file=sys.stderr, flush=True)
    # Before anything else: background jobs started by an earlier run of this
    # controller are still going, and their next hook must find the socket.
    write_hook_settings()
    start_hook_server()
    topic = CFG.get("AGENT_INBOX") or os.environ.get("TELEGRAM_CLAUDE_AGENT_INBOX", "")
    if topic:
        try:
            peers = [u for u in (CFG.get("AGENT_INBOX_PEERS") or os.environ.get("TELEGRAM_CLAUDE_AGENT_INBOX_PEERS", "")).split(",") if u.strip()]
            start_inbox(topic, state, [u.strip() for u in peers])
        except Exception as error:  # the Telegram side still works without it
            print(f"telegram-claude-control: agent inbox {topic} disabled: {error}", file=sys.stderr, flush=True)
    for chat_id, thread_id, text in reconcile_jobs():
        try:
            reply(chat_id, text, thread_id)
        except Exception as error:
            print(f"telegram-claude-control: startup job notice failed: {error}", file=sys.stderr, flush=True)
    while True:
        try:
            updates = api("getUpdates", {"offset": state.get("offset", 0), "timeout": 30, "allowed_updates": json.dumps(["message", "callback_query"])})
            for update in updates:
                state["offset"] = update["update_id"] + 1
                if "callback_query" in update:
                    handle_callback(update["callback_query"], state)
                else:
                    handle(update.get("message", {}), state)
                write_state(state)
        except KeyboardInterrupt:
            return
        except Exception as error:
            print(f"telegram-claude-control: {error}", file=sys.stderr, flush=True)
            time.sleep(5)


def check():
    """Validates the install without entering the polling loop. Returns True
    if every fatal check passed (tmux session availability is a warning
    only, matching install docs -- an idle bot may just not have one yet)."""
    ok = True

    def report(passed, message, fatal=True):
        nonlocal ok
        print(("ok   " if passed else ("FAIL " if fatal else "warn ")) + message)
        if fatal:
            ok = ok and passed

    try:
        mode = oct(os.stat(CONFIG_PATH).st_mode & 0o777)
        report(mode == "0o600", f"config file is mode {mode} (want 0o600): {CONFIG_PATH}")
    except OSError as error:
        report(False, f"config file: {error}")

    report(shutil.which("tmux") is not None, "tmux is on PATH")
    # /task hooks post their payload to the controller with curl.
    report(shutil.which("curl") is not None, "curl is on PATH (background-job hooks)")
    report(os.access(CLAUDE_BIN, os.X_OK) or shutil.which(CLAUDE_BIN) is not None, f"claude binary is executable: {CLAUDE_BIN}")
    # A sibling path, never HOOK_SOCKET_PATH itself: --check must not unlink
    # the socket a running controller's background jobs are reporting to.
    probe = HOOK_SOCKET_PATH + ".check"
    try:
        server = start_hook_server(probe)
        server.shutdown()
        server.server_close()
        os.unlink(probe)
        report(True, f"hook socket is bindable: {HOOK_SOCKET_PATH}")
    except Exception as error:
        report(False, f"hook socket {HOOK_SOCKET_PATH}: {error}")
    report(os.path.isdir(WORKSPACE), f"workspace directory exists: {WORKSPACE}")
    report(tmux("has-session", "-t", SESSION).returncode == 0, f"tmux session '{SESSION}' is available", fatal=False)

    try:
        api("getMe")
        report(True, "Telegram bot API connectivity (getMe)")
    except Exception as error:
        report(False, f"Telegram bot API connectivity: {error}")

    return ok


if __name__ == "__main__":
    if "--check" in sys.argv[1:]:
        sys.exit(0 if check() else 1)
    main()
