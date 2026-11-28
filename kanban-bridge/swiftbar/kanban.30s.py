#!/usr/bin/env python3
# <xbar.title>Kanban Bridge</xbar.title>
# <xbar.version>v1.0</xbar.version>
# <xbar.author>kanban-bridge</xbar.author>
# <xbar.desc>Shows Planka Kanban cards needing attention, running, or auditing, plus bridge health.</xbar.desc>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>false</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>

"""SwiftBar menu-bar plugin for kanban-bridge (Planka v2).

Install on macOS:
  ln -sf ~/workspace/kanban-bridge/swiftbar/kanban.30s.py ~/Library/Application\\ Support/SwiftBar/kanban.30s.py
(or symlink into your configured SwiftBar plugins folder).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

ENV_PATH = Path.home() / ".config" / "kanban-bridge" / "env"


def load_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    out: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out


def api_get(base_url: str, api_key: str, path: str) -> dict:
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api" + path,
        headers={"X-Api-Key": api_key},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read())


def bridge_health(port: int) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as resp:
            data = json.loads(resp.read())
            if data.get("ok"):
                return True, f"{data.get('agent', '?')} / {data.get('board', '?')}"
            return False, "unhealthy"
    except Exception:
        return False, "down"


def resolve_board(base_url: str, api_key: str, trigger_label: str) -> tuple[str, str]:
    data = api_get(base_url, api_key, "/projects") or {}
    boards = (data.get("included") or {}).get("boards") or []
    if not boards:
        raise RuntimeError("No Planka boards found")
    for preferred in ("general", "scratch", trigger_label.lower(), "agy"):
        for b in boards:
            if (b.get("name") or "").lower() == preferred:
                return str(b["id"]), b.get("name") or str(b["id"])
    return str(boards[0]["id"]), boards[0].get("name") or str(boards[0]["id"])


def sanitize_title(s: str) -> str:
    return (s or "").replace("|", "/").replace("\n", " ").strip()[:80]


def main() -> None:
    env = load_env()
    base_url = env.get("PLANKA_URL", "http://localhost:3000").rstrip("/")
    api_key = env.get("PLANKA_API_KEY", "")
    trigger = env.get("BRIDGE_TRIGGER_LABEL", "agy")
    port = int(env.get("BRIDGE_PORT", "8787"))
    pending_name = env.get("BRIDGE_PENDING_BUCKET", "pending").strip().lower()
    audit_name = env.get("BRIDGE_AUDIT_BUCKET", "auditing").strip().lower()

    if not api_key:
        print("⚠️ KB | color=orange")
        print("---")
        print(f"Missing PLANKA_API_KEY in {ENV_PATH}")
        return

    bridge_ok, bridge_info = bridge_health(port)

    try:
        bid, bname = resolve_board(base_url, api_key, trigger)
        bundle = api_get(base_url, api_key, f"/boards/{bid}") or {}
    except Exception as e:
        print("⚠️ KB | color=red")
        print("---")
        print(f"Planka unreachable: {e}")
        print(f"Open Planka | href={base_url}")
        print("Refresh | refresh=true")
        return

    inc = bundle.get("included") or {}
    lists_by_id = {
        str(l["id"]): (l.get("name") or "").strip().lower()
        for l in (inc.get("lists") or [])
        if l.get("type") in ("active", "closed")
    }
    by_bucket: dict[str, list[dict]] = {
        "need action": [],
        "flagged": [],
        "doing": [],
        audit_name: [],
        pending_name: [],
    }
    for card in sorted(inc.get("cards") or [], key=lambda c: c.get("position") or 0):
        bkt = lists_by_id.get(str(card.get("listId") or ""), "")
        if bkt in by_bucket:
            by_bucket[bkt].append(card)

    need_n = len(by_bucket["need action"])
    flag_n = len(by_bucket["flagged"])
    doing_n = len(by_bucket["doing"])
    audit_n = len(by_bucket[audit_name])
    pending_n = len(by_bucket[pending_name])
    attention = need_n + flag_n

    # Menu bar title
    parts: list[str] = []
    if not bridge_ok:
        parts.append("⚠️")
    if flag_n:
        parts.append(f"🚩{flag_n}")
    if need_n:
        parts.append(f"❓{need_n}")
    if doing_n:
        parts.append(f"⚙️{doing_n}")
    if audit_n:
        parts.append(f"🔍{audit_n}")
    if not parts:
        parts.append(f"📋{pending_n}" if pending_n else "📋")

    color = "red" if (not bridge_ok or flag_n) else ("orange" if attention else None)
    bar = " ".join(parts)
    print(f"{bar} | color={color}" if color else bar)
    print("---")

    if not bridge_ok:
        print("⚠️ Bridge is NOT running (port " + str(port) + ") | color=red")
    else:
        print(f"Bridge: online ({bridge_info}) | color=green")
    print(f"Open Board: {sanitize_title(bname)} | href={base_url}/boards/{bid}")

    sections = [
        ("🚩 Flagged", by_bucket["flagged"], "red"),
        ("❓ Need Action", by_bucket["need action"], "orange"),
        ("⚙️ Doing", by_bucket["doing"], None),
        ("🔍 Auditing", by_bucket[audit_name], None),
        ("✅ Pending Review", by_bucket[pending_name], None),
    ]
    for title, cards, sec_color in sections:
        if not cards:
            continue
        print("---")
        hdr = f"{title} ({len(cards)})"
        print(f"{hdr} | color={sec_color}" if sec_color else hdr)
        for c in cards:
            cid = str(c["id"])
            cname = sanitize_title(c.get("name") or f"Card {cid[-4:]}")
            print(f"-- {cname} | href={base_url}/cards/{cid}")

    print("---")
    print("Refresh | refresh=true")


if __name__ == "__main__":
    main()
