"""Tests for provider-aware session marker handling in bridge.py."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# bridge.py reads WEBHOOK_SECRET at import time; tests never use the webhook.
os.environ.setdefault("WEBHOOK_SECRET", "test-webhook-secret")

import bridge  # noqa: E402
from providers import Label, Task  # noqa: E402

AGY_SID = "e4ccc07f-dd84-44dd-997a-9dd9436b935d"
OPENCODE_SID = "ses_or_123"


def task_with_session(sid: str) -> Task:
    return Task(
        id="c1",
        title="Card",
        labels=[Label(id="__desc_session__", title=f"session:{sid}")],
    )


class TestSessionIdCompatibility(unittest.TestCase):
    def test_opencode_accepts_only_ses_ids(self):
        self.assertTrue(bridge.session_id_is_compatible("opencode", OPENCODE_SID))
        self.assertFalse(bridge.session_id_is_compatible("opencode", AGY_SID))

    def test_agy_and_claude_reject_opencode_ids(self):
        for provider in ("agy", "claude"):
            with self.subTest(provider=provider):
                self.assertTrue(bridge.session_id_is_compatible(provider, AGY_SID))
                self.assertFalse(bridge.session_id_is_compatible(provider, OPENCODE_SID))

    def test_unknown_provider_is_left_alone(self):
        self.assertTrue(bridge.session_id_is_compatible("kanban", AGY_SID))
        self.assertTrue(bridge.session_id_is_compatible("kanban", OPENCODE_SID))


class TestSessionOf(unittest.TestCase):
    def test_ignores_stale_agy_marker_when_running_opencode(self):
        with mock.patch.object(bridge, "AGENT", SimpleNamespace(name="opencode"), create=True):
            self.assertIsNone(bridge.session_of(task_with_session(AGY_SID)))

    def test_returns_opencode_marker_when_running_opencode(self):
        with mock.patch.object(bridge, "AGENT", SimpleNamespace(name="opencode"), create=True):
            self.assertEqual(bridge.session_of(task_with_session(OPENCODE_SID)), OPENCODE_SID)

    def test_ignores_stale_opencode_marker_when_running_agy(self):
        with mock.patch.object(bridge, "AGENT", SimpleNamespace(name="agy"), create=True):
            self.assertIsNone(bridge.session_of(task_with_session(OPENCODE_SID)))

    def test_returns_agy_marker_when_running_agy(self):
        with mock.patch.object(bridge, "AGENT", SimpleNamespace(name="agy"), create=True):
            self.assertEqual(bridge.session_of(task_with_session(AGY_SID)), AGY_SID)


if __name__ == "__main__":
    unittest.main()
