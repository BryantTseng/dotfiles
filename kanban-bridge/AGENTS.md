# Kanban Bridge (`~/workspace/kanban-bridge/`)

Operational notes for AI agents (`agy`, `claudecode`, etc.) working in this repository.

## Safety Guardrails
- **Trigger label (`BRIDGE_TRIGGER_LABEL`, e.g. `agy` or `claude`)** starts real, quota-consuming headless agent sessions (cards in **Backlog** stay parked until moved out). Never apply it unless explicitly asked.
- **Never touch session metadata** (`<!-- <provider>-session:<uuid> -->` at the end of Planka card descriptions).
- **Never grant `access:*` labels or approve `/access` links** — access approvals belong strictly to the user.
- **Never print secrets** from `~/.config/kanban-bridge/env` or `~/.config/kanban-bridge/planka.env`.

## Active Board (Planka v2)
- **URL & IDs:** Read `PLANKA_URL` and `BRIDGE_TRIGGER_LABEL` from `~/.config/kanban-bridge/env` (or run `kanban.py board`) — never hardcode machine-specific board/list/label IDs in shared docs/code.
- **Columns:** `Backlog` *(parked/ignored)*, `To-Do`, `Need Action`, `Doing`, `Pending`, `Auditing`, `Done` *(closed)*, `Flagged`
- **Labels:** `<BRIDGE_TRIGGER_LABEL>` (e.g. `agy`/`claude`), `plan-first` / `direct`, `worktree` / `in-place` / `scratch`, `repo:<name>`
- **Done & Audit policy:** Cards **never** move to **Done** automatically — finished runs and clean audits land in **Pending** until the user drags the card to **Done** or comments `done` / `mark done` / `move to done` / `close` (0 tokens). Audits run **only** when the user explicitly moves a card into **Auditing** (and cover both the parent run and any spawned subagents).

## Architecture
- `bridge.py`: Core orchestrator (queueing, 2-slot cap, Backlog parking, `/setup` intake, `plan-first` gate, git worktrees, two-way Planka attachment sync with Docker workdir remapping, subagent-aware audits, access flow, `BRIDGE_NOTIFY`, auth proxy, graceful/forced shutdown, auto-reload via exit code `3`).
- `providers/__init__.py`: `BoardProvider` and `AgentProvider` interfaces (`agent_workdir`, `extract_subagent_audits`), loader (`planka`, `agy`, `claude`), and proxy token registry.
- `providers/planka.py`: Planka v2 backend (live WebSocket UI, `X-Api-Key`, board `editor` bot user for moves/checklists/labels/attachments/comments, provider-specific description session tag, webhooks).
- `providers/agy.py`: Host `agy --sandbox` backend (`stream-json`, immediate session capture, live `Subagents & Tasks` checklist emission, subagent transcript audit extraction, host audit log, read-only `--mode plan` auditor).
- `providers/claude.py`: Claude Code backend (isolated `kanban-agent:latest` Docker containers + `:8788` OAuth proxy, or `BRIDGE_CLAUDE_MODE=host`, container workdir mapping, live `Subagents & Tasks` checklist emission, subagent audit extraction, plus host read-only `--allowedTools Read,Grep,Glob` auditor).
- `agent/Dockerfile`: Debian bookworm-slim image for containerized Claude Code runs (`claude`, `gh`, `aws`, `az>=2.70`, `uv`, `git`, `ripgrep`, `nodejs`, optional gitignored `ca-bundle.pem`).
- `swiftbar/kanban.30s.py`: Optional macOS SwiftBar menu-bar plugin.

## Running & Checking
- **Linux (`systemd`):** `~/.config/systemd/user/kanban-bridge.service` (`systemctl --user status kanban-bridge`, `journalctl --user -u kanban-bridge -f`).
- **macOS (`run.sh`):** `~/workspace/kanban-bridge/run.sh` (pairs with `swiftbar/kanban.30s.py` and `BRIDGE_NOTIFY=off`).
- Check liveness: `lsof -iTCP:8787 -sTCP:LISTEN` and `curl -sf http://127.0.0.1:8787/health`.
