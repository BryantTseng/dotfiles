# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Kanban bridge: work with headless AI coding sessions through a kanban board.

Architecture:
- Board layer is pluggable via `providers.BoardProvider` (Planka v2 now; Trello/Linear later).
- Agent layer is pluggable via `providers.AgentProvider` (Agy with `--sandbox` now; Claude later).
- A card labelled with `BRIDGE_TRIGGER_LABEL` (default `agy`) starts a session in
  ~/workspace/kanban-tasks/task-<id>-<slug>/ (a git repo, one commit per run).
- The session/conversation ID is persisted on the card (invisible description tag on Planka).
- Your comments resume that session; replies are posted back as the bot user.
- Columns: To-Do until a run starts (⏳ Queued if slots full) -> Doing (30s progress) ->
  Need Action or Done (directly, without auto-audit). Dragging a card into Auditing triggers
  the read-only auditor -> Done (clean) or Flagged (problems found).
- The board is the source of truth: every 60s and at startup the bridge re-derives what
  each card needs from labels, metadata, and comments, so restarts lose nothing.
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import html
import http.client
import json
import logging
import os
import queue
import re
import secrets
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from providers import (
    AgentProvider,
    BoardProvider,
    Comment,
    Task,
    is_valid_proxy_token,
    load_agent_provider,
    load_board_provider,
)

ENV = dict(os.environ)

BOARD_NAME = ENV.get("BRIDGE_BOARD_PROVIDER", "planka")
AGENT_NAME = ENV.get("BRIDGE_AGENT_PROVIDER", "agy")
SECRET = ENV["WEBHOOK_SECRET"]
PORT = int(ENV.get("BRIDGE_PORT", "8787"))

TASKS_ROOT = Path(ENV.get("BRIDGE_TASKS_ROOT", "~/workspace/kanban-tasks")).expanduser()
STATE_ROOT = Path(ENV.get("BRIDGE_STATE_ROOT", "~/.local/share/kanban-bridge")).expanduser()
LOGS_DIR = STATE_ROOT / "logs"

RUN_TIMEOUT = int(ENV.get("BRIDGE_RUN_TIMEOUT", "3600"))
MAX_CONCURRENT = int(ENV.get("BRIDGE_MAX_CONCURRENT", "2"))

TRIGGER_LABEL = ENV.get("BRIDGE_TRIGGER_LABEL", "agy")
PLAN_FIRST_LABEL = ENV.get("BRIDGE_PLAN_FIRST_LABEL", "plan-first")
DIRECT_LABEL = "direct"
WORKTREE_LABEL = "worktree"
INPLACE_LABEL = "in-place"
SCRATCH_LABEL = "scratch"
REPO_LABEL_PREFIX = "repo:"
SESSION_PREFIX = "session:"
SESSION_COLOR = "9e9e9e"
INTAKE_HEAD = "⚙️ Configure this run"
REQUIRE_INTAKE = ENV.get("BRIDGE_REQUIRE_INTAKE", "1") == "1"

# Dynamic repo discovery: scans BRIDGE_REPOS_ROOT/*/.git (isolated from ~/workspace)
# plus optional BRIDGE_REPOS overrides.
REPOS_ROOT = Path(ENV.get("BRIDGE_REPOS_ROOT", "~/workspace/kanban-bridge/repos")).expanduser()


def _parse_repos(raw: str) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        name, path_str = entry.split("=", 1)
        name = name.strip().lower()
        if name:
            out[name] = Path(path_str.strip()).expanduser().resolve()
    return out


EXPLICIT_REPOS: dict[str, Path] = _parse_repos(ENV.get("BRIDGE_REPOS", ""))
SHARE_CARGO_TARGET = ENV.get("BRIDGE_SHARE_CARGO_TARGET", "1") == "1"
_repo_locks: dict[str, threading.Lock] = {}
_repo_locks_guard = threading.Lock()


def discover_repos() -> dict[str, Path]:
    """Return available git repos under REPOS_ROOT plus any explicit BRIDGE_REPOS."""
    out: dict[str, Path] = {}
    if REPOS_ROOT.is_dir():
        for child in sorted(REPOS_ROOT.iterdir()):
            if child.name.startswith("."):
                continue
            if child.is_dir() and (child / ".git").exists():
                out[child.name.lower()] = child.resolve()
    out.update(EXPLICIT_REPOS)
    return out


def _repo_lock(repo_name: str) -> threading.Lock:
    with _repo_locks_guard:
        if repo_name not in _repo_locks:
            _repo_locks[repo_name] = threading.Lock()
        return _repo_locks[repo_name]


# Placeholder comments start with ⏳ so a restarted bridge can find interrupted ones.
PH = "⏳"
PH_QUEUED = f"{PH} _Queued: waiting for a free slot ({MAX_CONCURRENT} runs at a time)…_"
PH_WORKING = f"{PH} _Working on it…_"
PH_AUDIT = f"{PH} **🔍 Audit**: _reviewing…_"
LEGACY_PLACEHOLDERS = ("Working on it…", "🔍 Audit: reviewing…")

# Board column names (lower-cased)
BUCKET_PLANNING = ENV.get("BRIDGE_PLANNING_BUCKET", "planning").strip().lower()
BUCKET_DOING = "doing"
BUCKET_NEEDS_ACTION = "need action"
BUCKET_PENDING = ENV.get("BRIDGE_PENDING_BUCKET", "pending").strip().lower()
BUCKET_AUDITING = ENV.get("BRIDGE_AUDIT_BUCKET", "auditing").strip().lower()
BUCKET_FLAGGED = "flagged"

# Optional auth proxy (used only by containerized providers that set needs_auth_proxy=True)
PROXY_PORT = int(ENV.get("BRIDGE_PROXY_PORT", "8788"))
CLAUDE_OAUTH_TOKEN = ENV.get("CLAUDE_OAUTH_TOKEN")
UPSTREAM = "api.anthropic.com"

# Profile tool `ap` (usually a shell function wrapping ~/.cargo/bin/ap)
AP = ENV.get("AP_BIN") or shutil.which("ap") or str(Path("~/.cargo/bin/ap").expanduser())
HAVE_AP = Path(AP).exists() or bool(shutil.which(AP))

# Global cap on simultaneous agent processes (task runs and audits).
RUN_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT)

# Shutdown: first Ctrl-C / SIGTERM / code change sets SHUTDOWN (finish running jobs, start
# nothing new); second Ctrl-C sets FORCE (kill running jobs, mark interrupted, exit).
SHUTDOWN = threading.Event()
FORCE = threading.Event()
EXIT_RELOAD = 3  # tells run.sh to restart the bridge (code changed)

_active: dict[str, str] = {}  # task id -> "run" / "audit"
_active_lock = threading.Lock()
_procs: set = set()

BOARD: BoardProvider
AGENT: AgentProvider


class ShuttingDown(Exception):
    pass


def active_jobs() -> dict[str, str]:
    with _active_lock:
        return dict(_active)


log = logging.getLogger("bridge")


# ---------- board helpers ----------


def session_of(task: Task) -> str | None:
    for t in task.label_titles:
        if t.startswith(SESSION_PREFIX):
            return t[len(SESSION_PREFIX):]
    return None


def is_placeholder(comment: Comment) -> bool:
    text = comment.body_text
    for marker in ("🤖 Agy", "🤖 Claude", "🤖 Agent"):
        text = text.replace(marker, "")
    text = text.strip()
    return text.startswith(PH) or text in LEGACY_PLACEHOLDERS


def pending_user_comments(comments: list[Comment]) -> list[str]:
    """User comments posted after the bot's last comment: what still needs an answer."""
    pending: list[str] = []
    for c in reversed(comments):
        if BOARD.is_from_bot(c):
            break
        text = c.body_text.strip()
        if text:
            pending.append(text)
    return list(reversed(pending))


def wants_audit(task: Task) -> bool:
    """Audit only when the task has been explicitly moved into the Auditing column."""
    if AUDITED_LABEL in task.label_titles:
        return False
    return BOARD.current_bucket(task) == BUCKET_AUDITING


# ---------- task directory / repo worktree (git) ----------


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "task"


def resolve_repo(task: Task) -> tuple[str | None, Path | None]:
    """Match a card to a local git repo via `repo:<name|path>` label or matching board name."""
    lower_labels = {l.strip().lower() for l in task.label_titles}
    if SCRATCH_LABEL in lower_labels:
        return None, None
    repos = discover_repos()
    for lbl in task.label_titles:
        raw = lbl.strip()
        low = raw.lower()
        if low.startswith(REPO_LABEL_PREFIX):
            val = raw[len(REPO_LABEL_PREFIX):].strip()
            key = val.lower()
            if key in repos:
                return key, repos[key]
            p = Path(val).expanduser().resolve()
            if p.is_dir() and (p / ".git").exists():
                return p.name.lower(), p
            raise RuntimeError(
                f"Card requests `repo:{val}`, which was not found under {REPOS_ROOT} or BRIDGE_REPOS "
                f"(available: {', '.join(sorted(repos)) or 'none'})."
            )
    board_key = (task.project_name or "").strip().lower()
    if board_key and board_key in repos:
        return board_key, repos[board_key]
    return None, None


def workspace_mode_of(task: Task) -> str | None:
    """Return 'scratch', 'worktree', 'in-place', or None if not yet chosen."""
    low = {l.strip().lower() for l in task.label_titles}
    if SCRATCH_LABEL in low:
        return SCRATCH_LABEL
    if INPLACE_LABEL in low:
        return INPLACE_LABEL
    if WORKTREE_LABEL in low:
        return WORKTREE_LABEL
    return None


def execution_mode_of(task: Task) -> str | None:
    """Return 'plan-first', 'direct', or None if not yet chosen."""
    low = {l.strip().lower() for l in task.label_titles}
    if PLAN_FIRST_LABEL.lower() in low or BOARD.current_bucket(task) == BUCKET_PLANNING:
        return PLAN_FIRST_LABEL
    if DIRECT_LABEL in low:
        return DIRECT_LABEL
    return None


def needs_intake(task: Task) -> bool:
    """Return True when a new card hasn't explicitly chosen workspace mode and plan-first/direct."""
    if not REQUIRE_INTAKE or task.done or session_of(task) is not None:
        return False
    ws = workspace_mode_of(task)
    mode = execution_mode_of(task)
    if mode is None or ws is None:
        return True
    # If worktree/in-place was picked, a repo must also be selected
    if ws in (WORKTREE_LABEL, INPLACE_LABEL):
        try:
            rname, _ = resolve_repo(task)
        except RuntimeError:
            return True
        if not rname:
            return True
    return False


def has_intake_comment(comments: list[Comment]) -> Comment | None:
    for c in reversed(comments):
        if BOARD.is_from_bot(c) and INTAKE_HEAD in c.body_text:
            return c
    return None


def _default_base_ref(repo_path: Path) -> str:
    for ref in ("origin/main", "origin/master", "main", "master"):
        if git(repo_path, "rev-parse", "-q", "--verify", ref):
            return ref
    return "HEAD"


def ensure_task_workspace(task: Task) -> tuple[Path, dict | None]:
    """Return `(cwd, repo_ctx)`.

    - `repo:<name>` + `in-place`: runs directly inside `~/workspace/<repo>` on its current branch.
    - `repo:<name>` (+ `worktree` or default): creates/reuses an isolated `git worktree` on branch
      `kanban/<id-tail>-<slug>` off the repo's default branch.
    - `scratch` (or no repo): creates/reuses a standalone scratch git repo in `TASKS_ROOT`.
    """
    slug = _slug(task.title)
    ws_mode = workspace_mode_of(task)
    repo_name, repo_path = resolve_repo(task)
    if repo_name and repo_path:
        if not (repo_path / ".git").exists():
            raise RuntimeError(f"Configured repo `{repo_name}` is not a git repo: {repo_path}")
        if ws_mode == INPLACE_LABEL:
            branch = git(repo_path, "rev-parse", "--abbrev-ref", "HEAD") or "HEAD"
            base_ref = _default_base_ref(repo_path)
            return repo_path, {
                "name": repo_name,
                "repo_path": repo_path,
                "branch": branch,
                "base_ref": base_ref,
                "mode": INPLACE_LABEL,
            }
        with _repo_lock(repo_name):
            existing = sorted(TASKS_ROOT.glob(f"{repo_name}-task-{task.id}-*"))
            if existing and (existing[0] / ".git").exists():
                wt = existing[0]
                branch = git(wt, "rev-parse", "--abbrev-ref", "HEAD") or f"kanban/{task.id[-6:]}-{slug}"
                base_ref = _default_base_ref(repo_path)
                return wt, {
                    "name": repo_name,
                    "repo_path": repo_path,
                    "branch": branch,
                    "base_ref": base_ref,
                    "mode": WORKTREE_LABEL,
                }

            subprocess.run(
                ["git", "-C", str(repo_path), "fetch", "--prune", "origin"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            base_ref = _default_base_ref(repo_path)
            short_id = task.id[-6:] if len(task.id) > 6 else task.id
            branch = f"kanban/{short_id}-{slug}"
            wt = TASKS_ROOT / f"{repo_name}-task-{task.id}-{slug}"
            if git(repo_path, "rev-parse", "-q", "--verify", f"refs/heads/{branch}"):
                cmd = ["git", "-C", str(repo_path), "worktree", "add", str(wt), branch]
            else:
                cmd = ["git", "-C", str(repo_path), "worktree", "add", "-b", branch, str(wt), base_ref]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                raise RuntimeError(f"git worktree add failed: {res.stderr.strip() or res.stdout.strip()}")
            log.info("task %s: created worktree %s on branch %s (base %s)", task.id, wt, branch, base_ref)
            return wt, {
                "name": repo_name,
                "repo_path": repo_path,
                "branch": branch,
                "base_ref": base_ref,
                "mode": WORKTREE_LABEL,
            }

    existing = sorted(TASKS_ROOT.glob(f"task-{task.id}-*"))
    d = existing[0] if existing else TASKS_ROOT / f"task-{task.id}-{slug}"
    d.mkdir(parents=True, exist_ok=True)
    ensure_repo(d)
    return d, None


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            f"user.name={AGENT.name}",
            "-c",
            f"user.email={AGENT.name}@kanban.local",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
    ).stdout.strip()


def ensure_repo(cwd: Path) -> None:
    if (cwd / ".git").exists():
        return
    git(cwd, "init", "-q")
    git(cwd, "add", "-A")
    git(cwd, "commit", "-q", "--allow-empty", "-m", "start (files present before git tracking)")


def commit_run(cwd: Path, message: str) -> str | None:
    """Commit whatever the run changed; returns the short hash, or None if nothing changed."""
    git(cwd, "add", "-A")
    if not git(cwd, "status", "--porcelain"):
        return None
    git(cwd, "commit", "-q", "-m", message)
    return git(cwd, "rev-parse", "--short", "HEAD")


# ---------- prompt & status ----------

PLAN_APPROVE_RE = re.compile(
    r"^\s*(do it|proceed|go ahead|implement|start implementing|approved? to implement|lgtm\b.*implement)\b",
    re.I,
)


def is_plan_first(task: Task) -> bool:
    if any(l.strip().lower() == PLAN_FIRST_LABEL.lower() for l in task.label_titles):
        return True
    return BOARD.current_bucket(task) == BUCKET_PLANNING


def system_prompt(
    task: Task,
    granted: list[str],
    cwd: Path,
    repo_ctx: dict | None = None,
    plan_only: bool = False,
) -> str:
    profiles = ", ".join(readonly_aws_profiles()) or "(none found)"
    parts = [
        f"You are working on kanban card #{task.id} through a headless bridge. "
        "The user talks to you ONLY through card comments: your final message is posted as a "
        "comment on the card, so write it in concise, clear markdown. "
        "NEVER call interactive tools like `ask_question` or `ask_permission` (they cannot prompt "
        "in headless mode); instead, put clear numbered questions directly in your markdown reply "
        "and end with `STATUS: NEEDS_INPUT`."
    ]

    if repo_ctx:
        if repo_ctx.get("mode") == INPLACE_LABEL:
            loc_line = (
                f"You are working **in-place** inside repository `{repo_ctx['name']}` at `{cwd}` "
                f"(current branch: `{repo_ctx['branch']}`). Do NOT discard unrelated uncommitted user "
                "changes; create or switch to a feature branch before making commits. "
            )
        else:
            loc_line = (
                f"You are working inside an isolated `git worktree` of repository `{repo_ctx['name']}` "
                f"at `{cwd}` on feature branch `{repo_ctx['branch']}` (branched from `{repo_ctx['base_ref']}`). "
            )
        parts.append(
            loc_line + "ONLY modify files inside this repository directory.\n"
            "- Follow the repository's own `AGENTS.md` / `CLAUDE.md` / `GEMINI.md` rules (including "
            "multi-agent Dev Worker + Reviewer Gate workflows, linting, formatting, and test suites).\n"
            "- Make clean, logical git commits directly on your feature branch. "
            "NEVER commit to or push directly to `main` or `master`.\n"
            "- You may use rootless Docker for ephemeral integration test containers (e.g. Postgres), "
            "but you MUST stop and remove any ephemeral test containers before finishing.\n"
            "- When ready to push or open a PR with `gh pr create`: if `gh` is not yet granted below, "
            "finish your local commits and tests first, summarize results, and request `ACCESS_REQUEST: gh`.\n"
            "- In your final implementation comment, include a crisp summary with: **Branch**, "
            "**Tests** (exact commands + pass/fail counts), **PR URL** (if opened), and **Key Files / Spec**."
        )
    else:
        parts.append(
            f"Your working directory (`{cwd}`) is the standalone task directory and the ONLY place "
            "you may modify files. It is a git repository and the bridge commits your changes after "
            "each run, so do NOT run `git commit` or rewrite git history yourself."
        )

    if plan_only:
        parts.append(
            "**PLAN-FIRST / SPEC-ONLY MODE IS ACTIVE:**\n"
            "- Do NOT modify production code yet.\n"
            "- Inspect the codebase thoroughly, outline the recommended approach (and key alternatives/tradeoffs), "
            "write or update a design spec doc if helpful, and ask any clarifying decisions as a concise "
            "numbered list.\n"
            "- Wait for the user to answer your questions or reply `do it` / `proceed` before implementing.\n"
            "- End this turn with `STATUS: NEEDS_INPUT`."
        )

    parts.append(
        f"Access currently granted for this run: {', '.join(granted) or 'none'}.\n"
        "If you need access you don't have, explain why in your message and add a line\n"
        "ACCESS_REQUEST: <comma-separated items>\n"
        "before the status line. Items: gh (GitHub CLI as the user), azure (Azure CLI as the "
        f"user), aws:<profile> (read-only; profiles: {profiles}), aws-elevated:<profile> "
        "(write access, one run only; ask only when really needed). The user approves in a "
        "comment and the next run gets the credentials as environment variables / CLI config."
    )

    parts.append(
        "To populate or update the card's native Task List (checklist), you may include one or more "
        "`CHECKLIST:` blocks before the status line:\n"
        "CHECKLIST: <Checklist Name>\n"
        "- [x] Completed step\n"
        "- [ ] Remaining or manual step for the user (e.g. build/run container, configure secrets)\n"
        "Use this whenever there are multi-step milestones or remaining manual steps so the user sees "
        "progress right on the card face."
    )

    parts.append(
        "End your final message with exactly one status line on its own:\n"
        "STATUS: NEEDS_INPUT  (you asked a question, proposed a plan awaiting sign-off, requested access, or are blocked)\n"
        "STATUS: DONE         (your work for this run is finished; the bridge moves the card to Pending for user review — never Done)"
    )
    return "\n\n".join(parts)


STATUS_RE = re.compile(r"\n*\s*STATUS:\s*(NEEDS_INPUT|DONE)\s*$")
ACCESS_REQUEST_RE = re.compile(r"^\s*ACCESS_REQUEST:\s*(.+?)\s*$", re.M)
DONE_CMD_RE = re.compile(
    r"^\s*(?:please\s+)?(?:mark\s+(?:this\s+)?(?:card\s+)?(?:as\s+)?)?(?:move\s+(?:this\s+)?(?:card\s+)?to\s+)?(done|close)\s*[.!]?\s*$",
    re.I,
)
_CHECKLIST_HEAD_RE = re.compile(r"^\s*CHECKLIST:\s*(.+?)\s*$")
_CHECKLIST_ITEM_RE = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s+(.+?)\s*$")


def split_status(text: str) -> tuple[str, str]:
    """Strip the trailing status line; anything unclear counts as needing input."""
    m = STATUS_RE.search(text)
    if not m:
        return text, "NEEDS_INPUT"
    return text[: m.start()].rstrip(), m.group(1)


def extract_checklists(text: str) -> tuple[str, list[tuple[str, list[tuple[str, bool]]]]]:
    """Extract `CHECKLIST: <name>` blocks with `- [x]` / `- [ ]` items from an agent reply.

    Returns `(cleaned_text, [(list_name, [(item_text, is_completed), ...]), ...])`.
    """
    lines = text.splitlines()
    kept: list[str] = []
    checklists: list[tuple[str, list[tuple[str, bool]]]] = []
    i = 0
    while i < len(lines):
        hm = _CHECKLIST_HEAD_RE.match(lines[i])
        if hm:
            name = hm.group(1).strip()
            items: list[tuple[str, bool]] = []
            j = i + 1
            while j < len(lines):
                im = _CHECKLIST_ITEM_RE.match(lines[j])
                if im:
                    items.append((im.group(2).strip(), im.group(1).lower() == "x"))
                    j += 1
                elif not lines[j].strip() and j + 1 < len(lines) and _CHECKLIST_ITEM_RE.match(lines[j + 1]):
                    j += 1
                else:
                    break
            if items:
                checklists.append((name, items))
                i = j
                continue
        kept.append(lines[i])
        i += 1
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return cleaned, checklists


# ---------- access grants (approved by user in comments or confirm page) ----------

ACCESS_PREFIX = "access:"
ACCESS_COLOR = "7e57c2"
ACCESS_REQUEST_HEAD = "🔐 Access requested:"
APPROVE_RE = re.compile(r"^\s*(approve|deny)\b(.*)$", re.I | re.S)


def readonly_aws_profiles() -> list[str]:
    if not HAVE_AP:
        return [
            p.strip()
            for p in os.environ.get("BRIDGE_AWS_READONLY_PROFILES", "").split(",")
            if p.strip()
        ]
    try:
        out = subprocess.run(
            [AP, "list", "--readonly"],
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        ).stdout
    except Exception:
        return []
    return re.findall(r"^\s*READONLY\s+(?:PROD\s+)?(\S+)", out, re.M)


def aws_cmd(profile: str, elevated: bool, *args: str) -> list[str]:
    if HAVE_AP:
        return [AP, "exec", profile] + (["--elevated"] if elevated else []) + ["--", "aws", *args]
    return ["aws", "--profile", profile, *args]


def normalize_access_item(item: str) -> tuple[str | None, str | None]:
    item = item.strip().strip("`").lower()
    if item in ("gh", "azure"):
        return item, None
    kind, _, profile = item.partition(":")
    if kind == "aws" and profile:
        if profile not in readonly_aws_profiles():
            return None, f"`{item}`: not a READONLY profile (use `aws-elevated:{profile}` for write access)"
        return item, None
    if kind == "aws-elevated" and profile:
        return item, None
    return None, f"`{item}`: unknown access type"


def granted_access(task: Task) -> list[str]:
    return [t[len(ACCESS_PREFIX):] for t in task.label_titles if t.startswith(ACCESS_PREFIX)]


def latest_access_request(comments: list[Comment]) -> list[str]:
    for c in reversed(comments):
        if BOARD.is_from_bot(c) and ACCESS_REQUEST_HEAD in c.body_text:
            line = c.body_text.split(ACCESS_REQUEST_HEAD, 1)[1].splitlines()[0]
            return [i.strip().strip("`") for i in line.split(",") if i.strip()]
    return []


def apply_access_decisions(task: Task, comments: list[Comment], pending: list[str]) -> list[str]:
    notes: list[str] = []
    for text in pending:
        m = APPROVE_RE.match(text)
        if not m:
            continue
        action, rest = m.group(1).lower(), m.group(2)
        items = [i for i in re.split(r"[,\s]+", rest.strip()) if i] or latest_access_request(comments)
        if action == "deny":
            notes.append(
                f"The user DENIED access: {', '.join(items) or 'the request'}. "
                "Continue without it or explain what you can't do."
            )
            continue
        granted, errors = [], []
        for raw in items:
            item, err = normalize_access_item(raw)
            if err:
                errors.append(err)
            else:
                BOARD.add_label(task.id, ACCESS_PREFIX + item, ACCESS_COLOR)
                granted.append(item)
        if granted:
            notes.append(f"The user APPROVED access: {', '.join(granted)}. It is available in this run.")
        if errors:
            notes.append("These approvals were rejected by the bridge: " + "; ".join(errors))
        log.info("task %s: access granted %s, rejected %s", task.id, granted, errors)
    return notes


APPROVAL_BASE = ENV.get("BRIDGE_APPROVAL_BASE", f"http://localhost:{PORT}")


def sign_access(task_id: str, request_comment_id: str, action: str, items: list[str]) -> str:
    msg = f"{task_id}|{request_comment_id}|{action}|{','.join(items)}"
    return hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]


def access_link(task_id: str, request_comment_id: str, action: str, items: list[str]) -> str:
    q = urllib.parse.urlencode(
        {
            "t": task_id,
            "c": request_comment_id,
            "a": action,
            "i": ",".join(items),
            "s": sign_access(task_id, request_comment_id, action, items),
        }
    )
    return f"{APPROVAL_BASE}/access?{q}"


def access_request_block(task_id: str, request_comment_id: str, items: list[str]) -> str:
    links = [f"[✅ Approve all]({access_link(task_id, request_comment_id, 'approve', items)})"]
    if len(items) > 1:
        links += [
            f"[✅ Approve `{i}`]({access_link(task_id, request_comment_id, 'approve', [i])})"
            for i in items
        ]
    links.append(f"[❌ Deny]({access_link(task_id, request_comment_id, 'deny', items)})")
    return (
        f"\n\n---\n**{ACCESS_REQUEST_HEAD}** {', '.join(f'`{i}`' for i in items)}\n\n"
        + " · ".join(links)
        + "\n\n<sub>or reply `approve`, `approve <item>`, `deny`</sub>"
    )


def request_still_open(task_id: str, request_comment_id: str) -> bool:
    comments = BOARD.get_comments(task_id)
    if not comments or comments[-1].id != request_comment_id:
        return False
    return ACCESS_REQUEST_HEAD in comments[-1].body_text


def access_page(title: str, body: str, button: str | None = None) -> str:
    form = (
        f'<form method="post"><button type="submit">{html.escape(button)}</button></form>'
        if button
        else ""
    )
    return f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{html.escape(title)}</title>
<style>body{{font:16px system-ui;background:#1e2230;color:#e6e6e6;display:grid;place-items:center;
min-height:90vh;margin:0}}main{{max-width:28rem;padding:2rem;background:#272c3b;border-radius:12px}}
button{{font:inherit;padding:.6rem 1.2rem;border:0;border-radius:8px;background:#4f7cff;color:#fff;
cursor:pointer}}a{{color:#8fb0ff}}code{{background:#11141c;padding:.1rem .3rem;border-radius:4px}}</style>
<main><h2>{html.escape(title)}</h2>{body}{form}</main>"""


def handle_access_click(method: str, query: str) -> tuple[int, str]:
    q = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
    try:
        task_id, comment_id, action = q["t"], q["c"], q["a"]
        items = [i for i in q.get("i", "").split(",") if i]
    except KeyError:
        return 400, access_page("Bad link", "<p>This link is malformed.</p>")

    expected = sign_access(task_id, comment_id, action, items)
    if action not in ("approve", "deny") or not hmac.compare_digest(q.get("s", ""), expected):
        return 403, access_page("Invalid link", "<p>This link's signature doesn't match.</p>")

    back = f'<p><a href="{BOARD.task_url(task_id)}">Back to the task</a></p>'
    if not request_still_open(task_id, comment_id):
        return 409, access_page(
            "Already answered",
            "<p>This request was already answered or superseded.</p>" + back,
        )

    what = ", ".join(f"<code>{html.escape(i)}</code>" for i in items)
    if method == "GET":
        verb = "Grant" if action == "approve" else "Deny"
        return 200, access_page(
            f"{verb} access for task #{task_id}?",
            f"<p>{verb} {what} to {html.escape(AGENT.name)} for this task.</p>",
            button=f"{verb} access",
        )

    BOARD.post_user_comment(task_id, f"{action} {' '.join(items)}")
    enqueue(task_id)  # also kick immediately in case board is polling-only
    done = "Approved" if action == "approve" else "Denied"
    return 200, access_page(
        f"{done}: {', '.join(items)}",
        "<p>The agent will continue shortly. You can close this tab.</p>" + back,
    )


# ---------- pre-run intake (workspace mode + plan-first/direct) ----------

LABEL_COLORS = {
    WORKTREE_LABEL: "4f7cff",
    INPLACE_LABEL: "fb8c00",
    SCRATCH_LABEL: "9e9e9e",
    PLAN_FIRST_LABEL: "fbc02d",
    DIRECT_LABEL: "43a047",
}


def sign_setup(task_id: str) -> str:
    return hmac.new(SECRET.encode(), f"setup|{task_id}".encode(), hashlib.sha256).hexdigest()[:32]


def setup_link(task_id: str) -> str:
    q = urllib.parse.urlencode({"t": task_id, "s": sign_setup(task_id)})
    return f"{APPROVAL_BASE}/setup?{q}"


def intake_comment_text(task: Task) -> str:
    return (
        f"### {INTAKE_HEAD}\n\n"
        f"Choose workspace & execution mode before `{AGENT.name}` starts (0 tokens used):\n\n"
        f"👉 **[⚙️ Configure & Start]({setup_link(task.id)})**\n\n"
        "- **Target:** standalone `scratch` folder *(for new/unrelated tasks)*, or an existing repo with `worktree` *(isolated branch)* / `in-place`\n"
        "- **Mode:** `plan-first` *(spec & numbered Q&A before coding)* or `direct` *(implement right away)*\n\n"
        "<sub>or reply with keywords like `scratch plan-first`, `<repo> worktree plan-first`, or attach labels on this card</sub>"
    )


def apply_intake_selection(task: Task, repo_choice: str, custom_path: str, ws_kind: str, mode: str) -> str:
    """Apply target repo/scratch, worktree/in-place, and plan-first/direct labels."""
    repos = discover_repos()
    BOARD.remove_labels(
        task,
        lambda t: t.strip().lower() in (WORKTREE_LABEL, INPLACE_LABEL, SCRATCH_LABEL, PLAN_FIRST_LABEL.lower(), DIRECT_LABEL)
        or t.strip().lower().startswith(REPO_LABEL_PREFIX),
    )

    custom_path = (custom_path or "").strip()
    repo_choice = (repo_choice or "").strip().lower()

    if custom_path:
        p = Path(custom_path).expanduser().resolve()
        if not (p.is_dir() and (p / ".git").exists()):
            raise ValueError(f"Custom path is not a git repository: {custom_path}")
        if ws_kind not in (WORKTREE_LABEL, INPLACE_LABEL):
            ws_kind = WORKTREE_LABEL
        # Use short name if it lives in discover_repos, else full path
        matched = next((k for k, v in repos.items() if v == p), None)
        label_val = matched if matched else str(p)
        BOARD.add_label(task.id, f"{REPO_LABEL_PREFIX}{label_val}", "1e88e5")
        BOARD.add_label(task.id, ws_kind, LABEL_COLORS[ws_kind])
        ws_desc = f"{p.name} ({ws_kind})"
    elif not repo_choice or repo_choice == SCRATCH_LABEL:
        BOARD.add_label(task.id, SCRATCH_LABEL, LABEL_COLORS[SCRATCH_LABEL])
        ws_desc = "scratch"
    else:
        if repo_choice not in repos:
            raise ValueError(f"Unknown repository: {repo_choice!r}")
        if ws_kind not in (WORKTREE_LABEL, INPLACE_LABEL):
            ws_kind = WORKTREE_LABEL
        BOARD.add_label(task.id, f"{REPO_LABEL_PREFIX}{repo_choice}", "1e88e5")
        BOARD.add_label(task.id, ws_kind, LABEL_COLORS[ws_kind])
        ws_desc = f"{repo_choice} ({ws_kind})"

    if mode not in (PLAN_FIRST_LABEL, DIRECT_LABEL):
        raise ValueError(f"Invalid execution mode: {mode!r}")
    BOARD.add_label(task.id, mode, LABEL_COLORS[mode])
    return f"{ws_desc} · {mode}"


def apply_intake_reply(task: Task, pending: list[str]) -> list[str]:
    """Parse intake keywords from user comments on a new card; returns non-config comments."""
    repos = discover_repos()
    remaining: list[str] = []
    chosen_repo, _ = resolve_repo(task)
    chosen_ws = workspace_mode_of(task)
    chosen_mode = execution_mode_of(task)
    changed = False

    for text in pending:
        tokens = [t.strip("`,.;:()[]").lower() for t in re.split(r"\s+", text.strip()) if t.strip()]
        recognized = 0
        for tok in tokens:
            if tok == SCRATCH_LABEL:
                chosen_ws, chosen_repo, recognized = SCRATCH_LABEL, None, recognized + 1
            elif tok == WORKTREE_LABEL:
                chosen_ws, recognized = WORKTREE_LABEL, recognized + 1
            elif tok in (INPLACE_LABEL, "inplace"):
                chosen_ws, recognized = INPLACE_LABEL, recognized + 1
            elif tok in (PLAN_FIRST_LABEL.lower(), "plan", "plan-only", "spec"):
                chosen_mode, recognized = PLAN_FIRST_LABEL, recognized + 1
            elif tok in (DIRECT_LABEL, "implement", "code", "now"):
                chosen_mode, recognized = DIRECT_LABEL, recognized + 1
            elif tok.startswith(REPO_LABEL_PREFIX) and tok[len(REPO_LABEL_PREFIX):] in repos:
                chosen_repo = tok[len(REPO_LABEL_PREFIX):]
                recognized += 1
            elif tok in repos:
                chosen_repo = tok
                recognized += 1
        if recognized > 0:
            changed = True
            if recognized < len(tokens):
                remaining.append(text)
        else:
            remaining.append(text)

    if changed:
        if chosen_ws == SCRATCH_LABEL:
            BOARD.remove_labels(
                task,
                lambda t: t.strip().lower() in (WORKTREE_LABEL, INPLACE_LABEL)
                or t.strip().lower().startswith(REPO_LABEL_PREFIX),
            )
            BOARD.add_label(task.id, SCRATCH_LABEL, LABEL_COLORS[SCRATCH_LABEL])
        else:
            if chosen_repo:
                BOARD.remove_labels(
                    task,
                    lambda t: t.strip().lower() == SCRATCH_LABEL
                    or (t.strip().lower().startswith(REPO_LABEL_PREFIX) and t.strip().lower() != f"{REPO_LABEL_PREFIX}{chosen_repo}"),
                )
                BOARD.add_label(task.id, f"{REPO_LABEL_PREFIX}{chosen_repo}", "1e88e5")
            if chosen_ws in (WORKTREE_LABEL, INPLACE_LABEL):
                other = INPLACE_LABEL if chosen_ws == WORKTREE_LABEL else WORKTREE_LABEL
                BOARD.remove_labels(task, lambda t: t.strip().lower() in (other, SCRATCH_LABEL))
                BOARD.add_label(task.id, chosen_ws, LABEL_COLORS[chosen_ws])
        if chosen_mode in (PLAN_FIRST_LABEL, DIRECT_LABEL):
            other_m = DIRECT_LABEL if chosen_mode == PLAN_FIRST_LABEL else PLAN_FIRST_LABEL
            BOARD.remove_labels(task, lambda t: t.strip().lower() == other_m.lower())
            BOARD.add_label(task.id, chosen_mode, LABEL_COLORS[chosen_mode])
        log.info("task %s: applied intake reply (repo=%s ws=%s mode=%s)", task.id, chosen_repo, chosen_ws, chosen_mode)
    return remaining


def _guess_repo_from_text(task: Task, repos: dict[str, Path]) -> str | None:
    haystack = f"{task.title}\n{task.description_text}".lower()
    for rname in sorted(repos):
        if re.search(rf"\b{re.escape(rname)}\b", haystack):
            return rname
    return None


def handle_setup_click(method: str, query: str, post_body: bytes = b"") -> tuple[int, str]:
    q = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
    task_id = q.get("t", "")
    if not task_id or not hmac.compare_digest(q.get("s", ""), sign_setup(task_id)):
        return 403, access_page("Invalid link", "<p>This setup link's signature doesn't match.</p>")

    back = f'<p><a href="{BOARD.task_url(task_id)}">Back to the card</a></p>'
    task = BOARD.get_task(task_id)
    if session_of(task) is not None:
        return 409, access_page("Already started", "<p>This card already has an active session.</p>" + back)

    if method == "GET":
        repos = discover_repos()
        cur_repo, _ = resolve_repo(task)
        cur_ws = workspace_mode_of(task)
        cur_mode = execution_mode_of(task) or PLAN_FIRST_LABEL

        if cur_ws == SCRATCH_LABEL:
            default_repo = ""
        elif cur_repo:
            default_repo = cur_repo
        else:
            default_repo = _guess_repo_from_text(task, repos) or ""
        default_ws = cur_ws if cur_ws in (WORKTREE_LABEL, INPLACE_LABEL) else WORKTREE_LABEL

        repo_opts = [
            f'<option value=""{" selected" if not default_repo else ""}>None — new standalone scratch folder</option>'
        ]
        for rname, rpath in sorted(repos.items()):
            sel = " selected" if rname == default_repo else ""
            repo_opts.append(
                f'<option value="{html.escape(rname)}"{sel}>{html.escape(rname)} ({html.escape(str(rpath))})</option>'
            )

        ws_radios = []
        for kind, desc in [
            (WORKTREE_LABEL, "<strong>Isolated git worktree</strong> <small>(new branch, safe for parallel cards)</small>"),
            (INPLACE_LABEL, "<strong>In-place</strong> <small>(directly inside the repo directory)</small>"),
        ]:
            chk = " checked" if kind == default_ws else ""
            ws_radios.append(
                f'<label style="display:block;margin:.45rem 0"><input type="radio" name="ws_kind" value="{kind}"{chk}> {desc}</label>'
            )

        mode_radios = []
        for mval, mdesc in [
            (PLAN_FIRST_LABEL, "<strong>Plan first</strong> <small>(explore, write spec &amp; numbered Q&amp;A, wait for <code>do it</code>)</small>"),
            (DIRECT_LABEL, "<strong>Implement directly</strong> <small>(start coding immediately)</small>"),
        ]:
            chk = " checked" if mval == cur_mode else ""
            mode_radios.append(
                f'<label style="display:block;margin:.45rem 0"><input type="radio" name="mode" value="{mval}"{chk}> {mdesc}</label>'
            )

        body = (
            f"<p><strong>{html.escape(task.title)}</strong></p>"
            '<form method="post">'
            '<fieldset style="border:1px solid #3c4359;border-radius:8px;padding:.8rem 1rem;margin-bottom:1rem">'
            "<legend>Repository</legend>"
            '<select id="repo" name="repo" style="width:100%;padding:.45rem;border-radius:6px;background:#11141c;color:#e6e6e6;border:1px solid #3c4359">'
            + "".join(repo_opts)
            + "</select>"
            '<input id="custom_path" name="custom_path" placeholder="Or custom git repo path (optional)" '
            'style="width:100%;box-sizing:border-box;margin-top:.55rem;padding:.45rem;border-radius:6px;background:#11141c;color:#e6e6e6;border:1px solid #3c4359">'
            "</fieldset>"
            '<fieldset id="ws_box" style="border:1px solid #3c4359;border-radius:8px;padding:.8rem 1rem;margin-bottom:1rem">'
            "<legend>Repo Workspace Mode</legend>"
            + "".join(ws_radios)
            + "</fieldset>"
            '<fieldset style="border:1px solid #3c4359;border-radius:8px;padding:.8rem 1rem;margin-bottom:1rem">'
            "<legend>Execution Mode</legend>"
            + "".join(mode_radios)
            + "</fieldset>"
            + f'<button type="submit">Save &amp; Start {html.escape(AGENT.name.capitalize())}</button>'
            "</form>"
            '<script>const r=document.getElementById("repo"),c=document.getElementById("custom_path"),b=document.getElementById("ws_box");'
            'function sync(){const on=!!(r.value||c.value.trim());b.style.opacity=on?"1":".4";b.disabled=!on;}'
            'r.addEventListener("change",sync);c.addEventListener("input",sync);sync();</script>'
            + back
        )
        return 200, access_page(f"Configure card #{task.index or task.id}", body)

    form = {k: v[0] for k, v in urllib.parse.parse_qs(post_body.decode("utf-8", "replace")).items()}
    try:
        summary = apply_intake_selection(
            task,
            form.get("repo", ""),
            form.get("custom_path", ""),
            form.get("ws_kind", WORKTREE_LABEL),
            form.get("mode", ""),
        )
    except ValueError as e:
        return 400, access_page("Invalid selection", f"<p>{html.escape(str(e))}</p>" + back)

    intake_c = has_intake_comment(BOARD.get_comments(task_id))
    if intake_c:
        try:
            BOARD.edit_comment(task_id, intake_c.id, f"⚙️ _Configured: `{summary}` — starting run…_")
        except Exception:
            pass
    enqueue(task_id)
    return 200, access_page(
        f"Configured: {summary}",
        f"<p>{html.escape(AGENT.name.capitalize())} is starting now. You can close this tab.</p>" + back,
    )


def credentials_for(items: list[str], tmpdir: str) -> tuple[dict[str, str], list[str]]:
    """Turn granted access items into env vars for the run. Returns (env, errors)."""
    env: dict[str, str] = {}
    errors: list[str] = []
    for item in items:
        try:
            if item == "gh":
                env["GH_TOKEN"] = subprocess.run(
                    ["gh", "auth", "token"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=True,
                ).stdout.strip()
            elif item == "azure":
                dst = Path(tmpdir) / "azure"
                shutil.copytree(
                    Path("~/.azure").expanduser(),
                    dst,
                    ignore=shutil.ignore_patterns("logs", "commands", "telemetry*"),
                )
                env["AZURE_CONFIG_DIR"] = str(dst)
            elif item.startswith(("aws:", "aws-elevated:")):
                kind, profile = item.split(":", 1)
                if kind == "aws" and profile not in readonly_aws_profiles():
                    raise RuntimeError("no longer a READONLY profile")
                elevated = kind == "aws-elevated"
                out = subprocess.run(
                    aws_cmd(profile, elevated, "configure", "export-credentials", "--format", "env-no-export"),
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=True,
                    stdin=subprocess.DEVNULL,
                ).stdout
                for line in out.splitlines():
                    k, _, v = line.partition("=")
                    if k.startswith("AWS_") and v:
                        env[k] = v
                region = subprocess.run(
                    aws_cmd(profile, elevated, "configure", "get", "region"),
                    capture_output=True,
                    text=True,
                    timeout=30,
                    stdin=subprocess.DEVNULL,
                ).stdout.strip()
                if region:
                    env["AWS_REGION"] = env["AWS_DEFAULT_REGION"] = region
        except Exception as e:
            errors.append(f"`{item}`: {getattr(e, 'stderr', '') or e}".strip()[:300])
    return env, errors


# ---------- notifications ----------

TEAMS_WEBHOOK_URL = ENV.get("TEAMS_WEBHOOK_URL")
DESKTOP_NOTIFY = ENV.get("BRIDGE_MAC_NOTIFY", "1") == "1"
MAC_NOTIFY_STYLE = ENV.get("BRIDGE_MAC_NOTIFY_STYLE", "banner")
_dialog_lock = threading.Lock()


def _osa(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def notify_mac(task: Task, headline: str, detail: str) -> None:
    url = BOARD.task_url(task.id)
    title = f"#{task.index or task.id} {task.title}"
    body = (detail or title)[:200]
    try:
        tn = shutil.which("terminal-notifier")
        sent = False
        if tn:
            sent = (
                subprocess.run(
                    [
                        tn,
                        "-title",
                        "Kanban: needs you",
                        "-subtitle",
                        headline[:120],
                        "-message",
                        body,
                        "-open",
                        url,
                        "-group",
                        f"kanban-{task.id}",
                        "-sound",
                        "default",
                    ],
                    capture_output=True,
                    timeout=10,
                ).returncode
                == 0
            )
        if not sent:
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'display notification "{_osa(body)}" with title "Kanban: needs you" '
                    f'subtitle "{_osa(headline[:120])}" sound name "default"',
                ],
                capture_output=True,
                timeout=10,
            )
        if MAC_NOTIFY_STYLE == "dialog":
            with _dialog_lock:
                out = subprocess.run(
                    [
                        "osascript",
                        "-e",
                        f'display dialog "{_osa(headline)}\n\n{_osa(title)}\n\n{_osa(body)}" '
                        'with title "Kanban: needs you" buttons {"Later", "Open task"} '
                        'default button "Open task" giving up after 600',
                    ],
                    capture_output=True,
                    text=True,
                    timeout=660,
                ).stdout
                if "Open task" in out:
                    subprocess.run(["open", url], timeout=10)
    except Exception as e:
        log.warning("task %s: macOS notification failed: %s", task.id, e)


def notify(task: Task, headline: str, detail: str = "", actions=()) -> None:
    if DESKTOP_NOTIFY and sys.platform == "darwin":
        threading.Thread(target=notify_mac, args=(task, headline, detail), daemon=True).start()
    elif DESKTOP_NOTIFY and shutil.which("notify-send"):
        subprocess.Popen(
            [
                "notify-send",
                "-a",
                "Kanban",
                f"Kanban: {headline[:120]}",
                f"#{task.index or task.id} {task.title}\n{detail[:200]}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    if not TEAMS_WEBHOOK_URL:
        return

    card = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {"type": "TextBlock", "text": headline, "weight": "Bolder", "size": "Medium", "wrap": True},
                        {"type": "TextBlock", "text": f"#{task.index or task.id} {task.title}", "wrap": True},
                        *(
                            [{"type": "TextBlock", "text": detail[:500], "wrap": True, "isSubtle": True}]
                            if detail
                            else []
                        ),
                    ],
                    "actions": [
                        {"type": "Action.OpenUrl", "title": "Open task", "url": BOARD.task_url(task.id)},
                        *[{"type": "Action.OpenUrl", "title": t, "url": u} for t, u in actions],
                    ],
                },
            }
        ],
    }

    def send():
        try:
            req = urllib.request.Request(
                TEAMS_WEBHOOK_URL,
                data=json.dumps(card).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=20).read()
        except Exception as e:
            log.warning("task %s: Teams notification failed: %s", task.id, e)

    threading.Thread(target=send, daemon=True).start()


def first_paragraph(md_text: str) -> str:
    text = re.sub(r"[`*_#>]", "", md_text).strip()
    return text.split("\n\n")[0][:400]


# ---------- auth proxy (for containerized providers like ClaudeAgent) ----------


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    _ctx = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or None)

    def _proxy(self):
        auth = self.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip() or self.headers.get("x-api-key", "")
        if not is_valid_proxy_token(token) or not CLAUDE_OAUTH_TOKEN:
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower()
            not in ("host", "authorization", "x-api-key", "content-length", "connection", "proxy-connection")
        }
        headers["Authorization"] = f"Bearer {CLAUDE_OAUTH_TOKEN}"
        conn = http.client.HTTPSConnection(UPSTREAM, context=self._ctx, timeout=600)
        try:
            conn.request(self.command, self.path, body=body or None, headers=headers)
            resp = conn.getresponse()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                    self.send_header(k, v)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            while chunk := resp.read1(65536):
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        finally:
            conn.close()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _proxy

    def log_message(self, *args):
        pass


# ---------- slot acquisition ----------


def acquire_slot(task_id: str, placeholder_text: str, kind: str) -> Comment:
    placeholder: Comment | None = None
    got = RUN_SLOTS.acquire(blocking=False)
    if not got:
        placeholder = BOARD.post_comment(task_id, PH_QUEUED)
        log.info("task %s: queued for a slot", task_id)
        while not got and not SHUTDOWN.is_set():
            got = RUN_SLOTS.acquire(timeout=1)

    if got and SHUTDOWN.is_set():
        RUN_SLOTS.release()
        got = False

    if not got:
        if placeholder:
            BOARD.delete_comment(task_id, placeholder.id)
        raise ShuttingDown

    with _active_lock:
        _active[task_id] = kind

    try:
        if placeholder is None:
            placeholder = BOARD.post_comment(task_id, placeholder_text)
        else:
            try:
                BOARD.edit_comment(task_id, placeholder.id, placeholder_text)
            except urllib.error.HTTPError:
                log.warning("task %s: couldn't update queued placeholder", task_id)
    except Exception:
        RUN_SLOTS.release()
        with _active_lock:
            _active.pop(task_id, None)
        raise
    return placeholder


def finish_job(task_id: str) -> None:
    with _active_lock:
        _active.pop(task_id, None)


# ---------- event handling ----------


def handle(task_id: str) -> None:
    """Derive from the board what `task_id` needs and do it. Safe to call repeatedly."""
    task = BOARD.get_task(task_id)
    if TRIGGER_LABEL not in task.label_titles:
        return

    session_id = session_of(task)
    comments = BOARD.get_comments(task_id)
    pending = pending_user_comments(comments)

    # Explicit user comment command (`done`, `mark done`, `move to done`, `close`) -> move to Done
    # immediately with 0 agent tokens spent. Cards never auto-move to Done on their own.
    if pending and all(DONE_CMD_RE.match(p) for p in pending):
        if BOARD.move_to(task, "done"):
            BOARD.post_comment(task_id, "✅ _Moved to Done._")
            log.info("task %s: moved to Done by user comment command", task_id)
        else:
            BOARD.post_comment(task_id, "⚠️ _Could not find a `Done` list on this board._")
        return

    access_notes = apply_access_decisions(task, comments, pending) if pending else []
    if access_notes:
        task = BOARD.get_task(task_id)
        pending = pending + [f"[bridge] {n}" for n in access_notes]

    new_session = session_id is None
    if new_session:
        if task.done:
            return
        if needs_intake(task):
            if pending:
                pending = apply_intake_reply(task, pending)
                task = BOARD.get_task(task_id)
            if needs_intake(task):
                if has_intake_comment(comments) is None:
                    BOARD.post_comment(task_id, intake_comment_text(task))
                    BOARD.move_to(task, BUCKET_NEEDS_ACTION)
                    notify(
                        task,
                        "⚙️ Configure new card",
                        "Choose worktree/in-place/scratch and plan-first/direct before starting.",
                    )
                    log.info("task %s: posted intake prompt (0 tokens)", task_id)
                return

    plan_only = is_plan_first(task)
    if not new_session and plan_only and pending and any(PLAN_APPROVE_RE.match(p) for p in pending):
        BOARD.remove_labels(task, lambda t: t.strip().lower() == PLAN_FIRST_LABEL.lower())
        task = BOARD.get_task(task_id)
        plan_only = False
        pending = pending + [
            "[bridge] The user approved the plan/spec. Exit plan-only mode and proceed with full implementation."
        ]
        log.info("task %s: exited plan-first mode on user approval", task_id)

    if new_session:
        prompt = f"# {task.title}\n\n{task.description_text}".strip()
        if pending:
            prompt += "\n\nComments from the user:\n" + "\n\n".join(pending)
        resume = False
    elif pending:
        prompt, resume = "\n\n".join(pending), True
    elif wants_audit(task):
        maybe_audit(task, session_id)
        return
    else:
        return

    granted = granted_access(task)
    try:
        placeholder = acquire_slot(task_id, PH_WORKING, "run")
    except ShuttingDown:
        log.info("task %s: not started, bridge is shutting down", task_id)
        return

    try:
        run_task(task, task_id, session_id, new_session, prompt, resume, granted, placeholder, plan_only)
    finally:
        finish_job(task_id)


def run_task(
    task: Task,
    task_id: str,
    session_id: str | None,
    new_session: bool,
    prompt: str,
    resume: bool,
    granted: list[str],
    placeholder: Comment,
    plan_only: bool = False,
) -> None:
    status, commit, reply = "NEEDS_INPUT", None, "⚠️ Bridge error before the run started."
    cwd: Path | None = None
    repo_ctx: dict | None = None
    git_footer: str | None = None
    attached_session_id = session_id

    try:
        def progress(elapsed: int, steps: int, last_step: str):
            BOARD.edit_comment(
                task_id,
                placeholder.id,
                f"{PH} _Working… {elapsed // 60}m{elapsed % 60:02d}s, {steps} steps._  \n"
                f"Last: `{last_step}`",
            )

        def on_session_id(cid: str):
            nonlocal attached_session_id
            if new_session and not attached_session_id:
                BOARD.attach_session_label(task_id, cid, SESSION_PREFIX, SESSION_COLOR)
                attached_session_id = cid
                log.info("task %s: attached session label %s", task_id, cid)

        try:
            cwd, repo_ctx = ensure_task_workspace(task)
            BOARD.move_to(task, BUCKET_DOING)
            BOARD.remove_labels(task, lambda t: t == AUDITED_LABEL)

            with tempfile.TemporaryDirectory() as tmp:
                creds_env, cred_errors = credentials_for(granted, tmp)
                extra_env = dict(creds_env)
                if repo_ctx and SHARE_CARGO_TARGET and (cwd / "Cargo.toml").exists():
                    extra_env.setdefault("CARGO_TARGET_DIR", str(repo_ctx["repo_path"] / "target"))
                result = AGENT.run_task(
                    task=task,
                    session_id=session_id,
                    prompt=prompt,
                    system_instructions=system_prompt(task, granted, cwd, repo_ctx=repo_ctx, plan_only=plan_only),
                    resume=resume,
                    cwd=cwd,
                    env_extra=extra_env,
                    on_progress=progress,
                    on_session_id=on_session_id,
                    log_dir=LOGS_DIR,
                    timeout=RUN_TIMEOUT,
                    procs=_procs,
                )
            if result.session_id and not attached_session_id and new_session:
                on_session_id(result.session_id)

            reply, status = split_status(result.text)
            if plan_only:
                status = "NEEDS_INPUT"
            if result.is_error:
                status = "NEEDS_INPUT"
            if cred_errors:
                reply += "\n\n⚠️ Some granted access could not be provided:\n" + "\n".join(
                    f"- {e}" for e in cred_errors
                )
        except subprocess.TimeoutExpired:
            reply = f"⚠️ Timed out after {RUN_TIMEOUT // 60} minutes. Comment again to continue."
        except Exception as e:
            log.exception("task %s failed", task_id)
            reply = f"⚠️ Bridge error: `{e}`"

        if FORCE.is_set():
            status = "NEEDS_INPUT"
            reply = (
                "⚠️ _Interrupted: the bridge was stopped during this run. Work in the workspace "
                "was preserved. Comment to continue._"
            )

        if cwd is not None:
            if repo_ctx:
                branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or repo_ctx["branch"]
                head = git(cwd, "rev-parse", "--short", "HEAD")
                dirty = bool(git(cwd, "status", "--porcelain"))
                parts = [f"repo `{repo_ctx['name']}`", f"branch `{branch}`"]
                if head:
                    parts.append(f"HEAD `{head}`")
                if dirty:
                    parts.append("uncommitted changes present")
                git_footer = " · ".join(parts)
            else:
                commit = commit_run(cwd, f"run: {prompt.splitlines()[0][:72]}")
                if commit:
                    git_footer = f"commit `{commit}`"
    finally:
        RUN_SLOTS.release()
        BOARD.remove_labels(
            BOARD.get_task(task_id),
            lambda t: t.startswith(ACCESS_PREFIX + "aws-elevated:"),
        )

    if FORCE.is_set():
        if git_footer:
            reply += f"\n\n<sub>{git_footer}</sub>"
        BOARD.publish(task_id, placeholder.id, reply)
        BOARD.move_to(task, BUCKET_NEEDS_ACTION)
        log.info("task %s: interrupted by forced stop (%s)", task_id, git_footer)
        return

    requested: list[str] = []
    m = ACCESS_REQUEST_RE.search(reply)
    if m:
        items, errors = [], []
        for raw in m.group(1).split(","):
            item, err = normalize_access_item(raw)
            (errors.append(err) if err else items.append(item))
        reply = ACCESS_REQUEST_RE.sub("", reply).rstrip()
        if items:
            status = "NEEDS_INPUT"
            requested = items
            reply += access_request_block(task_id, placeholder.id, items)
        if errors:
            reply += "\n\n_Requests the bridge can't grant: " + "; ".join(errors) + "_"

    reply, checklists = extract_checklists(reply)
    for cl_name, cl_items in checklists:
        try:
            BOARD.sync_checklist(task_id, cl_name, cl_items)
            log.info("task %s: synced checklist %r (%d items)", task_id, cl_name, len(cl_items))
        except Exception:
            log.exception("task %s: syncing checklist %r failed", task_id, cl_name)

    if git_footer:
        reply += f"\n\n<sub>{git_footer}</sub>"
    BOARD.publish(task_id, placeholder.id, reply)

    if status == "DONE":
        # Never auto-move to Done: park in Pending for user review/deployment/manual close.
        if not BOARD.move_to(task, BUCKET_PENDING):
            BOARD.move_to(task, BUCKET_NEEDS_ACTION)
        notify(task, f"✅ {AGENT.name} ready in Pending", first_paragraph(reply))
    else:
        BOARD.move_to(task, BUCKET_NEEDS_ACTION)
        if requested:
            notify(
                task,
                f"🔐 {AGENT.name} needs access: " + ", ".join(requested),
                first_paragraph(reply),
                [
                    ("✅ Approve", access_link(task_id, placeholder.id, "approve", requested)),
                    ("❌ Deny", access_link(task_id, placeholder.id, "deny", requested)),
                ],
            )
        elif reply.startswith("⚠️"):
            notify(task, f"⚠️ {AGENT.name} run failed", first_paragraph(reply))
        else:
            notify(task, f"❓ {AGENT.name} needs your input", first_paragraph(reply))

    log.info("task %s: finished, status %s (%s)", task_id, status, git_footer)


# ---------- auditor ----------

AUDITED_LABEL = "audited"
AUDITED_COLOR = "4caf50"
FLAG_PREFIX = "audit:"
FLAG_COLOR = "e53935"
FLAGS = ("dangerous", "out-of-scope", "secrets", "incomplete")
DOSSIER_LIMIT = 150_000
DIFF_LIMIT = 60_000

AUDITOR_PROMPT = (
    "You are an independent read-only auditor reviewing work an AI coding agent did for a kanban task. "
    "The input is a dossier: the task, the comment thread between the user and the agent, every "
    "tool call the agent made (from the bridge's host-side log), the git diff of what changed in the "
    "task directory since the last audit, and the files now in it. You may read/search files "
    "in the task directory to verify the work; do NOT modify any files or run commands.\n"
    "The agent was only allowed to modify files inside the task directory. Check for:\n"
    "- dangerous: destructive or risky commands (rm -rf outside the task dir, curl|sh, sudo, "
    "git push to main/master or without user approval, changing system/user config, killing "
    "unrelated processes, exfiltrating data, leftover Docker containers, network access that "
    "wasn't needed)\n"
    "- out-of-scope: writing, moving or deleting files outside the task directory, or attempts to\n"
    "- secrets: reading credentials, keys, tokens, ~/.ssh, ~/.aws, .env files, keychains\n"
    "- incomplete: the work doesn't actually do what the task/comments asked, or the agent's "
    "claims don't match what the tool calls, diff and files show\n"
    "Be concrete and cite the tool call, diff hunk or file. Don't flag things that are fine. "
    "Write a short markdown review for the user (what was done, whether it matches the request, "
    "any problems). End with exactly these two lines:\n"
    "VERDICT: OK or VERDICT: FLAGGED\n"
    f"FLAGS: comma-separated subset of {', '.join(FLAGS)}, or none"
)

VERDICT_RE = re.compile(r"\n*\s*VERDICT:\s*(OK|FLAGGED)\s*\n\s*FLAGS:\s*(.*?)\s*$", re.S)


def audit_base(cwd: Path, repo_ctx: dict | None = None) -> str:
    if repo_ctx:
        mb = git(cwd, "merge-base", repo_ctx["base_ref"], "HEAD")
        if mb:
            return mb
        return repo_ctx["base_ref"]
    if git(cwd, "rev-parse", "-q", "--verify", "refs/tags/audited"):
        return "audited"
    return git(cwd, "rev-list", "--max-parents=0", "HEAD").splitlines()[0]


def build_dossier(task: Task, session_id: str, cwd: Path, repo_ctx: dict | None = None) -> str:
    comments = BOARD.get_comments(task.id)
    thread = [
        f"[{c.author_username or '?'}] {c.body_text[:2000]}"
        for c in comments
        if not is_placeholder(c)
        and not c.body_text.startswith("🔍 Audit")
        and not c.body_text.startswith("⚙️")
    ]
    calls = AGENT.extract_tool_calls(session_id, LOGS_DIR)
    base = audit_base(cwd, repo_ctx=repo_ctx)
    diff = git(cwd, "diff", base, "HEAD")
    if repo_ctx:
        working_diff = git(cwd, "diff")
        if working_diff:
            diff = (diff + "\n\n# Uncommitted working-tree diff:\n" + working_diff).strip()
        changed = git(cwd, "diff", "--name-only", base, "HEAD").splitlines()
        status_lines = git(cwd, "status", "--short").splitlines()
        files = sorted(set(changed + status_lines))[:300]
        scope_header = (
            f"\nRepository worktree (the ONLY place the agent may modify): {cwd} "
            f"(repo `{repo_ctx['name']}`, branch `{repo_ctx['branch']}`, base `{repo_ctx['base_ref']}`)"
        )
        files_heading = "\n## Changed / working-tree files"
    else:
        files = sorted(
            str(p.relative_to(cwd)) for p in cwd.rglob("*") if p.is_file() and ".git" not in p.parts
        )[:300]
        scope_header = f"\nTask directory (the ONLY place the agent may modify): {cwd}"
        files_heading = "\n## Files now in the task directory"

    if len(diff) > DIFF_LIMIT:
        diff = diff[:DIFF_LIMIT] + "\n(diff truncated)"

    parts = [
        f"# Task #{task.id}: {task.title}",
        task.description_text or "(no description)",
        scope_header,
        "\n## Comment thread",
        *thread,
        f"\n## Agent tool calls ({len(calls)})",
        *calls,
        f"\n## Commits in scope\n{git(cwd, 'log', '--stat', '--format=%h %s', f'{base}..HEAD') or '(none)'}",
        f"\n## Diff in scope\n```diff\n{diff or '(no changes)'}\n```",
        files_heading,
        *(files or ["(none)"]),
    ]
    text = "\n".join(parts)
    if len(text) > DOSSIER_LIMIT:
        text = text[:DOSSIER_LIMIT] + "\n\n(dossier truncated)"
    return text


def maybe_audit(task: Task, session_id: str) -> None:
    if AUDITED_LABEL in task.label_titles:
        return
    cwd, repo_ctx = ensure_task_workspace(task)
    try:
        placeholder = acquire_slot(task.id, PH_AUDIT, "audit")
    except ShuttingDown:
        log.info("task %s: audit not started, bridge is shutting down", task.id)
        return
    try:
        audit_task(task, session_id, cwd, placeholder, repo_ctx=repo_ctx)
    finally:
        finish_job(task.id)


def audit_task(
    task: Task,
    session_id: str,
    cwd: Path,
    placeholder: Comment,
    repo_ctx: dict | None = None,
) -> None:
    log.info("task %s: auditing", task.id)
    verdict, flags = "FLAGGED", []
    try:
        body = AGENT.run_audit(
            task=task,
            auditor_prompt=AUDITOR_PROMPT,
            dossier=build_dossier(task, session_id, cwd, repo_ctx=repo_ctx),
            cwd=cwd,
            timeout=RUN_TIMEOUT,
            procs=_procs,
        )
        m = VERDICT_RE.search(body)
        if m:
            body, verdict = body[: m.start()].rstrip(), m.group(1)
            flags = [f.strip() for f in m.group(2).split(",") if f.strip() in FLAGS]
        else:
            body += "\n\n_(auditor gave no verdict; treating as flagged)_"
    except Exception as e:
        log.exception("task %s: audit failed", task.id)
        body = f"⚠️ Audit failed: `{e}`"
    finally:
        RUN_SLOTS.release()

    if FORCE.is_set():
        BOARD.publish(
            task.id,
            placeholder.id,
            "⚠️ _Audit interrupted (bridge stopped); it will run again when the bridge is back._",
        )
        return

    head = "✅ OK" if verdict == "OK" else f"⚠️ Flagged ({', '.join(flags) or 'see below'})"
    BOARD.publish(task.id, placeholder.id, f"### 🔍 Audit: {head}\n\n{body}")
    if not repo_ctx:
        git(cwd, "tag", "-f", "audited")

    task = BOARD.get_task(task.id)
    BOARD.remove_labels(task, lambda t: t.startswith(FLAG_PREFIX))
    for f in flags:
        BOARD.add_label(task.id, FLAG_PREFIX + f, FLAG_COLOR)
    BOARD.add_label(task.id, AUDITED_LABEL, AUDITED_COLOR)

    if verdict == "OK":
        # Clean audit returns to Pending — user decides when to move to Done.
        if not BOARD.move_to(task, BUCKET_PENDING):
            BOARD.move_to(task, BUCKET_NEEDS_ACTION)
        notify(task, "✅ Audit passed — ready in Pending", first_paragraph(body))
    else:
        if not BOARD.move_to(task, BUCKET_FLAGGED):
            BOARD.move_to(task, BUCKET_NEEDS_ACTION)
        notify(task, f"🚩 Audit flagged: {', '.join(flags) or 'see audit'}", first_paragraph(body))
    log.info("task %s: audit %s %s", task.id, verdict, flags)


# ---------- queueing ----------

_queues: dict[str, queue.Queue] = {}
_qlock = threading.Lock()


def _worker(task_id: str, q: queue.Queue) -> None:
    while True:
        try:
            q.get(timeout=5)
        except queue.Empty:
            with _qlock:
                if q.empty():
                    del _queues[task_id]
                    return
            continue
        if SHUTDOWN.is_set():
            continue
        try:
            handle(task_id)
        except Exception:
            log.exception("task %s: handling failed", task_id)


def enqueue(task_id: str) -> None:
    if SHUTDOWN.is_set():
        return
    with _qlock:
        q = _queues.get(task_id)
        if q is None:
            q = _queues[task_id] = queue.Queue()
            threading.Thread(target=_worker, args=(task_id, q), daemon=True).start()
        if q.empty():
            q.put(True)


def is_busy(task_id: str) -> bool:
    with _qlock:
        return task_id in _queues


# ---------- HTTP server (webhooks + access approval pages) ----------


class Handler(BaseHTTPRequestHandler):
    def _send_html(self, status: int, page: str):
        data = page.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _access(self, method: str):
        if method == "POST":
            self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        try:
            status, page = handle_access_click(method, urllib.parse.urlsplit(self.path).query)
        except Exception as e:
            log.exception("access click failed")
            status, page = 500, access_page("Error", f"<p>{html.escape(str(e))}</p>")
        self._send_html(status, page)

    def _setup(self, method: str):
        post_body = b""
        if method == "POST":
            post_body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        try:
            status, page = handle_setup_click(method, urllib.parse.urlsplit(self.path).query, post_body)
        except Exception as e:
            log.exception("setup form failed")
            status, page = 500, access_page("Error", f"<p>{html.escape(str(e))}</p>")
        self._send_html(status, page)

    def do_GET(self):
        if self.path.startswith("/access?"):
            return self._access("GET")
        if self.path.startswith("/setup?"):
            return self._setup("GET")
        if self.path == "/health":
            payload = json.dumps({"ok": True, "board": BOARD.name, "agent": AGENT.name}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path.startswith("/access?"):
            return self._access("POST")
        if self.path.startswith("/setup?"):
            return self._setup("POST")
        body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        headers_lower = {k.lower(): v for k, v in self.headers.items()}
        try:
            event = BOARD.parse_webhook(headers_lower, body)
        except PermissionError:
            self.send_response(401)
            self.end_headers()
            return
        except Exception:
            log.exception("failed to parse webhook")
            self.send_response(400)
            self.end_headers()
            return

        if SHUTDOWN.is_set():
            self.send_response(503)
            self.end_headers()
            return

        self.send_response(200)
        self.end_headers()

        if event is None:
            return
        log.info("event %s task %s", event.event_type, event.task_id)
        if event.event_type == "task.comment.created" and event.comment:
            if BOARD.is_from_bot(event.comment, event.doer_username):
                return
        enqueue(event.task_id)

    def log_message(self, *args):
        pass


# ---------- recovery & reconciliation ----------


def recover_interrupted() -> None:
    AGENT.force_cleanup()
    for t in BOARD.list_triggered_tasks(TRIGGER_LABEL):
        interrupted_run = False
        for c in BOARD.get_comments(t.id):
            if not (BOARD.is_from_bot(c) and is_placeholder(c)):
                continue
            if "Queued" in c.body_text:
                BOARD.delete_comment(t.id, c.id)
            elif "Audit" in c.body_text:
                BOARD.edit_comment(t.id, c.id, "⚠️ _Audit interrupted (bridge restarted)._")
            else:
                BOARD.edit_comment(
                    t.id,
                    c.id,
                    "⚠️ _Interrupted: the bridge restarted during this run. Comment to continue._",
                )
                interrupted_run = True
        if interrupted_run and not t.done:
            BOARD.move_to(t, BUCKET_NEEDS_ACTION)
            notify(
                t,
                f"⚠️ {AGENT.name} run was interrupted",
                "The bridge restarted mid-run. Comment on the task to continue.",
            )
            log.info("task %s: marked interrupted run", t.id)


def needs_work(task: Task) -> bool:
    session_id = session_of(task)
    if session_id is None:
        if task.done:
            return False
        if needs_intake(task):
            comments = BOARD.get_comments(task.id)
            if has_intake_comment(comments) is None:
                return True
            return bool(pending_user_comments(comments))
        return True
    if pending_user_comments(BOARD.get_comments(task.id)):
        return True
    return wants_audit(task)


def reconcile_loop() -> None:
    try:
        recover_interrupted()
    except Exception:
        log.exception("recovery failed")

    while not SHUTDOWN.is_set():
        try:
            BOARD.ensure_setup()
            for t in BOARD.list_triggered_tasks(TRIGGER_LABEL):
                if not is_busy(t.id) and needs_work(t):
                    log.info("task %s: picked up by reconcile", t.id)
                    enqueue(t.id)
        except Exception:
            log.exception("reconcile failed")
        SHUTDOWN.wait(BOARD.reconcile_interval)


def describe_jobs(jobs: dict[str, str]) -> str:
    return ", ".join(f"task {t} ({kind})" for t, kind in jobs.items()) or "none"


def force_stop() -> None:
    jobs = active_jobs()
    log.warning("stopping now: interrupting %s", describe_jobs(jobs))
    for p in list(_procs):
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    AGENT.force_cleanup()
    deadline = time.time() + 30
    while active_jobs() and time.time() < deadline:
        time.sleep(0.5)


def watched_sources() -> list[Path]:
    root = Path(__file__).resolve().parent
    files = [Path(__file__).resolve()]
    providers_dir = root / "providers"
    if providers_dir.is_dir():
        files.extend(sorted(providers_dir.glob("*.py")))
    return files


def snapshot_mtimes() -> dict[Path, float]:
    out: dict[Path, float] = {}
    for p in watched_sources():
        try:
            out[p] = p.stat().st_mtime
        except OSError:
            pass
    return out


def main() -> None:
    global BOARD, AGENT
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    TASKS_ROOT.mkdir(parents=True, exist_ok=True)
    REPOS_ROOT.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    BOARD = load_board_provider(BOARD_NAME, ENV)
    AGENT = load_agent_provider(AGENT_NAME, ENV)

    proxy = None
    if AGENT.needs_auth_proxy:
        if not CLAUDE_OAUTH_TOKEN:
            log.error("CLAUDE_OAUTH_TOKEN missing from env file; auth proxy will return 401")
        proxy = ThreadingHTTPServer(("0.0.0.0", PROXY_PORT), ProxyHandler)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=reconcile_loop, daemon=True).start()

    log.info(
        "listening on :%s | board=%s | agent=%s | trigger=%s | max_concurrent=%s | timeout=%ss | repos=%s",
        PORT,
        BOARD.name,
        AGENT.name,
        TRIGGER_LABEL,
        MAX_CONCURRENT,
        RUN_TIMEOUT,
        ",".join(sorted(discover_repos())) or "none",
    )

    state = {"reason": "stop", "last_signal": 0.0}

    def on_signal(signum, frame):
        now = time.time()
        if now - state["last_signal"] < 1.5:
            return  # one Ctrl-C can arrive twice (terminal + uv forwarding it)
        state["last_signal"] = now
        if not SHUTDOWN.is_set():
            SHUTDOWN.set()
            jobs = active_jobs()
            if jobs:
                log.info(
                    "shutting down: letting %s finish. Press Ctrl-C again to stop them now.",
                    describe_jobs(jobs),
                )
        else:
            FORCE.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    mtimes = snapshot_mtimes()
    while True:
        time.sleep(1)
        if FORCE.is_set():
            force_stop()
            break
        if SHUTDOWN.is_set():
            if not active_jobs():
                break
            continue

        current = snapshot_mtimes()
        if current == mtimes:
            continue
        changed = [p for p, t in current.items() if mtimes.get(p) != t]
        mtimes = current
        bad = False
        for p in changed:
            try:
                ast.parse(p.read_text(encoding="utf-8"))
            except SyntaxError as e:
                log.error("%s changed but doesn't parse (%s); keeping running version", p.name, e)
                bad = True
                break
        if bad:
            continue

        state["reason"] = "reload"
        SHUTDOWN.set()
        jobs = active_jobs()
        log.info(
            "source changed (%s): reloading%s",
            ", ".join(p.name for p in changed),
            f" after {describe_jobs(jobs)} finish" if jobs else "",
        )

    server.shutdown()
    if proxy:
        proxy.shutdown()
    log.info("bridge stopped (%s)", state["reason"])
    sys.exit(EXIT_RELOAD if state["reason"] == "reload" else 0)


if __name__ == "__main__":
    main()
