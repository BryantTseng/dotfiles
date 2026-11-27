---
name: kanban-bridge
description: >-
  Operate, inspect, debug, modify, and back up the local Planka v2 <-> Agy
  kanban board and bridge (~/workspace/kanban-bridge/ ->
  ~/workspace/dotfiles/kanban-bridge/). Trigger whenever listing/creating/moving/
  commenting on kanban cards, checking board or bridge status, managing the
  kanban-bridge systemd service, inspecting kanban task directories, or adding
  new board/agent providers (e.g. Trello, Linear, Claude).
---

# Kanban Bridge (`~/workspace/kanban-bridge/`)

Self-hosted **Planka v2** board (live WebSockets + PostgreSQL) bridged to headless **`agy --sandbox`** or **Claude Code (`claude`)** sessions via pluggable `BoardProvider` and `AgentProvider` interfaces (`BRIDGE_AGENT_PROVIDER=agy|claude`).

## Critical Safety Rules

1. **Always ask before creating/triggering cards:** Whenever the user asks you to create a kanban card (and didn't already specify these), explicitly ask:
   - **Workspace mode:** existing repo (`--repo clerk` / `--repo dotfiles`) in **`--workspace worktree`** *(isolated branch, safe for parallel cards)* vs. **`--workspace in-place`** *(directly in repo)*, or **`--workspace scratch`** *(standalone folder)*?
   - **Execution mode:** **`--plan-first`** *(explore, write spec & numbered Q&A before coding)* or **`--direct`** *(implement immediately)*?
   - **Start now?** Attach **`--trigger`** (`agy`) immediately or leave unstarted in Backlog/To-Do?
2. **Trigger label starts real runs:** Adding the `agy` label (`1916814904292017170`) to any open card starts the bridge flow. If workspace or mode labels are missing, the bridge posts a zero-token `⚙️ Configure this run` prompt (`/setup?...`) on the card and waits in **Need Action** before spending any agent tokens. Never pass `--trigger` / `--confirm-trigger` unless explicitly instructed by the user.
3. **Never touch session metadata:** Cards persist their conversation ID as an invisible Markdown comment `<!-- agy-session:<conversation-id> -->` at the bottom of the card description. Never add, edit, or delete it manually (`scripts/kanban.py edit` preserves it automatically).
4. **Never grant access or click approvals:** `access:*` labels (`access:gh`, `access:azure`, `access:aws:<profile>`, `access:aws-elevated:<profile>`) and `/access?...` approval links are strictly the user's to grant. Never add `access:*` labels, post `approve`/`deny` comments, or POST to `/access`.
5. **Never print secrets:** `~/.config/kanban-bridge/env` and `~/.config/kanban-bridge/planka.env` hold tokens/secrets (`chmod 600`). Never echo or log their contents.

## Operating the Board (`scripts/kanban.py`)

Always prefer the bundled helper CLI (it loads `PLANKA_API_KEY` from `~/.config/kanban-bridge/env` silently and enforces all safety guardrails in code):

```bash
KANBAN=~/workspace/dotfiles/skills/shared/kanban-bridge/scripts/kanban.py

# Check Planka API + bridge health
$KANBAN health

# List all columns and cards (add --json for machine-readable output)
$KANBAN board
$KANBAN board --json

# Inspect a card's details and full comment thread
$KANBAN show <card_id>

# Create a card (defaults to Backlog, does NOT trigger an agy run)
$KANBAN create "Task title" --desc "Details..." --list "To-Do"

# Create a repo-backed, plan-first card (isolated git worktree of clerk, spec/Q&A before coding)
$KANBAN create "Backtest feature" --desc "Details..." --list "To-Do" --repo clerk --workspace worktree --plan-first

# Create an in-place or standalone scratch card implementing directly
$KANBAN create "Quick fix" --desc "Details..." --list "To-Do" --repo clerk --workspace in-place --direct
$KANBAN create "Scratch note" --desc "Details..." --list "To-Do" --workspace scratch --direct

# Create AND immediately start an agy run (ONLY when user explicitly asks!)
$KANBAN create "Task title" --desc "Details..." --list "To-Do" --repo clerk --workspace worktree --plan-first --trigger

# Move a card between columns (e.g. drag to Auditing to trigger read-only audit)
$KANBAN move <card_id> "Auditing"

# Edit title/description (automatically preserves <!-- agy-session:... -->)
$KANBAN edit <card_id> --desc "Updated details"

# Post a comment (resumes the card's agy session if triggered)
$KANBAN comment <card_id> "Please also add unit tests."

# Manage non-access labels (--confirm-trigger required for 'agy')
$KANBAN label <card_id> add <label>
$KANBAN label <card_id> remove <label>

# Manage native Planka checklists (Task Lists)
$KANBAN checklist <card_id> add-list "Implementation"
$KANBAN checklist <card_id> add "Build Docker image" --list "Implementation"
$KANBAN checklist <card_id> check "Build Docker image"
$KANBAN checklist <card_id> uncheck "Build Docker image"

# Delete a card
$KANBAN delete <card_id>
```

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
| 3 | Need Action | `1916814903922918413` | Blocked mid-run (Q&A, `/setup`, access request, or failure) |
| 4 | Doing | `1916814904006804494` | Actively running (30s progress updates) |
| 5 | Pending | `1917262480980051556` | Finished run or passed audit — waiting for user review / close |
| 6 | Auditing | `1916814904073913359` | **Manual audit trigger** — move card here for read-only audit |
| 7 | Done | `1916814904141022224` | Closed list — **user-only** (drag manually or comment `done` / `mark done`) |
| 8 | Flagged | `1916814904208131089` | Auditor found problems (`audit:<flag>` labels) |

## Paths, Repo Worktrees, Service Management & Backups

- **Source of truth:** `~/workspace/dotfiles/kanban-bridge/` (symlinked at `~/workspace/kanban-bridge`)
- **Task repos & worktrees:**
  - Dedicated bridge repos dir: `~/workspace/kanban-bridge/repos/<name>` (auto-discovered via `BRIDGE_REPOS_ROOT`; keeps the user's personal `~/workspace` untouched).
  - Scratch cards: `~/workspace/kanban-tasks/task-<id>-<slug>/`
  - Repo-backed cards (`repo:<name>` + `worktree` or `in-place`): isolated `git worktree` at `~/workspace/kanban-tasks/<repo>-task-<id>-<slug>/` on branch `kanban/<id-tail>-<slug>` with shared `CARGO_TARGET_DIR` (clean up after PR merge with `git -C ~/workspace/kanban-bridge/repos/<repo> worktree remove <path>`).
- **Plan-first workflow:** Attach `plan-first` to run spec/Q&A first without modifying production code; replying `do it` / `proceed` automatically removes `plan-first` and unlocks implementation.
- **Live Task Lists (`Subagents & Tasks`):** Subagents, swarm workers, workflows, backgrounded commands (`agy`), and `Agent`/`Task`/`TodoWrite` calls (`claude`) automatically stream live into a native `Subagents & Tasks` checklist on the card (`showOnFrontOfCard: true`) and check off when finished.
- **Host audit logs:** `~/.local/share/kanban-bridge/logs/<conversation-id>.jsonl`
- **Service commands:**
  ```bash
  lsof -iTCP:8787 -sTCP:LISTEN
  curl -sf http://127.0.0.1:8787/health
  systemctl --user status|restart|stop kanban-bridge
  journalctl --user -u kanban-bridge -f
  ```
- **Editing `bridge.py` or `providers/*.py`** triggers graceful drain + exit `3`, and systemd restarts automatically.
- **Backup Planka DB:**
  ```bash
  docker exec planka-postgres pg_dump -U postgres planka | gzip > planka-$(date +%F).sql.gz
  ```
