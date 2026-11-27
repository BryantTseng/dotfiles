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
    Attachment,
    ModelUsage,
    Task,
    estimate_model_cost_usd,
    format_usage_summary,
    is_valid_proxy_token,
    load_agent_provider,
    register_proxy_token,
    unregister_proxy_token,
)
from providers.agy import AgyAgent
from providers.claude import ClaudeAgent, _worktree_git_mount
from providers.planka import PlankaBoard, SESSION_TAG_RE, _encode_multipart


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
        def _step(
            idx: int,
            name: str,
            state: str,
            params: dict | None = None,
            output: str = "",
            step_type: str = "tool",
            duration: float | None = None,
            subagent_info: dict | None = None,
        ) -> dict:
            su: dict = {
                "conversation_id": "conv-sub",
                "step_index": idx,
                "state": state,
                "step_type": step_type,
                "tool_name": name,
            }
            if params is not None or output:
                su["tool_info"] = {"name": name, "parameters": params or {}, "output": output}
            if duration is not None:
                su["duration_seconds"] = duration
            if subagent_info is not None:
                su["subagent_info"] = subagent_info
            return {"event": "step_update", "step_update": su}

        base_events = [
            {"event": "init", "conversation_id": "conv-sub"},
            # Real agy stream-json schema for invoke_subagent: step_type="subagent" + subagent_info
            _step(
                1,
                "invoke_subagent",
                "ACTIVE",
                step_type="subagent",
                subagent_info={
                    "subagents": [
                        {"type_name": "research", "role": "Codebase Researcher"},
                        {"type_name": "self", "role": "API Auditor"},
                    ]
                },
            ),
            _step(2, "swarm", "ACTIVE", {"action": "add", "name": "dev", "model": "medium", "n": 2}),
            _step(3, "swarm", "ACTIVE", {"action": "kill", "name": "dev-1"}),
            _step(4, "run_workflow", "ACTIVE", {"Description": "Fan out lint checks"}),
            _step(4, "run_workflow", "DONE", {"Description": "Fan out lint checks"}, output="All checks passed"),
            # Fast command (<2s): should NOT appear in checklist
            _step(5, "run_command", "ACTIVE", {"CommandLine": "git status"}),
            _step(5, "run_command", "DONE", {"CommandLine": "git status"}, output="Command exited with code 0.", duration=0.3),
            # Long/backgrounded command (>=2s, exit 0): appears and marks checked
            _step(6, "run_command", "ACTIVE", {"CommandLine": "sleep 2 && echo bg-ok"}),
            _step(6, "run_command", "DONE", {"CommandLine": "sleep 2 && echo bg-ok"}, output="Command exited with code 0.\nbg-ok", duration=4.28),
            # Failed long command (exit 101): stays unchecked even on SUCCESS
            _step(7, "run_command", "ACTIVE", {"CommandLine": "cargo test --broken"}),
            _step(7, "run_command", "DONE", {"CommandLine": "cargo test --broken"}, output="Command exited with code 101.", duration=12.5),
            # Explicit background task output
            _step(
                8,
                "run_command",
                "DONE",
                {"toolSummary": "Cargo test build", "CommandLine": "cargo test"},
                output="Tool is running as a background task (id: bg-1)",
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            agent = AgyAgent(agy_bin=sys.executable, base_env={})

            # 1) Clean SUCCESS auto-completes remaining non-failed subtasks
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
                    ("Subagents & Tasks", "[cmd] sleep 2 && echo bg-ok", True),
                    ("Subagents & Tasks", "[cmd] cargo test --broken", False),
                    ("Subagents & Tasks", "[cmd] Cargo test build", False),
                    ("Subagents & Tasks", "[subagent] Codebase Researcher", True),
                    ("Subagents & Tasks", "[subagent] API Auditor (self)", True),
                    ("Subagents & Tasks", "[swarm] dev-2 (medium)", True),
                    ("Subagents & Tasks", "[cmd] Cargo test build", True),
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
                    ("Subagents & Tasks", "[cmd] sleep 2 && echo bg-ok", True),
                    ("Subagents & Tasks", "[cmd] cargo test --broken", False),
                    ("Subagents & Tasks", "[cmd] Cargo test build", False),
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

    def test_extract_attachments_and_path_validation(self):
        import os
        os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
        import bridge

        raw = (
            "Here is the chart and example:\n\n"
            "```markdown\n"
            "ATTACH: example_inside_code_block.png\n"
            "```\n\n"
            "ATTACH: `out/diagram.png` | Architecture Diagram\n"
            "ATTACH: report.txt\n"
            "All done."
        )
        cleaned, specs = bridge.extract_attachments(raw)
        self.assertEqual(
            specs,
            [
                ("out/diagram.png", "Architecture Diagram"),
                ("report.txt", None),
            ],
        )
        self.assertIn("ATTACH: example_inside_code_block.png", cleaned)
        self.assertNotIn("ATTACH: report.txt", cleaned)
        self.assertIn("All done.", cleaned)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            cwd = root / "task"
            (cwd / "out").mkdir(parents=True)
            (cwd / ".git").mkdir()
            ok_file = cwd / "out" / "diagram.png"
            ok_file.write_bytes(b"\x89PNG\r\n\x1a\nfake")
            env_file = cwd / ".env"
            env_file.write_text("SECRET=1", encoding="utf-8")
            pem_file = cwd / "private.pem"
            pem_file.write_text("key", encoding="utf-8")
            git_file = cwd / ".git" / "config"
            git_file.write_text("git", encoding="utf-8")
            outside = root / "outside.txt"
            outside.write_text("nope", encoding="utf-8")

            res, err = bridge.resolve_attachable_path("out/diagram.png", cwd)
            self.assertIsNone(err)
            self.assertEqual(res, ok_file)

            res, err = bridge.resolve_attachable_path(f"file://{ok_file}", cwd)
            self.assertIsNone(err)
            self.assertEqual(res, ok_file)

            for bad in ("../outside.txt", ".env", "private.pem", ".git/config", "missing.png"):
                res, err = bridge.resolve_attachable_path(bad, cwd)
                self.assertIsNone(res, f"should reject {bad}")
                self.assertIsNotNone(err)

        body, ctype = _encode_multipart(
            fields=[("type", "file"), ("name", "chart.png")],
            file_field="file",
            filename="chart.png",
            content_type="image/png",
            data=b"\x89PNGDATA",
        )
        self.assertIn("multipart/form-data; boundary=", ctype)
        # Sails/Skipper requires text fields before file payload
        self.assertLess(body.index(b'name="type"'), body.index(b'name="file"'))
        self.assertLess(body.index(b'name="name"'), body.index(b'name="file"'))
        self.assertIn(b"\x89PNGDATA", body)


class TestPlankaSessionTagCompat(unittest.TestCase):
    def test_session_tag_matches_agy_claude_and_kanban(self):
        for prefix in ("agy", "claude", "kanban"):
            m = SESSION_TAG_RE.search(f"Body text\n\n<!-- {prefix}-session:abc-123 -->")
            self.assertIsNotNone(m, f"failed for prefix {prefix}")
            self.assertEqual(m.group(1), "abc-123")

    def test_bot_api_key_used_for_moves_checklists_and_ignores_self_echo(self):
        board = PlankaBoard(
            base_url="http://127.0.0.1:3000",
            api_key="admin-key",
            webhook_url="http://127.0.0.1:8787/planka",
            webhook_secret="sec",
            bot_api_key="bot-key",
            bot_username="agy",
        )
        calls: list[tuple[str, str, str | None]] = []

        def fake_api(method: str, path: str, payload=None, *, api_key=None):
            calls.append((method, path, api_key))
            if path == "/boards/b1":
                return {"included": {"lists": [{"id": "l2", "name": "Pending", "type": "active"}]}}
            if path == "/cards/c1" and method == "GET":
                return {"item": {"id": "c1", "boardId": "b1", "description": "hi"}, "included": {"taskLists": [], "tasks": []}}
            if path == "/cards/c1/task-lists":
                return {"item": {"id": "tl1", "name": "Subagents & Tasks"}}
            if path == "/task-lists/tl1/tasks":
                return {"item": {"id": "t1", "name": "[subagent] x", "isCompleted": False}}
            return {"item": {}}

        board._api = fake_api  # type: ignore[method-assign]
        t = Task(id="c1", title="Card", project_id="b1")
        self.assertTrue(board.move_to(t, "Pending"))
        board.sync_checklist("c1", "Subagents & Tasks", [("[subagent] x", False)])
        board.attach_session_label("c1", "sess-1", "session:", "grey")

        mut_keys = {k for m, _, k in calls if m in ("POST", "PATCH", "DELETE")}
        self.assertEqual(mut_keys, {"bot-key"})

        # Bot self-echo cardUpdate is ignored; human cardUpdate is processed
        bot_ev = board.parse_webhook(
            {"authorization": "Bearer sec"},
            json.dumps({"event": "cardUpdate", "data": {"item": {"id": "c1"}}, "user": {"username": "agy"}}).encode(),
        )
        self.assertIsNone(bot_ev)
        human_ev = board.parse_webhook(
            {"authorization": "Bearer sec"},
            json.dumps({"event": "cardUpdate", "data": {"item": {"id": "c1"}}, "user": {"username": "bryant"}}).encode(),
        )
        self.assertIsNotNone(human_ev)
        assert human_ev is not None
        self.assertEqual(human_ev.task_id, "c1")

    def test_planka_upload_attachment_url_and_headers(self):
        import io
        from unittest import mock

        board = PlankaBoard(
            base_url="http://127.0.0.1:3000",
            api_key="admin-key",
            webhook_url="http://127.0.0.1:8787/planka",
            webhook_secret="sec",
            bot_api_key="bot-key",
            bot_username="agy",
        )
        seen_req = []

        class _FakeResp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=60):
            seen_req.append(req)
            return _FakeResp(
                json.dumps(
                    {
                        "item": {
                            "id": "att1",
                            "name": "Badge",
                            "type": "file",
                            "data": {"url": "http://localhost:3000/attachments/att1/download/badge.png", "mimeType": "image/png", "sizeInBytes": 8, "image": {"width": 1, "height": 1}},
                        }
                    }
                ).encode()
            )

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "badge.png"
            p.write_bytes(b"\x89PNGDATA")
            with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                att = board.upload_attachment("c1", p, name="Badge")
            self.assertIsNotNone(att)
            assert att is not None
            self.assertEqual(att.id, "att1")
            self.assertTrue(att.is_image)
            self.assertEqual(seen_req[0].full_url, "http://127.0.0.1:3000/api/cards/c1/attachments")
            self.assertEqual(seen_req[0].headers.get("X-api-key"), "bot-key")

    def test_provider_specific_session_marker(self):
        board = PlankaBoard(
            base_url="http://127.0.0.1:3000",
            api_key="admin-key",
            webhook_url="http://127.0.0.1:8787/planka",
            webhook_secret="sec",
            bot_api_key="bot-key",
            bot_username="claude",
        )
        patched_descs: list[str] = []

        def fake_api(method: str, path: str, payload=None, *, api_key=None):
            if method == "GET":
                return {"item": {"id": "c1", "description": "Card body"}}
            if method == "PATCH":
                patched_descs.append(payload["description"])
                return {"item": {}}
            return {}

        board._api = fake_api  # type: ignore[method-assign]
        board.attach_session_label("c1", "sess-claude", "session:", "grey", provider="claude")
        board.attach_session_label("c1", "sess-agy", "session:", "grey", provider="agy")
        self.assertEqual(patched_descs[0], "Card body\n\n<!-- claude-session:sess-claude -->")
        self.assertEqual(patched_descs[1], "Card body\n\n<!-- agy-session:sess-agy -->")


class TestCrossPlatformWorkdirBacklogAndSubagentAudits(unittest.TestCase):
    def test_agent_workdir_for_agy_and_claude_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            scratch = root / "scratch"
            scratch.mkdir()

            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-q", "-m", "init"],
                cwd=repo,
                check=True,
            )
            wt = root / "wt"
            subprocess.run(["git", "-C", str(repo), "worktree", "add", "-b", "feat/wt", str(wt)], capture_output=True, check=True)

            agy = AgyAgent(agy_bin=sys.executable, base_env={})
            self.assertEqual(agy.agent_workdir(scratch), scratch)

            claude_host = ClaudeAgent(claude_bin=sys.executable, mode="host", base_env={})
            self.assertEqual(claude_host.agent_workdir(scratch), scratch)

            claude_docker = ClaudeAgent(claude_bin=sys.executable, mode="docker", base_env={})
            self.assertEqual(claude_docker.agent_workdir(scratch), Path("/work"))
            self.assertEqual(claude_docker.agent_workdir(wt), wt)

    def test_docker_workdir_mapping_for_inbound_and_outbound_attachments(self):
        import os

        os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
        import bridge

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            cwd = root / "task"
            (cwd / "out").mkdir(parents=True)
            (cwd / ".kanban-attachments").mkdir(parents=True)
            chart = cwd / "out" / "chart.png"
            chart.write_bytes(b"\x89PNGDATA")
            inbound = cwd / ".kanban-attachments" / "123456-spec.png"
            inbound.write_bytes(b"\x89PNGIN")
            outside = root / "secret.txt"
            outside.write_text("nope", encoding="utf-8")

            # Container path `/work/out/chart.png` maps cleanly to host `cwd/out/chart.png`
            res, err = bridge.resolve_attachable_path("/work/out/chart.png", cwd, agent_cwd=Path("/work"))
            self.assertIsNone(err)
            self.assertEqual(res, chart)

            # Traversal climbing out of `/work` is still rejected
            res, err = bridge.resolve_attachable_path("/work/../secret.txt", cwd, agent_cwd=Path("/work"))
            self.assertIsNone(res)
            self.assertIsNotNone(err)

            # Inbound prompt block renders `/work/.kanban-attachments/...` in Docker mode
            block = bridge.format_inbound_attachments(
                [Attachment(id="1", name="spec.png", kind="file", is_image=True, size_bytes=6, local_path=inbound)],
                host_cwd=cwd,
                agent_cwd=Path("/work"),
            )
            self.assertIn("`/work/.kanban-attachments/123456-spec.png`", block)

    def test_backlog_parking_ignores_cards_in_backlog(self):
        import os
        from unittest import mock

        os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
        import bridge

        t = Task(id="7", title="Draft", project_id="b1")
        fake_backlog = mock.Mock(current_bucket=mock.Mock(return_value="backlog"))
        with mock.patch.object(bridge, "BOARD", fake_backlog, create=True):
            self.assertTrue(bridge.is_parked_in_backlog(t))
            self.assertFalse(bridge.needs_work(t))
        fake_todo = mock.Mock(current_bucket=mock.Mock(return_value="to-do"))
        with mock.patch.object(bridge, "BOARD", fake_todo, create=True):
            self.assertFalse(bridge.is_parked_in_backlog(t))

    def test_claude_subagent_audit_extraction_separates_parent_and_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            events = [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "id": "tu_parent", "name": "Read", "input": {"file_path": "/work/a.py"}},
                            {
                                "type": "tool_use",
                                "id": "tu_sub",
                                "name": "Agent",
                                "input": {
                                    "description": "Audit deps",
                                    "subagent_type": "Explore",
                                    "prompt": "Check Cargo.toml for risky deps",
                                },
                            },
                        ]
                    },
                },
                {
                    "type": "assistant",
                    "parent_tool_use_id": "tu_sub",
                    "message": {
                        "content": [
                            {"type": "tool_use", "id": "tu_child_1", "name": "Bash", "input": {"command": "cargo tree"}},
                        ]
                    },
                },
                {
                    "type": "user",
                    "parent_tool_use_id": "tu_sub",
                    "message": {
                        "content": [
                            {"type": "tool_result", "tool_use_id": "tu_child_1", "is_error": True},
                        ]
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tu_sub",
                                "content": [{"type": "text", "text": "No risky deps found."}],
                            }
                        ]
                    },
                },
            ]
            (log_dir / "sess-sub.jsonl").write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")

            agent = ClaudeAgent(claude_bin=sys.executable, mode="host", base_env={}, state_root=log_dir / "state")
            parent_calls = agent.extract_tool_calls("sess-sub", log_dir)
            self.assertEqual(len(parent_calls), 2)
            self.assertFalse(any("cargo tree" in c for c in parent_calls))

            subs = agent.extract_subagent_audits("sess-sub", log_dir)
            self.assertEqual(len(subs), 1)
            self.assertEqual(subs[0].name, "Audit deps (Explore)")
            self.assertEqual(subs[0].prompt, "Check Cargo.toml for risky deps")
            self.assertEqual(len(subs[0].tool_calls), 1)
            self.assertIn("- Bash [ERROR/DENIED]:", subs[0].tool_calls[0])
            self.assertEqual(subs[0].final_report, "No risky deps found.")

    def test_agy_subagent_audit_extraction_reads_child_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child_log = root / "child-transcript.jsonl"
            child_steps = [
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "tool_calls": [{"name": "run_command", "args": {"CommandLine": "ls -la"}}],
                },
                {"step_index": 2, "type": "GENERIC", "status": "DONE", "content": "ok"},
                {
                    "step_index": 3,
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "tool_calls": [
                        {"name": "send_message", "args": {"Message": '"Only .git exists."', "Recipient": '"parent"'}}
                    ],
                },
            ]
            child_log.write_text("\n".join(json.dumps(s) for s in child_steps), encoding="utf-8")

            parent_events = [
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": "parent-1",
                        "step_index": 2,
                        "state": "DONE",
                        "step_type": "subagent",
                        "tool_name": "invoke_subagent",
                        "subagent_info": {
                            "subagents": [
                                {
                                    "type_name": "research",
                                    "role": "Smoke Test Researcher",
                                    "initial_prompt": "Inspect scratch dir",
                                    "conversation_id": "child-1",
                                    "log_uri": f"file://{child_log}",
                                }
                            ]
                        },
                    },
                }
            ]
            (root / "parent-1.jsonl").write_text("\n".join(json.dumps(e) for e in parent_events), encoding="utf-8")

            agent = AgyAgent(agy_bin=sys.executable, base_env={})
            subs = agent.extract_subagent_audits("parent-1", root)
            self.assertEqual(len(subs), 1)
            self.assertEqual(subs[0].name, "Smoke Test Researcher")
            self.assertEqual(subs[0].prompt, "Inspect scratch dir")
            self.assertEqual(len(subs[0].tool_calls), 2)
            self.assertIn("- run_command:", subs[0].tool_calls[0])
            self.assertEqual(subs[0].final_report, "Only .git exists.")

    def test_claude_model_detection_and_reported_cost_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            events = [
                {
                    "type": "assistant",
                    "message": {
                        "id": "m1",
                        "model": "claude-sonnet-4-6",
                        "content": [{"type": "text", "text": "Working"}],
                        "usage": {"input_tokens": 1000, "output_tokens": 200},
                    },
                },
                {
                    "type": "assistant",
                    "parent_tool_use_id": "tu_sub",
                    "message": {
                        "id": "m2",
                        "model": "claude-haiku-4-5-20251001",
                        "content": [{"type": "text", "text": "Subagent"}],
                        "usage": {"input_tokens": 500, "output_tokens": 100},
                    },
                },
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "Done.\nSTATUS: DONE",
                    "total_cost_usd": 0.0425,
                    "modelUsage": {
                        "claude-sonnet-4-6": {
                            "inputTokens": 1000,
                            "outputTokens": 200,
                            "cacheReadInputTokens": 4000,
                            "costUSD": 0.04,
                        },
                        "claude-haiku-4-5-20251001": {
                            "inputTokens": 500,
                            "outputTokens": 100,
                            "costUSD": 0.0025,
                        },
                    },
                },
            ]
            fake_cmd = [sys.executable, "-c", "print(" + repr("\n".join(json.dumps(e) for e in events)) + ")"]
            agent = ClaudeAgent(claude_bin=sys.executable, mode="host", base_env={}, state_root=log_dir / "state")
            res = agent._run_streaming(
                cmd=fake_cmd,
                cwd=log_dir,
                env={},
                timeout=30,
                procs=set(),
                log_path=log_dir / "s-cost.jsonl",
                session_id="s-cost",
            )
            # Parent model is sonnet (not overridden by subagent haiku)
            self.assertEqual(res.model, "claude-sonnet-4-6")

            summary = agent.extract_usage_summary("s-cost", log_dir)
            self.assertAlmostEqual(summary.reported_cost_usd or 0.0, 0.0425, places=6)
            md = format_usage_summary(summary)
            self.assertIn("💰 **Estimated Cost:** **$0.0425**", md)
            self.assertIn("`claude-sonnet-4-6`", md)
            self.assertIn("reported by Claude Code", md)

    def test_agy_model_detection_and_usage_cost_estimation_with_subagents(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent_transcript = root / "parent-transcript.jsonl"
            parent_transcript.write_text(
                "\n".join(
                    json.dumps(x)
                    for x in [
                        {
                            "type": "PLANNER_RESPONSE",
                            "model": "gemini-4-hydrogen-medium",
                            "input_tokens": 10000,
                            "cache_read_tokens": 20000,
                            "output_tokens": 1000,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            child_transcript = root / "child-transcript.jsonl"
            child_transcript.write_text(
                "\n".join(
                    json.dumps(x)
                    for x in [
                        {
                            "type": "PLANNER_RESPONSE",
                            "model": "gemini-3.5-flash-lite",
                            "input_tokens": 5000,
                            "cache_read_tokens": 0,
                            "output_tokens": 500,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            parent_log = root / "agy-s1.jsonl"
            parent_log.write_text(
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "step_type": "subagent",
                            "step_index": 2,
                            "state": "DONE",
                            "subagent_info": {
                                "subagents": [
                                    {"role": "Researcher", "conversation_id": "c1", "log_uri": f"file://{child_transcript}"}
                                ]
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            agent = AgyAgent(
                agy_bin=sys.executable,
                base_env={
                    "BRIDGE_STATE_ROOT": str(root),
                    "BRIDGE_PRICING_URL": "",
                    "BRIDGE_MODEL_PRICING": json.dumps(
                        {
                            "gemini-4-hydrogen": [1.50, 7.50, 0.075, 0.0],
                            "gemini-3.5-flash-lite": [0.30, 2.50, 0.03, 0.0],
                        }
                    ),
                },
            )
            with mock.patch.object(AgyAgent, "_transcript_path", return_value=parent_transcript):
                self.assertEqual(agent._detect_model("agy-s1"), "gemini-4-hydrogen-medium")
                summary = agent.extract_usage_summary("agy-s1", root)

            self.assertEqual([u.model for u in summary.models], ["gemini-4-hydrogen-medium"])
            self.assertEqual([u.model for u in summary.subagent_models], ["gemini-3.5-flash-lite"])
            self.assertIsNotNone(summary.estimated_cost_usd)
            assert summary.estimated_cost_usd is not None
            # 10k*1.5/1M + 20k*0.075/1M + 1k*7.5/1M + 5k*0.3/1M + 500*2.5/1M = 0.015 + 0.0015 + 0.0075 + 0.0015 + 0.00125 = 0.02675
            self.assertAlmostEqual(summary.estimated_cost_usd, 0.02675, places=6)
            md = format_usage_summary(summary, env=agent.base_env)
            self.assertIn("💰 **Estimated Cost:** **~$0.0268**", md)
            self.assertIn("`gemini-4-hydrogen-medium`", md)
            self.assertIn("subagent `gemini-3.5-flash-lite`", md)

    def test_openrouter_pricing_6hr_cache_and_slug_matching(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            env = {"BRIDGE_STATE_ROOT": str(state_root)}
            fetch_count = 0
            sample_payload = {
                "data": [
                    {
                        "id": "google/gemini-4-hydrogen",
                        "pricing": {"prompt": "0.0000015", "completion": "0.0000075", "input_cache_read": "0.000000075"},
                    },
                    {
                        "id": "anthropic/claude-sonnet-4.6:batch",
                        "pricing": {"prompt": "0.0000015", "completion": "0.0000075"},
                    },
                    {
                        "id": "anthropic/claude-sonnet-4.6",
                        "pricing": {"prompt": "0.000003", "completion": "0.000015", "input_cache_read": "0.0000003"},
                    },
                    {
                        "id": "anthropic/claude-haiku-4.5",
                        "pricing": {"prompt": "0.000001", "completion": "0.000005", "input_cache_read": "0.0000001"},
                    },
                ]
            }

            def fake_fetch(url: str) -> dict:
                nonlocal fetch_count
                fetch_count += 1
                return sample_payload

            # First call fetches & writes disk cache
            c1 = estimate_model_cost_usd(
                ModelUsage(model="gemini-4-hydrogen-medium", input_tokens=1_000_000, output_tokens=1_000_000),
                env=env,
                now=1000.0,
                fetcher=fake_fetch,
            )
            self.assertAlmostEqual(c1 or 0.0, 9.0, places=6)
            self.assertEqual(fetch_count, 1)

            # Second call within 6 hours hits disk cache (no refetch) and matches dot/hyphen & date suffix
            c2 = estimate_model_cost_usd(
                ModelUsage(model="claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=0),
                env=env,
                now=1000.0 + 3600.0,
                fetcher=fake_fetch,
            )
            self.assertAlmostEqual(c2 or 0.0, 3.0, places=6)  # NOT 1.5 (:batch ignored!)
            self.assertEqual(fetch_count, 1)

            c3 = estimate_model_cost_usd(
                ModelUsage(model="claude-haiku-4-5-20251001", input_tokens=1_000_000, output_tokens=0),
                env=env,
                now=1000.0 + 3600.0,
                fetcher=fake_fetch,
            )
            self.assertAlmostEqual(c3 or 0.0, 1.0, places=6)
            self.assertEqual(fetch_count, 1)

            # After 6 hours (21600s), cache expires and refetches
            c4 = estimate_model_cost_usd(
                ModelUsage(model="claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=0),
                env=env,
                now=1000.0 + 6 * 3600 + 5,
                fetcher=fake_fetch,
            )
            self.assertAlmostEqual(c4 or 0.0, 3.0, places=6)
            self.assertEqual(fetch_count, 2)

            # If refetch fails later, stale cache is still used gracefully
            def fail_fetch(url: str) -> dict:
                raise RuntimeError("offline")

            c5 = estimate_model_cost_usd(
                ModelUsage(model="claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=0),
                env=env,
                now=1000.0 + 20 * 3600,
                fetcher=fail_fetch,
            )
            self.assertAlmostEqual(c5 or 0.0, 3.0, places=6)

    def test_agy_swarm_worker_cost_rollup_from_transcript(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            coord_t = root / "coord.jsonl"
            worker_t = root / "worker.jsonl"
            worker_t.write_text(
                json.dumps(
                    {
                        "type": "PLANNER_RESPONSE",
                        "model": "gemini-4-hydrogen-medium",
                        "input_tokens": 20000,
                        "cache_read_tokens": 40000,
                        "output_tokens": 2000,
                    }
                ),
                encoding="utf-8",
            )
            coord_t.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "PLANNER_RESPONSE",
                                "model": "gemini-4-neon-high",
                                "input_tokens": 10000,
                                "cache_read_tokens": 0,
                                "output_tokens": 500,
                            }
                        ),
                        json.dumps(
                            {
                                "type": "GENERIC",
                                "content": 'Created At: ...\n{"status":"added","name":"dev","model":"gemini-4-hydrogen-medium","conversation_id":"worker-cid-1"}',
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )

            def fake_tpath(cid):
                return {"coord-cid": coord_t, "worker-cid-1": worker_t}.get(cid)

            agent = AgyAgent(
                agy_bin=sys.executable,
                base_env={
                    "BRIDGE_STATE_ROOT": str(root),
                    "BRIDGE_PRICING_URL": "",
                    "BRIDGE_MODEL_PRICING": json.dumps(
                        {
                            "gemini-4-neon": [3.0, 15.0, 0.15, 0.0],
                            "gemini-4-hydrogen": [1.5, 7.5, 0.075, 0.0],
                        }
                    ),
                },
            )
            with mock.patch.object(AgyAgent, "_transcript_path", side_effect=fake_tpath):
                summary = agent.extract_usage_summary("coord-cid", root)

            self.assertEqual([u.model for u in summary.models], ["gemini-4-neon-high"])
            self.assertEqual([u.model for u in summary.subagent_models], ["gemini-4-hydrogen-medium"])
            self.assertEqual(summary.subagent_models[0].input_tokens, 20000)
            self.assertEqual(summary.subagent_models[0].cache_read_tokens, 40000)
            self.assertIsNotNone(summary.estimated_cost_usd)
            # coord: 10k*3/1M + 500*15/1M = 0.0375; worker: 20k*1.5/1M + 40k*0.075/1M + 2k*7.5/1M = 0.048 => 0.0855
            self.assertAlmostEqual(summary.estimated_cost_usd or 0.0, 0.0855, places=6)


if __name__ == "__main__":
    unittest.main()
