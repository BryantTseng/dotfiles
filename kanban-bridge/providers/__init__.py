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
import logging
import os
from pathlib import Path
import re
import ssl
import threading
import time
from typing import Callable
import urllib.request

log = logging.getLogger("bridge.providers")

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


OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
PRICING_CACHE_TTL = 6 * 3600  # 6 hours
_DATE_SUFFIX_RE = re.compile(r"-20\d{6}$")
_EFFORT_SUFFIX_RE = re.compile(r"-(?:minimal|low|medium|high|max)$")
_pricing_lock = threading.Lock()
_pricing_mem_cache: tuple[float, dict[str, tuple[float, float, float, float]]] | None = None


def _pricing_cache_path(env: dict[str, str] | None = None) -> Path:
    src = env if env is not None else os.environ
    state_root = Path(src.get("BRIDGE_STATE_ROOT", "~/.local/share/kanban-bridge")).expanduser()
    return state_root / "openrouter-pricing-cache.json"


def _parse_openrouter_models(payload: dict) -> dict[str, tuple[float, float, float, float]]:
    """Extract per-token USD rates `(in, out, cache_read, cache_write)` from OpenRouter's /models JSON."""
    rates: dict[str, tuple[float, float, float, float]] = {}
    for m in payload.get("data") or []:
        mid = str(m.get("id") or "").strip().lower()
        if not mid or ":" in mid:  # skip :batch, :free, :nitro, etc.
            continue
        p = m.get("pricing") or {}
        try:
            r_in = float(p.get("prompt") or 0.0)
            r_out = float(p.get("completion") or 0.0)
            r_cr = float(p.get("input_cache_read") or 0.0)
            r_cw = float(p.get("input_cache_write") or 0.0)
        except (TypeError, ValueError):
            continue
        if r_in < 0 or r_out < 0:
            continue
        tuple_val = (r_in, r_out, r_cr, r_cw)
        rates[mid] = tuple_val
        if "/" in mid:
            slug = mid.split("/", 1)[1]
            rates.setdefault(slug, tuple_val)
    return rates


def _load_openrouter_pricing(
    env: dict[str, str] | None = None,
    *,
    now: float | None = None,
    fetcher: Callable[[str], dict] | None = None,
) -> dict[str, tuple[float, float, float, float]]:
    """Load OpenRouter per-token USD pricing with a 6-hour disk + in-memory cache."""
    global _pricing_mem_cache
    src = env if env is not None else os.environ
    ttl = int(src.get("BRIDGE_PRICING_CACHE_TTL", str(PRICING_CACHE_TTL)) or PRICING_CACHE_TTL)
    url = src.get("BRIDGE_PRICING_URL", OPENROUTER_MODELS_URL).strip()
    ts_now = time.time() if now is None else now
    cache_file = _pricing_cache_path(src)

    with _pricing_lock:
        if fetcher is None and _pricing_mem_cache is not None and (ts_now - _pricing_mem_cache[0]) < ttl:
            return _pricing_mem_cache[1]

        stale: dict[str, tuple[float, float, float, float]] | None = None
        if cache_file.is_file():
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                fetched_at = float(cached.get("fetched_at") or 0.0)
                raw_models = cached.get("models") or {}
                parsed = {
                    k: (float(v[0]), float(v[1]), float(v[2]), float(v[3]))
                    for k, v in raw_models.items()
                    if isinstance(v, list) and len(v) == 4
                }
                if parsed:
                    stale = parsed
                    if (ts_now - fetched_at) < ttl:
                        if fetcher is None:
                            _pricing_mem_cache = (fetched_at, parsed)
                        return parsed
            except Exception:
                log.warning("could not read pricing cache %s", cache_file, exc_info=True)

        if not url:
            return stale or {}

        try:
            if fetcher is not None:
                payload = fetcher(url)
            else:
                ctx = ssl.create_default_context(cafile=src.get("SSL_CERT_FILE") or None)
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "kanban-bridge/1.0", "Accept": "application/json"},
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                    payload = json.loads(resp.read())
            fresh = _parse_openrouter_models(payload)
            if fresh:
                try:
                    cache_file.parent.mkdir(parents=True, exist_ok=True)
                    tmp = cache_file.with_suffix(".json.tmp")
                    tmp.write_text(
                        json.dumps({"fetched_at": ts_now, "models": {k: list(v) for k, v in fresh.items()}}),
                        encoding="utf-8",
                    )
                    tmp.replace(cache_file)
                except Exception:
                    log.warning("could not write pricing cache %s", cache_file, exc_info=True)
                if fetcher is None:
                    _pricing_mem_cache = (ts_now, fresh)
                return fresh
        except Exception as e:
            log.warning("fetching OpenRouter pricing failed (%s); using cached data if available", e)

        return stale or {}


def _load_pricing_table(
    env: dict[str, str] | None = None,
    *,
    now: float | None = None,
    fetcher: Callable[[str], dict] | None = None,
) -> dict[str, tuple[float, float, float, float]]:
    """Return per-token USD rates from OpenRouter (6h cache) merged with `BRIDGE_MODEL_PRICING` overrides."""
    table = dict(_load_openrouter_pricing(env, now=now, fetcher=fetcher))
    raw = (env if env is not None else os.environ).get("BRIDGE_MODEL_PRICING", "").strip()
    if raw:
        try:
            overrides = json.loads(raw)
            if isinstance(overrides, dict):
                for k, v in overrides.items():
                    if isinstance(v, (list, tuple)) and len(v) >= 2:
                        # BRIDGE_MODEL_PRICING values are USD per 1M tokens for human ergonomics.
                        inp = float(v[0]) / 1_000_000.0
                        out = float(v[1]) / 1_000_000.0
                        cr = (float(v[2]) / 1_000_000.0) if len(v) >= 3 else inp * 0.1
                        cw = (float(v[3]) / 1_000_000.0) if len(v) >= 4 else 0.0
                        table[str(k).strip().lower()] = (inp, out, cr, cw)
        except Exception:
            pass
    return table


def _lookup_model_rates(
    model_name: str, table: dict[str, tuple[float, float, float, float]]
) -> tuple[float, float, float, float] | None:
    if not model_name or not table:
        return None
    m = model_name.strip().lower()
    norm_index: dict[str, tuple[float, float, float, float]] = {}
    for k, v in table.items():
        norm_index[k] = v
        norm_index.setdefault(k.replace(".", "-"), v)

    candidates = [m]
    if "/" in m:
        candidates.append(m.split("/", 1)[1])
    for base in list(candidates):
        stripped_date = _DATE_SUFFIX_RE.sub("", base)
        stripped_effort = _EFFORT_SUFFIX_RE.sub("", stripped_date)
        for c in (stripped_date, stripped_effort):
            if c and c not in candidates:
                candidates.append(c)

    for c in candidates:
        for variant in (c, c.replace(".", "-")):
            if variant in norm_index:
                return norm_index[variant]

    # Longest boundary-aligned prefix match (e.g. `gemini-4-hydrogen-medium` -> `gemini-4-hydrogen`)
    for c in candidates:
        cn = c.replace(".", "-")
        for key in sorted(norm_index, key=len, reverse=True):
            if cn.startswith(key + "-"):
                return norm_index[key]
    return None


def estimate_model_cost_usd(
    usage: ModelUsage,
    env: dict[str, str] | None = None,
    *,
    now: float | None = None,
    fetcher: Callable[[str], dict] | None = None,
) -> float | None:
    """Return reported USD cost if present, else estimate from OpenRouter (6h cache) / overrides."""
    if usage.reported_cost_usd is not None:
        return usage.reported_cost_usd
    if not (usage.input_tokens or usage.output_tokens or usage.cache_read_tokens or usage.cache_write_tokens):
        return 0.0
    table = _load_pricing_table(env, now=now, fetcher=fetcher)
    rates = _lookup_model_rates(usage.model, table)
    if rates is None:
        return None
    r_in, r_out, r_cr, r_cw = rates
    return (
        usage.input_tokens * r_in
        + usage.output_tokens * r_out
        + usage.cache_read_tokens * r_cr
        + usage.cache_write_tokens * r_cw
    )


def format_usage_summary(summary: UsageSummary, env: dict[str, str] | None = None) -> str:
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
        mc = estimate_model_cost_usd(u, env=env)
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
