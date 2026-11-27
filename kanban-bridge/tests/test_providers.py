"""Fast, isolated unit tests for kanban-bridge providers (no network or live API calls)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from providers import (
    Task,
    is_valid_proxy_token,
    load_agent_provider,
    register_proxy_token,
    unregister_proxy_token,
)
from providers.agy import AgyAgent
from providers.claude import ClaudeAgent, _worktree_git_mount
from providers.planka import SESSION_TAG_RE


class TestProviderLoadingAndRegistry(unittest.TestCase):
    def test_load_agy_and_claude_providers(self):
        agy = load_agent_provider("agy", {"AGY_BIN": "/usr/bin/agy"})
        self.assertIsInstance(agy, AgyAgent)
        self.assertFalse(agy.needs_auth_proxy)

        claude_docker = load_agent_provider("claude", {"BRIDGE_CLAUDE_MODE": "docker"})
        self.assertIsInstance(claude_docker, ClaudeAgent)
        self.assertTrue(claude_docker.needs_auth_proxy)

        claude_host = load_agent_provider("claude", {"BRIDGE_CLAUDE_MODE": "host"})
        self.assertIsInstance(claude_host, ClaudeAgent)
        self.assertFalse(claude_host.needs_auth_proxy)

    def test_proxy_token_lifecycle(self):
        tok = "ephemeral-test-token-123"
        self.assertFalse(is_valid_proxy_token(tok))
        self.assertFalse(is_valid_proxy_token(""))
        register_proxy_token(tok, "task-42")
        self.assertTrue(is_valid_proxy_token(tok))
        unregister_proxy_token(tok)
        self.assertFalse(is_valid_proxy_token(tok))

    def test_secrets_stripped_from_agent_base_env(self):
        dirty_env = {
            "PATH": "/usr/bin",
            "PLANKA_API_KEY": "secret-planka",
            "PLANKA_BOT_API_KEY": "secret-bot",
            "WEBHOOK_SECRET": "secret-hook",
            "CLAUDE_OAUTH_TOKEN": "secret-oauth",
            "TEAMS_WEBHOOK_URL": "https://secret",
            "SAFE_VAR": "keep-me",
        }
        for name in ("agy", "claude"):
            agent = load_agent_provider(name, dirty_env)
            for secret_key in ("PLANKA_API_KEY", "PLANKA_BOT_API_KEY", "WEBHOOK_SECRET", "CLAUDE_OAUTH_TOKEN", "TEAMS_WEBHOOK_URL"):
                self.assertNotIn(secret_key, agent.base_env)
            self.assertEqual(agent.base_env.get("SAFE_VAR"), "keep-me")


class TestClaudeAgent(unittest.TestCase):
    def test_docker_cmd_uses_proxy_token_not_real_secret_and_mounts_azure(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cwd = tmp_path / "work"
            cwd.mkdir()
            azure_dir = tmp_path / "azure-copy"
            azure_dir.mkdir()
            state_root = tmp_path / "state-root"

            agent = ClaudeAgent(
                claude_bin="/usr/local/bin/claude",
                mode="docker",
                state_root=state_root,
                docker_net="host",
                docker_user="0:0",
                base_env={"CLAUDE_OAUTH_TOKEN": "real-token-must-never-leak"},
            )
            task = Task(id="99", title="Test card")
            cmd = agent._build_docker_cmd(
                task=task,
                session_id="11111111-2222-3333-4444-555555555555",
                prompt="Do the thing",
                system_instructions="Be concise.",
                resume=False,
                cwd=cwd,
                env_extra={
                    "GH_TOKEN": "gh-temp-tok",
                    "AZURE_CONFIG_DIR": str(azure_dir),
                    "CARGO_TARGET_DIR": "/host/target",
                },
                run_token="per-run-proxy-secret",
                tmp_dir=tmp_path,
                container_name="kanban-task-99-abcd",
            )

            self.assertIn("--net=host", cmd)
            self.assertIn("--user", cmd)
            self.assertIn("0:0", cmd)
            self.assertIn("--session-id", cmd)
            self.assertIn(f"{azure_dir}:/home/agent/.azure", cmd)
            self.assertIn(f"{cwd}:/work", cmd)

            env_text = (tmp_path / "env").read_text(encoding="utf-8")
            self.assertIn("CLAUDE_CODE_OAUTH_TOKEN=per-run-proxy-secret", env_text)
            self.assertIn("ANTHROPIC_BASE_URL=http://127.0.0.1:8788", env_text)
            self.assertIn("GH_TOKEN=gh-temp-tok", env_text)
            self.assertNotIn("real-token-must-never-leak", env_text)
            self.assertNotIn("CARGO_TARGET_DIR", env_text)

    def test_worktree_git_mount_includes_parent_git_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.name=test", "-c", "user.email=t@t", "commit", "--allow-empty", "-q", "-m", "init"],
                cwd=repo,
                check=True,
            )
            wt = tmp_path / "wt"
            subprocess.run(["git", "-C", str(repo), "worktree", "add", "-b", "feat/x", str(wt)], capture_output=True, check=True)

            workdir, mounts = _worktree_git_mount(wt)
            self.assertEqual(workdir, str(wt))
            self.assertIn(f"{wt}:{wt}", mounts)
            self.assertIn(f"{repo / '.git'}:{repo / '.git'}", mounts)

    def test_stream_json_parsing_and_tool_call_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            log_path = tmp_path / "sess-1.jsonl"
            events = [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"file_path": "/work/a.py"}},
                            {"type": "tool_use", "id": "tu_2", "name": "Bash", "input": {"command": "false"}},
                        ]
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {"type": "tool_result", "tool_use_id": "tu_2", "is_error": True},
                        ]
                    },
                },
                {"type": "result", "subtype": "success", "is_error": False, "result": "All done.\nSTATUS: DONE"},
            ]
            # Fake claude binary emitting stream-json lines via python3
            fake_cmd = [
                sys.executable,
                "-c",
                "import sys; print(" + repr("\n".join(json.dumps(e) for e in events)) + ")",
            ]
            agent = ClaudeAgent(claude_bin=sys.executable, mode="host", base_env={})
            res = agent._run_streaming(
                cmd=fake_cmd,
                cwd=tmp_path,
                env={},
                timeout=30,
                procs=set(),
                log_path=log_path,
                session_id="sess-1",
            )
            self.assertFalse(res.is_error)
            self.assertEqual(res.text, "All done.\nSTATUS: DONE")

            calls = agent.extract_tool_calls("sess-1", tmp_path)
            self.assertEqual(len(calls), 2)
            self.assertIn("- Read:", calls[0])
            self.assertIn("- Bash [ERROR/DENIED]:", calls[1])


class TestPlankaSessionTagCompat(unittest.TestCase):
    def test_session_tag_matches_agy_claude_and_kanban(self):
        for prefix in ("agy", "claude", "kanban"):
            m = SESSION_TAG_RE.search(f"Body text\n\n<!-- {prefix}-session:abc-123 -->")
            self.assertIsNotNone(m, f"failed for prefix {prefix}")
            self.assertEqual(m.group(1), "abc-123")


if __name__ == "__main__":
    unittest.main()
