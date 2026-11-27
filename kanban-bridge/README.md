# Kanban Bridge (Planka v2 ↔ Headless `agy` / `claude`)

A self-hosted **Planka v2** kanban board bridged to headless **`agy --sandbox`** or **Claude Code (`claude`)** sessions, with pluggable `BoardProvider` and `AgentProvider` interfaces so you can swap boards (e.g. Trello, Linear) or agent runtimes (`agy` ↔ `claude`) without touching the orchestrator.

---

## Architecture & Workflow

- **Live Board UI:** Self-hosted Planka v2 (`http://localhost:3000`) backed by PostgreSQL 16 with real-time WebSocket auto-refresh (no manual page reloads).
- **Trigger:** Applying the configured trigger label (`BRIDGE_TRIGGER_LABEL`, default `agy` — or `claude`) to any open card outside **Backlog** starts a headless `agy --sandbox` or `claude` session in `~/workspace/kanban-tasks/task-<id>-<slug>/`.
- **Session Persistence:** The session/conversation ID is stored invisibly at the bottom of the card description as `<!-- <provider>-session:<uuid> -->` (`agy-session` or `claude-session`), keeping Planka's label picker clean. Every human comment on the card resumes that exact session.
- **Bot User:** Card moves, live checklist updates, labels, attachments, and replies are performed as a dedicated Planka `boardUser` (`BOT_USERNAME`, board `editor` role) so Planka's **Actions** feed cleanly separates bot updates from your manual drags/edits.
- **Columns:**
  1. **Backlog** — **parked / drafts**; ignored by the bridge even if carrying the trigger label (configurable via `BRIDGE_BACKLOG_BUCKET=backlog`) until moved out
  2. **To-Do** — ready / waiting (`⏳ Queued` comment if 2 slots are busy)
  3. **Need Action** — agent asked a question, requested access, needs `/setup` config, or failed
  4. **Doing** — actively running (30-second live progress updates on placeholder comment)
  5. **Pending** — agent finished its run (`STATUS: DONE`) or passed **Auditing**; ready for **your** review, live test, or deployment
  6. **Auditing** — **manual audit trigger**; drag any card here to run the read-only auditor across the parent run **and** every spawned subagent, including a **💰 Estimated Cost** breakdown (exact USD when reported by Claude Code, or API-equivalent token estimate; clean → **Pending**, problems → **Flagged**)
  7. **Done** *(closed list)* — **never entered automatically**; only moved here when you drag the card manually or comment `done` / `mark done` / `move to done` / `close` (0 tokens)
  8. **Flagged** — auditor found issues (`audit:<flag>` red labels)
- **Model Labels (`model:<name>`):** The bridge automatically attaches the model used by the run (e.g. `model:gemini-4-hydrogen-medium`, `model:claude-sonnet-4-6`) as a sky-blue label on the card.
- **Notifications:** Linux uses `notify-send` when `BRIDGE_NOTIFY=auto` (default); set `BRIDGE_NOTIFY=off` to disable desktop popups (e.g. on macOS when using the optional `swiftbar/kanban.30s.py` menu-bar plugin).
- **Native Planka Checklists (`CHECKLIST:` + Live `Subagents & Tasks`):**
  - **Live automatic tracking:** Whenever an agent spawns a subagent, swarm worker, workflow, or background command (`agy`: `invoke_subagent`, `swarm`, `run_workflow`, backgrounded `run_command`; `claude`: `Agent`/`Task`, `TodoWrite`), the bridge syncs each one live into a **`Subagents & Tasks`** Task List on the card (`showOnFrontOfCard: true`) and checks items off as they finish (or on clean turn completion).
  - **Explicit `CHECKLIST:` blocks:** Agents can also include `CHECKLIST: <Title>` blocks with `- [x]` / `- [ ]` items before `STATUS:` to sync deliverables/follow-ups onto the card face.
- **Two-Way Planka Attachments (Images & Files):**
  - **Inbound (Planka → Agent):** Before every run, any files/images attached to the card are downloaded into `<task-dir>/.kanban-attachments/` (excluded from git via `.git/info/exclude`) and listed in the prompt so the agent can inspect screenshots, PDFs, or logs directly.
  - **Outbound (Agent → Planka):** When you ask the agent to generate an image, diagram, or file, it emits `ATTACH: <path>` (or `ATTACH: <path> | <title>`) before `STATUS:`, and the bridge uploads the file directly onto the card as **`Agy`** (validating workspace/session boundaries and blocking sensitive files like `.env` / `*.pem`).
- **Pre-Run Intake (`⚙️ Configure this run` — 0 tokens):** When a brand-new `agy` card doesn't yet specify both a workspace choice (`worktree` / `in-place` / `scratch`) and execution mode (`plan-first` / `direct`), the bridge posts a signed 1-click setup form link (`http://localhost:8787/setup?...`) on the card (plus comment-reply shortcuts like `clerk worktree plan-first`) and moves it to **Need Action** before spending any agent tokens. Cards that already have those labels start immediately.
- **Git Tracking & Isolated Repos (`~/workspace/kanban-bridge/repos/`):**
  - **Standalone scratch cards (`scratch`):** Each card gets its own git repo in `~/workspace/kanban-tasks/task-<id>-<slug>/` (one commit per run).
  - **Repo worktree cards (`repo:<name>` + `worktree`):** Clone repos the bridge should work on into `~/workspace/kanban-bridge/repos/<name>` (keeping your personal `~/workspace` untouched). The bridge auto-discovers `kanban-bridge/repos/*/.git` and creates an isolated `git worktree` at `~/workspace/kanban-tasks/<repo>-task-<id>-<slug>/` on a fresh feature branch `kanban/<id-tail>-<slug>` branched from `origin/main`, so concurrent cards never collide.
  - **Repo in-place cards (`repo:<name>` + `in-place`):** Runs directly inside `~/workspace/kanban-bridge/repos/<name>` (preserving uncommitted changes and branching safely per `AGENTS.md`).
  - Both repo modes defer to the repo's `AGENTS.md` (e.g., Dev Worker + Mandatory Reviewer Gate swarm), let the agent make clean PR commits, share `CARGO_TARGET_DIR` for fast Rust builds, and diff against `merge-base origin/main HEAD` during manual **Auditing**.
- **Plan-First Spec Gate (`plan-first` vs `direct`):** Add `plan-first` to explore the codebase, write a design spec, and ask numbered clarifying questions on the card without touching production code. Reply `do it` / `proceed` (or remove `plan-first`) to unlock implementation.
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
│   ├── __init__.py           # Pluggable BoardProvider & AgentProvider ABCs + proxy token registry
│   ├── planka.py             # Planka v2 provider (WebSockets, X-Api-Key, webhooks)
│   ├── agy.py                # Host `agy --sandbox` provider + read-only auditor
│   └── claude.py             # Claude Code provider (Docker+auth-proxy or host mode) + auditor
├── agent/
│   └── Dockerfile            # Isolated container image for Claude Code (`kanban-agent:latest`, optional `ca-bundle.pem`)
├── swiftbar/
│   └── kanban.30s.py         # Optional macOS SwiftBar menu-bar status & quick-jump plugin
├── planka/
│   ├── docker-compose.yml    # Planka v2 + PostgreSQL 16 (localhost-only, --net=host)
│   └── setup-planka.sh       # Interactive one-time setup (API keys, bot user, board & lists)
├── tests/
│   └── test_providers.py     # Fast isolated unit tests for providers and auth proxy
├── kanban-bridge.service     # systemd user service unit
├── run.sh                    # Standalone runner (alternative to systemd)
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

### Service Management (Linux `systemd` vs. macOS `run.sh` + SwiftBar)
- **Linux (`systemd` user service):**
  ```bash
  systemctl --user status|restart|stop kanban-bridge
  journalctl --user -u kanban-bridge -f
  ```
- **macOS (`run.sh` + optional SwiftBar menu-bar plugin):**
  ```bash
  ~/workspace/kanban-bridge/run.sh
  # Optional SwiftBar plugin (shows Need Action / Flagged / Doing / Auditing counts & click-through links):
  ln -sf ~/workspace/kanban-bridge/swiftbar/kanban.30s.py ~/Library/Application\ Support/SwiftBar/kanban.30s.py
  ```
  Set `BRIDGE_NOTIFY=off` in `~/.config/kanban-bridge/env` when using SwiftBar.
  Behind corporate TLS inspection (e.g. Netskope/Zscaler), place your PEM CA bundle at `~/workspace/kanban-bridge/agent/ca-bundle.pem` (gitignored) before building `kanban-agent:latest`.

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

## Using Claude Code (`BRIDGE_AGENT_PROVIDER=claude`)

You can run the exact same Planka board, `/setup` intake, `plan-first` gate, git worktrees, and manual auditing with **Claude Code** instead of `agy`:

### Option A — Isolated Docker Containers + Auth Proxy *(default)*
Containers never see your real `CLAUDE_OAUTH_TOKEN`; each run receives a per-run secret valid only at the bridge's local proxy (`:8788`), which swaps in the real token upstream.

1. **Build the agent image:**
   ```bash
   docker build -t kanban-agent:latest ~/workspace/kanban-bridge/agent/
   docker run --rm kanban-agent:latest sh -c 'claude --version; gh --version; aws --version; az version'
   ```
2. **Generate a Claude OAuth token** in your own terminal (`claude setup-token`) and add to `~/.config/kanban-bridge/env`:
   ```bash
   BRIDGE_AGENT_PROVIDER=claude
   BRIDGE_CLAUDE_MODE=docker
   CLAUDE_OAUTH_TOKEN=<token-from-claude-setup-token>
   # Optional: rename trigger/bot from agy -> claude (run setup-planka.sh with BOT_USERNAME=claude)
   # BRIDGE_TRIGGER_LABEL=claude
   # BOT_USERNAME=claude
   # On Linux rootless Docker so container-created files stay owned by your host UID:
   # BRIDGE_DOCKER_NET=host
   # BRIDGE_DOCKER_USER=0:0
   ```
3. **Restart the bridge:**
   ```bash
   systemctl --user restart kanban-bridge.service
   curl -sf http://127.0.0.1:8787/health   # -> {"ok": true, "board": "planka", "agent": "claude"}
   ```

### Option B — Host Claude CLI (`BRIDGE_CLAUDE_MODE=host`)
Skip Docker and the auth proxy when cards need local toolchains (e.g. Rust/Cargo or rootless Docker integration tests):
```bash
BRIDGE_AGENT_PROVIDER=claude
BRIDGE_CLAUDE_MODE=host
```
Uses your local `claude` CLI login directly in `cwd` (stripping all bridge secrets from the child environment).

---

## Adding New Providers Later

- **New Board (`BoardProvider`):** Implement `providers/<board>.py`, register in `providers/__init__.py`, set `BRIDGE_BOARD_PROVIDER=<board>` in `~/.config/kanban-bridge/env`, and restart `kanban-bridge.service`.
- **New Agent (`AgentProvider`):** Implement `providers/<agent>.py`, register in `providers/__init__.py`, set `BRIDGE_AGENT_PROVIDER=<agent>`, and restart `kanban-bridge.service`.
