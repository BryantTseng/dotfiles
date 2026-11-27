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
import json
import os
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
class Attachment:
    id: str
    name: str
    kind: str = "file"  # "file" | "link"
    url: str = ""
    mime_type: str = ""
    size_bytes: int = 0
    is_image: bool = False
    local_path: Path | None = None


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
    model: str | None = None


@dataclass
class SubagentAudit:
    id: str
    name: str
    prompt: str = ""
    tool_calls: list[str] = field(default_factory=list)
    final_report: str = ""


@dataclass
class ModelUsage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reported_cost_usd: float | None = None


@dataclass
class UsageSummary:
    models: list[ModelUsage] = field(default_factory=list)
    subagent_models: list[ModelUsage] = field(default_factory=list)
    reported_cost_usd: float | None = None
    estimated_cost_usd: float | None = None
    cost_note: str = ""

    @property
    def primary_cost_usd(self) -> float | None:
        return self.reported_cost_usd if self.reported_cost_usd is not None else self.estimated_cost_usd


# Published API-equivalent USD per 1M tokens: (input, output, cache_read, cache_write).
# Override or extend via `BRIDGE_MODEL_PRICING` JSON env var, e.g. '{"my-model": [1.5, 6.0, 0.15, 0.0]}'.
DEFAULT_MODEL_PRICING: dict[str, tuple[float, float, float, float]] = {
    # Claude 4.x / 3.x
    "claude-opus-4": (15.0, 75.0, 1.50, 18.75),
    "claude-sonnet-4": (3.0, 15.0, 0.30, 3.75),
    "claude-haiku-4": (1.0, 5.0, 0.10, 1.25),
    "claude-3-7-sonnet": (3.0, 15.0, 0.30, 3.75),
    "claude-3-5-sonnet": (3.0, 15.0, 0.30, 3.75),
    "claude-3-5-haiku": (0.80, 4.0, 0.08, 1.00),
    "claude-3-opus": (15.0, 75.0, 1.50, 18.75),
    # Gemini / Agy tiers (API-equivalent estimates)
    "gemini-2.5-pro": (1.25, 10.0, 0.3125, 0.0),
    "gemini-2.5-flash-lite": (0.10, 0.40, 0.025, 0.0),
    "gemini-2.5-flash": (0.30, 2.50, 0.075, 0.0),
    "gemini-3.5-flash-lite": (0.10, 0.40, 0.025, 0.0),
    "gemini-3.5-flash": (0.30, 2.50, 0.075, 0.0),
    "gemini-4-hydrogen-high": (2.50, 15.0, 0.625, 0.0),
    "gemini-4-hydrogen-medium": (1.25, 10.0, 0.3125, 0.0),
    "gemini-4-hydrogen-low": (0.50, 3.0, 0.125, 0.0),
    # Generic substring fallbacks
    "opus": (15.0, 75.0, 1.50, 18.75),
    "sonnet": (3.0, 15.0, 0.30, 3.75),
    "haiku": (1.0, 5.0, 0.10, 1.25),
    "flash-lite": (0.10, 0.40, 0.025, 0.0),
    "flash_lite": (0.10, 0.40, 0.025, 0.0),
    "flash": (0.30, 2.50, 0.075, 0.0),
    "hydrogen": (1.25, 10.0, 0.3125, 0.0),
    "pro": (1.25, 10.0, 0.3125, 0.0),
}


def _load_pricing_table(env: dict[str, str] | None = None) -> dict[str, tuple[float, float, float, float]]:
    table = dict(DEFAULT_MODEL_PRICING)
    raw = (env if env is not None else os.environ).get("BRIDGE_MODEL_PRICING", "").strip()
    if raw:
        try:
            overrides = json.loads(raw)
            if isinstance(overrides, dict):
                for k, v in overrides.items():
                    if isinstance(v, (list, tuple)) and len(v) >= 2:
                        inp = float(v[0])
                        out = float(v[1])
                        cr = float(v[2]) if len(v) >= 3 else inp * 0.1
                        cw = float(v[3]) if len(v) >= 4 else 0.0
                        table[str(k).strip().lower()] = (inp, out, cr, cw)
        except Exception:
            pass
    return table


def estimate_model_cost_usd(
    usage: ModelUsage, env: dict[str, str] | None = None
) -> float | None:
    """Return reported USD cost if present, else estimate from token counts & rate table."""
    if usage.reported_cost_usd is not None:
        return usage.reported_cost_usd
    if not (usage.input_tokens or usage.output_tokens or usage.cache_read_tokens or usage.cache_write_tokens):
        return 0.0
    table = _load_pricing_table(env)
    m = (usage.model or "").strip().lower()
    rates: tuple[float, float, float, float] | None = None
    # Prefer longest prefix match, then longest substring match.
    for key in sorted(table, key=len, reverse=True):
        if m.startswith(key):
            rates = table[key]
            break
    if rates is None:
        for key in sorted(table, key=len, reverse=True):
            if key in m:
                rates = table[key]
                break
    if rates is None:
        return None
    r_in, r_out, r_cr, r_cw = rates
    return (
        usage.input_tokens * r_in
        + usage.output_tokens * r_out
        + usage.cache_read_tokens * r_cr
        + usage.cache_write_tokens * r_cw
    ) / 1_000_000.0


def format_usage_summary(summary: UsageSummary) -> str:
    """Render a compact markdown cost & token summary for audit dossiers and comments."""
    all_rows = [(False, u) for u in summary.models] + [(True, u) for u in summary.subagent_models]
    if not all_rows and summary.primary_cost_usd is None:
        return ""

    cost = summary.primary_cost_usd
    if cost is not None:
        prefix = "" if summary.reported_cost_usd is not None else "~"
        cost_str = f"**{prefix}${cost:.4f}**" if cost < 0.10 else f"**{prefix}${cost:.2f}**"
    else:
        cost_str = "**unknown**"
    note = f" _{summary.cost_note}_" if summary.cost_note else ""

    tot_in = sum(u.input_tokens for _, u in all_rows)
    tot_cr = sum(u.cache_read_tokens for _, u in all_rows)
    tot_cw = sum(u.cache_write_tokens for _, u in all_rows)
    tot_out = sum(u.output_tokens for _, u in all_rows)

    tok_parts = [f"{tot_in:,} in"]
    if tot_cr:
        tok_parts.append(f"{tot_cr:,} cache-read")
    if tot_cw:
        tok_parts.append(f"{tot_cw:,} cache-write")
    tok_parts.append(f"{tot_out:,} out")

    lines = [f"💰 **Estimated Cost:** {cost_str} ({', '.join(tok_parts)}){note}"]
    for is_sub, u in all_rows:
        tag = f"subagent `{u.model}`" if is_sub else f"`{u.model}`"
        mc = estimate_model_cost_usd(u)
        mc_str = f" (~${mc:.4f})" if mc is not None else ""
        u_parts = [f"{u.input_tokens:,} in"]
        if u.cache_read_tokens:
            u_parts.append(f"{u.cache_read_tokens:,} cache-read")
        if u.cache_write_tokens:
            u_parts.append(f"{u.cache_write_tokens:,} cache-write")
        u_parts.append(f"{u.output_tokens:,} out")
        lines.append(f"- {tag}: {', '.join(u_parts)}{mc_str}")
    return "\n".join(lines)


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
    def attach_session_label(
        self, task_id: str, session_id: str, prefix: str, color: str, provider: str = "agy"
    ) -> None:
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

    def sync_checklist(self, task_id: str, list_name: str, items: list[tuple[str, bool]]) -> None:
        """Create or update a named checklist (`[(text, is_completed), ...]`) on a card."""
        return None

    def sync_attachments_in(
        self, task_id: str, dest_dir: Path, max_bytes: int = 25 * 1024 * 1024
    ) -> list[Attachment]:
        """Download file attachments on `task_id` into `dest_dir` and return attachment metadata."""
        return []

    def upload_attachment(
        self, task_id: str, file_path: Path, name: str | None = None
    ) -> Attachment | None:
        """Upload a local file as a native attachment on `task_id`."""
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
        on_subtask: Callable[[str, str, bool], None] | None = None,
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

    def agent_workdir(self, cwd: Path) -> Path:
        """Return the directory path where the agent sees `cwd` (e.g. `/work` in Docker)."""
        return cwd

    @abstractmethod
    def extract_tool_calls(self, session_id: str, log_dir: Path) -> list[str]:
        """Return human-readable tool-call lines from the host-side audit log for `session_id`."""

    def extract_subagent_audits(
        self, session_id: str, log_dir: Path, task_id: str | None = None
    ) -> list[SubagentAudit]:
        """Return per-subagent prompts, tool calls, and final reports for the auditor dossier."""
        return []

    def extract_usage_summary(
        self, session_id: str, log_dir: Path, task_id: str | None = None
    ) -> UsageSummary:
        """Return token counts and reported/estimated USD cost across parent + subagents."""
        return UsageSummary()

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
