# Kanban Bridge (Planka v2 ↔ Headless `agy`)

A self-hosted **Planka v2** kanban board bridged to headless **`agy --sandbox`** sessions, with pluggable `BoardProvider` and `AgentProvider` interfaces so you can swap boards (e.g. Trello, Linear) or agent runtimes (e.g. Claude Code) without touching the orchestrator.

---

## Architecture & Workflow

- **Live Board UI:** Self-hosted Planka v2 (`http://localhost:3000`) backed by PostgreSQL 16 with real-time WebSocket auto-refresh (no manual page reloads).
- **Trigger:** Applying the **`agy`** label to any open card starts a headless `agy -p --output-format stream-json --sandbox` session in `~/workspace/kanban-tasks/task-<id>-<slug>/`.
- **Session Persistence:** The `agy` `conversation_id` is stored invisibly at the bottom of the card description as `<!-- agy-session:<uuid> -->` (keeping Planka's label picker clean). Every human comment on the card resumes that exact session with `--conversation <uuid>`.
- **Bot User:** Replies are posted as a dedicated Planka `boardUser` (**`agy`**) with `viewer` + `canComment: true` permissions.
- **Columns (Board `Agy`):**
  1. **Backlog** — unscheduled ideas
  2. **To-Do** — ready / waiting (`⏳ Queued` comment if 2 slots are busy)
  3. **Need Action** — agent asked a question, requested access, or failed (fires `notify-send` desktop notification)
  4. **Doing** — actively running (30-second live progress updates on placeholder comment)
  5. **Auditing** — **manual audit trigger**; drag any card here to run the read-only `--mode plan` auditor
  6. **Done** *(closed list)* — finished runs (`STATUS: DONE`) move straight here **without** auto-auditing; clean audits return here with green `audited` label
  7. **Flagged** — auditor found issues (`audit:<flag>` red labels + desktop notification)
- **Git Tracking & Repo Worktrees (`BRIDGE_REPOS`):**
  - **Standalone scratch cards:** Each card gets its own git repo in `~/workspace/kanban-tasks/task-<id>-<slug>/` (one commit per run).
  - **Repo-backed cards (`repo:<name>` label or Planka board matching `BRIDGE_REPOS`):** Creates an isolated `git worktree` at `~/workspace/kanban-tasks/<repo>-task-<id>-<slug>/` on a fresh feature branch `kanban/<id-tail>-<slug>` branched from `origin/main`, so concurrent cards never collide. Defers to the repo's `AGENTS.md` (e.g., Dev Worker + Mandatory Reviewer Gate swarm), lets the agent make clean PR commits, shares `CARGO_TARGET_DIR` for fast Rust builds, and diffs against `merge-base origin/main HEAD` during manual **Auditing**.
- **Plan-First Spec Gate (`plan-first` label):** Add `plan-first` to explore the codebase, write a design spec, and ask numbered clarifying questions on the card without touching production code. Reply `do it` / `proceed` (or remove `plan-first`) to unlock implementation.
- **Long Runs & Skills:** Default `BRIDGE_RUN_TIMEOUT=3600` (60 min) with slash-commands/skills enabled by default so heavy Rust builds, ephemeral Docker Postgres integration tests, and multi-agent swarm reviews finish cleanly.
- **Explicit Access Grants:** The agent never gets `gh`, AWS, or Azure credentials by default. It requests them with `ACCESS_REQUEST: <items>`, and the bridge posts HMAC-signed **✅ Approve / ❌ Deny** links served on `http://localhost:8787/access?...`. Approving attaches `access:<item>` and injects credentials for the run (`aws-elevated:*` is removed after one run).
- **Board as Source of Truth:** On startup and every 60 seconds, the bridge reconciles all `agy`-labelled cards, recovers interrupted placeholders, and catches any comments posted while offline.
- **Auto-Reload & Graceful Stop:** Editing `bridge.py` or `providers/*.py` drains active runs and exits with code `3`, triggering `systemd` to restart cleanly. `systemctl --user stop` (or first Ctrl-C) lets running jobs finish; a second Ctrl-C interrupts them and marks their cards in **Need Action**.

---

## Directory Layout

```text
kanban-bridge/
├── README.md                 # This setup plan & runbook
├── AGENTS.md                 # Operational guardrails (symlinked as CLAUDE.md & GEMINI.md)
├── bridge.py                 # Core orchestrator (uv PEP 723 script)
├── providers/
│   ├── __init__.py           # Pluggable BoardProvider & AgentProvider ABCs + loader
│   ├── planka.py             # Planka v2 provider (WebSockets, X-Api-Key, webhooks)
│   └── agy.py                # Host `agy --sandbox` provider + read-only auditor
├── planka/
│   ├── docker-compose.yml    # Planka v2 + PostgreSQL 16 (localhost-only, --net=host)
│   └── setup-planka.sh       # Interactive one-time setup (API keys, bot user, board & lists)
├── kanban-bridge.service     # systemd user service unit
├── run.sh                    # Standalone foreground runner (alternative to systemd)
├── env.example               # Template for ~/.config/kanban-bridge/env (no secrets)
└── install.sh                # Fresh-machine bootstrap script
```

---

## Fresh-Machine Setup Plan

### Prerequisites
- Linux (tested on EndeavourOS / Arch with COSMIC desktop)
- `docker` + `docker compose`, `uv`, `agy` (authenticated), `git`, `python3`, `openssl`, `notify-send`
- Optional CLIs for approved access grants: `gh`, `aws`, `az`

### Step 1 — Rootless Docker Host Loopback (if using Rootless Docker)
On rootless Docker (`rootlesskit`), `planka` runs with `network_mode: host` (bound strictly to `HOST=127.0.0.1` and `PORT=3000`) so it can POST webhooks to `http://127.0.0.1:8787/planka` reliably even offline or across Wi-Fi changes.

If your rootless Docker runs with `--disable-host-loopback`, add a systemd drop-in:
```bash
mkdir -p ~/.config/systemd/user/docker.service.d
printf '[Service]\nEnvironment="DOCKERD_ROOTLESS_ROOTLESSKIT_DISABLE_HOST_LOOPBACK=false"\n' \
  > ~/.config/systemd/user/docker.service.d/override.conf
systemctl --user daemon-reload
systemctl --user restart docker.service
```

### Step 2 — Bootstrap Config, Planka Stack, and Systemd Service
```bash
~/workspace/dotfiles/kanban-bridge/install.sh
```
This links `~/workspace/kanban-bridge`, creates `~/.config/kanban-bridge/{env,planka.env}` (`chmod 600` in a `700` dir), starts `planka` + `planka-postgres`, installs `kanban-bridge.service`, and links the dual-compatible skill.

### Step 3 — Create Planka Admin User & Provision Board
```bash
# 1. Create your Planka admin account interactively
docker exec -it planka npm run db:create-admin-user

# 2. Mint API keys, create 'agy' bot user, 'Agy' board, 7 lists, and 'agy' label
~/workspace/kanban-bridge/planka/setup-planka.sh
```

### Step 4 — Start & Verify the Bridge
```bash
systemctl --user start kanban-bridge.service
systemctl --user status kanban-bridge.service
curl -sf http://127.0.0.1:8787/health
lsof -iTCP:8787 -sTCP:LISTEN
```
Open **http://localhost:3000**, open the **Agy** board, create a card in **To-Do**, and attach the **`agy`** label.

---

## Daily Operations & Backups

### Service Management
```bash
systemctl --user status kanban-bridge
systemctl --user restart kanban-bridge
systemctl --user stop kanban-bridge
journalctl --user -u kanban-bridge -f
```

### Backup & Restore
State lives in four places:
1. **Planka PostgreSQL DB:**
   ```bash
   docker exec planka-postgres pg_dump -U postgres planka | gzip > planka-$(date +%F).sql.gz
   ```
   Restore: `gunzip -c planka-YYYY-MM-DD.sql.gz | docker exec -i planka-postgres psql -U postgres -d planka`
2. **Planka uploaded files:** Docker volumes `planka_attachments`, `planka_avatars`, `planka_backgrounds`, `planka_favicons`.
3. **Task git repos & host audit logs:** `~/workspace/kanban-tasks/` and `~/.local/share/kanban-bridge/logs/`.
4. **Secrets (back up encrypted only, e.g. `restic`/`borg`):** `~/.config/kanban-bridge/`.

---

## Adding New Providers Later

- **New Board (`BoardProvider`):** Implement `providers/<board>.py`, register in `providers/__init__.py`, set `BRIDGE_BOARD_PROVIDER=<board>` in `~/.config/kanban-bridge/env`, and restart `kanban-bridge.service`.
- **New Agent (`AgentProvider`):** Implement `providers/<agent>.py`, register in `providers/__init__.py`, set `BRIDGE_AGENT_PROVIDER=<agent>`, and restart `kanban-bridge.service`.
