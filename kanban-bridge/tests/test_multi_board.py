"""Tests for multi-board routing, board-aware intake, and dashboard overview."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("WEBHOOK_SECRET", "test-webhook-secret")

import bridge  # noqa: E402
from providers import Label, Task  # noqa: E402
from providers.claude import _worktree_git_mount  # noqa: E402
from providers.planka import PlankaBoard  # noqa: E402


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@local"], check=True, capture_output=True)
    (path / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True)


class TestMultiBoardRouting(unittest.TestCase):
    def setUp(self):
        self.repos = {"clerk": Path("/tmp/repos/clerk"), "dotfiles": Path("/tmp/repos/dotfiles")}
        self.workdirs = {k: [(k, v)] for k, v in self.repos.items()}
        self.mock_board = SimpleNamespace(
            current_bucket=lambda t: "to-do",
            list_boards=lambda: [],
        )
        self.mock_agent = SimpleNamespace(name="agy")

    def test_repo_board_infers_worktree_and_skips_workspace_intake(self):
        task = Task(
            id="c1",
            title="Fix bug",
            project_name="clerk",
            labels=[Label(id="1", title="agy"), Label(id="2", title="direct")],
        )
        with (
            mock.patch.object(bridge, "discover_repos", return_value=self.repos),
            mock.patch.object(bridge, "discover_workdirs", return_value=self.workdirs),
            mock.patch.object(bridge, "BOARD", self.mock_board, create=True),
            mock.patch.object(bridge, "AGENT", self.mock_agent, create=True),
        ):
            self.assertEqual(bridge.workspace_mode_of(task), "worktree")
            rname, rpath = bridge.resolve_repo(task)
            self.assertEqual(rname, "clerk")
            self.assertEqual(rpath, self.repos["clerk"])
            self.assertFalse(bridge.needs_intake(task))

    def test_general_board_infers_scratch(self):
        task = Task(
            id="c2",
            title="Quick note about clerk",
            project_name="General",
            labels=[Label(id="1", title="agy"), Label(id="2", title="plan-first"), Label(id="3", title="repo:clerk")],
        )
        with (
            mock.patch.object(bridge, "discover_repos", return_value=self.repos),
            mock.patch.object(bridge, "discover_workdirs", return_value=self.workdirs),
            mock.patch.object(bridge, "BOARD", self.mock_board, create=True),
            mock.patch.object(bridge, "AGENT", self.mock_agent, create=True),
        ):
            self.assertEqual(bridge.workspace_mode_of(task), "scratch")
            self.assertEqual(bridge.resolve_workdir(task), (None, []))
            self.assertFalse(bridge.needs_intake(task))

    def test_explicit_labels_override_board_inference(self):
        task_scratch_on_repo = Task(
            id="c3",
            title="Scratch on clerk",
            project_name="clerk",
            labels=[Label(id="1", title="scratch"), Label(id="2", title="direct")],
        )
        task_inplace_on_repo = Task(
            id="c4",
            title="In-place on clerk",
            project_name="clerk",
            labels=[Label(id="1", title="in-place"), Label(id="2", title="direct")],
        )
        with (
            mock.patch.object(bridge, "discover_repos", return_value=self.repos),
            mock.patch.object(bridge, "discover_workdirs", return_value=self.workdirs),
            mock.patch.object(bridge, "BOARD", self.mock_board, create=True),
        ):
            self.assertEqual(bridge.workspace_mode_of(task_scratch_on_repo), "scratch")
            self.assertEqual(bridge.resolve_workdir(task_scratch_on_repo), (None, []))
            self.assertEqual(bridge.workspace_mode_of(task_inplace_on_repo), "in-place")
            self.assertEqual(bridge.resolve_workdir(task_inplace_on_repo)[0], "clerk")

    def test_missing_execution_mode_still_triggers_intake_with_board_hint(self):
        task = Task(id="c5", title="New feature", project_name="clerk", labels=[Label(id="1", title="agy")])
        with (
            mock.patch.object(bridge, "discover_repos", return_value=self.repos),
            mock.patch.object(bridge, "discover_workdirs", return_value=self.workdirs),
            mock.patch.object(bridge, "BOARD", self.mock_board, create=True),
            mock.patch.object(bridge, "AGENT", self.mock_agent, create=True),
        ):
            self.assertTrue(bridge.needs_intake(task))
            txt = bridge.intake_comment_text(task)
            self.assertIn("Board Target:", txt)
            self.assertIn("clerk", txt)
            self.assertIn("worktree", txt)


class TestBoardsOverviewAndPlankaListBoards(unittest.TestCase):
    def test_boards_overview_maps_worktree_scratch_missing_cols_and_unlinked(self):
        workdirs = {
            "clerk": [("clerk", Path("/tmp/repos/clerk"))],
            "suite": [("api", Path("/tmp/repos/suite/api")), ("web", Path("/tmp/repos/suite/web"))],
        }
        fake_boards = [
            {
                "id": "b1",
                "name": "General",
                "url": "http://localhost:3000/boards/b1",
                "columns": list(bridge.STANDARD_COLUMNS),
                "card_count": 3,
            },
            {
                "id": "b2",
                "name": "clerk",
                "url": "http://localhost:3000/boards/b2",
                "columns": ["Backlog", "To-Do", "Doing", "Done"],
                "card_count": 5,
            },
        ]
        mock_board = SimpleNamespace(list_boards=lambda: fake_boards)
        with (
            mock.patch.object(bridge, "discover_workdirs", return_value=workdirs),
            mock.patch.object(bridge, "BOARD", mock_board, create=True),
        ):
            ov = bridge.boards_overview(force=True)
        self.assertIsNone(ov["error"])
        self.assertEqual(len(ov["boards"]), 2)
        self.assertEqual(ov["boards"][0]["mode"], "scratch")
        self.assertEqual(ov["boards"][0]["missing_columns"], [])
        self.assertEqual(ov["boards"][1]["mode"], "worktree")
        self.assertEqual(ov["boards"][1]["repo_name"], "clerk")
        self.assertEqual(ov["boards"][1]["repos"], ["clerk"])
        self.assertEqual(
            ov["boards"][1]["missing_columns"],
            ["Need Action", "Pending", "Auditing", "Flagged"],
        )
        self.assertEqual(
            ov["unlinked_repos"],
            [{"name": "suite", "path": "/tmp/repos/suite", "repos": ["api", "web"]}],
        )

    def test_boards_overview_handles_provider_error(self):
        def boom():
            raise RuntimeError("planka down")

        mock_board = SimpleNamespace(list_boards=boom)
        with (
            mock.patch.object(bridge, "discover_repos", return_value={}),
            mock.patch.object(bridge, "BOARD", mock_board, create=True),
        ):
            ov = bridge.boards_overview(force=True)
        self.assertEqual(ov["boards"], [])
        self.assertIn("planka down", ov["error"])

    def test_planka_list_boards(self):
        pb = PlankaBoard(
            base_url="http://localhost:3000",
            api_key="k",
            webhook_secret="s",
            webhook_url="http://127.0.0.1:8787/planka",
        )

        def fake_api(method, path, body=None, api_key=None):
            if path == "/projects":
                return {"included": {"boards": [{"id": "10", "name": "General"}, {"id": "20", "name": "clerk"}]}}
            if path == "/boards/10":
                return {
                    "item": {"id": "10", "name": "General"},
                    "included": {
                        "lists": [
                            {"id": "l2", "name": "To-Do", "type": "active", "position": 200},
                            {"id": "l1", "name": "Backlog", "type": "active", "position": 100},
                            {"id": "l3", "name": "Trash", "type": "trash", "position": 300},
                        ],
                        "cards": [
                            {"id": "c1", "listId": "l1"},
                            {"id": "c2", "listId": "l3"},
                        ],
                    },
                }
            if path == "/boards/20":
                return {
                    "item": {"id": "20", "name": "clerk"},
                    "included": {"lists": [], "cards": []},
                }
            return {}

        with mock.patch.object(pb, "_api", side_effect=fake_api):
            boards = pb.list_boards()
        self.assertEqual(len(boards), 2)
        self.assertEqual(boards[0]["name"], "General")
        self.assertEqual(boards[0]["columns"], ["Backlog", "To-Do"])
        self.assertEqual(boards[0]["card_count"], 1)
        self.assertEqual(boards[1]["name"], "clerk")


class TestMultiRepoWorkdirWorktrees(unittest.TestCase):
    def test_discover_workdirs_and_create_umbrella_worktrees(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repos_root = root / "repos"
            tasks_root = root / "tasks"
            tasks_root.mkdir()
            _init_git_repo(repos_root / "clerk")
            _init_git_repo(repos_root / "suite" / "api")
            _init_git_repo(repos_root / "suite" / "web")

            with (
                mock.patch.object(bridge, "REPOS_ROOT", repos_root),
                mock.patch.object(bridge, "TASKS_ROOT", tasks_root),
                mock.patch.object(bridge, "EXPLICIT_REPOS", {}),
                mock.patch.object(bridge, "AGENT", SimpleNamespace(name="agy"), create=True),
            ):
                wds = bridge.discover_workdirs()
                self.assertEqual([n for n, _ in wds["clerk"]], ["clerk"])
                self.assertEqual([n for n, _ in wds["suite"]], ["api", "web"])

                t_single = Task(id="1001", title="Single fix", project_name="clerk")
                cwd_s, ctx_s = bridge.ensure_task_workspace(t_single)
                self.assertTrue((cwd_s / ".git").is_file())
                self.assertFalse(ctx_s.get("multi_repo", False))

                t_multi = Task(id="1002", title="Cross repo feature", project_name="suite")
                cwd_m, ctx_m = bridge.ensure_task_workspace(t_multi)
                self.assertTrue(ctx_m["multi_repo"])
                self.assertEqual([r["name"] for r in ctx_m["repos"]], ["api", "web"])
                self.assertTrue((cwd_m / "api" / ".git").is_file())
                self.assertTrue((cwd_m / "web" / ".git").is_file())

                workdir, mounts = _worktree_git_mount(cwd_m)
                self.assertEqual(workdir, str(cwd_m.resolve()))
                joined = " ".join(mounts)
                self.assertIn(str((repos_root / "suite" / "api" / ".git").resolve()), joined)
                self.assertIn(str((repos_root / "suite" / "web" / ".git").resolve()), joined)


if __name__ == "__main__":
    unittest.main()
