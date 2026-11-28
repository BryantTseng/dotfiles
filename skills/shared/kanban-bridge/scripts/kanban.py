#!/usr/bin/env python3
"""Safe CLI for operating the local Planka v2 kanban board from agent sessions.

Reads PLANKA_URL and PLANKA_API_KEY directly from ~/.config/kanban-bridge/env (never prints
secrets). Enforces the kanban-bridge safety guardrails in code:
  - Refuses to add/remove `access:*` or `session:*` labels.
  - Refuses to post `approve` / `deny` comments.
  - Preserves `<!-- <provider>-session:... -->` footers when editing card descriptions.
  - Never attaches the trigger label (`BRIDGE_TRIGGER_LABEL`) unless `--trigger` / `--confirm-trigger` is passed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENV_PATH = Path.home() / ".config/kanban-bridge/env"
SESSION_TAG_RE = re.compile(r"\n*<!--\s*(?:agy|claude|opencode|kanban)-session:([A-Za-z0-9._-]+)\s*-->\s*$")
APPROVE_RE = re.compile(r"^\s*(approve|deny)\b", re.I)


def load_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        raise SystemExit(f"Config file not found: {ENV_PATH}")
    out: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out


class PlankaClient:
    def __init__(self, env: dict[str, str]):
        self.base_url = env.get("PLANKA_URL", "http://localhost:3000").rstrip("/")
        self.api_url = self.base_url + "/api"
        self.api_key = env.get("PLANKA_API_KEY", "")
        self.trigger_label = env.get("BRIDGE_TRIGGER_LABEL", "agy")
        self.bridge_port = int(env.get("BRIDGE_PORT", "8787"))
        if not self.api_key:
            raise SystemExit("PLANKA_API_KEY is missing in ~/.config/kanban-bridge/env")

    def req(self, method: str, path: str, body=None):
        r = urllib.request.Request(
            self.api_url + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"X-Api-Key": self.api_key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                raw = resp.read()
                return json.loads(raw, strict=False) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            raise SystemExit(f"Planka API error {e.code} on {method} {path}: {detail}")

    def resolve_board(self, board_arg: str | None) -> tuple[str, str]:
        data = self.req("GET", "/projects") or {}
        boards = (data.get("included") or {}).get("boards") or []
        if not boards:
            raise SystemExit("No Planka boards found.")
        if board_arg:
            for b in boards:
                if str(b["id"]) == board_arg or (b.get("name") or "").lower() == board_arg.lower():
                    return str(b["id"]), b.get("name") or str(b["id"])
            raise SystemExit(f"Board not found: {board_arg!r}")
        for preferred in ("general", "scratch", self.trigger_label.lower(), "agy"):
            for b in boards:
                if (b.get("name") or "").lower() == preferred:
                    return str(b["id"]), b["name"]
        return str(boards[0]["id"]), boards[0].get("name") or str(boards[0]["id"])

    def board_bundle(self, board_id: str) -> dict:
        return self.req("GET", f"/boards/{board_id}") or {}


def cmd_health(cli: PlankaClient, args) -> None:
    me = cli.req("GET", "/users/me")["item"]
    bridge_status = "down"
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{cli.bridge_port}/health", timeout=3) as r:
            bridge_status = r.read().decode("utf-8").strip()
    except Exception as e:
        bridge_status = f"unreachable ({e})"
    print(f"Planka: ok ({cli.base_url}, user={me.get('username') or me.get('email')})")
    print(f"Bridge: {bridge_status}")


def cmd_boards(cli: PlankaClient, args) -> None:
    data = cli.req("GET", "/projects") or {}
    boards = (data.get("included") or {}).get("boards") or []
    if args.json:
        print(json.dumps([
            {"id": str(b["id"]), "name": b.get("name") or str(b["id"]), "url": f"{cli.base_url}/boards/{b['id']}"}
            for b in boards
        ], indent=2))
        return
    if not boards:
        print("No Planka boards found.")
        return
    for b in boards:
        print(f"- {b.get('name') or b['id']}  (id: {b['id']})  {cli.base_url}/boards/{b['id']}")


def cmd_board(cli: PlankaClient, args) -> None:
    bid, bname = cli.resolve_board(args.board)
    inc = cli.board_bundle(bid).get("included") or {}
    lists = sorted(
        [l for l in (inc.get("lists") or []) if l.get("type") in ("active", "closed") and l.get("name")],
        key=lambda l: l.get("position") or 0,
    )
    labels_by_id = {str(l["id"]): l.get("name") or "" for l in (inc.get("labels") or [])}
    card_labels: dict[str, list[str]] = {}
    for cl in inc.get("cardLabels") or []:
        name = labels_by_id.get(str(cl["labelId"]), "")
        if name:
            card_labels.setdefault(str(cl["cardId"]), []).append(name)

    cards_by_list: dict[str, list[dict]] = {}
    for c in inc.get("cards") or []:
        cards_by_list.setdefault(str(c.get("listId")), []).append(c)

    if args.json:
        out = {
            "board": {"id": bid, "name": bname, "url": f"{cli.base_url}/boards/{bid}"},
            "lists": [],
        }
        for l in lists:
            lid = str(l["id"])
            cards = sorted(cards_by_list.get(lid, []), key=lambda c: c.get("position") or 0)
            out["lists"].append({
                "id": lid,
                "name": l["name"],
                "type": l.get("type"),
                "cards": [
                    {
                        "id": str(c["id"]),
                        "name": c.get("name") or "",
                        "labels": card_labels.get(str(c["id"]), []),
                        "has_session": bool(SESSION_TAG_RE.search(c.get("description") or "")),
                        "url": f"{cli.base_url}/cards/{c['id']}",
                    }
                    for c in cards
                ],
            })
        print(json.dumps(out, indent=2))
        return

    print(f"Board: {bname} ({cli.base_url}/boards/{bid})\n")
    for l in lists:
        lid = str(l["id"])
        cards = sorted(cards_by_list.get(lid, []), key=lambda c: c.get("position") or 0)
        print(f"## {l['name']} ({len(cards)})  [list:{lid}]")
        if not cards:
            print("  (empty)")
        for c in cards:
            cid = str(c["id"])
            lbls = card_labels.get(cid, [])
            tag_str = f"  [{', '.join(lbls)}]" if lbls else ""
            sess = " (session)" if SESSION_TAG_RE.search(c.get("description") or "") else ""
            print(f"  - {cid}  {c.get('name') or ''}{tag_str}{sess}")
        print()


def cmd_show(cli: PlankaClient, args) -> None:
    data = cli.req("GET", f"/cards/{args.card_id}") or {}
    card = data["item"]
    bid = str(card["boardId"])
    inc = cli.board_bundle(bid).get("included") or {}
    lists = {str(l["id"]): l.get("name") or "" for l in (inc.get("lists") or [])}
    labels_by_id = {str(l["id"]): l.get("name") or "" for l in (inc.get("labels") or [])}
    lbls = [
        labels_by_id.get(str(cl["labelId"]), "")
        for cl in ((data.get("included") or {}).get("cardLabels") or [])
        if str(cl.get("cardId")) == str(card["id"])
    ]
    raw_desc = card.get("description") or ""
    has_sess = bool(SESSION_TAG_RE.search(raw_desc))
    desc = SESSION_TAG_RE.sub("", raw_desc).strip()

    cdata = cli.req("GET", f"/cards/{args.card_id}/comments") or {}
    users = {str(u["id"]): u.get("username") or u.get("name") or "?" for u in ((cdata.get("included") or {}).get("users") or [])}
    comments = sorted(cdata.get("items") or [], key=lambda c: (c.get("createdAt") or "", int(c["id"])))

    card_inc = data.get("included") or {}
    raw_tls = sorted(card_inc.get("taskLists") or [], key=lambda x: x.get("position") or 0)
    raw_tasks = sorted(card_inc.get("tasks") or [], key=lambda x: x.get("position") or 0)
    checklists = []
    for tl in raw_tls:
        tlid = str(tl["id"])
        items = [
            {
                "id": str(t["id"]),
                "name": t.get("name") or "",
                "isCompleted": bool(t.get("isCompleted")),
            }
            for t in raw_tasks
            if str(t.get("taskListId")) == tlid
        ]
        checklists.append({"id": tlid, "name": tl.get("name") or "", "items": items})

    if args.json:
        print(json.dumps({
            "id": str(card["id"]),
            "name": card.get("name") or "",
            "list": lists.get(str(card.get("listId")), ""),
            "labels": [x for x in lbls if x],
            "has_session": has_sess,
            "description": desc,
            "url": f"{cli.base_url}/cards/{card['id']}",
            "checklists": checklists,
            "comments": [
                {
                    "id": str(c["id"]),
                    "author": users.get(str(c.get("userId")), "?"),
                    "createdAt": c.get("createdAt"),
                    "text": c.get("text") or "",
                }
                for c in comments
            ],
        }, indent=2))
        return

    print(f"# {card.get('name')}  (id: {card['id']})")
    print(f"URL:     {cli.base_url}/cards/{card['id']}")
    print(f"List:    {lists.get(str(card.get('listId')), '?')}")
    print(f"Labels:  {', '.join(x for x in lbls if x) or '(none)'}")
    print(f"Session: {'attached' if has_sess else 'none'}")
    if desc:
        print(f"\n## Description\n{desc}")
    if checklists:
        print("\n## Checklists")
        for cl in checklists:
            done_n = sum(1 for x in cl["items"] if x["isCompleted"])
            print(f"### {cl['name']} ({done_n}/{len(cl['items'])})  [list:{cl['id']}]")
            for item in cl["items"]:
                mark = "x" if item["isCompleted"] else " "
                print(f"  - [{mark}] {item['name']}  [item:{item['id']}]")
    print(f"\n## Comments ({len(comments)})")
    for c in comments:
        author = users.get(str(c.get("userId")), "?")
        print(f"\n[{author} @ {c.get('createdAt', '')}]\n{(c.get('text') or '').strip()}")


def cmd_create(cli: PlankaClient, args) -> None:
    board_arg = args.board
    if not board_arg and args.repo:
        # If a board matching --repo exists, route the card there automatically
        data = cli.req("GET", "/projects") or {}
        boards = (data.get("included") or {}).get("boards") or []
        if any((b.get("name") or "").lower() == args.repo.strip().lower() for b in boards):
            board_arg = args.repo.strip()
    bid, _ = cli.resolve_board(board_arg)
    inc = cli.board_bundle(bid).get("included") or {}
    lists = [l for l in (inc.get("lists") or []) if l.get("type") in ("active", "closed") and l.get("name")]
    target = args.list.strip().lower()
    lst = next((l for l in lists if l["name"].strip().lower() == target), None)
    if not lst:
        names = ", ".join(l["name"] for l in lists)
        raise SystemExit(f"List {args.list!r} not found. Available: {names}")

    payload = {"type": "project", "position": 65536, "name": args.title}
    if args.desc:
        payload["description"] = args.desc
    card = cli.req("POST", f"/lists/{lst['id']}/cards", payload)["item"]
    cid = str(card["id"])

    existing_labels = {l.get("name"): l for l in (inc.get("labels") or []) if l.get("name")}
    max_pos = max((l.get("position") or 0 for l in (inc.get("labels") or [])), default=0)

    def ensure_and_attach(label_name: str, color: str):
        nonlocal max_pos
        lbl = existing_labels.get(label_name)
        if not lbl:
            max_pos += 65536
            lbl = cli.req(
                "POST",
                f"/boards/{bid}/labels",
                {"name": label_name, "color": color, "position": max_pos},
            )["item"]
            existing_labels[label_name] = lbl
        cli.req("POST", f"/cards/{cid}/card-labels", {"labelId": str(lbl["id"])})

    attached = []
    ws = (args.workspace or "").strip().lower()
    if args.repo:
        repo_lbl = f"repo:{args.repo.strip().lower()}"
        ensure_and_attach(repo_lbl, "antique-blue")
        attached.append(repo_lbl)
        if ws in ("worktree", "in-place"):
            ensure_and_attach(ws, "lagoon-blue" if ws == "worktree" else "pumpkin-orange")
            attached.append(ws)
    elif ws == "scratch":
        ensure_and_attach("scratch", "muddy-grey")
        attached.append("scratch")
    if args.plan_first:
        ensure_and_attach("plan-first", "egg-yellow")
        attached.append("plan-first")
    elif args.direct:
        ensure_and_attach("direct", "sunny-grass")
        attached.append("direct")
    if args.orchestrator:
        ensure_and_attach("orchestrator", "lilac-eyes")
        attached.append("orchestrator")
    if args.trigger:
        ensure_and_attach(cli.trigger_label, "lagoon-blue")
        attached.append(cli.trigger_label)

    print(json.dumps({
        "id": cid,
        "name": card["name"],
        "list": lst["name"],
        "labels": attached,
        "triggered": bool(args.trigger),
        "url": f"{cli.base_url}/cards/{cid}",
    }, indent=2))


def cmd_move(cli: PlankaClient, args) -> None:
    card = cli.req("GET", f"/cards/{args.card_id}")["item"]
    inc = cli.board_bundle(str(card["boardId"])).get("included") or {}
    lists = [l for l in (inc.get("lists") or []) if l.get("type") in ("active", "closed") and l.get("name")]
    target = args.list.strip().lower()
    lst = next((l for l in lists if l["name"].strip().lower() == target), None)
    if not lst:
        names = ", ".join(l["name"] for l in lists)
        raise SystemExit(f"List {args.list!r} not found. Available: {names}")
    cli.req("PATCH", f"/cards/{args.card_id}", {"listId": str(lst["id"]), "position": 65536})
    print(f"Moved card {args.card_id} -> {lst['name']}")


def cmd_edit(cli: PlankaClient, args) -> None:
    card = cli.req("GET", f"/cards/{args.card_id}")["item"]
    updates = {}
    if args.title is not None:
        updates["name"] = args.title
    if args.desc is not None:
        # Strictly preserve any existing invisible session tag
        old_desc = card.get("description") or ""
        m = SESSION_TAG_RE.search(old_desc)
        clean_new = SESSION_TAG_RE.sub("", args.desc).rstrip()
        if m:
            tag = m.group(0).strip()
            updates["description"] = f"{clean_new}\n\n{tag}" if clean_new else tag
        else:
            updates["description"] = clean_new
    if not updates:
        raise SystemExit("Nothing to update (pass --title and/or --desc).")
    cli.req("PATCH", f"/cards/{args.card_id}", updates)
    print(f"Updated card {args.card_id}")


def cmd_comment(cli: PlankaClient, args) -> None:
    text = args.text.strip()
    if not text:
        raise SystemExit("Comment text cannot be empty.")
    if APPROVE_RE.match(text):
        raise SystemExit("Refused: agents may never post 'approve' or 'deny' access decisions.")
    c = cli.req("POST", f"/cards/{args.card_id}/comments", {"text": text})["item"]
    print(f"Posted comment {c['id']} on card {args.card_id}")


def cmd_label(cli: PlankaClient, args) -> None:
    name = args.label.strip()
    lower = name.lower()
    if lower.startswith(("access:", "session:")):
        raise SystemExit(f"Refused: agents may never modify {name!r} labels.")
    if args.action == "add" and lower == cli.trigger_label.lower() and not args.confirm_trigger:
        raise SystemExit(
            f"Refused: adding {cli.trigger_label!r} starts a real billed/quota run. "
            "Pass --confirm-trigger only if the user explicitly asked to start the card."
        )

    card = cli.req("GET", f"/cards/{args.card_id}")["item"]
    bid = str(card["boardId"])
    inc = cli.board_bundle(bid).get("included") or {}
    lbl = next((l for l in (inc.get("labels") or []) if l.get("name") == name), None)
    if args.action == "add":
        if not lbl:
            max_pos = max((l.get("position") or 0 for l in (inc.get("labels") or [])), default=0)
            lbl = cli.req(
                "POST",
                f"/boards/{bid}/labels",
                {"name": name, "color": "lagoon-blue", "position": max_pos + 65536},
            )["item"]
        try:
            cli.req("POST", f"/cards/{args.card_id}/card-labels", {"labelId": str(lbl["id"])})
        except SystemExit as e:
            if "409" not in str(e):
                raise
        print(f"Added label {name!r} to card {args.card_id}")
    else:
        if not lbl:
            raise SystemExit(f"Label {name!r} not found on board {bid}")
        cli.req("DELETE", f"/cards/{args.card_id}/card-labels/labelId:{lbl['id']}")
        print(f"Removed label {name!r} from card {args.card_id}")


def cmd_delete(cli: PlankaClient, args) -> None:
    cli.req("DELETE", f"/cards/{args.card_id}")
    print(f"Deleted card {args.card_id}")


def cmd_checklist(cli: PlankaClient, args) -> None:
    data = cli.req("GET", f"/cards/{args.card_id}") or {}
    inc = data.get("included") or {}
    tls = sorted(inc.get("taskLists") or [], key=lambda x: x.get("position") or 0)
    tasks = sorted(inc.get("tasks") or [], key=lambda x: x.get("position") or 0)

    def find_list(name_or_id: str) -> dict | None:
        key = name_or_id.strip().lower()
        return next(
            (t for t in tls if str(t["id"]) == name_or_id or (t.get("name") or "").strip().lower() == key),
            None,
        )

    def find_item(name_or_id: str) -> dict | None:
        key = name_or_id.strip().lower()
        return next(
            (t for t in tasks if str(t["id"]) == name_or_id or (t.get("name") or "").strip().lower() == key),
            None,
        )

    act = args.action
    if act == "add-list":
        max_pos = max((t.get("position") or 0 for t in tls), default=0)
        tl = cli.req(
            "POST",
            f"/cards/{args.card_id}/task-lists",
            {"name": args.target, "position": max_pos + 65536, "showOnFrontOfCard": True},
        )["item"]
        print(f"Created checklist {tl['name']!r} (id: {tl['id']})")
    elif act == "add":
        list_name = args.list_name or "Tasks"
        tl = find_list(list_name)
        if not tl:
            max_pos = max((t.get("position") or 0 for t in tls), default=0)
            tl = cli.req(
                "POST",
                f"/cards/{args.card_id}/task-lists",
                {"name": list_name, "position": max_pos + 65536, "showOnFrontOfCard": True},
            )["item"]
        tl_tasks = [t for t in tasks if str(t.get("taskListId")) == str(tl["id"])]
        max_ipos = max((t.get("position") or 0 for t in tl_tasks), default=0)
        item = cli.req(
            "POST",
            f"/task-lists/{tl['id']}/tasks",
            {"name": args.target, "position": max_ipos + 65536, "isCompleted": bool(args.done)},
        )["item"]
        print(f"Added checklist item {item['name']!r} (id: {item['id']}, done={bool(args.done)})")
    elif act in ("check", "uncheck"):
        item = find_item(args.target)
        if not item:
            raise SystemExit(f"Checklist item not found: {args.target!r}")
        cli.req("PATCH", f"/tasks/{item['id']}", {"isCompleted": act == "check"})
        print(f"{'Checked' if act == 'check' else 'Unchecked'} item {item['name']!r}")
    elif act == "delete-item":
        item = find_item(args.target)
        if not item:
            raise SystemExit(f"Checklist item not found: {args.target!r}")
        cli.req("DELETE", f"/tasks/{item['id']}")
        print(f"Deleted checklist item {item['id']}")
    elif act == "delete-list":
        tl = find_list(args.target)
        if not tl:
            raise SystemExit(f"Checklist not found: {args.target!r}")
        cli.req("DELETE", f"/task-lists/{tl['id']}")
        print(f"Deleted checklist {tl['id']}")


def main() -> None:
    p = argparse.ArgumentParser(description="Safe CLI for operating the local Planka kanban board.")
    p.add_argument("--board", help="Board ID or name (defaults to 'General')")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health", help="Check Planka API and bridge /health")

    p_boards = sub.add_parser("boards", help="List all Planka boards")
    p_boards.add_argument("--json", action="store_true")

    p_board = sub.add_parser("board", help="List columns and cards")
    p_board.add_argument("--json", action="store_true")

    p_show = sub.add_parser("show", help="Show card details, checklists, and comments")
    p_show.add_argument("card_id")
    p_show.add_argument("--json", action="store_true")

    p_create = sub.add_parser("create", help="Create a card (defaults to Backlog, no trigger)")
    p_create.add_argument("title")
    p_create.add_argument("--desc", default="")
    p_create.add_argument("--list", default="Backlog")
    p_create.add_argument("--repo", help="Attach repo:<name> label (e.g. clerk, dotfiles)")
    p_create.add_argument(
        "--workspace",
        choices=["worktree", "in-place", "scratch"],
        help="Workspace mode: isolated git worktree, in-place repo dir, or standalone scratch dir",
    )
    p_create.add_argument("--plan-first", action="store_true", help="Attach plan-first label (spec/Q&A before coding)")
    p_create.add_argument("--direct", action="store_true", help="Attach direct label (start coding immediately)")
    p_create.add_argument("--orchestrator", action="store_true", help="Attach orchestrator label (coordinator mode: delegate coding & review to subagents)")
    p_create.add_argument("--trigger", action="store_true", help="Attach the configured trigger label to start a run")

    p_move = sub.add_parser("move", help="Move a card to a column by name")
    p_move.add_argument("card_id")
    p_move.add_argument("list")

    p_edit = sub.add_parser("edit", help="Edit a card title/description (preserves session metadata)")
    p_edit.add_argument("card_id")
    p_edit.add_argument("--title")
    p_edit.add_argument("--desc")

    p_comment = sub.add_parser("comment", help="Post a comment on a card")
    p_comment.add_argument("card_id")
    p_comment.add_argument("text")

    p_label = sub.add_parser("label", help="Add or remove a non-access label on a card")
    p_label.add_argument("card_id")
    p_label.add_argument("action", choices=["add", "remove"])
    p_label.add_argument("label")
    p_label.add_argument("--confirm-trigger", action="store_true")

    p_cl = sub.add_parser("checklist", help="Manage native Planka Task Lists (checklists) on a card")
    p_cl.add_argument("card_id")
    p_cl.add_argument("action", choices=["add-list", "add", "check", "uncheck", "delete-item", "delete-list"])
    p_cl.add_argument("target", help="Checklist name/ID or item text/ID")
    p_cl.add_argument("--list", dest="list_name", help="Checklist name/ID when using 'add' (default: Tasks)")
    p_cl.add_argument("--done", action="store_true", help="Mark newly added item completed")

    p_del = sub.add_parser("delete", help="Delete a card")
    p_del.add_argument("card_id")

    args = p.parse_args()
    cli = PlankaClient(load_env())
    {
        "health": cmd_health,
        "boards": cmd_boards,
        "board": cmd_board,
        "show": cmd_show,
        "create": cmd_create,
        "move": cmd_move,
        "edit": cmd_edit,
        "comment": cmd_comment,
        "label": cmd_label,
        "checklist": cmd_checklist,
        "delete": cmd_delete,
    }[args.cmd](cli, args)


if __name__ == "__main__":
    main()
