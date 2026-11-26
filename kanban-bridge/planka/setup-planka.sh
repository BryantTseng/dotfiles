#!/usr/bin/env bash
# Interactive one-time setup for Planka v2:
#   1. Logs in locally with your Planka admin credentials (password is not echoed).
#   2. Mints your PLANKA_API_KEY via POST /api/users/<id>/api-key.
#   3. Creates the "agy" bot user (boardUser, viewer + canComment) & mints PLANKA_BOT_API_KEY.
#   4. Creates Project "Agy", Board "Agy", the 7 Kanban lists, and the "agy" trigger label.
#   5. Saves PLANKA_URL, PLANKA_API_KEY, PLANKA_BOT_API_KEY to ~/.config/kanban-bridge/env.
set -euo pipefail

PLANKA_URL="${PLANKA_URL:-http://localhost:3000}"
ENV_FILE="${HOME}/.config/kanban-bridge/env"

read -rp "Planka admin email or username: " PLANKA_USER
read -rsp "Planka admin password: " PLANKA_PASS
echo ""

PLANKA_USER="$PLANKA_USER" PLANKA_PASS="$PLANKA_PASS" PLANKA_URL="$PLANKA_URL" ENV_FILE="$ENV_FILE" \
python3 - <<'PY'
import json
import os
import secrets
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ["PLANKA_URL"].rstrip("/")
API = BASE + "/api"
ENV_FILE = Path(os.environ["ENV_FILE"])


def api(method, path, body=None, bearer=None, api_key=None):
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    elif api_key:
        headers["X-Api-Key"] = api_key
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {e.code} on {method} {path}: {detail}")


# 1. Login and resolve current admin user
tok = api("POST", "/access-tokens", {
    "emailOrUsername": os.environ["PLANKA_USER"],
    "password": os.environ["PLANKA_PASS"],
})["item"]
me = api("GET", "/users/me", bearer=tok)["item"]
my_id = str(me["id"])

# 2. Mint admin API key
admin_key = api("POST", f"/users/{my_id}/api-key", bearer=tok)["included"]["apiKey"]

# 3. Create or reuse bot user "agy" and mint its API key
users = (api("GET", "/users", api_key=admin_key) or {}).get("items") or []
bot = next((u for u in users if u.get("username") == "agy"), None)
if not bot:
    pw = "AgyBot!" + secrets.token_hex(12) + "9aZ"
    bot = api("POST", "/users", {
        "email": "agy@kanban.local",
        "password": pw,
        "role": "boardUser",
        "name": "Agy",
        "username": "agy",
    }, api_key=admin_key)["item"]
bot_id = str(bot["id"])
bot_key = api("POST", f"/users/{bot_id}/api-key", api_key=admin_key)["included"]["apiKey"]

# 4. Create Project "Agy", Board "Agy", 7 lists, and "agy" trigger label
proj_bundle = api("GET", "/projects", api_key=admin_key) or {}
proj = next((p for p in (proj_bundle.get("items") or []) if p.get("name") == "Agy"), None)
if not proj:
    proj = api("POST", "/projects", {
        "type": "shared",
        "name": "Agy",
        "description": "Kanban board for Agy agent tasks",
    }, api_key=admin_key)["item"]
pid = str(proj["id"])

proj_bundle = api("GET", "/projects", api_key=admin_key) or {}
boards = [b for b in ((proj_bundle.get("included") or {}).get("boards") or []) if str(b.get("projectId")) == pid]
board = next((b for b in boards if b.get("name") == "Agy"), None)
if not board:
    board = api("POST", f"/projects/{pid}/boards", {
        "name": "Agy",
        "position": 65536,
    }, api_key=admin_key)["item"]
bid = str(board["id"])

# Ensure admin is editor and bot is viewer+canComment on board
bundle = api("GET", f"/boards/{bid}", api_key=admin_key) or {}
inc = bundle.get("included") or {}
members = inc.get("boardMemberships") or []
if not any(str(m.get("userId")) == my_id for m in members):
    api("POST", f"/boards/{bid}/board-memberships", {"userId": my_id, "role": "editor"}, api_key=admin_key)
if not any(str(m.get("userId")) == bot_id for m in members):
    api("POST", f"/boards/{bid}/board-memberships", {"userId": bot_id, "role": "viewer", "canComment": True}, api_key=admin_key)

existing_lists = {(l.get("name") or "").strip().lower(): l for l in (inc.get("lists") or []) if l.get("name")}
desired = [
    ("Backlog", "active"),
    ("To-Do", "active"),
    ("Need Action", "active"),
    ("Doing", "active"),
    ("Auditing", "active"),
    ("Done", "closed"),
    ("Flagged", "active"),
]
for idx, (name, ltype) in enumerate(desired, start=1):
    if name.lower() not in existing_lists:
        api("POST", f"/boards/{bid}/lists", {
            "name": name,
            "type": ltype,
            "position": idx * 65536,
        }, api_key=admin_key)

existing_labels = {l.get("name"): l for l in (inc.get("labels") or []) if l.get("name")}
default_labels = [
    ("agy", "lagoon-blue"),
    ("plan-first", "egg-yellow"),
    ("direct", "sunny-grass"),
    ("worktree", "lagoon-blue"),
    ("in-place", "pumpkin-orange"),
    ("scratch", "muddy-grey"),
]
for idx, (lname, lcolor) in enumerate(default_labels, start=1):
    if lname not in existing_labels:
        api("POST", f"/boards/{bid}/labels", {
            "name": lname,
            "color": lcolor,
            "position": idx * 65536,
        }, api_key=admin_key)

# 5. Update ~/.config/kanban-bridge/env (preserving all existing bridge keys)
lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
updates = {
    "PLANKA_URL": BASE,
    "PLANKA_API_KEY": admin_key,
    "PLANKA_BOT_API_KEY": bot_key,
    "PLANKA_WEBHOOK_URL": "http://127.0.0.1:8787/planka",
}
out = []
seen = set()
for line in lines:
    key = line.split("=", 1)[0].strip() if "=" in line else ""
    if key in updates:
        out.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        out.append(line)
for k, v in updates.items():
    if k not in seen:
        out.append(f"{k}={v}")

ENV_FILE.write_text("\n".join(out) + "\n")
ENV_FILE.chmod(0o600)

final = api("GET", f"/boards/{bid}", api_key=admin_key)["included"]
print(f"Planka setup complete! Board URL: {BASE}/boards/{bid}")
print("Lists:", [(l["name"], l["type"], l["id"]) for l in final.get("lists", []) if l.get("name")])
print("Saved PLANKA_API_KEY and PLANKA_BOT_API_KEY to", ENV_FILE)
PY
