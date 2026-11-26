"""Planka v2 board provider implementation.

Connects kanban-bridge to a self-hosted Planka v2 instance (with live WebSocket UI updates,
PostgreSQL persistence, native Markdown comments, and outbound webhooks).

Design choices tailored for Planka:
- Uses `X-Api-Key` authentication (separate optional `PLANKA_BOT_API_KEY` for a dedicated
  `viewer` + `canComment=true` bot user, falling back to a `🤖 Agy` prefix if omitted).
- Stores `session:<uuid>` as an invisible Markdown HTML comment (`<!-- agy-session:<uuid> -->`)
  at the end of the card description so Planka's board label picker stays uncluttered with only
  status/access labels (`agy`, `audited`, `audit:*`, `access:*`).
- Maps hex colors to Planka's named label color palette automatically.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Callable

from providers import BoardProvider, Comment, Label, Task, WebhookEvent

log = logging.getLogger("bridge.planka")

PLANKA_EVENTS = ["cardCreate", "cardUpdate", "cardLabelCreate", "cardLabelDelete", "commentCreate"]
SESSION_TAG_RE = re.compile(r"\n*<!--\s*agy-session:([A-Za-z0-9._-]+)\s*-->\s*$")

# Map bridge hex colors to Planka v2 Label.COLORS
HEX_TO_PLANKA_COLOR = {
    "4caf50": "fresh-salad",     # audited
    "e53935": "berry-red",       # audit:*
    "7e57c2": "lilac-eyes",      # access:*
    "9e9e9e": "muddy-grey",      # session:* / scratch
    "4f7cff": "lagoon-blue",     # agy trigger / worktree
    "fbc02d": "egg-yellow",      # plan-first
    "1e88e5": "antique-blue",    # repo:*
    "fb8c00": "pumpkin-orange",  # in-place
    "43a047": "sunny-grass",     # direct
}


def _planka_color(color: str) -> str:
    c = (color or "").strip().lstrip("#").lower()
    if c in HEX_TO_PLANKA_COLOR:
        return HEX_TO_PLANKA_COLOR[c]
    if "-" in c:
        return c  # already a Planka named color
    return "lagoon-blue"


class PlankaBoard(BoardProvider):
    name = "planka"
    webhook_path = "/planka"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        webhook_secret: str,
        webhook_url: str,
        bot_api_key: str | None = None,
        bot_username: str = "agy",
        bot_marker: str = "🤖 Agy",
        reconcile_interval: int = 60,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_url = self.base_url + "/api"
        self.api_key = api_key
        self.webhook_secret = webhook_secret
        self.webhook_url = webhook_url
        self.bot_api_key = bot_api_key or None
        self.bot_username = bot_username
        self.bot_marker = bot_marker
        self.reconcile_interval = reconcile_interval
        self._bot_user_id: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "PlankaBoard":
        port = int(env.get("BRIDGE_PORT", "8787"))
        bot_user = env.get("BOT_USERNAME", "agy")
        return cls(
            base_url=env.get("PLANKA_URL", "http://localhost:3000"),
            api_key=env["PLANKA_API_KEY"],
            webhook_secret=env["WEBHOOK_SECRET"],
            webhook_url=env.get("PLANKA_WEBHOOK_URL", f"http://127.0.0.1:{port}/planka"),
            bot_api_key=env.get("PLANKA_BOT_API_KEY"),
            bot_username=bot_user,
            bot_marker=env.get("BOT_MARKER", f"🤖 {bot_user.capitalize()}"),
            reconcile_interval=int(env.get("BRIDGE_RECONCILE_EVERY", "60")),
        )

    # ---------- low-level HTTP ----------

    def _api(self, method: str, path: str, body=None, api_key: str | None = None):
        req = urllib.request.Request(
            self.api_url + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "X-Api-Key": api_key or self.api_key,
                "Content-Type": "application/json",
            },
        )
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = r.read()
                return json.loads(raw, strict=False) if raw else None
            except urllib.error.HTTPError as e:
                if e.code < 500 or attempt == 3:
                    raise
                time.sleep(0.5 * 2**attempt)

    def _format_bot_text(self, md_text: str) -> str:
        if self.bot_api_key:
            return md_text
        return f"**{self.bot_marker}**\n\n{md_text}"

    def _resolve_bot_user_id(self) -> str | None:
        if self._bot_user_id:
            return self._bot_user_id
        if not self.bot_api_key:
            return None
        try:
            data = self._api("GET", "/users/me", api_key=self.bot_api_key) or {}
            uid = (data.get("item") or {}).get("id")
            if uid:
                self._bot_user_id = str(uid)
        except Exception:
            log.warning("could not resolve Planka bot user ID", exc_info=True)
        return self._bot_user_id

    def _get_board_bundle(self, board_id: str) -> dict:
        return self._api("GET", f"/boards/{board_id}") or {}

    def _build_task(
        self,
        card: dict,
        board_labels_by_id: dict[str, dict],
        card_label_ids: list[str],
        lists_by_id: dict[str, dict],
        board_name: str = "",
    ) -> Task:
        raw_desc = card.get("description") or ""
        labels: list[Label] = []
        for lid in card_label_ids:
            lbl = board_labels_by_id.get(str(lid))
            if lbl and lbl.get("name"):
                labels.append(
                    Label(id=str(lbl["id"]), title=lbl["name"], color=lbl.get("color") or "")
                )

        m = SESSION_TAG_RE.search(raw_desc)
        if m:
            session_id = m.group(1)
            desc_clean = raw_desc[: m.start()].rstrip()
            if not any(l.title.startswith("session:") for l in labels):
                labels.append(Label(id="__desc_session__", title=f"session:{session_id}", color="muddy-grey"))
        else:
            desc_clean = raw_desc.strip()

        lst = lists_by_id.get(str(card.get("listId") or "")) or {}
        list_name = (lst.get("name") or "").strip().lower()
        is_done = list_name == "done" or lst.get("type") == "closed"

        return Task(
            id=str(card["id"]),
            title=card.get("name") or "",
            description_text=desc_clean,
            project_id=str(card.get("boardId") or ""),
            project_name=board_name,
            done=is_done,
            index=str(card["id"])[-4:],
            labels=labels,
        )

    def _to_comment(self, raw: dict, users_by_id: dict[str, dict]) -> Comment:
        text = (raw.get("text") or "").strip()
        uid = str(raw.get("userId") or "")
        user = users_by_id.get(uid) or {}
        username = user.get("username") or user.get("name") or ""
        return Comment(
            id=str(raw["id"]),
            body_text=text,
            body_raw=text,
            author_username=username,
        )

    # ---------- BoardProvider methods ----------

    def ensure_setup(self) -> None:
        # 1. Ensure global Planka webhook pointing to our listener exists with current events
        webhooks = (self._api("GET", "/webhooks") or {}).get("items") or []
        desired_events = ",".join(PLANKA_EVENTS)
        existing_hook = None
        for h in webhooks:
            url = h.get("url") or ""
            if url == self.webhook_url:
                existing_hook = h
            elif url.endswith("/planka") and ":8787/" in url:
                self._api("DELETE", f"/webhooks/{h['id']}")
                log.info("removed old Planka webhook %s", url)
        if existing_hook is None:
            self._api(
                "POST",
                "/webhooks",
                {
                    "name": "kanban-bridge",
                    "url": self.webhook_url,
                    "accessToken": self.webhook_secret,
                    "events": desired_events,
                },
            )
            log.info("registered Planka webhook %s", self.webhook_url)
        elif set(existing_hook.get("events") or []) != set(PLANKA_EVENTS):
            self._api("PATCH", f"/webhooks/{existing_hook['id']}", {"events": desired_events})
            log.info("updated Planka webhook events -> %s", desired_events)

        # 2. Ensure bot user has viewer+canComment membership on every board
        bot_uid = self._resolve_bot_user_id()
        if not bot_uid:
            return
        proj_data = self._api("GET", "/projects") or {}
        boards = (proj_data.get("included") or {}).get("boards") or []
        memberships = (proj_data.get("included") or {}).get("boardMemberships") or []
        for b in boards:
            bid = str(b["id"])
            has_bot = any(
                str(m.get("boardId")) == bid and str(m.get("userId")) == bot_uid for m in memberships
            )
            if not has_bot:
                try:
                    self._api(
                        "POST",
                        f"/boards/{bid}/board-memberships",
                        {"userId": bot_uid, "role": "viewer", "canComment": True},
                    )
                    log.info("added bot user %s to Planka board %s (%s)", self.bot_username, bid, b.get("name"))
                except urllib.error.HTTPError as e:
                    if e.code != 409:  # already member
                        log.warning("could not add bot to board %s: %s", bid, e)

    def get_task(self, task_id: str) -> Task:
        data = self._api("GET", f"/cards/{task_id}") or {}
        card = data["item"]
        board_id = str(card["boardId"])
        board = self._get_board_bundle(board_id)
        board_name = (board.get("item") or {}).get("name") or ""
        inc = board.get("included") or {}
        labels_by_id = {str(l["id"]): l for l in (inc.get("labels") or [])}
        lists_by_id = {str(l["id"]): l for l in (inc.get("lists") or [])}
        card_label_ids = [
            str(cl["labelId"])
            for cl in ((data.get("included") or {}).get("cardLabels") or [])
            if str(cl.get("cardId")) == str(card["id"])
        ]
        return self._build_task(card, labels_by_id, card_label_ids, lists_by_id, board_name=board_name)

    def list_triggered_tasks(self, trigger_label: str) -> list[Task]:
        out: list[Task] = []
        proj_data = self._api("GET", "/projects") or {}
        boards = (proj_data.get("included") or {}).get("boards") or []
        for b in boards:
            bundle = self._get_board_bundle(str(b["id"]))
            board_name = (bundle.get("item") or {}).get("name") or b.get("name") or ""
            inc = bundle.get("included") or {}
            labels_by_id = {str(l["id"]): l for l in (inc.get("labels") or [])}
            lists_by_id = {str(l["id"]): l for l in (inc.get("lists") or [])}
            cl_by_card: dict[str, list[str]] = {}
            for cl in inc.get("cardLabels") or []:
                cl_by_card.setdefault(str(cl["cardId"]), []).append(str(cl["labelId"]))
            for card in inc.get("cards") or []:
                lst = lists_by_id.get(str(card.get("listId") or "")) or {}
                if lst.get("type") in ("archive", "trash"):
                    continue
                task = self._build_task(
                    card, labels_by_id, cl_by_card.get(str(card["id"]), []), lists_by_id, board_name=board_name
                )
                if trigger_label in task.label_titles:
                    out.append(task)
        return out

    def get_comments(self, task_id: str) -> list[Comment]:
        data = self._api("GET", f"/cards/{task_id}/comments") or {}
        items = data.get("items") or []
        users_by_id = {str(u["id"]): u for u in ((data.get("included") or {}).get("users") or [])}
        # Sort oldest -> newest deterministically
        items_sorted = sorted(items, key=lambda c: (c.get("createdAt") or "", int(c["id"])))
        return [self._to_comment(c, users_by_id) for c in items_sorted]

    def post_comment(self, task_id: str, md_text: str) -> Comment:
        data = self._api(
            "POST",
            f"/cards/{task_id}/comments",
            {"text": self._format_bot_text(md_text)},
            api_key=self.bot_api_key,
        )
        raw = data["item"]
        return Comment(
            id=str(raw["id"]),
            body_text=(raw.get("text") or "").strip(),
            body_raw=(raw.get("text") or "").strip(),
            author_username=self.bot_username if self.bot_api_key else "",
        )

    def edit_comment(self, task_id: str, comment_id: str, md_text: str) -> None:
        self._api(
            "PATCH",
            f"/comments/{comment_id}",
            {"text": self._format_bot_text(md_text)},
            api_key=self.bot_api_key,
        )

    def delete_comment(self, task_id: str, comment_id: str) -> None:
        try:
            self._api("DELETE", f"/comments/{comment_id}", api_key=self.bot_api_key)
        except urllib.error.HTTPError:
            pass

    def publish(self, task_id: str, placeholder_id: str, md_text: str) -> None:
        try:
            self.edit_comment(task_id, placeholder_id, md_text)
        except urllib.error.HTTPError:
            log.warning("card %s: editing comment %s failed; posting a new one", task_id, placeholder_id)
            self.post_comment(task_id, md_text)
            self.delete_comment(task_id, placeholder_id)

    def post_user_comment(self, task_id: str, text: str) -> None:
        self._api("POST", f"/cards/{task_id}/comments", {"text": text})

    def _get_or_create_label(self, board_id: str, title: str, color: str) -> dict:
        bundle = self._get_board_bundle(board_id)
        labels = (bundle.get("included") or {}).get("labels") or []
        for l in labels:
            if l.get("name") == title:
                return l
        max_pos = max((l.get("position") or 0 for l in labels), default=0)
        res = self._api(
            "POST",
            f"/boards/{board_id}/labels",
            {"name": title, "color": _planka_color(color), "position": max_pos + 65536},
        )
        return res["item"]

    def add_label(self, task_id: str, title: str, color: str) -> None:
        card = (self._api("GET", f"/cards/{task_id}") or {})["item"]
        lbl = self._get_or_create_label(str(card["boardId"]), title, color)
        try:
            self._api("POST", f"/cards/{task_id}/card-labels", {"labelId": str(lbl["id"])})
        except urllib.error.HTTPError as e:
            if e.code != 409:  # already attached
                raise

    def attach_session_label(self, task_id: str, session_id: str, prefix: str, color: str) -> None:
        """Store session ID as an invisible Markdown comment in card description."""
        card = (self._api("GET", f"/cards/{task_id}") or {})["item"]
        desc = (card.get("description") or "").rstrip()
        desc = SESSION_TAG_RE.sub("", desc).rstrip()
        tag = f"<!-- agy-session:{session_id} -->"
        new_desc = f"{desc}\n\n{tag}" if desc else tag
        self._api("PATCH", f"/cards/{task_id}", {"description": new_desc})

    def remove_labels(self, task: Task, predicate: Callable[[str], bool]) -> None:
        for l in task.labels:
            if l.id == "__desc_session__":
                continue
            if predicate(l.title):
                try:
                    self._api("DELETE", f"/cards/{task.id}/card-labels/labelId:{l.id}")
                except urllib.error.HTTPError as e:
                    if e.code != 404:
                        raise

    def move_to(self, task: Task, bucket_name: str) -> bool:
        try:
            bundle = self._get_board_bundle(task.project_id)
            lists = (bundle.get("included") or {}).get("lists") or []
            target = bucket_name.strip().lower()
            lst = next(
                (
                    l
                    for l in lists
                    if l.get("type") in ("active", "closed")
                    and (l.get("name") or "").strip().lower() == target
                ),
                None,
            )
            if not lst:
                return False
            self._api(
                "PATCH",
                f"/cards/{task.id}",
                {"listId": str(lst["id"]), "position": 65536},
            )
            return True
        except urllib.error.HTTPError as e:
            log.warning("card %s: moving to %r failed: %s", task.id, bucket_name, e)
            return False

    def current_bucket(self, task: Task) -> str | None:
        card = (self._api("GET", f"/cards/{task.id}") or {}).get("item")
        if not card:
            return None
        bundle = self._get_board_bundle(str(card["boardId"]))
        lists = (bundle.get("included") or {}).get("lists") or []
        lst = next((l for l in lists if str(l["id"]) == str(card.get("listId"))), None)
        return (lst.get("name") or "").strip().lower() if lst else None

    def task_url(self, task_id: str) -> str:
        return f"{self.base_url}/cards/{task_id}"

    def is_from_bot(self, comment: Comment, doer_username: str | None = None) -> bool:
        if self.bot_marker in comment.body_raw or "🤖 Claude" in comment.body_raw:
            return True
        authors = {comment.author_username, doer_username}
        return bool(self.bot_api_key) and self.bot_username in authors

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent | None:
        auth = headers.get("authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        if not hmac.compare_digest(token, self.webhook_secret):
            raise PermissionError("invalid Planka webhook accessToken")

        payload = json.loads(body)
        ev = payload.get("event") or ""
        data = payload.get("data") or {}
        item = data.get("item") or {}
        user = payload.get("user") or {}
        doer = user.get("username") or user.get("name")

        if ev == "commentCreate":
            card_id = item.get("cardId")
            if not card_id:
                return None
            text = (item.get("text") or "").strip()
            return WebhookEvent(
                event_type="task.comment.created",
                task_id=str(card_id),
                comment=Comment(
                    id=str(item.get("id", "")),
                    body_text=text,
                    body_raw=text,
                    author_username=doer or "",
                ),
                doer_username=doer,
            )
        if ev in ("cardLabelCreate", "cardLabelDelete"):
            card_id = item.get("cardId")
            return WebhookEvent(event_type="task.updated", task_id=str(card_id), doer_username=doer) if card_id else None
        if ev == "cardCreate":
            card_id = item.get("id")
            return WebhookEvent(event_type="task.created", task_id=str(card_id), doer_username=doer) if card_id else None
        if ev == "cardUpdate":
            card_id = item.get("id")
            return WebhookEvent(event_type="task.updated", task_id=str(card_id), doer_username=doer) if card_id else None
        return None
