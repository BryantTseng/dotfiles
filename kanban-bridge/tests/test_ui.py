"""Unit tests for the kanban-bridge Web UI helpers."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ui import (  # noqa: E402
    get_pricing_overview,
    list_pricing_models,
    list_recent_logs,
    read_editable_settings,
    read_log_tail,
    update_env_file,
)


class TestUiEnvSettings(unittest.TestCase):
    def test_update_env_file_preserves_secrets_comments_and_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "env"
            env_path.write_text(
                "# Secret section\n"
                "PLANKA_API_KEY=super-secret-planka\n"
                "OPENROUTER_API_KEY=sk-or-secret\n"
                "BRIDGE_AGENT_PROVIDER=agy\n"
                "BRIDGE_OPENCODE_MODEL=openrouter/anthropic/claude-haiku-5.5\n",
                encoding="utf-8",
            )
            os.chmod(env_path, 0o600)

            updated = update_env_file(
                env_path,
                {
                    "BRIDGE_AGENT_PROVIDER": "opencode",
                    "BRIDGE_OPENCODE_MODEL": "openrouter/google/gemini-2.5-flash, openrouter/anthropic/claude-haiku-5.5",
                    "BRIDGE_MAX_CONCURRENT": "3",
                },
            )
            self.assertEqual(updated["BRIDGE_AGENT_PROVIDER"], "opencode")
            self.assertEqual(updated["BRIDGE_MAX_CONCURRENT"], "3")

            raw = env_path.read_text(encoding="utf-8")
            self.assertIn("# Secret section", raw)
            self.assertIn("PLANKA_API_KEY=super-secret-planka", raw)
            self.assertIn("OPENROUTER_API_KEY=sk-or-secret", raw)
            self.assertIn("BRIDGE_AGENT_PROVIDER=opencode", raw)
            self.assertIn(
                'BRIDGE_OPENCODE_MODEL="openrouter/google/gemini-2.5-flash, openrouter/anthropic/claude-haiku-5.5"',
                raw,
            )
            self.assertIn("BRIDGE_MAX_CONCURRENT=3", raw)

            mode = stat.S_IMODE(env_path.stat().st_mode)
            self.assertEqual(mode, 0o600)

            read_back = read_editable_settings(env_path, {})
            self.assertEqual(read_back["BRIDGE_AGENT_PROVIDER"], "opencode")
            self.assertEqual(
                read_back["BRIDGE_OPENCODE_MODEL"],
                "openrouter/google/gemini-2.5-flash, openrouter/anthropic/claude-haiku-5.5",
            )
            self.assertNotIn("PLANKA_API_KEY", read_back)
            self.assertNotIn("OPENROUTER_API_KEY", read_back)

    def test_rejects_secret_or_invalid_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "env"
            with self.assertRaises(ValueError):
                update_env_file(env_path, {"OPENROUTER_API_KEY": "hack"})
            with self.assertRaises(ValueError):
                update_env_file(env_path, {"BRIDGE_AGENT_PROVIDER": "unknown"})
            with self.assertRaises(ValueError):
                update_env_file(env_path, {"BRIDGE_MAX_CONCURRENT": "0"})
            with self.assertRaises(ValueError):
                update_env_file(env_path, {"BRIDGE_RUN_TIMEOUT": "5"})


class TestUiLogsAndPricing(unittest.TestCase):
    def test_list_and_tail_logs_with_traversal_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs_dir = Path(tmp)
            p1 = logs_dir / "ses_one.jsonl"
            p1.write_text("line1\nline2\nline3\n", encoding="utf-8")

            items = list_recent_logs(logs_dir)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["session_id"], "ses_one")

            tail = read_log_tail(logs_dir, "ses_one", tail=2)
            self.assertEqual(tail["total_lines"], 3)
            self.assertEqual(tail["lines"], ["line2", "line3"])

            with self.assertRaises(ValueError):
                read_log_tail(logs_dir, "../secret")

    def test_pricing_overview_and_model_list(self):
        fake_table = {
            "google/gemini-2.5-flash": (0.3e-6, 2.5e-6, 0.0, 0.0),
            "anthropic/claude-haiku-5.5": (0.1e-6, 0.5e-6, 0.0, 0.0),
        }
        env = {"OPENROUTER_API_KEY": "sk-or-test"}
        settings = {
            "BRIDGE_OPENCODE_MODEL": "openrouter/google/gemini-2.5-flash, openrouter/anthropic/claude-haiku-5.5",
        }
        with mock.patch("ui._load_openrouter_pricing", return_value=fake_table):
            overview = get_pricing_overview(env, settings)
            models = list_pricing_models(env, query="gemini")

        self.assertTrue(overview["openrouter_key_configured"])
        self.assertNotIn("sk-or-test", json.dumps(overview))
        items = overview["configured"]["BRIDGE_OPENCODE_MODEL"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["input_per_m"], 0.3)
        self.assertEqual(items[0]["output_per_m"], 2.5)
        self.assertEqual(items[1]["input_per_m"], 0.1)
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["openrouter_id"], "openrouter/google/gemini-2.5-flash")


if __name__ == "__main__":
    unittest.main()
