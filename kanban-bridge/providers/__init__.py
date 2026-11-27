"""Pluggable provider interfaces for kanban-bridge.

Adding a new board (e.g. Trello, Linear, GitHub Projects):
  1. Create `providers/<name>.py` implementing `BoardProvider`.
  2. Register it in `load_board_provider()` below.
  3. Set `BRIDGE_BOARD_PROVIDER=<name>` in `~/.config/kanban-bridge/env`.

Adding a new agent runtime (e.g. Claude Code, Codex):
  1. Create `providers/<name>.py` implementing `AgentProvider`.
  2. Register it in `load_agent_provider()` below.
  3. Set `BRIDGE_AGENT_PROVIDER=<name>` in `~/.config/kanban-bridge/env`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
import threading
from typing import Callable

# Thread-safe registry of ephemeral per-run auth-proxy tokens (used by containerized providers
# like ClaudeAgent so the real OAuth token never enters a container).
_proxy_tokens: dict[str, str] = {}
_proxy_tokens_lock = threading.Lock()


def register_proxy_token(token: str, task_id: str) -> None:
    with _proxy_tokens_lock:
        _proxy_tokens[token] = task_id


def unregister_proxy_token(token: str) -> None:
    with _proxy_tokens_lock:
        _proxy_tokens.pop(token, None)


def is_valid_proxy_token(token: str) -> bool:
    if not token:
        return False
    with _proxy_tokens_lock:
        return token in _proxy_tokens


@dataclass
class Label:
    id: str
    title: str
    color: str = ""


@dataclass
class Comment:
    id: str
    body_text: str
    body_raw: str = ""
    author_username: str = ""


@dataclass
class Task:
    id: str
    title: str
    description_text: str = ""
    project_id: str = ""
    project_name: str = ""
    done: bool = False
    index: str = ""
    labels: list[Label] = field(default_factory=list)

    @property
    def label_titles(self) -> list[str]:
        return [l.title for l in self.labels]


@dataclass
class WebhookEvent:
    event_type: str  # "task.created" | "task.updated" | "task.comment.created"
    task_id: str
    comment: Comment | None = None
    doer_username: str | None = None


@dataclass
class AgentResult:
    text: str
    session_id: str | None = None
    is_error: bool = False


class BoardProvider(ABC):
    """Abstract interface for a kanban board backend (Planka, Trello, Linear, etc.)."""

    name: str = "base"
    webhook_path: str = "/webhook"
    reconcile_interval: int = 60

    @abstractmethod
    def ensure_setup(self) -> None:
        """Idempotent startup/reconcile setup (sharing boards, registering webhooks, etc.)."""

    @abstractmethod
    def get_task(self, task_id: str) -> Task:
        """Fetch a single task/card by ID."""

    @abstractmethod
    def list_triggered_tasks(self, trigger_label: str) -> list[Task]:
        """Return all active/done cards across boards carrying `trigger_label`."""

    @abstractmethod
    def get_comments(self, task_id: str) -> list[Comment]:
        """Return comments on a card in chronological (oldest -> newest) order."""

    @abstractmethod
    def post_comment(self, task_id: str, md_text: str) -> Comment:
        """Post a comment as the agent/bot."""

    @abstractmethod
    def edit_comment(self, task_id: str, comment_id: str, md_text: str) -> None:
        """Edit an existing agent/bot comment."""

    @abstractmethod
    def delete_comment(self, task_id: str, comment_id: str) -> None:
        """Delete a comment (best-effort)."""

    @abstractmethod
    def publish(self, task_id: str, placeholder_id: str, md_text: str) -> None:
        """Replace a placeholder comment with final text, falling back safely if edit fails."""

    @abstractmethod
    def post_user_comment(self, task_id: str, text: str) -> None:
        """Post a comment as the human user (used by web Approve/Deny confirmation clicks)."""

    @abstractmethod
    def add_label(self, task_id: str, title: str, color: str) -> None:
        """Attach a label to a card, creating the label first if needed."""

    @abstractmethod
    def attach_session_label(self, task_id: str, session_id: str, prefix: str, color: str) -> None:
        """Persist the session/conversation ID on the card."""

    @abstractmethod
    def remove_labels(self, task: Task, predicate: Callable[[str], bool]) -> None:
        """Remove all labels on `task` whose title matches `predicate`."""

    @abstractmethod
    def move_to(self, task: Task, bucket_name: str) -> bool:
        """Move `task` to the named column (`'done'` uses the board's done list). Returns success."""

    @abstractmethod
    def current_bucket(self, task: Task) -> str | None:
        """Return lower-cased column name the card is currently in, or None."""

    @abstractmethod
    def task_url(self, task_id: str) -> str:
        """Return browser URL for opening this card."""

    @abstractmethod
    def is_from_bot(self, comment: Comment, doer_username: str | None = None) -> bool:
        """Return True if `comment` was authored by the bridge/agent rather than the human user."""

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent | None:
        """Verify and parse an incoming webhook payload.

        Raise PermissionError on invalid signature. Return None if irrelevant or unsupported
        (e.g. for polling-only providers).
        """
        return None


class AgentProvider(ABC):
    """Abstract interface for an AI coding agent runtime (Agy, Claude Code, etc.)."""

    name: str = "base"
    needs_auth_proxy: bool = False

    @abstractmethod
    def run_task(
        self,
        task: Task,
        session_id: str | None,
        prompt: str,
        system_instructions: str,
        resume: bool,
        cwd: Path,
        env_extra: dict[str, str],
        on_progress: Callable[[int, int, str], None] | None,
        on_session_id: Callable[[str], None] | None,
        log_dir: Path,
        timeout: int,
        procs: set,
    ) -> AgentResult:
        """Run one agent turn on `task` inside `cwd`."""

    @abstractmethod
    def run_audit(
        self,
        task: Task,
        auditor_prompt: str,
        dossier: str,
        cwd: Path,
        timeout: int,
        procs: set,
    ) -> str:
        """Run a read-only audit pass over `dossier` in `cwd` and return its markdown output."""

    @abstractmethod
    def extract_tool_calls(self, session_id: str, log_dir: Path) -> list[str]:
        """Return human-readable tool-call lines from the host-side audit log for `session_id`."""

    def force_cleanup(self) -> None:
        """Optional cleanup of leftover agent processes/containers on startup or forced stop."""
        return None


def load_board_provider(name: str, env: dict[str, str]) -> BoardProvider:
    key = (name or "planka").strip().lower()
    if key == "planka":
        from providers.planka import PlankaBoard

        return PlankaBoard.from_env(env)
    raise ValueError(f"Unknown board provider {name!r}. Supported: planka")


def load_agent_provider(name: str, env: dict[str, str]) -> AgentProvider:
    key = (name or "agy").strip().lower()
    if key == "agy":
        from providers.agy import AgyAgent

        return AgyAgent.from_env(env)
    if key == "claude":
        from providers.claude import ClaudeAgent

        return ClaudeAgent.from_env(env)
    raise ValueError(f"Unknown agent provider {name!r}. Supported: agy, claude")
