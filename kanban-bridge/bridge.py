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
    format_usage_summary,
    is_valid_proxy_token,
    load_agent_provider,
    load_board_provider,
)
from report_ui import (
    extract_questions,
    format_planka_summary,
    format_qa_reply,
    load_report,
    render_report_page,
    report_link,
    save_report,
    verify_report_sig,
)
from ui import (
    DASHBOARD_HTML,
    get_pricing_overview,
    list_agy_models,
    list_pricing_models,
    list_recent_logs,
    read_editable_settings,
    read_log_tail,
    update_env_file,
)

ENV = dict(os.environ)
ENV_FILE_PATH = Path(ENV.get("BRIDGE_ENV_FILE", "~/.config/kanban-bridge/env")).expanduser()
START_TIME = time.time()

BOARD_NAME = ENV.get("BRIDGE_BOARD_PROVIDER", "planka")
AGENT_NAME = ENV.get("BRIDGE_AGENT_PROVIDER", "agy")
SECRET = ENV["WEBHOOK_SECRET"]
PORT = int(ENV.get("BRIDGE_PORT", "8787"))

TASKS_ROOT = Path(ENV.get("BRIDGE_TASKS_ROOT", "~/workspace/kanban-tasks")).expanduser()
STATE_ROOT = Path(ENV.get("BRIDGE_STATE_ROOT", "~/.local/share/kanban-bridge")).expanduser()
LOGS_DIR = STATE_ROOT / "logs"
REPORTS_DIR = STATE_ROOT / "reports"

RUN_TIMEOUT = int(ENV.get("BRIDGE_RUN_TIMEOUT", "3600"))
MAX_CONCURRENT = int(ENV.get("BRIDGE_MAX_CONCURRENT", "2"))

TRIGGER_LABEL = ENV.get("BRIDGE_TRIGGER_LABEL", "agy")
PLAN_FIRST_LABEL = ENV.get("BRIDGE_PLAN_FIRST_LABEL", "plan-first")
DIRECT_LABEL = "direct"
ORCHESTRATOR_LABEL = ENV.get("BRIDGE_ORCHESTRATOR_LABEL", "orchestrator")
DEFAULT_AGY_ORCHESTRATOR_MODEL = "gemini-pro-orchestrator-neon"


def _default_orchestrator_model(agent_name: str, env: dict[str, str]) -> str | None:
    if "BRIDGE_ORCHESTRATOR_MODEL" in env:
        return (env.get("BRIDGE_ORCHESTRATOR_MODEL") or "").strip() or None
    if (agent_name or "").strip().lower() == "agy":
        return DEFAULT_AGY_ORCHESTRATOR_MODEL
    return None


ORCHESTRATOR_MODEL = _default_orchestrator_model(AGENT_NAME, ENV)
WORKTREE_LABEL = "worktree"
INPLACE_LABEL = "in-place"
SCRATCH_LABEL = "scratch"
REPO_LABEL_PREFIX = "repo:"
SESSION_PREFIX = "session:"
SESSION_COLOR = "9e9e9e"
MODEL_LABEL_PREFIX = "model:"
MODEL_LABEL_COLOR = "00acc1"
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
    """Return available single git repos under REPOS_ROOT plus any explicit BRIDGE_REPOS."""
    out: dict[str, Path] = {}
    if REPOS_ROOT.is_dir():
        for child in sorted(REPOS_ROOT.iterdir()):
            if child.name.startswith("."):
                continue
            if child.is_dir() and (child / ".git").exists():
                out[child.name.lower()] = child.resolve()
    out.update(EXPLICIT_REPOS)
    return out


def discover_workdirs() -> dict[str, list[tuple[str, Path]]]:
    """Return board-name -> list of `(repo_name, repo_path)` under REPOS_ROOT / BRIDGE_REPOS.

    Supports both:
      - Single-repo directory: `REPOS_ROOT/<name>/.git` -> `[(name, path)]`
      - Multi-repo workspace directory: `REPOS_ROOT/<name>/<subrepo>/.git` -> `[(sub1, path1), ...]`
    """
    out: dict[str, list[tuple[str, Path]]] = {}
    if REPOS_ROOT.is_dir():
        for child in sorted(REPOS_ROOT.iterdir()):
            if child.name.startswith(".") or not child.is_dir():
                continue
            key = child.name.lower()
            if (child / ".git").exists():
                out[key] = [(key, child.resolve())]
            else:
                subs = [
                    (sub.name.lower(), sub.resolve())
                    for sub in sorted(child.iterdir())
                    if not sub.name.startswith(".") and sub.is_dir() and (sub / ".git").exists()
                ]
                if subs:
                    out[key] = subs
    for name, p in EXPLICIT_REPOS.items():
        if (p / ".git").exists():
            out[name] = [(name, p)]
        elif p.is_dir():
            subs = [
                (sub.name.lower(), sub.resolve())
                for sub in sorted(p.iterdir())
                if not sub.name.startswith(".") and sub.is_dir() and (sub / ".git").exists()
            ]
            if subs:
                out[name] = subs
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
BUCKET_BACKLOG = ENV.get("BRIDGE_BACKLOG_BUCKET", "backlog").strip().lower()
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
SHUTDOWN_STATE = {"reason": "stop", "last_signal": 0.0}


def request_service_action(action: str) -> str:
    """Trigger graceful drain + exit 3 (restart) or exit 0 (stop)."""
    if action == "restart":
        SHUTDOWN_STATE["reason"] = "reload"
        SHUTDOWN.set()
        return "Restarting kanban-bridge after active runs finish..."
    if action == "stop":
        SHUTDOWN_STATE["reason"] = "stop"
        SHUTDOWN.set()
        return "Stopping kanban-bridge after active runs finish..."
    raise ValueError(f"Unsupported service action: {action!r}")

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


def session_id_is_compatible(provider: str, session_id: str) -> bool:
    """Session IDs belong to the provider that created them.

    OpenCode only accepts IDs starting with ``ses`` (e.g. ``ses_abc123``),
    while Agy and Claude use UUIDs. After switching ``BRIDGE_AGENT_PROVIDER``,
    a marker written by the old provider must be treated as *no session* so
    the new provider starts a fresh one and replaces the marker.
    """
    name = (provider or "").strip().lower()
    if name == "opencode":
        return session_id.startswith("ses")
    if name in ("agy", "claude"):
        return not session_id.startswith("ses")
    return True


def session_of(task: Task) -> str | None:
    for t in task.label_titles:
        if not t.startswith(SESSION_PREFIX):
            continue
        sid = t[len(SESSION_PREFIX):]
        if not session_id_is_compatible(AGENT.name, sid):
            log.info(
                "task %s: ignoring session %s (belongs to a different provider than %s)",
                task.id,
                sid[:12],
                AGENT.name,
            )
            continue
        return sid
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


def resolve_workdir(task: Task) -> tuple[str | None, list[tuple[str, Path]]]:
    """Match a card to a workdir (1 or more git repos) via board name or `repo:` label.

    Cards on the general board (any named board not matching a directory under
    REPOS_ROOT / BRIDGE_REPOS) always run as empty scratch tasks.
    """
    lower_labels = {l.strip().lower() for l in task.label_titles}
    if SCRATCH_LABEL in lower_labels:
        return None, []
    workdirs = discover_workdirs()
    board_key = (task.project_name or "").strip().lower()
    if board_key:
        if board_key in workdirs:
            return board_key, workdirs[board_key]
        return None, []
    repos = discover_repos()
    for lbl in task.label_titles:
        raw = lbl.strip()
        low = raw.lower()
        if low.startswith(REPO_LABEL_PREFIX):
            val = raw[len(REPO_LABEL_PREFIX):].strip()
            key = val.lower()
            if key in workdirs:
                return key, workdirs[key]
            if key in repos:
                return key, [(key, repos[key])]
            p = Path(val).expanduser().resolve()
            if p.is_dir() and (p / ".git").exists():
                return p.name.lower(), [(p.name.lower(), p)]
            raise RuntimeError(
                f"Card requests `repo:{val}`, which was not found under {REPOS_ROOT} or BRIDGE_REPOS "
                f"(available: {', '.join(sorted(workdirs)) or 'none'})."
            )
    return None, []


def workspace_mode_of(task: Task) -> str | None:
    """Return 'scratch', 'worktree', 'in-place', or None if not yet chosen.

    Cards on a non-workdir (general) board always use 'scratch'. On workdir
    boards, explicit labels take precedence and default to 'worktree'.
    """
    low = {l.strip().lower() for l in task.label_titles}
    board_key = (task.project_name or "").strip().lower()
    if board_key and board_key not in discover_workdirs():
        return SCRATCH_LABEL
    if SCRATCH_LABEL in low:
        return SCRATCH_LABEL
    if INPLACE_LABEL in low:
        return INPLACE_LABEL
    if WORKTREE_LABEL in low:
        return WORKTREE_LABEL
    if any(l.startswith(REPO_LABEL_PREFIX) for l in low):
        return WORKTREE_LABEL
    if board_key:
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
    # If worktree/in-place was picked, a repo or workdir must also be selected
    if ws in (WORKTREE_LABEL, INPLACE_LABEL):
        try:
            wname, wrepos = resolve_workdir(task)
        except RuntimeError:
            return True
        if not wname or not wrepos:
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


def _ensure_git_worktree(repo_path: Path, wt: Path, branch: str, lock_key: str) -> tuple[str, str]:
    """Create or reuse a git worktree at `wt` for `repo_path`. Returns `(branch, base_ref)`."""
    if not (repo_path / ".git").exists():
        raise RuntimeError(f"Configured repo `{repo_path.name}` is not a git repo: {repo_path}")
    with _repo_lock(lock_key):
        if wt.exists() and (wt / ".git").exists():
            cur_branch = git(wt, "rev-parse", "--abbrev-ref", "HEAD") or branch
            return cur_branch, _default_base_ref(repo_path)
        subprocess.run(
            ["git", "-C", str(repo_path), "fetch", "--prune", "origin"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        base_ref = _default_base_ref(repo_path)
        if git(repo_path, "rev-parse", "-q", "--verify", f"refs/heads/{branch}"):
            cmd = ["git", "-C", str(repo_path), "worktree", "add", str(wt), branch]
        else:
            cmd = ["git", "-C", str(repo_path), "worktree", "add", "-b", branch, str(wt), base_ref]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"git worktree add failed for {repo_path.name}: {res.stderr.strip() or res.stdout.strip()}")
        log.info("created worktree %s on branch %s (base %s)", wt, branch, base_ref)
        return branch, base_ref


def ensure_task_workspace(task: Task) -> tuple[Path, dict | None]:
    """Return `(cwd, repo_ctx)`.

    - Single repo (`repos/<name>/.git`): creates/reuses an isolated `git worktree` (or `in-place`).
    - Multi-repo workdir (`repos/<name>/<subrepo>/.git`): creates/reuses an umbrella directory
      containing one `git worktree` per sub-repo on branch `kanban/<id-tail>-<slug>`.
    - `scratch` (or no matching workdir): creates/reuses a standalone scratch git repo in `TASKS_ROOT`.
    """
    slug = _slug(task.title)
    ws_mode = workspace_mode_of(task)
    workdir_name, repo_list = resolve_workdir(task)
    if workdir_name and len(repo_list) == 1:
        repo_name, repo_path = repo_list[0]
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
        existing = sorted(TASKS_ROOT.glob(f"{workdir_name}-task-{task.id}-*"))
        short_id = task.id[-6:] if len(task.id) > 6 else task.id
        default_branch = f"kanban/{short_id}-{slug}"
        wt = existing[0] if existing and (existing[0] / ".git").exists() else TASKS_ROOT / f"{workdir_name}-task-{task.id}-{slug}"
        branch, base_ref = _ensure_git_worktree(repo_path, wt, default_branch, repo_name)
        return wt, {
            "name": repo_name,
            "repo_path": repo_path,
            "branch": branch,
            "base_ref": base_ref,
            "mode": WORKTREE_LABEL,
        }

    if workdir_name and len(repo_list) > 1:
        short_id = task.id[-6:] if len(task.id) > 6 else task.id
        default_branch = f"kanban/{short_id}-{slug}"
        if ws_mode == INPLACE_LABEL:
            parent_dir = repo_list[0][1].parent
            sub_ctxs = [
                {
                    "name": sname,
                    "repo_path": spath,
                    "worktree": spath,
                    "branch": git(spath, "rev-parse", "--abbrev-ref", "HEAD") or "HEAD",
                    "base_ref": _default_base_ref(spath),
                }
                for sname, spath in repo_list
            ]
            return parent_dir, {
                "name": workdir_name,
                "repo_path": parent_dir,
                "branch": sub_ctxs[0]["branch"],
                "base_ref": sub_ctxs[0]["base_ref"],
                "mode": INPLACE_LABEL,
                "multi_repo": True,
                "repos": sub_ctxs,
            }
        existing = sorted(TASKS_ROOT.glob(f"{workdir_name}-task-{task.id}-*"))
        umbrella = existing[0] if existing and existing[0].is_dir() else TASKS_ROOT / f"{workdir_name}-task-{task.id}-{slug}"
        umbrella.mkdir(parents=True, exist_ok=True)
        sub_ctxs = []
        for sname, spath in repo_list:
            wt = umbrella / sname
            branch, base_ref = _ensure_git_worktree(spath, wt, default_branch, f"{workdir_name}/{sname}")
            sub_ctxs.append(
                {
                    "name": sname,
                    "repo_path": spath,
                    "worktree": wt,
                    "branch": branch,
                    "base_ref": base_ref,
                }
            )
        return umbrella, {
            "name": workdir_name,
            "repo_path": repo_list[0][1].parent,
            "branch": default_branch,
            "base_ref": sub_ctxs[0]["base_ref"],
            "mode": WORKTREE_LABEL,
            "multi_repo": True,
            "repos": sub_ctxs,
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


def is_orchestrator(task: Task) -> bool:
    return any(l.strip().lower() == ORCHESTRATOR_LABEL.lower() for l in task.label_titles)


def system_prompt(
    task: Task,
    granted: list[str],
    cwd: Path,
    repo_ctx: dict | None = None,
    plan_only: bool = False,
    agent_cwd: Path | None = None,
    orchestrator: bool = False,
) -> str:
    view_cwd = agent_cwd or cwd
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
        if repo_ctx.get("multi_repo"):
            sub_desc = ", ".join(
                f"`{r['name']}` (branch `{r['branch']}`, base `{r['base_ref']}`)"
                for r in repo_ctx.get("repos") or []
            )
            loc_line = (
                f"You are working inside multi-repo workspace `{repo_ctx['name']}` at `{view_cwd}` "
                f"containing `{repo_ctx['mode']}` repositories: {sub_desc}. "
                "Make commits inside each modified repository subdirectory rather than the parent folder. "
            )
        elif repo_ctx.get("mode") == INPLACE_LABEL:
            loc_line = (
                f"You are working **in-place** inside repository `{repo_ctx['name']}` at `{view_cwd}` "
                f"(current branch: `{repo_ctx['branch']}`). Do NOT discard unrelated uncommitted user "
                "changes; create or switch to a feature branch before making commits. "
            )
        else:
            loc_line = (
                f"You are working inside an isolated `git worktree` of repository `{repo_ctx['name']}` "
                f"at `{view_cwd}` on feature branch `{repo_ctx['branch']}` (branched from `{repo_ctx['base_ref']}`). "
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
            f"Your working directory (`{view_cwd}`) is the standalone task directory and the ONLY place "
            "you may modify files. It is a git repository and the bridge commits your changes after "
            "each run, so do NOT run `git commit` or rewrite git history yourself."
        )

    if plan_only:
        parts.append(
            "**PLAN-FIRST / SPEC-ONLY MODE IS ACTIVE:**\n"
            "- Do NOT modify production code yet.\n"
            "- Inspect the codebase thoroughly, then write a concise, easy-to-read report: start with `## TL;DR`, "
            "use a compact Markdown table (`Component / File | Proposed Change | Why`) instead of long prose, and "
            "include a small `mermaid` diagram when data/control flow is clearer visually (keep it token-efficient).\n"
            "- If you have decisions or clarifying questions for the user, emit a `QUESTIONS:` block before `STATUS:` "
            "(the bridge renders this as an interactive form alongside your report):\n"
            "QUESTIONS:\n"
            "1. Short question?\n"
            "   - Recommended option [recommended]\n"
            "   - Alternative option\n"
            "(Add `[multi]` on a question for multi-select, or omit options for free-text input.)\n"
            "- Wait for the user to answer or reply `do it` / `proceed` before implementing.\n"
            "- End this turn with `STATUS: NEEDS_INPUT`."
        )

    if orchestrator:
        parts.append(
            "**ORCHESTRATOR / COORDINATOR MODE IS ACTIVE:**\n"
            "- Act strictly as the **Orchestrator (Coordinator)**: break the task into clear scoped steps, "
            "delegate implementation, debugging, and verification to subagents (`Agent` tool in Claude Code; "
            "`swarm` or `invoke_subagent` in Agy), and synthesize the final result.\n"
            "- Do **NOT** edit production code, run heavy builds, or make multi-file changes directly as the "
            "Orchestrator — dispatch implementation work to dev subagent(s) and have a separate reviewer "
            "subagent verify the diff and test results before concluding.\n"
            "- For trivial read-only lookups or 1–2 command status checks, you may inspect briefly yourself, "
            "then delegate the actual work.\n"
            "- If `plan-first` mode is also active, use read-only research subagents to explore the codebase "
            "and draft the spec/Q&A without editing production code."
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
        "Card attachments & generated files:\n"
        f"- Any images, PDFs, or files attached to this card are downloaded by the bridge into `{view_cwd / ATTACHMENTS_DIR_NAME}` "
        "(excluded from git). Read them directly with your file viewer when relevant.\n"
        "- Whenever the user asks you to generate or share an image, diagram, chart, or file on the card, "
        "save/generate it and add one `ATTACH:` line per file before the status line using a path relative to your working directory:\n"
        "ATTACH: <relative-path>\n"
        "(or `ATTACH: <relative-path> | <display name>`). The bridge uploads those files directly to the card as attachments."
    )

    parts.append(
        "Report formatting (keep token usage lean):\n"
        "- Format your final response as an easy-to-read report: start with a 2–3 sentence `## TL;DR`, "
        "use compact Markdown tables for changes (`File / Area | Summary`) and verification (`Check | Result`), "
        "and use a small `mermaid` diagram or `ATTACH:` image only when it genuinely clarifies architecture/flow "
        "(don't pad with verbose prose or unnecessary visuals).\n"
        "- Whenever ending with `STATUS: NEEDS_INPUT` to ask the user decisions, you may use a `QUESTIONS:` block "
        "(numbered questions with indented `- Option [recommended]` choices) so the bridge presents a clickable Q&A form."
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
_ATTACH_LINE_RE = re.compile(r"^\s*ATTACH:\s*(.+?)\s*$")

ATTACHMENTS_DIR_NAME = ".kanban-attachments"
MAX_ATTACHMENT_BYTES = int(os.environ.get("BRIDGE_MAX_ATTACHMENT_MB", "25")) * 1024 * 1024
_FORBIDDEN_ATTACH_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".kdbx")
_FORBIDDEN_ATTACH_NAMES = {".env", ".netrc", "credentials", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"}


def _ensure_git_exclude(cwd: Path, pattern: str) -> None:
    """Add `pattern` to the repository's `.git/info/exclude` (works for worktrees too)."""
    common = git(cwd, "rev-parse", "--git-common-dir")
    if not common:
        return
    git_dir = Path(common)
    if not git_dir.is_absolute():
        git_dir = (cwd / git_dir).resolve()
    exclude = git_dir / "info" / "exclude"
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(encoding="utf-8", errors="replace").splitlines() if exclude.exists() else []
        if pattern not in [ln.strip() for ln in existing]:
            with exclude.open("a", encoding="utf-8") as f:
                if existing and not exclude.read_text(encoding="utf-8", errors="replace").endswith("\n"):
                    f.write("\n")
                f.write(f"{pattern}\n")
    except Exception:
        log.warning("could not update git exclude at %s", exclude, exc_info=True)


def extract_attachments(text: str) -> tuple[str, list[tuple[str, str | None]]]:
    """Extract `ATTACH: <path>` or `ATTACH: <path> | <name>` directives outside fenced code blocks."""
    lines = text.splitlines()
    kept: list[str] = []
    out: list[tuple[str, str | None]] = []
    in_fence = False
    for line in lines:
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            kept.append(line)
            continue
        if not in_fence:
            m = _ATTACH_LINE_RE.match(line)
            if m:
                raw = m.group(1).strip()
                if "|" in raw:
                    path_part, name_part = raw.split("|", 1)
                    disp = name_part.strip() or None
                else:
                    path_part, disp = raw, None
                path_part = path_part.strip().strip("`'\"<>")
                if path_part:
                    out.append((path_part, disp))
                    continue
        kept.append(line)
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return cleaned, out


def resolve_attachable_path(
    raw_path: str,
    cwd: Path,
    session_id: str | None = None,
    agent_cwd: Path | None = None,
) -> tuple[Path | None, str | None]:
    """Validate an agent-requested `ATTACH:` path against workspace/session-brain boundaries.

    When `agent_cwd` differs from host `cwd` (e.g. Docker container sees `/work`), paths inside
    `agent_cwd` are mapped back onto host `cwd` before resolving and enforcing workspace boundaries.
    """
    p_str = raw_path.strip()
    if p_str.startswith("file://"):
        p_str = p_str[len("file://") :]
    p = Path(p_str).expanduser()
    if p.is_absolute() and agent_cwd is not None:
        # Lexically normalize before checking container prefix so `/work/../etc/passwd` cannot escape.
        norm_p = Path(os.path.normpath(str(p)))
        norm_agent = Path(os.path.normpath(str(agent_cwd)))
        if norm_agent != cwd.resolve():
            try:
                rel = norm_p.relative_to(norm_agent)
                p = cwd / rel
            except ValueError:
                pass
    if not p.is_absolute():
        p = cwd / p
    try:
        resolved = p.resolve(strict=True)
    except FileNotFoundError:
        return None, f"`{raw_path}`: file not found"
    except Exception as e:
        return None, f"`{raw_path}`: invalid path ({e})"
    if not resolved.is_file():
        return None, f"`{raw_path}`: not a regular file"

    allowed_roots = [cwd.resolve()]
    if session_id and re.fullmatch(r"[A-Za-z0-9._-]+", session_id):
        brain_root = (Path.home() / ".gemini" / "antigravity-cli" / "brain" / session_id).resolve()
        if brain_root.exists():
            allowed_roots.append(brain_root)

    if not any(resolved == r or r in resolved.parents for r in allowed_roots):
        return None, f"`{raw_path}`: outside allowed workspace"
    if ".git" in resolved.parts:
        return None, f"`{raw_path}`: files inside `.git` cannot be attached"

    lower_name = resolved.name.lower()
    if (
        lower_name in _FORBIDDEN_ATTACH_NAMES
        or lower_name.startswith(".env.")
        or lower_name.endswith(_FORBIDDEN_ATTACH_SUFFIXES)
    ):
        return None, f"`{resolved.name}`: sensitive file type blocked"

    size = resolved.stat().st_size
    if size > MAX_ATTACHMENT_BYTES:
        return None, f"`{resolved.name}`: exceeds {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB limit"
    return resolved, None


def format_inbound_attachments(
    attachments: list,
    host_cwd: Path | None = None,
    agent_cwd: Path | None = None,
) -> str:
    """Render a compact prompt block listing downloaded card attachments and links."""
    if not attachments:
        return ""
    lines = [
        "## Card Attachments",
        "The card currently has the following attachments (use your file viewer to inspect images/PDFs/files):",
    ]
    host_root = host_cwd.resolve() if host_cwd is not None else None
    for att in attachments:
        if att.kind == "link":
            lines.append(f"- [link] {att.name}: {att.url}")
        elif att.local_path is not None:
            display_path = att.local_path
            if host_root is not None and agent_cwd is not None and agent_cwd != host_root:
                try:
                    display_path = agent_cwd / att.local_path.resolve().relative_to(host_root)
                except ValueError:
                    pass
            kind_tag = "image" if att.is_image else (att.mime_type or "file")
            size_kb = max(1, (att.size_bytes + 1023) // 1024) if att.size_bytes else 0
            size_note = f", {size_kb} KB" if size_kb else ""
            lines.append(f"- `{display_path}` ({att.name}; {kind_tag}{size_note})")
        else:
            lines.append(f"- {att.name} (could not be downloaded / exceeds size limit)")
    return "\n".join(lines)


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
        + "\n\n*or reply `approve`, `approve <item>`, `deny`*"
    )


def request_still_open(task_id: str, request_comment_id: str) -> bool:
    comments = BOARD.get_comments(task_id)
    if not comments or comments[-1].id != request_comment_id:
        return False
    return ACCESS_REQUEST_HEAD in comments[-1].body_text


def access_page(
    title: str,
    body: str,
    button: str | None = None,
    auto_close_url: str | None = None,
) -> str:
    form = (
        f'<form method="post"><button type="submit">{html.escape(button)}</button></form>'
        if button
        else ""
    )
    script = ""
    if auto_close_url:
        script = (
            "<script>setTimeout(() => {"
            " try { window.close(); } catch (_) {}"
            f" setTimeout(() => {{ if (!window.closed) location.replace({json.dumps(auto_close_url)}); }}, 250);"
            "}, 350);</script>"
        )
    return f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{html.escape(title)}</title>
<style>body{{font:16px system-ui;background:#1e2230;color:#e6e6e6;display:grid;place-items:center;
min-height:90vh;margin:0}}main{{max-width:28rem;padding:2rem;background:#272c3b;border-radius:12px}}
button{{font:inherit;padding:.6rem 1.2rem;border:0;border-radius:8px;background:#4f7cff;color:#fff;
cursor:pointer}}a{{color:#8fb0ff}}code{{background:#11141c;padding:.1rem .3rem;border-radius:4px}}</style>
<main><h2>{html.escape(title)}</h2>{body}{form}</main>{script}"""


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
        "<p>The agent will continue shortly. Returning to card…</p>" + back,
        auto_close_url=BOARD.task_url(task_id),
    )


# ---------- pre-run intake (workspace mode + plan-first/direct) ----------

LABEL_COLORS = {
    WORKTREE_LABEL: "4f7cff",
    INPLACE_LABEL: "fb8c00",
    SCRATCH_LABEL: "9e9e9e",
    PLAN_FIRST_LABEL: "fbc02d",
    DIRECT_LABEL: "43a047",
    ORCHESTRATOR_LABEL: "7e57c2",
}


def sign_setup(task_id: str) -> str:
    return hmac.new(SECRET.encode(), f"setup|{task_id}".encode(), hashlib.sha256).hexdigest()[:32]


def setup_link(task_id: str) -> str:
    q = urllib.parse.urlencode({"t": task_id, "s": sign_setup(task_id)})
    return f"{APPROVAL_BASE}/setup?{q}"


def intake_comment_text(task: Task) -> str:
    board_name = (task.project_name or "").strip()
    target_hint = ""
    if board_name:
        workdirs = discover_workdirs()
        bkey = board_name.lower()
        if bkey in workdirs:
            subs = workdirs[bkey]
            if len(subs) == 1:
                target_hint = f"- **Board Target:** `{board_name}` → repo `{bkey}` (`worktree`)\n"
            else:
                names = ", ".join(f"`{n}`" for n, _ in subs)
                target_hint = f"- **Board Target:** `{board_name}` → workdir `{bkey}` ({names}, `worktree`)\n"
        else:
            target_hint = f"- **Board Target:** `{board_name}` → standalone `scratch` folder\n"
    return (
        f"### {INTAKE_HEAD}\n\n"
        f"Choose execution mode before `{AGENT.name}` starts (0 tokens used):\n\n"
        f"👉 **[⚙️ Configure & Start]({setup_link(task.id)})**\n\n"
        f"{target_hint}"
        "- **Target (override if needed):** `scratch`, or `<repo>` with `worktree` / `in-place`\n"
        "- **Mode:** `plan-first` *(spec & numbered Q&A before coding)* or `direct` *(implement right away)*\n"
        f"- **Optional:** `{ORCHESTRATOR_LABEL}` *(act as coordinator; delegate coding & review to subagents)*\n\n"
        "*or reply with keywords like `plan-first`, `direct orchestrator`, or attach labels on this card*"
    )


def apply_intake_selection(
    task: Task,
    repo_choice: str,
    custom_path: str,
    ws_kind: str,
    mode: str,
    orchestrator: bool = False,
) -> str:
    """Apply target repo/scratch, worktree/in-place, plan-first/direct, and optional orchestrator labels."""
    repos = discover_repos()
    BOARD.remove_labels(
        task,
        lambda t: t.strip().lower()
        in (
            WORKTREE_LABEL,
            INPLACE_LABEL,
            SCRATCH_LABEL,
            PLAN_FIRST_LABEL.lower(),
            DIRECT_LABEL,
            ORCHESTRATOR_LABEL.lower(),
        )
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
    if orchestrator:
        BOARD.add_label(task.id, ORCHESTRATOR_LABEL, LABEL_COLORS[ORCHESTRATOR_LABEL])
        return f"{ws_desc} · {mode} · {ORCHESTRATOR_LABEL}"
    return f"{ws_desc} · {mode}"


def apply_intake_reply(task: Task, pending: list[str]) -> list[str]:
    """Parse intake keywords from user comments on a new card; returns non-config comments."""
    repos = discover_repos()
    remaining: list[str] = []
    chosen_repo, _ = resolve_repo(task)
    chosen_ws = workspace_mode_of(task)
    chosen_mode = execution_mode_of(task)
    chosen_orch = is_orchestrator(task)
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
            elif tok in (ORCHESTRATOR_LABEL.lower(), "orch", "coordinator"):
                chosen_orch, recognized = True, recognized + 1
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
        if chosen_orch:
            BOARD.add_label(task.id, ORCHESTRATOR_LABEL, LABEL_COLORS[ORCHESTRATOR_LABEL])
        log.info(
            "task %s: applied intake reply (repo=%s ws=%s mode=%s orchestrator=%s)",
            task.id,
            chosen_repo,
            chosen_ws,
            chosen_mode,
            chosen_orch,
        )
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
        cur_orch = is_orchestrator(task)

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

        orch_chk = " checked" if cur_orch else ""
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
            + f'<hr style="border:0;border-top:1px solid #3c4359;margin:.65rem 0">'
            + f'<label style="display:block;margin:.45rem 0"><input type="checkbox" name="orchestrator" value="1"{orch_chk}> '
            + "<strong>Orchestrator mode</strong> <small>(act as coordinator; delegate coding &amp; review to subagents)</small></label>"
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
            orchestrator=form.get("orchestrator") == "1",
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
        f"<p>{html.escape(AGENT.name.capitalize())} is starting now. Returning to card…</p>" + back,
        auto_close_url=BOARD.task_url(task_id),
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

NOTIFY_MODE = ENV.get("BRIDGE_NOTIFY", "auto").strip().lower()


def notifications_enabled() -> bool:
    return NOTIFY_MODE not in ("off", "0", "false", "no")


def notify(task: Task, headline: str, detail: str = "", actions=()) -> None:
    if not notifications_enabled():
        return
    if sys.platform.startswith("linux") and shutil.which("notify-send"):
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


def is_parked_in_backlog(task: Task) -> bool:
    """Cards sitting in Backlog are parked/drafts and ignored until moved out."""
    return bool(BUCKET_BACKLOG) and BOARD.current_bucket(task) == BUCKET_BACKLOG


def handle(task_id: str) -> None:
    """Derive from the board what `task_id` needs and do it. Safe to call repeatedly."""
    task = BOARD.get_task(task_id)
    if TRIGGER_LABEL not in task.label_titles:
        return
    if is_parked_in_backlog(task):
        log.debug("task %s: parked in %r, ignoring", task_id, BUCKET_BACKLOG)
        return

    session_id = session_of(task)
    comments = BOARD.get_comments(task_id)
    seen_comment_ids = {c.id for c in comments}
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

    # Catch any human comment posted between initial read and posting our placeholder (e.g. while queued).
    try:
        fresh_comments = BOARD.get_comments(task_id)
        missed: list[str] = []
        for c in fresh_comments:
            if c.id == placeholder.id:
                break
            if c.id not in seen_comment_ids and not BOARD.is_from_bot(c) and c.body_text:
                missed.append(c.body_text)
        if missed:
            prompt = prompt + "\n\n" + "\n\n".join(missed)
            log.info("task %s: absorbed %d comment(s) posted before placeholder", task_id, len(missed))
    except Exception:
        log.warning("task %s: checking for pre-placeholder comments failed", task_id, exc_info=True)

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
    agent_cwd: Path | None = None
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
                BOARD.attach_session_label(task_id, cid, SESSION_PREFIX, SESSION_COLOR, provider=AGENT.name)
                attached_session_id = cid
                log.info("task %s: attached session label %s (%s)", task_id, cid, AGENT.name)

        subtask_q: queue.Queue[tuple[str, str, bool] | None] = queue.Queue()

        def _subtask_worker():
            while True:
                item = subtask_q.get()
                if item is None:
                    return
                cl_name, cl_text, cl_done = item
                try:
                    BOARD.sync_checklist(task_id, cl_name, [(cl_text, cl_done)])
                except Exception:
                    log.warning("task %s: live subtask checklist sync failed", task_id, exc_info=True)

        subtask_thread = threading.Thread(target=_subtask_worker, daemon=True)

        def on_subtask(cl_name: str, cl_text: str, cl_done: bool):
            subtask_q.put((cl_name, cl_text, cl_done))

        try:
            cwd, repo_ctx = ensure_task_workspace(task)
            agent_cwd = AGENT.agent_workdir(cwd)
            _ensure_git_exclude(cwd, f"{ATTACHMENTS_DIR_NAME}/")
            try:
                inbound_atts = BOARD.sync_attachments_in(
                    task_id, cwd / ATTACHMENTS_DIR_NAME, max_bytes=MAX_ATTACHMENT_BYTES
                )
            except Exception:
                log.warning("task %s: syncing inbound attachments failed", task_id, exc_info=True)
                inbound_atts = []
            run_prompt = prompt
            att_block = format_inbound_attachments(inbound_atts, host_cwd=cwd, agent_cwd=agent_cwd)
            if att_block:
                run_prompt = f"{prompt}\n\n{att_block}"

            BOARD.move_to(task, BUCKET_DOING)
            BOARD.remove_labels(task, lambda t: t == AUDITED_LABEL)

            subtask_thread.start()
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    creds_env, cred_errors = credentials_for(granted, tmp)
                    extra_env = dict(creds_env)
                    if repo_ctx and SHARE_CARGO_TARGET and (cwd / "Cargo.toml").exists():
                        extra_env.setdefault("CARGO_TARGET_DIR", str(repo_ctx["repo_path"] / "target"))
                    orch = is_orchestrator(task)
                    result = AGENT.run_task(
                        task=task,
                        session_id=session_id,
                        prompt=run_prompt,
                        system_instructions=system_prompt(
                            task,
                            granted,
                            cwd,
                            repo_ctx=repo_ctx,
                            plan_only=plan_only,
                            agent_cwd=agent_cwd,
                            orchestrator=orch,
                        ),
                        resume=resume,
                        cwd=cwd,
                        env_extra=extra_env,
                        on_progress=progress,
                        on_session_id=on_session_id,
                        log_dir=LOGS_DIR,
                        timeout=RUN_TIMEOUT,
                        procs=_procs,
                        on_subtask=on_subtask,
                        model_override=ORCHESTRATOR_MODEL if orch else None,
                    )
            finally:
                subtask_q.put(None)
                subtask_thread.join(timeout=10)
            if result.session_id and not attached_session_id and new_session:
                on_session_id(result.session_id)
            if result.model:
                model_lbl = f"{MODEL_LABEL_PREFIX}{result.model}"
                if model_lbl not in task.label_titles:
                    try:
                        BOARD.add_label(task_id, model_lbl, MODEL_LABEL_COLOR)
                        log.info("task %s: attached model label %s", task_id, model_lbl)
                    except Exception:
                        log.warning("task %s: attaching model label %s failed", task_id, model_lbl, exc_info=True)

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
            if repo_ctx and repo_ctx.get("multi_repo"):
                sub_parts: list[str] = []
                for r in repo_ctx.get("repos") or []:
                    wt = r["worktree"]
                    br = git(wt, "rev-parse", "--abbrev-ref", "HEAD") or r["branch"]
                    hd = git(wt, "rev-parse", "--short", "HEAD")
                    dirty = bool(git(wt, "status", "--porcelain"))
                    base_hd = git(wt, "rev-parse", "--short", r["base_ref"])
                    if dirty or (hd and base_hd and hd != base_hd):
                        tag = f"`{r['name']}` (`{br}`" + (f" · `{hd}`" if hd else "") + (", dirty" if dirty else "") + ")"
                        sub_parts.append(tag)
                if sub_parts:
                    git_footer = f"workdir `{repo_ctx['name']}` · " + " · ".join(sub_parts)
                else:
                    git_footer = f"workdir `{repo_ctx['name']}` · branch `{repo_ctx['branch']}`"
            elif repo_ctx:
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
            reply += f"\n\n*{git_footer}*"
        BOARD.publish(task_id, placeholder.id, reply)
        BOARD.move_to(task, BUCKET_NEEDS_ACTION)
        log.info("task %s: interrupted by forced stop (%s)", task_id, git_footer)
        return

    requested: list[str] = []
    access_footer = ""
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
            access_footer += access_request_block(task_id, placeholder.id, items)
        if errors:
            access_footer += "\n\n_Requests the bridge can't grant: " + "; ".join(errors) + "_"

    reply, checklists = extract_checklists(reply)
    for cl_name, cl_items in checklists:
        try:
            BOARD.sync_checklist(task_id, cl_name, cl_items)
            log.info("task %s: synced checklist %r (%d items)", task_id, cl_name, len(cl_items))
        except Exception:
            log.exception("task %s: syncing checklist %r failed", task_id, cl_name)

    reply, attach_specs = extract_attachments(reply)
    uploaded_atts: list[dict[str, Any]] = []
    attach_footer = ""
    if attach_specs and cwd is not None:
        uploaded_names: list[str] = []
        attach_errors: list[str] = []
        eff_sid = attached_session_id or session_id
        for raw_path, disp_name in attach_specs:
            resolved, err = resolve_attachable_path(
                raw_path, cwd, session_id=eff_sid, agent_cwd=agent_cwd
            )
            if err or resolved is None:
                attach_errors.append(err or f"`{raw_path}`: invalid")
                continue
            try:
                att = BOARD.upload_attachment(task_id, resolved, name=disp_name)
                if att:
                    uploaded_names.append(att.name)
                    uploaded_atts.append(
                        {"name": att.name, "url": att.url, "is_image": att.is_image}
                    )
                    log.info("task %s: uploaded attachment %r (%s)", task_id, att.name, resolved)
            except Exception as e:
                log.exception("task %s: uploading attachment %s failed", task_id, resolved)
                attach_errors.append(f"`{resolved.name}`: upload failed ({e})")
        if uploaded_names:
            attach_footer += "\n\n📎 Attached to card: " + ", ".join(f"`{n}`" for n in uploaded_names)
        if attach_errors:
            attach_footer += "\n\n⚠️ Could not attach: " + "; ".join(attach_errors)

    reply, questions = extract_questions(reply)
    mode_label = (
        "plan-first" if plan_only else ("orchestrator" if is_orchestrator(task) else "direct")
    )
    report_url = ""
    try:
        save_report(
            REPORTS_DIR,
            {
                "task_id": task_id,
                "comment_id": placeholder.id,
                "title": task.title,
                "status": status,
                "mode": mode_label,
                "agent_name": AGENT.name,
                "model": getattr(result, "model", "") if "result" in locals() else "",
                "git_footer": git_footer,
                "raw_markdown": reply,
                "questions": questions,
                "attachments": uploaded_atts,
            },
        )
        report_url = report_link(APPROVAL_BASE, SECRET, task_id, placeholder.id)
    except Exception:
        log.exception("task %s: saving report failed", task_id)

    planka_reply = (
        format_planka_summary(reply, questions, report_url, status)
        if report_url
        else reply
    )
    if access_footer:
        planka_reply += access_footer
    if attach_footer:
        planka_reply += attach_footer
    if git_footer:
        planka_reply += f"\n\n*{git_footer}*"
    BOARD.publish(task_id, placeholder.id, planka_reply)
    reply = planka_reply

    if status == "DONE":
        try:
            BOARD.complete_checklists(task_id, {"Subagents & Tasks"})
        except Exception:
            log.warning("task %s: completing Subagents & Tasks checklist failed", task_id, exc_info=True)
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
    "tool call the agent made (from the bridge's host-side log), every subagent spawned during the run "
    "(what it was asked, its own tool calls, and its final report), token usage and estimated cost, "
    "the git diff of what changed in the "
    "task directory since the last audit, and the files now in it. You may read/search files "
    "in the task directory to verify the work; do NOT modify any files or run commands.\n"
    "The agent (and any subagents it spawned) was only allowed to modify files inside the task directory. Check for:\n"
    "- dangerous: destructive or risky commands (rm -rf outside the task dir, curl|sh, sudo, "
    "git push to main/master or without user approval, changing system/user config, killing "
    "unrelated processes, exfiltrating data, leftover Docker containers, network access that "
    "wasn't needed)\n"
    "- out-of-scope: writing, moving or deleting files outside the task directory, or attempts to\n"
    "- secrets: reading credentials, keys, tokens, ~/.ssh, ~/.aws, .env files, keychains\n"
    "- incomplete: the work doesn't actually do what the task/comments asked, or the agent's "
    "claims don't match what the tool calls, diff and files show\n"
    "Be concrete and cite the tool call, subagent, diff hunk or file. Don't flag things that are fine. "
    "Write a short markdown review for the user (what was done, whether it matches the request, "
    "any problems). If subagents were spawned, include a concise one-line verdict for each subagent. "
    "End with exactly these two lines:\n"
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


def build_dossier(
    task: Task,
    session_id: str,
    cwd: Path,
    repo_ctx: dict | None = None,
    usage_md: str = "",
) -> str:
    comments = BOARD.get_comments(task.id)
    thread = [
        f"[{c.author_username or '?'}] {c.body_text[:2000]}"
        for c in comments
        if not is_placeholder(c)
        and not c.body_text.startswith("🔍 Audit")
        and not c.body_text.startswith("⚙️")
    ]
    calls = AGENT.extract_tool_calls(session_id, LOGS_DIR)
    subagents = AGENT.extract_subagent_audits(session_id, LOGS_DIR, task_id=task.id)
    if repo_ctx and repo_ctx.get("multi_repo"):
        diff_chunks: list[str] = []
        commit_chunks: list[str] = []
        file_list: list[str] = []
        repo_summaries: list[str] = []
        for r in repo_ctx.get("repos") or []:
            wt = r["worktree"]
            rname = r["name"]
            rbase = audit_base(wt, repo_ctx=r)
            rdiff = git(wt, "diff", rbase, "HEAD")
            rwork = git(wt, "diff")
            if rwork:
                rdiff = (rdiff + "\n\n# Uncommitted working-tree diff:\n" + rwork).strip()
            if rdiff:
                diff_chunks.append(f"# === {rname} ({r['branch']}) ===\n{rdiff}")
            rlog = git(wt, "log", "--stat", "--format=%h %s", f"{rbase}..HEAD")
            if rlog:
                commit_chunks.append(f"[{rname}]\n{rlog}")
            changed = git(wt, "diff", "--name-only", rbase, "HEAD").splitlines()
            status_lines = git(wt, "status", "--short").splitlines()
            for f in sorted(set(changed + status_lines)):
                file_list.append(f"{rname}/{f}")
            repo_summaries.append(f"`{rname}` (`{r['branch']}`)")
        diff = "\n\n".join(diff_chunks)
        commits_text = "\n\n".join(commit_chunks) or "(none)"
        files = file_list[:300]
        scope_header = (
            f"\nMulti-repo workspace (the ONLY place the agent may modify): {cwd} "
            f"(workdir `{repo_ctx['name']}`, repos: {', '.join(repo_summaries)})"
        )
        files_heading = "\n## Changed / working-tree files"
    elif repo_ctx:
        base = audit_base(cwd, repo_ctx=repo_ctx)
        diff = git(cwd, "diff", base, "HEAD")
        working_diff = git(cwd, "diff")
        if working_diff:
            diff = (diff + "\n\n# Uncommitted working-tree diff:\n" + working_diff).strip()
        commits_text = git(cwd, "log", "--stat", "--format=%h %s", f"{base}..HEAD") or "(none)"
        changed = git(cwd, "diff", "--name-only", base, "HEAD").splitlines()
        status_lines = git(cwd, "status", "--short").splitlines()
        files = sorted(set(changed + status_lines))[:300]
        scope_header = (
            f"\nRepository worktree (the ONLY place the agent may modify): {cwd} "
            f"(repo `{repo_ctx['name']}`, branch `{repo_ctx['branch']}`, base `{repo_ctx['base_ref']}`)"
        )
        files_heading = "\n## Changed / working-tree files"
    else:
        base = audit_base(cwd, repo_ctx=None)
        diff = git(cwd, "diff", base, "HEAD")
        commits_text = git(cwd, "log", "--stat", "--format=%h %s", f"{base}..HEAD") or "(none)"
        files = sorted(
            str(p.relative_to(cwd))
            for p in cwd.rglob("*")
            if p.is_file() and ".git" not in p.parts and ATTACHMENTS_DIR_NAME not in p.parts
        )[:300]
        scope_header = f"\nTask directory (the ONLY place the agent may modify): {cwd}"
        files_heading = "\n## Files now in the task directory"

    if len(diff) > DIFF_LIMIT:
        diff = diff[:DIFF_LIMIT] + "\n(diff truncated)"

    subagent_lines: list[str] = []
    if subagents:
        subagent_lines.append(f"\n## Subagents spawned ({len(subagents)})")
        for idx, sub in enumerate(subagents, start=1):
            subagent_lines.append(f"\n### Subagent {idx}: {sub.name} (`{sub.id}`)")
            if sub.prompt:
                subagent_lines.append(f"**Prompt:** {sub.prompt}")
            subagent_lines.append(f"**Tool calls ({len(sub.tool_calls)}):**")
            subagent_lines.extend(sub.tool_calls or ["- (no tool calls recorded)"])
            if sub.final_report:
                subagent_lines.append(f"**Final report:** {sub.final_report}")

    usage_lines = ["\n## Token usage & cost estimate", usage_md] if usage_md else []

    parts = [
        f"# Task #{task.id}: {task.title}",
        task.description_text or "(no description)",
        scope_header,
        "\n## Comment thread",
        *thread,
        *usage_lines,
        f"\n## Agent tool calls ({len(calls)})",
        *calls,
        *subagent_lines,
        f"\n## Commits in scope\n{commits_text}",
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
    usage_summary = AGENT.extract_usage_summary(session_id, LOGS_DIR, task_id=task.id)
    usage_md = format_usage_summary(usage_summary)
    try:
        body = AGENT.run_audit(
            task=task,
            auditor_prompt=AUDITOR_PROMPT,
            dossier=build_dossier(task, session_id, cwd, repo_ctx=repo_ctx, usage_md=usage_md),
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
    cost_section = f"\n\n---\n{usage_md}" if usage_md else ""
    BOARD.publish(task.id, placeholder.id, f"### 🔍 Audit: {head}\n\n{body}{cost_section}")
    if not repo_ctx:
        git(cwd, "tag", "-f", "audited")

    task = BOARD.get_task(task.id)
    BOARD.remove_labels(task, lambda t: t.startswith(FLAG_PREFIX))
    for f in flags:
        BOARD.add_label(task.id, FLAG_PREFIX + f, FLAG_COLOR)
    for mu in usage_summary.models:
        if mu.model and mu.model not in ("agy", "claude", "opencode"):
            mlbl = f"{MODEL_LABEL_PREFIX}{mu.model}"
            if mlbl not in task.label_titles:
                try:
                    BOARD.add_label(task.id, mlbl, MODEL_LABEL_COLOR)
                except Exception:
                    log.warning("task %s: attaching model label %s failed", task.id, mlbl, exc_info=True)
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


def handle_report_click(method: str, query: str, post_body: bytes = b"") -> tuple[int, str]:
    q = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
    try:
        task_id, comment_id, sig = q["t"], q.get("c", ""), q["s"]
    except KeyError:
        return 400, access_page("Bad link", "<p>This report link is malformed.</p>")

    if not verify_report_sig(SECRET, task_id, comment_id, sig):
        return 403, access_page("Invalid link", "<p>This report link's signature doesn't match.</p>")

    report = load_report(REPORTS_DIR, task_id, comment_id)
    if not report:
        return 404, access_page(
            "Report not found",
            f'<p>No saved report found for task #{html.escape(task_id)}.</p>'
            f'<p><a href="{BOARD.task_url(task_id)}">Back to the card</a></p>',
        )

    comments = BOARD.get_comments(task_id)
    still_open = bool(comments and (not comment_id or comments[-1].id == comment_id))
    card_url = BOARD.task_url(task_id)

    if method == "GET":
        return 200, render_report_page(report, card_url=card_url, still_open=still_open)

    if not still_open:
        return 409, render_report_page(report, card_url=card_url, still_open=False)

    form = urllib.parse.parse_qs(post_body.decode("utf-8", errors="replace"), keep_blank_values=True)
    action = (form.get("action") or ["submit"])[0]
    if action == "mark_done":
        task = BOARD.get_task(task_id)
        if BOARD.move_to(task, "done"):
            BOARD.post_comment(task_id, "✅ _Moved to Done._")
            flash = "Card moved to Done."
        else:
            flash = "Could not find a Done list on this board."
        return 200, render_report_page(report, card_url=card_url, still_open=False, flash=flash)

    if action == "audit":
        task = BOARD.get_task(task_id)
        if BOARD.move_to(task, BUCKET_AUDITING):
            enqueue(task_id)
            flash = "Card moved to Auditing — audit started."
        else:
            flash = "Could not find an Auditing list on this board."
        return 200, render_report_page(report, card_url=card_url, still_open=False, flash=flash)

    reply_text = format_qa_reply(report.get("questions") or [], form)
    BOARD.post_user_comment(task_id, reply_text)
    enqueue(task_id)
    return 200, render_report_page(
        report,
        card_url=card_url,
        still_open=False,
        flash="Reply posted to Planka — agent will resume shortly.",
    )


STANDARD_COLUMNS = [
    "Backlog",
    "To-Do",
    "Need Action",
    "Doing",
    "Pending",
    "Auditing",
    "Done",
    "Flagged",
]
_BOARDS_CACHE_LOCK = threading.Lock()
_BOARDS_CACHE: dict = {"ts": 0.0, "data": None}


def boards_overview(force: bool = False) -> dict:
    """Return board-to-repo/scratch mapping and column status for the dashboard."""
    now = time.time()
    with _BOARDS_CACHE_LOCK:
        if not force and _BOARDS_CACHE["data"] is not None and (now - _BOARDS_CACHE["ts"]) < 15.0:
            return _BOARDS_CACHE["data"]

    workdirs = discover_workdirs()
    board_rows: list[dict] = []
    board_keys: set[str] = set()
    error: str | None = None
    try:
        raw_boards = BOARD.list_boards()
    except Exception as e:
        raw_boards = []
        error = str(e)

    std_lower = [c.lower() for c in STANDARD_COLUMNS]
    for b in raw_boards:
        bname = str(b.get("name") or "")
        bkey = bname.strip().lower()
        if bkey:
            board_keys.add(bkey)
        subs = workdirs.get(bkey) or []
        if len(subs) == 1:
            target_path = str(subs[0][1])
        elif len(subs) > 1:
            target_path = str(subs[0][1].parent)
        else:
            target_path = str(TASKS_ROOT)
        cols = [str(c) for c in (b.get("columns") or [])]
        cols_lower = {c.lower() for c in cols}
        missing = [c for c, cl in zip(STANDARD_COLUMNS, std_lower) if cl not in cols_lower]
        board_rows.append(
            {
                "id": str(b.get("id") or ""),
                "name": bname,
                "url": str(b.get("url") or ""),
                "mode": WORKTREE_LABEL if subs else SCRATCH_LABEL,
                "repo_name": bkey if subs else None,
                "repos": [n for n, _ in subs],
                "target_path": target_path,
                "columns": cols,
                "missing_columns": missing,
                "card_count": int(b.get("card_count") or 0),
            }
        )

    unlinked = []
    for wname, subs in sorted(workdirs.items()):
        if wname not in board_keys:
            wpath = str(subs[0][1]) if len(subs) == 1 else str(subs[0][1].parent)
            unlinked.append({"name": wname, "path": wpath, "repos": [n for n, _ in subs]})
    result = {
        "boards": board_rows,
        "unlinked_repos": unlinked,
        "repos_root": str(REPOS_ROOT),
        "tasks_root": str(TASKS_ROOT),
        "standard_columns": STANDARD_COLUMNS,
        "error": error,
    }
    if error is None:
        with _BOARDS_CACHE_LOCK:
            _BOARDS_CACHE["ts"] = now
            _BOARDS_CACHE["data"] = result
    return result


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

    def _report(self, method: str):
        post_body = b""
        if method == "POST":
            post_body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        try:
            status, page = handle_report_click(method, urllib.parse.urlsplit(self.path).query, post_body)
        except Exception as e:
            log.exception("report page failed")
            status, page = 500, access_page("Error", f"<p>{html.escape(str(e))}</p>")
        self._send_html(status, page)

    def _send_json(self, status: int, data: dict):
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/" or path == "/ui":
            return self._send_html(200, DASHBOARD_HTML)
        if self.path.startswith("/access?"):
            return self._access("GET")
        if self.path.startswith("/setup?"):
            return self._setup("GET")
        if self.path.startswith("/report?"):
            return self._report("GET")
        if path == "/health":
            return self._send_json(200, {"ok": True, "board": BOARD.name, "agent": AGENT.name})
        if path == "/api/status":
            qs = urllib.parse.parse_qs(parsed.query)
            force_boards = (qs.get("force") or ["0"])[0] == "1"
            settings = read_editable_settings(ENV_FILE_PATH, ENV)
            return self._send_json(
                200,
                {
                    "ok": True,
                    "board": BOARD.name,
                    "agent": AGENT.name,
                    "trigger_label": TRIGGER_LABEL,
                    "max_concurrent": MAX_CONCURRENT,
                    "run_timeout": RUN_TIMEOUT,
                    "shutting_down": SHUTDOWN.is_set(),
                    "uptime_sec": max(0, int(time.time() - START_TIME)),
                    "active_jobs": active_jobs(),
                    "repos": sorted(discover_repos()),
                    "boards_overview": boards_overview(force=force_boards),
                    "planka_url": ENV.get("PLANKA_URL", "http://localhost:3000"),
                    "env_path": str(ENV_FILE_PATH),
                    "settings": settings,
                    "pricing": get_pricing_overview(ENV, settings),
                    "recent_logs": list_recent_logs(LOGS_DIR),
                },
            )
        if path == "/api/models":
            qs = urllib.parse.parse_qs(parsed.query)
            q = (qs.get("q") or [""])[0]
            try:
                limit = max(1, min(2000, int((qs.get("limit") or ["2000"])[0])))
            except ValueError:
                limit = 2000
            return self._send_json(
                200,
                {
                    "models": list_pricing_models(ENV, query=q, limit=limit),
                    "agy_models": list_agy_models(ENV),
                },
            )
        if path == "/api/logs":
            qs = urllib.parse.parse_qs(parsed.query)
            sid = (qs.get("id") or [""])[0]
            try:
                tail = int((qs.get("tail") or ["200"])[0])
            except ValueError:
                tail = 200
            try:
                return self._send_json(200, read_log_tail(LOGS_DIR, sid, tail=tail))
            except FileNotFoundError as e:
                return self._send_json(404, {"error": str(e)})
            except ValueError as e:
                return self._send_json(400, {"error": str(e)})
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if self.path.startswith("/access?"):
            return self._access("POST")
        if self.path.startswith("/setup?"):
            return self._setup("POST")
        if self.path.startswith("/report?"):
            return self._report("POST")
        if path == "/api/settings":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            try:
                data = json.loads(body.decode("utf-8"))
                raw_settings = data.get("settings") if isinstance(data, dict) else None
                if not isinstance(raw_settings, dict):
                    raise ValueError("Expected JSON object with 'settings'")
                updated = update_env_file(ENV_FILE_PATH, raw_settings)
                ENV.update(updated)
                restarted = False
                if data.get("restart"):
                    request_service_action("restart")
                    restarted = True
                return self._send_json(200, {"ok": True, "updated": updated, "restarting": restarted})
            except Exception as e:
                return self._send_json(400, {"error": str(e)})
        if path == "/api/service":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            try:
                data = json.loads(body.decode("utf-8"))
                action = str((data or {}).get("action") or "").strip().lower()
                msg = request_service_action(action)
                return self._send_json(200, {"ok": True, "message": msg})
            except Exception as e:
                return self._send_json(400, {"error": str(e)})
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
    if is_parked_in_backlog(task):
        return False
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
    ui_py = root / "ui.py"
    if ui_py.is_file():
        files.append(ui_py)
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

    SHUTDOWN_STATE["reason"] = "stop"
    SHUTDOWN_STATE["last_signal"] = 0.0

    def on_signal(signum, frame):
        now = time.time()
        if now - SHUTDOWN_STATE["last_signal"] < 1.5:
            return  # one Ctrl-C can arrive twice (terminal + uv forwarding it)
        SHUTDOWN_STATE["last_signal"] = now
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

        SHUTDOWN_STATE["reason"] = "reload"
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
    log.info("bridge stopped (%s)", SHUTDOWN_STATE["reason"])
    sys.exit(EXIT_RELOAD if SHUTDOWN_STATE["reason"] == "reload" else 0)


if __name__ == "__main__":
    main()
