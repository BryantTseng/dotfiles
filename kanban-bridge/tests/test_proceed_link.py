"""Tests for the signed "Do it" proceed link on plan-first replies."""

from __future__ import annotations

import os
import sys
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("WEBHOOK_SECRET", "test-webhook-secret")

import bridge  # noqa: E402


def fake_board(comments, post=None):
    return SimpleNamespace(
        get_comments=mock.Mock(return_value=comments),
        post_user_comment=post or mock.Mock(),
        task_url=lambda task_id: f"http://board/{task_id}",
    )


class TestProceedLink(unittest.TestCase):
    def test_block_contains_signed_link(self):
        block = bridge.proceed_block("c1", "m9")
        url = block.split("(", 1)[1].split(")", 1)[0]
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.assertEqual(query["t"], "c1")
        self.assertEqual(query["c"], "m9")
        self.assertEqual(query["s"], bridge.sign_proceed("c1", "m9"))
        self.assertNotEqual(query["s"], bridge.sign_proceed("c1", "other"))

    def test_rejects_bad_signature(self):
        status, _ = bridge.handle_proceed_click("POST", "t=c1&c=m9&s=deadbeef")
        self.assertEqual(status, 403)

    def test_stale_spec_is_rejected(self):
        sig = bridge.sign_proceed("c1", "m9")
        newer = SimpleNamespace(id="m10")
        with mock.patch.object(bridge, "BOARD", fake_board([newer]), create=True):
            status, _ = bridge.handle_proceed_click("POST", f"t=c1&c=m9&s={sig}")
        self.assertEqual(status, 409)

    def test_get_shows_confirm_without_side_effects(self):
        sig = bridge.sign_proceed("c1", "m9")
        last = SimpleNamespace(id="m9")
        board = fake_board([last])
        with mock.patch.object(bridge, "BOARD", board, create=True), \
                mock.patch.object(bridge, "AGENT", SimpleNamespace(name="agy"), create=True), \
                mock.patch.object(bridge, "enqueue") as enq:
            status, page = bridge.handle_proceed_click("GET", f"t=c1&c=m9&s={sig}")
        self.assertEqual(status, 200)
        self.assertIn("Do it", page)
        board.post_user_comment.assert_not_called()
        enq.assert_not_called()

    def test_post_replies_do_it_and_enqueues(self):
        sig = bridge.sign_proceed("c1", "m9")
        last = SimpleNamespace(id="m9")
        board = fake_board([last])
        with mock.patch.object(bridge, "BOARD", board, create=True), \
                mock.patch.object(bridge, "enqueue") as enq:
            status, _ = bridge.handle_proceed_click("POST", f"t=c1&c=m9&s={sig}")
        self.assertEqual(status, 200)
        board.post_user_comment.assert_called_once_with("c1", "do it")
        enq.assert_called_once_with("c1")


if __name__ == "__main__":
    unittest.main()
