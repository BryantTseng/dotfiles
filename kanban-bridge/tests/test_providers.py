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

    def test_live_subtask_sync_for_agents_and_todowrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            events = [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tu_agent_ok",
                                "name": "Agent",
                                "input": {"description": "Check auth flow", "subagent_type": "Explore"},
                            },
                            {
                                "type": "tool_use",
                                "id": "tu_agent_err",
                                "name": "Task",
                                "input": {"description": "Explore flaky check", "subagent_type": "Explore"},
                            },
                            {
                                "type": "tool_use",
                                "id": "tu_todo_1",
                                "name": "TodoWrite",
                                "input": {
                                    "todos": [
                                        {"content": "Write tests", "status": "in_progress"},
                                        {"content": "Update docs", "status": "pending"},
                                    ]
                                },
                            },
                        ]
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {"type": "tool_result", "tool_use_id": "tu_agent_ok", "is_error": False},
                            {"type": "tool_result", "tool_use_id": "tu_agent_err", "is_error": True},
                        ]
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tu_todo_2",
                                "name": "TodoWrite",
                                "input": {
                                    "todos": [
                                        {"content": "Write tests", "status": "completed"},
                                        {"content": "Update docs", "status": "pending"},
                                    ]
                                },
                            }
                        ]
                    },
                },
                {"type": "result", "subtype": "success", "is_error": False, "result": "Done.\nSTATUS: DONE"},
            ]
            fake_cmd = [
                sys.executable,
                "-c",
                "import sys; print(" + repr("\n".join(json.dumps(e) for e in events)) + ")",
            ]
            captured: list[tuple[str, str, bool]] = []
            agent = ClaudeAgent(claude_bin=sys.executable, mode="host", base_env={})
            res = agent._run_streaming(
                cmd=fake_cmd,
                cwd=tmp_path,
                env={},
                timeout=30,
                procs=set(),
                log_path=tmp_path / "s.jsonl",
                session_id="s",
                on_subtask=lambda cl, text, done: captured.append((cl, text, done)),
            )
            self.assertFalse(res.is_error)
            self.assertEqual(
                captured,
                [
                    ("Subagents & Tasks", "[subagent] Check auth flow (Explore)", False),
                    ("Subagents & Tasks", "[subagent] Explore flaky check", False),
                    ("Subagents & Tasks", "Write tests", False),
                    ("Subagents & Tasks", "Update docs", False),
                    ("Subagents & Tasks", "[subagent] Check auth flow (Explore)", True),
                    ("Subagents & Tasks", "Write tests", True),
                ],
            )


class TestAgyFinalResponseAndBridgeParsers(unittest.TestCase):
    def test_agy_strips_intermediate_background_wait_chatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            events = [
                {"event": "init", "conversation_id": "conv-abc"},
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": "conv-abc",
                        "step_index": 10,
                        "state": "DONE",
                        "step_type": "agent_response",
                        "text_delta": "Running `cargo test` to compile; waiting for build to finish.\n",
                    },
                },
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": "conv-abc",
                        "step_index": 71,
                        "state": "ACTIVE",
                        "step_type": "agent_response",
                        "text_delta": "Implemented the Rust notifier.\n",
                    },
                },
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": "conv-abc",
                        "step_index": 71,
                        "state": "DONE",
                        "step_type": "agent_response",
                        "text_delta": "STATUS: DONE\n",
                    },
                },
                {
                    "event": "result",
                    "result": {
                        "conversation_id": "conv-abc",
                        "status": "SUCCESS",
                        "response": "Running `cargo test` to compile; waiting for build to finish.\nImplemented the Rust notifier.\nSTATUS: DONE\n",
                    },
                },
            ]
            fake_cmd = [
                sys.executable,
                "-c",
                "import sys; print(" + repr("\n".join(json.dumps(e) for e in events)) + ")",
            ]
            agent = AgyAgent(agy_bin=sys.executable, base_env={})
            res = agent._run_streaming(
                cmd=fake_cmd,
                cwd=tmp_path,
                env={},
                timeout=30,
                procs=set(),
                log_dir=tmp_path,
            )
            self.assertFalse(res.is_error)
            self.assertEqual(res.session_id, "conv-abc")
            self.assertNotIn("waiting for build to finish", res.text)
            self.assertEqual(res.text, "Implemented the Rust notifier.\nSTATUS: DONE")

    def test_agy_live_subtask_sync_on_success_and_error(self):
        def _step(idx: int, name: str, state: str, params: dict, output: str = "") -> dict:
            return {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "conv-sub",
                    "step_index": idx,
                    "state": state,
                    "step_type": "tool",
                    "tool_name": name,
                    "tool_info": {"name": name, "parameters": params, "output": output},
                },
            }

        base_events = [
            {"event": "init", "conversation_id": "conv-sub"},
            _step(1, "invoke_subagent", "ACTIVE", {"Role": "Codebase Researcher", "TypeName": "research"}),
            _step(2, "invoke_subagent", "ACTIVE", {"Role": "API Auditor", "TypeName": "self"}),
            _step(3, "swarm", "ACTIVE", {"action": "add", "name": "dev", "model": "medium", "n": 2}),
            _step(4, "swarm", "ACTIVE", {"action": "kill", "name": "dev-1"}),
            _step(5, "run_workflow", "ACTIVE", {"Description": "Fan out lint checks"}),
            _step(5, "run_workflow", "DONE", {"Description": "Fan out lint checks"}, output="All checks passed"),
            _step(
                6,
                "run_command",
                "DONE",
                {"toolSummary": "Cargo test build", "CommandLine": "cargo test"},
                output="Tool is running as a background task (id: bg-1)",
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            agent = AgyAgent(agy_bin=sys.executable, base_env={})

            # 1) Clean SUCCESS auto-completes remaining open subtasks
            ok_events = base_events + [
                {"event": "result", "result": {"conversation_id": "conv-sub", "status": "SUCCESS", "response": "Done"}}
            ]
            ok_cmd = [sys.executable, "-c", "print(" + repr("\n".join(json.dumps(e) for e in ok_events)) + ")"]
            ok_captured: list[tuple[str, str, bool]] = []
            res_ok = agent._run_streaming(
                cmd=ok_cmd,
                cwd=tmp_path,
                env={},
                timeout=30,
                procs=set(),
                log_dir=tmp_path,
                on_subtask=lambda cl, text, done: ok_captured.append((cl, text, done)),
            )
            self.assertFalse(res_ok.is_error)
            self.assertEqual(
                ok_captured,
                [
                    ("Subagents & Tasks", "[subagent] Codebase Researcher", False),
                    ("Subagents & Tasks", "[subagent] API Auditor (self)", False),
                    ("Subagents & Tasks", "[swarm] dev-1 (medium)", False),
                    ("Subagents & Tasks", "[swarm] dev-2 (medium)", False),
                    ("Subagents & Tasks", "[swarm] dev-1 (medium)", True),
                    ("Subagents & Tasks", "[workflow] Fan out lint checks", False),
                    ("Subagents & Tasks", "[workflow] Fan out lint checks", True),
                    ("Subagents & Tasks", "[bg] Cargo test build", False),
                    ("Subagents & Tasks", "[subagent] Codebase Researcher", True),
                    ("Subagents & Tasks", "[subagent] API Auditor (self)", True),
                    ("Subagents & Tasks", "[swarm] dev-2 (medium)", True),
                    ("Subagents & Tasks", "[bg] Cargo test build", True),
                ],
            )

            # 2) Non-SUCCESS status leaves still-open subtasks unchecked
            err_events = base_events + [
                {"event": "result", "result": {"conversation_id": "conv-sub", "status": "ERROR", "error": "boom"}}
            ]
            err_cmd = [sys.executable, "-c", "print(" + repr("\n".join(json.dumps(e) for e in err_events)) + ")"]
            err_captured: list[tuple[str, str, bool]] = []
            res_err = agent._run_streaming(
                cmd=err_cmd,
                cwd=tmp_path,
                env={},
                timeout=30,
                procs=set(),
                log_dir=tmp_path,
                on_subtask=lambda cl, text, done: err_captured.append((cl, text, done)),
            )
            self.assertTrue(res_err.is_error)
            self.assertEqual(
                err_captured,
                [
                    ("Subagents & Tasks", "[subagent] Codebase Researcher", False),
                    ("Subagents & Tasks", "[subagent] API Auditor (self)", False),
                    ("Subagents & Tasks", "[swarm] dev-1 (medium)", False),
                    ("Subagents & Tasks", "[swarm] dev-2 (medium)", False),
                    ("Subagents & Tasks", "[swarm] dev-1 (medium)", True),
                    ("Subagents & Tasks", "[workflow] Fan out lint checks", False),
                    ("Subagents & Tasks", "[workflow] Fan out lint checks", True),
                    ("Subagents & Tasks", "[bg] Cargo test build", False),
                ],
            )

    def test_extract_checklists_and_done_command_regex(self):
        import os
        os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
        import bridge

        raw = (
            "Summary of work.\n\n"
            "CHECKLIST: Implementation\n"
            "- [x] Build Rust notifier\n"
            "- [X] Pass unit tests\n"
            "- [ ] Run docker compose with Discord token\n\n"
            "All ready for your review."
        )
        cleaned, cls = bridge.extract_checklists(raw)
        self.assertEqual(len(cls), 1)
        self.assertEqual(cls[0][0], "Implementation")
        self.assertEqual(
            cls[0][1],
            [
                ("Build Rust notifier", True),
                ("Pass unit tests", True),
                ("Run docker compose with Discord token", False),
            ],
        )
        self.assertNotIn("CHECKLIST:", cleaned)
        self.assertIn("Summary of work.", cleaned)
        self.assertIn("All ready for your review.", cleaned)

        for ok_msg in ("done", "Done.", "mark done", "move to done", "please mark this card as done!", "close"):
            self.assertIsNotNone(bridge.DONE_CMD_RE.match(ok_msg), f"should match: {ok_msg!r}")
        for no_msg in ("done with step 1, now add tests", "not done yet", "close the modal after click"):
            self.assertIsNone(bridge.DONE_CMD_RE.match(no_msg), f"should NOT match: {no_msg!r}")


class TestPlankaSessionTagCompat(unittest.TestCase):
    def test_session_tag_matches_agy_claude_and_kanban(self):
        for prefix in ("agy", "claude", "kanban"):
            m = SESSION_TAG_RE.search(f"Body text\n\n<!-- {prefix}-session:abc-123 -->")
            self.assertIsNotNone(m, f"failed for prefix {prefix}")
            self.assertEqual(m.group(1), "abc-123")


if __name__ == "__main__":
    unittest.main()
