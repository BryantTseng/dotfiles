---
name: kanban-bridge
description: >-
  Operational guide and safety rules for the local Planka v2 <-> Agy kanban bridge
  (~/workspace/kanban-bridge/ -> ~/workspace/dotfiles/kanban-bridge/). Trigger
  whenever inspecting, debugging, modifying, backing up, or interacting with the
  local Planka kanban board, kanban-bridge systemd service, kanban task directories,
  or adding new board/agent providers (e.g. Trello, Linear, Claude).
---

# Kanban Bridge (`~/workspace/kanban-bridge/`)

Self-hosted **Planka v2** board (live WebSockets + PostgreSQL) bridged to headless `agy --sandbox` sessions via pluggable `BoardProvider` and `AgentProvider` interfaces.

## Critical Safety Rules

1. **Trigger label starts real runs:** Adding the `agy` label (`1916814904292017170`) to any open card immediately starts a real, quota-consuming headless `agy` session. Never add it unless explicitly instructed by the user.
2. **Never touch session metadata:** Cards persist their conversation ID as an invisible Markdown comment `<!-- agy-session:<conversation-id> -->` at the bottom of the card description. Never add, edit, or delete it manually.
3. **Never grant access or click approvals:** `access:*` labels (`access:gh`, `access:azure`, `access:aws:<profile>`, `access:aws-elevated:<profile>`) and `/access?...` approval links are strictly the user's to grant. An agent must never add `access:*` labels, post `approve` comments, or POST to `/access`.
4. **Never print secrets:** `~/.config/kanban-bridge/env` and `~/.config/kanban-bridge/planka.env` hold tokens/secrets (`chmod 600`). Never echo or log their contents.

## Board & List IDs (Planka v2)

- **UI:** `http://localhost:3000/boards/1916814609835099142`
- **API:** `http://localhost:3000/api` (`X-Api-Key`)
- **Project:** `Agy` (`1916814609407280132`), **Board:** `Agy` (`1916814609835099142`)
- **Trigger label:** `agy` (`1916814904292017170`)
- **Bot user:** `agy` (`1916814609130456067`, role `viewer` + `canComment: true`)
- **Compose stack:** `~/workspace/kanban-bridge/planka/docker-compose.yml` (`planka` + `planka-postgres` on `127.0.0.1:5433`)

| Order | List | List ID | Role |
| :---: | :--- | :--- | :--- |
| 1 | Backlog | `1916814903729980427` | Unscheduled ideas |
| 2 | To-Do | `1916814903839032332` | Ready / queued (`⏳ Queued` if 2 slots busy) |
| 3 | Need Action | `1916814903922918413` | Agent asked a question, requested access, or failed |
| 4 | Doing | `1916814904006804494` | Actively running (30s progress updates) |
| 5 | Auditing | `1916814904073913359` | **Manual audit trigger** — drag card here for read-only audit |
| 6 | Done | `1916814904141022224` | Closed list — finished runs land here directly (no auto-audit) |
| 7 | Flagged | `1916814904208131089` | Auditor found problems (`audit:<flag>` labels) |

## Workflow, Paths & Backups

- **Source of truth:** `~/workspace/dotfiles/kanban-bridge/` (symlinked at `~/workspace/kanban-bridge`)
- **Task repos:** `~/workspace/kanban-tasks/task-<id>-<slug>/` (one git repo per card, one commit per run)
- **Host audit logs:** `~/.local/share/kanban-bridge/logs/<conversation-id>.jsonl`
- **Audit policy:** Finished runs (`STATUS: DONE`) move directly to **Done** without auditing. Auditing runs **only** when the user explicitly drags a card into **Auditing** (`BRIDGE_AUDIT_BUCKET`, default `auditing`).
- **Backup:**
  - Planka DB: `docker exec planka-postgres pg_dump -U postgres planka | gzip > planka.sql.gz`
  - Plus `~/workspace/kanban-tasks/`, `~/.local/share/kanban-bridge/logs/`, and encrypted `~/.config/kanban-bridge/`.

## Health Checks & Service Management

```bash
lsof -iTCP:8787 -sTCP:LISTEN
curl -sf http://127.0.0.1:8787/health
systemctl --user status kanban-bridge
journalctl --user -u kanban-bridge -f
```

- Editing `bridge.py` or `providers/*.py` triggers graceful drain + exit `3`, and systemd restarts automatically.
