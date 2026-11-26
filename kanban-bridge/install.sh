#!/usr/bin/env bash
# Idempotent bootstrap for kanban-bridge from this dotfiles directory:
#   1. Links ~/workspace/kanban-bridge -> this directory (unless already present)
#   2. Creates ~/.config/kanban-bridge/ (700) with generated planka.env & env (600)
#   3. Starts Planka v2 + PostgreSQL via Docker Compose
#   4. Installs & enables ~/.config/systemd/user/kanban-bridge.service
#   5. Links the dual-compatible skill into ~/.agents & ~/.claude if workspace exists
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
DOTFILES_DIR="$(cd "$SRC_DIR/.." && pwd)"
CFG_DIR="${HOME}/.config/kanban-bridge"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
WORKSPACE_LINK="${HOME}/workspace/kanban-bridge"

mkdir -p "$CFG_DIR" "$SYSTEMD_USER_DIR" "${HOME}/workspace/kanban-tasks"
chmod 700 "$CFG_DIR"

# 1. Symlink ~/workspace/kanban-bridge -> dotfiles/kanban-bridge
if [ ! -e "$WORKSPACE_LINK" ]; then
  ln -s "$SRC_DIR" "$WORKSPACE_LINK"
  echo "Linked $WORKSPACE_LINK -> $SRC_DIR"
elif [ -L "$WORKSPACE_LINK" ]; then
  echo "Symlink already exists: $WORKSPACE_LINK -> $(readlink "$WORKSPACE_LINK")"
else
  echo "Note: $WORKSPACE_LINK exists and is not a symlink; leaving it as-is."
fi

# 2. Generate secret env files if missing (never overwrites existing secrets)
python3 - <<'PY'
import secrets
from pathlib import Path

cfg = Path.home() / ".config/kanban-bridge"

planka_env = cfg / "planka.env"
if not planka_env.exists():
    pw = secrets.token_hex(20)
    sk = secrets.token_hex(32)
    planka_env.write_text(
        "POSTGRES_DB=planka\n"
        "POSTGRES_USER=postgres\n"
        f"POSTGRES_PASSWORD={pw}\n"
        f"DATABASE_URL=postgresql://postgres:{pw}@127.0.0.1:5433/planka\n"
        f"SECRET_KEY={sk}\n"
    )
    planka_env.chmod(0o600)
    print("Created", planka_env)

env_file = cfg / "env"
if not env_file.exists():
    env_file.write_text(
        f"WEBHOOK_SECRET={secrets.token_hex(24)}\n"
        "BRIDGE_BOARD_PROVIDER=planka\n"
        "BRIDGE_AGENT_PROVIDER=agy\n"
        "BRIDGE_TRIGGER_LABEL=agy\n"
        "BOT_USERNAME=agy\n"
        "PLANKA_URL=http://localhost:3000\n"
        "PLANKA_WEBHOOK_URL=http://127.0.0.1:8787/planka\n"
    )
    env_file.chmod(0o600)
    print("Created", env_file)
PY

# 3. Start Planka + PostgreSQL
docker compose -f "$SRC_DIR/planka/docker-compose.yml" up -d

# 4. Install systemd user service
cp "$SRC_DIR/kanban-bridge.service" "$SYSTEMD_USER_DIR/kanban-bridge.service"
systemctl --user daemon-reload
systemctl --user enable kanban-bridge.service

# 5. Link shared skill if workspace skill dirs exist
SKILL_SRC="$DOTFILES_DIR/skills/shared/kanban-bridge"
if [ -d "$SKILL_SRC" ] && [ -d "${HOME}/workspace/.agents/skills" ]; then
  ln -sfn "$SKILL_SRC" "${HOME}/workspace/.agents/skills/kanban-bridge"
fi
if [ -d "${HOME}/workspace/.claude/skills" ]; then
  ln -sfn "../../.agents/skills/kanban-bridge" "${HOME}/workspace/.claude/skills/kanban-bridge"
fi

echo ""
echo "Bootstrap complete! Next interactive steps (for a fresh Planka database):"
echo "  1. Create your Planka admin user:"
echo "       docker exec -it planka npm run db:create-admin-user"
echo "  2. Provision API keys, 'agy' bot user, board, 7 lists, and 'agy' label:"
echo "       $SRC_DIR/planka/setup-planka.sh"
echo "  3. Start the bridge service:"
echo "       systemctl --user start kanban-bridge.service"
echo "       curl -sf http://127.0.0.1:8787/health"
