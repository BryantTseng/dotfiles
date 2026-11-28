"""Built-in Web Management UI and JSON API for kanban-bridge."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from providers import (
    _load_openrouter_pricing,
    _lookup_model_rates,
    _pricing_cache_path,
    parse_model_list,
)

# Strict allowlist of non-secret operational settings editable via the Web UI.
# Secrets (API keys, tokens, passwords, webhook secrets) are NEVER read or exposed here.
EDITABLE_SETTINGS_KEYS: tuple[str, ...] = (
    "BRIDGE_AGENT_PROVIDER",
    "BRIDGE_OPENCODE_MODEL",
    "BRIDGE_WORKER_MODEL",
    "BRIDGE_ORCHESTRATOR_MODEL",
    "BRIDGE_AUDITOR_MODEL",
    "BRIDGE_OPENCODE_AGENT",
    "BRIDGE_AGY_MODEL",
    "BRIDGE_AGY_EFFORT",
    "BRIDGE_CLAUDE_MODEL",
    "BRIDGE_MAX_CONCURRENT",
    "BRIDGE_RUN_TIMEOUT",
)

VALID_PROVIDERS = {"opencode", "agy", "claude"}
VALID_EFFORTS = {"", "minimal", "low", "medium", "high", "max"}
_SAFE_SESSION_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def _strip_quotes(val: str) -> str:
    s = val.strip()
    if len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
        return s[1:-1]
    return s


def _format_env_value(val: str) -> str:
    s = val.strip()
    if any(ch in s for ch in (" ", "#", '"', "'")):
        escaped = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return s


def read_editable_settings(env_path: Path, env: dict[str, str]) -> dict[str, str]:
    """Read only allowlisted non-secret settings from env_path (falling back to env)."""
    out = {k: str(env.get(k, "") or "").strip() for k in EDITABLE_SETTINGS_KEYS}
    if not env_path.is_file():
        return out
    try:
        lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _ENV_LINE_RE.match(line)
        if not m:
            continue
        key, raw_val = m.group(1), m.group(2)
        if key in out:
            out[key] = _strip_quotes(raw_val)
    return out


def validate_settings_updates(raw: dict[str, Any]) -> dict[str, str]:
    """Validate and normalize incoming settings updates."""
    updates: dict[str, str] = {}
    for key, val in raw.items():
        if key not in EDITABLE_SETTINGS_KEYS:
            raise ValueError(f"Setting {key!r} is not editable via UI")
        sval = str(val if val is not None else "").strip()
        if "\n" in sval or "\r" in sval:
            raise ValueError(f"Setting {key!r} must be single-line")
        if key == "BRIDGE_AGENT_PROVIDER" and sval.lower() not in VALID_PROVIDERS:
            raise ValueError(f"BRIDGE_AGENT_PROVIDER must be one of {sorted(VALID_PROVIDERS)}")
        if key == "BRIDGE_AGY_EFFORT" and sval.lower() not in VALID_EFFORTS:
            raise ValueError(f"BRIDGE_AGY_EFFORT must be one of {sorted(VALID_EFFORTS - {''})} or empty")
        if key == "BRIDGE_MAX_CONCURRENT" and sval:
            try:
                n = int(sval)
            except ValueError as e:
                raise ValueError("BRIDGE_MAX_CONCURRENT must be an integer") from e
            if n < 1 or n > 16:
                raise ValueError("BRIDGE_MAX_CONCURRENT must be between 1 and 16")
            sval = str(n)
        if key == "BRIDGE_RUN_TIMEOUT" and sval:
            try:
                t = int(sval)
            except ValueError as e:
                raise ValueError("BRIDGE_RUN_TIMEOUT must be an integer") from e
            if t < 30 or t > 86400:
                raise ValueError("BRIDGE_RUN_TIMEOUT must be between 30 and 86400 seconds")
            sval = str(t)
        updates[key] = sval.lower() if key in ("BRIDGE_AGENT_PROVIDER", "BRIDGE_AGY_EFFORT") else sval
    return updates


def update_env_file(env_path: Path, raw_updates: dict[str, Any]) -> dict[str, str]:
    """Update allowlisted keys in env_path while preserving comments, order, and secrets."""
    updates = validate_settings_updates(raw_updates)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines: list[str] = []
    if env_path.is_file():
        existing_lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()

    seen: set[str] = set()
    new_lines: list[str] = []
    for line in existing_lines:
        m = _ENV_LINE_RE.match(line)
        if m and m.group(1) in updates:
            key = m.group(1)
            seen.add(key)
            new_lines.append(f"{key}={_format_env_value(updates[key])}")
        else:
            new_lines.append(line)

    for key in EDITABLE_SETTINGS_KEYS:
        if key in updates and key not in seen:
            new_lines.append(f"{key}={_format_env_value(updates[key])}")

    content = "\n".join(new_lines) + ("\n" if new_lines else "")
    tmp_path = env_path.with_suffix(".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp_path, env_path)
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass
    return updates


def get_pricing_overview(env: dict[str, str], settings: dict[str, str]) -> dict[str, Any]:
    """Return cached OpenRouter pricing info and per-1M rates for configured models."""
    table = _load_openrouter_pricing(env)
    cache_file = _pricing_cache_path(env)
    cache_age_sec: int | None = None
    if cache_file.is_file():
        try:
            cache_age_sec = max(0, int(time.time() - cache_file.stat().st_mtime))
        except OSError:
            pass

    model_fields = (
        "BRIDGE_OPENCODE_MODEL",
        "BRIDGE_WORKER_MODEL",
        "BRIDGE_ORCHESTRATOR_MODEL",
        "BRIDGE_AUDITOR_MODEL",
        "BRIDGE_AGY_MODEL",
        "BRIDGE_CLAUDE_MODEL",
    )
    configured: dict[str, list[dict[str, Any]]] = {}
    for field in model_fields:
        items: list[dict[str, Any]] = []
        for model_name in parse_model_list(settings.get(field)):
            rates = _lookup_model_rates(model_name, table)
            items.append(
                {
                    "model": model_name,
                    "input_per_m": round(rates[0] * 1_000_000, 4) if rates else None,
                    "output_per_m": round(rates[1] * 1_000_000, 4) if rates else None,
                    "cache_read_per_m": round(rates[2] * 1_000_000, 4) if rates else None,
                }
            )
        configured[field] = items

    return {
        "openrouter_key_configured": bool((env.get("OPENROUTER_API_KEY") or "").strip()),
        "cached_models_count": len(table),
        "cache_age_sec": cache_age_sec,
        "configured": configured,
    }


def list_pricing_models(env: dict[str, str], query: str = "", limit: int = 2000) -> list[dict[str, Any]]:
    """Return searchable OpenRouter models with per-1M USD rates."""
    table = _load_openrouter_pricing(env)
    q = query.strip().lower()
    out: list[dict[str, Any]] = []
    for model_id in sorted(table):
        if q and q not in model_id:
            continue
        r = table[model_id]
        out.append(
            {
                "id": model_id,
                "openrouter_id": f"openrouter/{model_id}",
                "input_per_m": round(r[0] * 1_000_000, 4),
                "output_per_m": round(r[1] * 1_000_000, 4),
            }
        )
        if len(out) >= limit:
            break
    return out


DEFAULT_AGY_MODELS: list[dict[str, str]] = [
    {"id": "gemini-4-neon-high", "label": "Gemini 4 Neon (High)"},
    {"id": "gemini-4-neon-xhigh", "label": "Gemini 4 Neon (XHigh)"},
    {"id": "gemini-4-neon-medium", "label": "Gemini 4 Neon (Medium)"},
    {"id": "gemini-4-neon-low", "label": "Gemini 4 Neon (Low)"},
    {"id": "gemini-4-carbon-medium", "label": "Gemini 4 Carbon (Medium)"},
    {"id": "gemini-4-carbon-high", "label": "Gemini 4 Carbon (High)"},
    {"id": "gemini-4-carbon-low", "label": "Gemini 4 Carbon (Low)"},
    {"id": "gemini-4-argon-medium", "label": "Gemini 4 Argon (Medium)"},
    {"id": "gemini-4-argon-high", "label": "Gemini 4 Argon (High)"},
    {"id": "gemini-4-argon-low", "label": "Gemini 4 Argon (Low)"},
    {"id": "gemini-4-hydrogen-high", "label": "Gemini 4 Hydrogen (High)"},
    {"id": "gemini-4-hydrogen-xhigh", "label": "Gemini 4 Hydrogen (XHigh)"},
    {"id": "gemini-4-hydrogen-medium", "label": "Gemini 4 Hydrogen (Medium)"},
    {"id": "gemini-4-hydrogen-low", "label": "Gemini 4 Hydrogen (Low)"},
    {"id": "gemini-pro-orchestrator-neon", "label": "Orchestrator (Neon)"},
    {"id": "gemini-pro-orchestrator", "label": "Orchestrator (Carbon)"},
    {"id": "gemini-pro-orchestrator-argon", "label": "Orchestrator (Argon)"},
    {"id": "gemini-3.8-flash-high", "label": "Gemini 3.8 Flash (High)"},
    {"id": "gemini-3.8-flash-medium", "label": "Gemini 3.8 Flash (Medium)"},
    {"id": "gemini-3.8-flash-low", "label": "Gemini 3.8 Flash (Low)"},
    {"id": "claude-opus-5-5-low", "label": "Claude Opus 5.5 (Low)"},
    {"id": "claude-opus-5-5-medium", "label": "Claude Opus 5.5 (Medium)"},
    {"id": "claude-opus-5-5-high", "label": "Claude Opus 5.5 (High)"},
    {"id": "claude-sonnet-5-5-low", "label": "Claude Sonnet 5.5 (Low)"},
    {"id": "claude-sonnet-5-5-medium", "label": "Claude Sonnet 5.5 (Medium)"},
    {"id": "claude-sonnet-5-5-high", "label": "Claude Sonnet 5.5 (High)"},
    {"id": "gpt-oss-120b-medium", "label": "GPT-OSS 120B (Medium)"},
]
_agy_models_cache: tuple[float, list[dict[str, str]]] | None = None


def _refresh_agy_models_bg(agy_bin: str) -> None:
    global _agy_models_cache
    try:
        proc = subprocess.run(
            [agy_bin, "models"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout:
            parsed: list[dict[str, str]] = []
            for raw in proc.stdout.splitlines():
                line = re.sub(r"\r.*?\r", "", raw).strip()
                if not line or line.startswith("Fetching "):
                    continue
                m = re.match(r"^([a-z0-9][a-z0-9._-]+)\s{2,}(.+)$", line)
                if m:
                    parsed.append({"id": m.group(1).strip(), "label": m.group(2).strip()})
            if parsed:
                _agy_models_cache = (time.time(), parsed)
    except Exception:
        pass


def list_agy_models(env: dict[str, str]) -> list[dict[str, str]]:
    """Return available `agy models` (cached 1h), refreshing in background without blocking UI."""
    global _agy_models_cache
    now = time.time()
    if _agy_models_cache and (now - _agy_models_cache[0]) < 3600:
        return _agy_models_cache[1]
    agy_bin = env.get("AGY_BIN") or shutil.which("agy")
    if agy_bin:
        import threading

        _agy_models_cache = (now, list(DEFAULT_AGY_MODELS))
        threading.Thread(target=_refresh_agy_models_bg, args=(agy_bin,), daemon=True).start()
    return list(DEFAULT_AGY_MODELS)


def list_recent_logs(logs_dir: Path, limit: int = 30) -> list[dict[str, Any]]:
    """List recent session JSONL logs sorted newest-first."""
    if not logs_dir.is_dir():
        return []
    entries: list[tuple[float, Path]] = []
    try:
        for p in logs_dir.iterdir():
            if p.is_file() and p.suffix == ".jsonl":
                try:
                    entries.append((p.stat().st_mtime, p))
                except OSError:
                    continue
    except OSError:
        return []
    entries.sort(key=lambda item: item[0], reverse=True)
    out: list[dict[str, Any]] = []
    for mtime, p in entries[:limit]:
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        out.append(
            {
                "session_id": p.stem,
                "size_bytes": size,
                "mtime": int(mtime),
            }
        )
    return out


def read_log_tail(logs_dir: Path, session_id: str, tail: int = 200) -> dict[str, Any]:
    """Safely read the last `tail` lines of a session log."""
    sid = (session_id or "").strip()
    if not sid or not _SAFE_SESSION_RE.match(sid):
        raise ValueError("Invalid session_id")
    path = (logs_dir / f"{sid}.jsonl").resolve()
    root = logs_dir.resolve()
    if root not in path.parents:
        raise ValueError("Invalid log path")
    if not path.is_file():
        raise FileNotFoundError(f"Log not found: {sid}")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    capped = max(1, min(1000, tail))
    return {
        "session_id": sid,
        "total_lines": total,
        "lines": lines[-capped:],
    }


DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kanban Bridge Console</title>
<style>
  :root {
    --bg: #0d1117; --card: #161b22; --border: #30363d; --text: #e6edf3;
    --muted: #8b949e; --accent: #58a6ff; --green: #3fb950; --yellow: #d29922;
    --red: #f85149; --input: #0b0f14;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.5; padding: 24px;
  }
  .wrap { max-width: 1120px; margin: 0 auto; display: grid; gap: 18px; }
  header {
    display: flex; flex-wrap: wrap; gap: 12px; align-items: center;
    justify-content: space-between; background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; padding: 16px 20px;
  }
  h1 { margin: 0; font-size: 1.25rem; display: flex; align-items: center; gap: 10px; }
  .pill {
    font-size: 0.78rem; padding: 3px 10px; border-radius: 999px;
    border: 1px solid var(--border); color: var(--muted); background: var(--input);
  }
  .pill.ok { color: var(--green); border-color: rgba(63,185,80,0.4); }
  .pill.warn { color: var(--yellow); border-color: rgba(210,153,34,0.4); }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; }
  button {
    cursor: pointer; border-radius: 8px; border: 1px solid var(--border);
    background: #21262d; color: var(--text); padding: 8px 14px; font-size: 0.88rem;
    font-weight: 500;
  }
  button:hover { border-color: var(--accent); }
  button.primary { background: #1f6feb; border-color: #388bfd; }
  button.danger { background: rgba(248,81,73,0.12); border-color: rgba(248,81,73,0.45); color: #ff7b72; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 16px 20px;
  }
  .card h2 { margin: 0 0 12px; font-size: 1rem; color: var(--text); }
  .stat-label { font-size: 0.78rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
  .stat-val { font-size: 1.15rem; font-weight: 600; margin-top: 4px; }
  .form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(310px, 1fr)); gap: 14px; }
  label { display: block; font-size: 0.82rem; color: var(--muted); margin-bottom: 5px; }
  input, select {
    width: 100%; background: var(--input); color: var(--text);
    border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px;
    font-size: 0.9rem; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  input:focus, select:focus { outline: none; border-color: var(--accent); }
  .hint { font-size: 0.76rem; color: var(--muted); margin-top: 4px; }
  .prices { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
  .price-tag {
    font-size: 0.74rem; padding: 2px 8px; border-radius: 6px;
    background: var(--input); border: 1px solid var(--border); color: var(--muted);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .price-tag strong { color: var(--text); font-weight: 500; }
  .hidden { display: none !important; }
  .model-stack { display: grid; gap: 6px; }
  .model-row { display: flex; gap: 6px; align-items: center; }
  .model-row select { flex: 1; min-width: 0; }
  button.mini { padding: 5px 10px; font-size: 0.78rem; }
  .row-actions { display: flex; gap: 10px; align-items: center; margin-top: 16px; flex-wrap: wrap; }
  #toast { font-size: 0.85rem; color: var(--green); }
  #toast.err { color: var(--red); }
  .logs-layout { display: grid; grid-template-columns: 280px 1fr; gap: 14px; }
  @media (max-width: 820px) { .logs-layout { grid-template-columns: 1fr; } }
  .log-list {
    max-height: 360px; overflow-y: auto; border: 1px solid var(--border);
    border-radius: 8px; background: var(--input);
  }
  .log-item {
    padding: 8px 10px; border-bottom: 1px solid var(--border); cursor: pointer;
    font-size: 0.82rem; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .log-item:hover, .log-item.active { background: #1f2937; }
  .log-meta { font-size: 0.72rem; color: var(--muted); font-family: sans-serif; }
  pre {
    margin: 0; background: var(--input); border: 1px solid var(--border); border-radius: 8px;
    padding: 12px; max-height: 360px; overflow: auto; font-size: 0.78rem;
    white-space: pre-wrap; word-break: break-word;
  }
  .board-table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  .board-table th, .board-table td {
    text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: middle;
  }
  .board-table th {
    font-size: 0.74rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em;
  }
  .board-table a { color: var(--accent); text-decoration: none; font-weight: 600; }
  .board-table a:hover { text-decoration: underline; }
  .board-table code { font-size: 0.78rem; color: var(--text); }
  .badge {
    display: inline-block; font-size: 0.74rem; padding: 2px 8px; border-radius: 999px;
    border: 1px solid var(--border); font-weight: 500;
  }
  .badge.worktree { color: #79c0ff; border-color: rgba(88,166,255,0.4); background: rgba(56,139,253,0.12); }
  .badge.scratch { color: var(--muted); background: var(--input); }
  .col-ok { color: var(--green); font-size: 0.8rem; }
  .col-warn { color: var(--yellow); font-size: 0.8rem; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Kanban Bridge Console <span id="status-pill" class="pill ok">Online</span></h1>
      <div class="hint" id="subtitle">Loading status...</div>
    </div>
    <div class="actions">
      <a id="planka-link" href="http://localhost:3000" target="_blank" rel="noopener"><button type="button">Open Planka</button></a>
      <button type="button" onclick="refreshAll(true)">Refresh</button>
      <button type="button" class="primary" onclick="serviceAction('restart')">Restart Bridge</button>
      <button type="button" class="danger" onclick="serviceAction('stop')">Stop Bridge</button>
    </div>
  </header>

  <div class="grid">
    <div class="card"><div class="stat-label">Active Runs</div><div class="stat-val" id="stat-active">-</div><div class="hint" id="stat-active-detail"></div></div>
    <div class="card"><div class="stat-label">Provider / Trigger</div><div class="stat-val" id="stat-provider">-</div><div class="hint" id="stat-repos"></div></div>
    <div class="card"><div class="stat-label">OpenRouter BYOK</div><div class="stat-val" id="stat-or">-</div><div class="hint" id="stat-pricing-cache"></div></div>
    <div class="card"><div class="stat-label">Uptime</div><div class="stat-val" id="stat-uptime">-</div><div class="hint" id="stat-env-path"></div></div>
  </div>

  <div class="card">
    <h2>Boards &amp; Workspaces</h2>
    <div class="hint" id="boards-hint" style="margin-bottom:10px;">
      Boards matching a repo directory name run in isolated git worktrees; other boards (e.g. General) run standalone scratch tasks.
    </div>
    <div style="overflow-x:auto;">
      <table class="board-table">
        <thead>
          <tr><th>Board</th><th>Mode</th><th>Target Directory</th><th>Columns</th><th>Cards</th></tr>
        </thead>
        <tbody id="boards-body">
          <tr><td colspan="5" class="hint">Loading boards...</td></tr>
        </tbody>
      </table>
    </div>
    <div class="hint" id="unlinked-repos" style="margin-top:10px;"></div>
  </div>

  <div class="card">
    <h2>Provider &amp; Model Fallback Settings</h2>
    <div class="hint" style="margin-bottom:12px;">
      Model fields accept a comma-separated fallback list (e.g. <code>openrouter/google/gemini-2.5-flash, openrouter/anthropic/claude-haiku-5.5</code>). On quota, rate-limit, or provider errors, the bridge automatically tries the next model in order.
    </div>
    <form id="settings-form" onsubmit="return saveSettings(false)">
      <div class="form-grid">
        <div data-providers="all">
          <label for="BRIDGE_AGENT_PROVIDER">Agent Provider</label>
          <select id="BRIDGE_AGENT_PROVIDER" name="BRIDGE_AGENT_PROVIDER" onchange="onProviderChange()">
            <option value="opencode">opencode (OpenRouter / multi-provider)</option>
            <option value="agy">agy (Antigravity)</option>
            <option value="claude">claude (Claude Code)</option>
          </select>
        </div>
        <div data-providers="all">
          <label for="BRIDGE_MAX_CONCURRENT">Max Concurrent Runs</label>
          <input id="BRIDGE_MAX_CONCURRENT" name="BRIDGE_MAX_CONCURRENT" type="number" min="1" max="16" oninput="formDirty=true">
        </div>
        <div data-providers="all">
          <label for="BRIDGE_RUN_TIMEOUT">Run Timeout (seconds)</label>
          <input id="BRIDGE_RUN_TIMEOUT" name="BRIDGE_RUN_TIMEOUT" type="number" min="30" max="86400" oninput="formDirty=true">
        </div>

        <div data-providers="opencode">
          <label>OpenCode Primary Model(s)</label>
          <div class="model-stack" id="stack-BRIDGE_OPENCODE_MODEL"></div>
          <div class="row-actions" style="margin-top:6px;">
            <button type="button" class="mini" onclick="addFallback('BRIDGE_OPENCODE_MODEL')">+ Add fallback</button>
          </div>
          <div class="prices" id="prices-BRIDGE_OPENCODE_MODEL"></div>
        </div>
        <div data-providers="opencode">
          <label>OpenCode Worker Subagent Model</label>
          <div class="model-stack" id="stack-BRIDGE_WORKER_MODEL"></div>
          <div class="prices" id="prices-BRIDGE_WORKER_MODEL"></div>
        </div>
        <div data-providers="opencode">
          <label for="BRIDGE_OPENCODE_AGENT">OpenCode Agent Name (optional)</label>
          <input id="BRIDGE_OPENCODE_AGENT" name="BRIDGE_OPENCODE_AGENT" placeholder="build" oninput="formDirty=true">
        </div>

        <div data-providers="agy">
          <label>Agy Model(s)</label>
          <div class="model-stack" id="stack-BRIDGE_AGY_MODEL"></div>
          <div class="row-actions" style="margin-top:6px;">
            <button type="button" class="mini" onclick="addFallback('BRIDGE_AGY_MODEL')">+ Add fallback</button>
          </div>
        </div>
        <div data-providers="agy">
          <label for="BRIDGE_AGY_EFFORT">Agy Effort</label>
          <select id="BRIDGE_AGY_EFFORT" name="BRIDGE_AGY_EFFORT" onchange="formDirty=true">
            <option value="">(default)</option>
            <option value="minimal">minimal</option>
            <option value="low">low</option>
            <option value="medium">medium</option>
            <option value="high">high</option>
            <option value="max">max</option>
          </select>
        </div>

        <div data-providers="claude">
          <label>Claude Model(s)</label>
          <div class="model-stack" id="stack-BRIDGE_CLAUDE_MODEL"></div>
          <div class="row-actions" style="margin-top:6px;">
            <button type="button" class="mini" onclick="addFallback('BRIDGE_CLAUDE_MODEL')">+ Add fallback</button>
          </div>
        </div>

        <div data-providers="all">
          <label>Orchestrator Model(s)</label>
          <div class="model-stack" id="stack-BRIDGE_ORCHESTRATOR_MODEL"></div>
          <div class="row-actions" style="margin-top:6px;">
            <button type="button" class="mini" onclick="addFallback('BRIDGE_ORCHESTRATOR_MODEL')">+ Add fallback</button>
          </div>
          <div class="prices" id="prices-BRIDGE_ORCHESTRATOR_MODEL"></div>
        </div>
        <div data-providers="all">
          <label>Auditor Model(s)</label>
          <div class="model-stack" id="stack-BRIDGE_AUDITOR_MODEL"></div>
          <div class="row-actions" style="margin-top:6px;">
            <button type="button" class="mini" onclick="addFallback('BRIDGE_AUDITOR_MODEL')">+ Add fallback</button>
          </div>
          <div class="prices" id="prices-BRIDGE_AUDITOR_MODEL"></div>
        </div>
      </div>
      <div class="row-actions">
        <button type="submit" class="primary">Save Settings</button>
        <button type="button" onclick="saveSettings(true)">Save &amp; Restart Bridge</button>
        <span id="toast"></span>
      </div>
    </form>
  </div>

  <div class="card">
    <h2>Recent Session Logs</h2>
    <div class="logs-layout">
      <div class="log-list" id="log-list"></div>
      <div>
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
          <span class="hint" id="log-title">Select a session log to inspect</span>
          <button type="button" onclick="reloadSelectedLog()">Reload Tail</button>
        </div>
        <pre id="log-content">(no log selected)</pre>
      </div>
    </div>
  </div>
</div>

<script>
let selectedSession = null;
let formDirty = false;
let orModels = [];
let agyModels = [];
let currentSettings = {};

const MODEL_FIELDS = [
  "BRIDGE_OPENCODE_MODEL",
  "BRIDGE_WORKER_MODEL",
  "BRIDGE_ORCHESTRATOR_MODEL",
  "BRIDGE_AUDITOR_MODEL",
  "BRIDGE_AGY_MODEL",
  "BRIDGE_CLAUDE_MODEL",
];
const SINGLE_MODEL_FIELDS = new Set(["BRIDGE_WORKER_MODEL"]);

const STATIC_CATALOGS = {
  opencode: [
    "openrouter/google/gemini-2.5-flash",
    "openrouter/google/gemini-2.5-pro",
    "openrouter/google/gemini-2.5-flash-lite",
    "openrouter/anthropic/claude-haiku-5.5",
    "openrouter/mistralai/devstral-2512",
    "openrouter/deepseek/deepseek-v4.1-flash",
    "openrouter/z-ai/glm-5.3",
  ],
  agy: [
    "gemini-4-neon-high",
    "gemini-4-neon-xhigh",
    "gemini-4-neon-medium",
    "gemini-4-neon-low",
    "gemini-4-carbon-medium",
    "gemini-4-carbon-high",
    "gemini-4-carbon-low",
    "gemini-4-argon-medium",
    "gemini-4-argon-high",
    "gemini-4-argon-low",
    "gemini-4-hydrogen-high",
    "gemini-4-hydrogen-xhigh",
    "gemini-4-hydrogen-medium",
    "gemini-4-hydrogen-low",
    "gemini-pro-orchestrator-neon",
    "gemini-pro-orchestrator",
    "gemini-pro-orchestrator-argon",
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-low",
    "claude-opus-5-5-low",
    "claude-opus-5-5-medium",
    "claude-opus-5-5-high",
    "claude-sonnet-5-5-low",
    "claude-sonnet-5-5-medium",
    "claude-sonnet-5-5-high",
    "gpt-oss-120b-medium",
  ],
  claude: [
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-haiku-4-5",
    "sonnet",
    "opus",
    "haiku",
  ],
};

function fmtDur(sec) {
  if (sec == null) return "-";
  if (sec < 60) return sec + "s";
  if (sec < 3600) return Math.floor(sec / 60) + "m " + (sec % 60) + "s";
  return Math.floor(sec / 3600) + "h " + Math.floor((sec % 3600) / 60) + "m";
}

function setToast(msg, isErr) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = isErr ? "err" : "";
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

function parseList(val) {
  if (!val) return [];
  return String(val).split(",").map(s => s.trim()).filter(Boolean);
}

function getFieldProvider(field) {
  if (field === "BRIDGE_AGY_MODEL") return "agy";
  if (field === "BRIDGE_CLAUDE_MODEL") return "claude";
  if (field === "BRIDGE_OPENCODE_MODEL" || field === "BRIDGE_WORKER_MODEL") return "opencode";
  return document.getElementById("BRIDGE_AGENT_PROVIDER").value || "opencode";
}

function valueMatchesProvider(val, prov) {
  if (!val) return true;
  if (prov === "opencode") return val.startsWith("openrouter/") || !val.includes("gemini-4-");
  return !val.startsWith("openrouter/");
}

function buildOptionsForField(field, selectedValues) {
  const prov = getFieldProvider(field);
  const opts = [];
  const seen = new Set();

  function addOpt(val, label) {
    if (!val || seen.has(val)) return;
    seen.add(val);
    opts.push({ val, label: label || val });
  }

  for (const v of selectedValues) {
    if (valueMatchesProvider(v, prov)) addOpt(v, v);
  }
  if (prov === "opencode") {
    for (const m of orModels) {
      addOpt(m.openrouter_id, `${m.openrouter_id} ($${m.input_per_m}/M in · $${m.output_per_m}/M out)`);
    }
    for (const v of STATIC_CATALOGS.opencode) addOpt(v, v);
  } else if (prov === "agy") {
    for (const m of agyModels) {
      addOpt(m.id, m.label ? `${m.id} — ${m.label}` : m.id);
    }
    for (const v of STATIC_CATALOGS.agy) addOpt(v, v);
  } else {
    for (const v of (STATIC_CATALOGS[prov] || [])) addOpt(v, v);
  }
  return opts;
}

function renderModelField(field, values) {
  const stack = document.getElementById("stack-" + field);
  if (!stack) return;
  const prov = getFieldProvider(field);
  const filtered = values.filter(v => valueMatchesProvider(v, prov));
  const list = filtered.length ? filtered : [""];
  const opts = buildOptionsForField(field, list);
  const allowMulti = !SINGLE_MODEL_FIELDS.has(field);

  stack.innerHTML = list.map((val, idx) => {
    const optHtml = ['<option value="">(unset / default)</option>']
      .concat(opts.map(o => `<option value="${escapeHtml(o.val)}"${o.val === val ? " selected" : ""}>${escapeHtml(o.label)}</option>`))
      .join("");
    const rmBtn = (allowMulti && idx > 0)
      ? `<button type="button" class="mini danger" onclick="removeFallback('${field}', ${idx})">×</button>`
      : "";
    return `<div class="model-row">
      <select data-model-field="${field}" onchange="onModelSelectChange('${field}')">${optHtml}</select>
      ${rmBtn}
    </div>`;
  }).join("");
  updateLivePrices(field);
}

function readModelFieldValues(field) {
  const selects = document.querySelectorAll(`select[data-model-field="${field}"]`);
  return Array.from(selects).map(s => s.value.trim()).filter(Boolean);
}

function onModelSelectChange(field) {
  formDirty = true;
  updateLivePrices(field);
}

function addFallback(field) {
  formDirty = true;
  const vals = readModelFieldValues(field);
  vals.push("");
  renderModelField(field, vals);
}

function removeFallback(field, idx) {
  formDirty = true;
  const stack = document.getElementById("stack-" + field);
  const vals = Array.from(stack.querySelectorAll("select")).map(s => s.value.trim());
  vals.splice(idx, 1);
  renderModelField(field, vals.filter((v, i) => v || i === 0));
}

function updateLivePrices(field) {
  const box = document.getElementById("prices-" + field);
  if (!box) return;
  if (getFieldProvider(field) !== "opencode") {
    box.innerHTML = "";
    return;
  }
  const vals = readModelFieldValues(field);
  const byId = new Map(orModels.map(m => [m.openrouter_id, m]));
  box.innerHTML = vals.map(v => {
    const info = byId.get(v);
    const rate = info ? `$${info.input_per_m}/M in · $${info.output_per_m}/M out` : "no cached price";
    return `<span class="price-tag"><strong>${escapeHtml(v)}</strong>: ${rate}</span>`;
  }).join("");
}

function updateProviderVisibility() {
  const prov = document.getElementById("BRIDGE_AGENT_PROVIDER").value || "opencode";
  for (const el of document.querySelectorAll("[data-providers]")) {
    const allowed = el.getAttribute("data-providers").split(",");
    el.classList.toggle("hidden", !allowed.includes("all") && !allowed.includes(prov));
  }
}

function onProviderChange() {
  formDirty = true;
  updateProviderVisibility();
  renderModelField("BRIDGE_ORCHESTRATOR_MODEL", readModelFieldValues("BRIDGE_ORCHESTRATOR_MODEL"));
  renderModelField("BRIDGE_AUDITOR_MODEL", readModelFieldValues("BRIDGE_AUDITOR_MODEL"));
}

async function refreshAll(force = false) {
  try {
    const res = await fetch(force ? "/api/status?force=1" : "/api/status");
    const data = await res.json();
    const pill = document.getElementById("status-pill");
    pill.textContent = data.shutting_down ? "Draining..." : "Online";
    pill.className = "pill " + (data.shutting_down ? "warn" : "ok");
    document.getElementById("subtitle").textContent =
      `Board: ${data.board} · Trigger: ${data.trigger_label} · Agent: ${data.agent}`;
    if (data.planka_url) document.getElementById("planka-link").href = data.planka_url;

    const jobs = Object.entries(data.active_jobs || {});
    document.getElementById("stat-active").textContent = `${jobs.length} / ${data.max_concurrent}`;
    document.getElementById("stat-active-detail").textContent =
      jobs.length ? jobs.map(([id, kind]) => `${id} (${kind})`).join(", ") : "Idle";
    document.getElementById("stat-provider").textContent = `${data.agent} (${data.trigger_label})`;
    document.getElementById("stat-repos").textContent =
      "Repos: " + (data.repos && data.repos.length ? data.repos.join(", ") : "none");
    document.getElementById("stat-or").textContent =
      data.pricing.openrouter_key_configured ? "Key Configured" : "Key Not Set";
    document.getElementById("stat-pricing-cache").textContent =
      `${data.pricing.cached_models_count} models cached` +
      (data.pricing.cache_age_sec != null ? ` (${fmtDur(data.pricing.cache_age_sec)} ago)` : "");
    document.getElementById("stat-uptime").textContent = fmtDur(data.uptime_sec);
    document.getElementById("stat-env-path").textContent = data.env_path || "";

    const bo = data.boards_overview || {};
    const boardsHint = document.getElementById("boards-hint");
    if (bo.repos_root) {
      boardsHint.innerHTML =
        `Boards matching a repo directory in <code>${escapeHtml(bo.repos_root)}</code> run in isolated git <code>worktree</code>s; other boards run standalone <code>scratch</code> tasks in <code>${escapeHtml(bo.tasks_root || "")}</code>. Standard columns: <code>${(bo.standard_columns || []).map(escapeHtml).join(" · ")}</code>`;
    }
    const boardsBody = document.getElementById("boards-body");
    const boardsList = bo.boards || [];
    if (bo.error) {
      boardsBody.innerHTML = `<tr><td colspan="5" class="col-warn">Failed to query boards: ${escapeHtml(bo.error)}</td></tr>`;
    } else if (!boardsList.length) {
      boardsBody.innerHTML = '<tr><td colspan="5" class="hint">No boards found</td></tr>';
    } else {
      const totalCols = (bo.standard_columns || []).length || 8;
      boardsBody.innerHTML = boardsList.map(b => {
        const isWt = b.mode === "worktree";
        const subRepos = b.repos || [];
        const wtLabel = subRepos.length > 1
          ? `worktree (${subRepos.length}: ${subRepos.map(escapeHtml).join(", ")})`
          : "worktree repo";
        const badge = `<span class="badge ${isWt ? "worktree" : "scratch"}">${isWt ? wtLabel : "general scratch"}</span>`;
        const missing = b.missing_columns || [];
        const colCell = missing.length
          ? `<span class="col-warn">Missing: ${missing.map(escapeHtml).join(", ")}</span>`
          : `<span class="col-ok">${b.columns.length}/${totalCols} OK</span>`;
        const link = b.url
          ? `<a href="${escapeHtml(b.url)}" target="_blank" rel="noopener">${escapeHtml(b.name)}</a>`
          : escapeHtml(b.name);
        return `<tr>
          <td>${link}</td>
          <td>${badge}</td>
          <td><code>${escapeHtml(b.target_path || "")}</code></td>
          <td>${colCell}</td>
          <td>${b.card_count}</td>
        </tr>`;
      }).join("");
    }
    const unlinkedEl = document.getElementById("unlinked-repos");
    const unlinked = bo.unlinked_repos || [];
    if (unlinked.length) {
      unlinkedEl.innerHTML =
        "Repos without a matching board: " +
        unlinked.map(r => `<code>${escapeHtml(r.name)}</code> (${escapeHtml(r.path)})`).join(", ") +
        " — create a board with the same name and the 8 standard columns to route worktree tasks there.";
    } else {
      unlinkedEl.textContent = "";
    }

    currentSettings = data.settings || {};
    if (!formDirty) {
      for (const k of ["BRIDGE_AGENT_PROVIDER", "BRIDGE_MAX_CONCURRENT", "BRIDGE_RUN_TIMEOUT", "BRIDGE_OPENCODE_AGENT", "BRIDGE_AGY_EFFORT"]) {
        const el = document.getElementById(k);
        if (el) el.value = currentSettings[k] || "";
      }
      updateProviderVisibility();
      for (const f of MODEL_FIELDS) {
        renderModelField(f, parseList(currentSettings[f]));
      }
    }

    const listEl = document.getElementById("log-list");
    listEl.innerHTML = (data.recent_logs || []).map(l => {
      const dt = new Date(l.mtime * 1000).toLocaleString();
      const kb = (l.size_bytes / 1024).toFixed(1);
      const cls = selectedSession === l.session_id ? "log-item active" : "log-item";
      return `<div class="${cls}" onclick="selectLog('${escapeHtml(l.session_id)}')">
        <div>${escapeHtml(l.session_id)}</div>
        <div class="log-meta">${dt} · ${kb} KB</div>
      </div>`;
    }).join("") || '<div class="log-item">No session logs found</div>';
  } catch (e) {
    setToast("Failed to load status: " + e.message, true);
  }
}

async function loadModelSuggestions() {
  try {
    const res = await fetch("/api/models?limit=2000");
    const data = await res.json();
    orModels = data.models || [];
    agyModels = data.agy_models || [];
    if (!formDirty) {
      for (const f of MODEL_FIELDS) {
        renderModelField(f, parseList(currentSettings[f]));
      }
    }
  } catch (_) {}
}

async function saveSettings(andRestart) {
  const form = document.getElementById("settings-form");
  const payload = { restart: !!andRestart, settings: {} };
  for (const el of form.elements) {
    if (el.name) payload.settings[el.name] = el.value;
  }
  for (const f of MODEL_FIELDS) {
    payload.settings[f] = readModelFieldValues(f).join(", ");
  }
  try {
    const res = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Save failed");
    formDirty = false;
    setToast(andRestart ? "Settings saved. Restarting bridge..." : "Settings saved to env file.", false);
    if (!andRestart) await refreshAll();
  } catch (e) {
    setToast(e.message, true);
  }
  return false;
}

async function serviceAction(action) {
  if (action === "stop" && !confirm("Stop kanban-bridge cleanly after active runs finish?")) return;
  try {
    const res = await fetch("/api/service", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Action failed");
    setToast(data.message || "OK", false);
    setTimeout(refreshAll, 1500);
  } catch (e) {
    setToast(e.message, true);
  }
}

async function selectLog(sid) {
  selectedSession = sid;
  await reloadSelectedLog();
  await refreshAll();
}

async function reloadSelectedLog() {
  if (!selectedSession) return;
  document.getElementById("log-title").textContent = "Session: " + selectedSession;
  try {
    const res = await fetch(`/api/logs?id=${encodeURIComponent(selectedSession)}&tail=200`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Failed to read log");
    document.getElementById("log-content").textContent = (data.lines || []).join("\\n") || "(empty log)";
  } catch (e) {
    document.getElementById("log-content").textContent = "Error: " + e.message;
  }
}

refreshAll().then(loadModelSuggestions);
setInterval(refreshAll, 5000);
</script>
</body>
</html>
"""
