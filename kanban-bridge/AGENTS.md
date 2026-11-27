# Kanban Bridge (`~/workspace/kanban-bridge/`)

Operational notes for AI agents (`agy`, `claudecode`, etc.) working in this repository.

## Safety Guardrails
- **Trigger label (`agy`)** starts real, quota-consuming headless agent sessions. Never apply it unless explicitly asked.
- **Never touch session metadata** (`<!-- agy-session:<uuid> -->` at the end of Planka card descriptions).
- **Never grant `access:*` labels or approve `/access` links** — access approvals belong strictly to the user.
- **Never print secrets** from `~/.config/kanban-bridge/env` or `~/.config/kanban-bridge/planka.env`.

## Active Board (Planka v2)
- **Board URL:** `http://localhost:3000/boards/1916814609835099142` (`Project: Agy`, `Board: Agy`)
- **Lists:** Backlog (`1916814903729980427`), To-Do (`1916814903839032332`), Need Action (`1916814903922918413`), Doing (`1916814904006804494`), Pending (`1917262480980051556`), Auditing (`1916814904073913359`), Done (`1916814904141022224`, closed), Flagged (`1916814904208131089`)
- **Labels:** `agy` (`1916814904292017170`), `plan-first` / `direct`, `worktree` / `in-place` / `scratch`, `repo:<name>`
- **Done & Audit policy:** Cards **never** move to **Done** automatically — finished runs and clean audits land in **Pending** until the user drags the card to **Done** or comments `done` / `mark done` / `move to done` / `close` (0 tokens). Audits run **only** when the user explicitly moves a card into **Auditing**.

## Architecture
- `bridge.py`: Core orchestrator (queueing, 2-slot cap, `/setup` intake, `plan-first` gate, git worktrees, access flow, notifications, auth proxy, graceful/forced shutdown, auto-reload via exit code `3`).
- `providers/__init__.py`: `BoardProvider` and `AgentProvider` interfaces, loader (`planka`, `agy`, `claude`), and proxy token registry.
- `providers/planka.py`: Planka v2 backend (live WebSocket UI, `X-Api-Key`, `viewer+canComment` bot user, invisible description session tag, webhooks).
- `providers/agy.py`: Host `agy --sandbox` backend (`stream-json`, immediate session capture, live `Subagents & Tasks` checklist emission for `invoke_subagent`/`swarm`/`run_workflow`/backgrounded `run_command`, host audit log, read-only `--mode plan` auditor).
- `providers/claude.py`: Claude Code backend (isolated `kanban-agent:latest` Docker containers + `:8788` OAuth proxy, or `BRIDGE_CLAUDE_MODE=host`, live `Subagents & Tasks` checklist emission for `Agent`/`Task`/`TodoWrite`, plus host read-only `--allowedTools Read,Grep,Glob` auditor).
- `agent/Dockerfile`: Debian bookworm-slim image for containerized Claude Code runs (`claude`, `gh`, `aws`, `az`, `uv`, `git`, `ripgrep`, `nodejs`).

## Running & Checking
- Systemd user unit: `~/.config/systemd/user/kanban-bridge.service` (source: `kanban-bridge.service`).
- Check liveness: `lsof -iTCP:8787 -sTCP:LISTEN` and `curl -sf http://127.0.0.1:8787/health`.
- Logs: `journalctl --user -u kanban-bridge -f`.
