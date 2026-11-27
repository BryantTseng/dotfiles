"""Claude Code (`claude`) agent provider.

Supports two execution modes configured via `BRIDGE_CLAUDE_MODE`:
  - `docker` (default): Runs each turn in a fresh `kanban-agent:latest` container with only
    the task directory (`/work`) and per-task Claude state mounted. The real `CLAUDE_OAUTH_TOKEN`
    never enters the container—each run gets an ephemeral per-run secret valid only at the
    bridge's local auth proxy (`BRIDGE_PROXY_PORT`, default 8788).
  - `host`: Runs the host `claude` CLI directly in `cwd` (no Docker container or auth proxy;
    uses your local Claude login with bridge secrets stripped from the environment).

In both modes:
  - Streams Claude `stream-json` events into `~/.local/share/kanban-bridge/logs/<session>.jsonl`
    for tamper-proof auditing.
  - Runs the read-only auditor on the host with `--allowedTools Read,Grep,Glob`.
  - Runs agent subprocesses in their own session (`start_new_session=True`) so first Ctrl-C
    drains cleanly and second Ctrl-C kills the process group / container cleanly.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from providers import (
    AgentProvider,
    AgentResult,
    SubagentAudit,
    Task,
    register_proxy_token,
    unregister_proxy_token,
)

log = logging.getLogger("bridge.claude")

BRIDGE_SECRET_KEYS = {
    "PLANKA_API_KEY",
    "PLANKA_BOT_API_KEY",
    "VIKUNJA_TOKEN",
    "WEBHOOK_SECRET",
    "BOT_TOKEN",
    "BOT_PASSWORD",
    "CLAUDE_OAUTH_TOKEN",
    "TEAMS_WEBHOOK_URL",
}

PROGRESS_EVERY = 30
SUBTASK_LIST = "Subagents & Tasks"


def _describe_tool(item: dict) -> str:
    inp = item.get("input") or {}
    detail = (
        inp.get("description")
        or inp.get("file_path")
        or inp.get("pattern")
        or inp.get("command")
        or inp.get("url")
        or ""
    )
    detail = str(detail).replace("\n", " ").strip()
    return f"{item.get('name')}: {detail[:80]}" if detail else item.get("name", "?")


def _kill_proc_group(p: subprocess.Popen, container: str | None = None) -> None:
    if container:
        subprocess.run(["docker", "kill", container], capture_output=True)
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def _worktree_git_mount(cwd: Path) -> tuple[str, list[str]]:
    """Detect if `cwd` is a git worktree whose `.git` file points to an external parent repo.

    Returns `(container_workdir, extra_mounts)`:
      - Regular directory / standalone repo: `("/work", ["-v", f"{cwd}:/work"])`
      - Git worktree: mounts `cwd` at its exact host path AND mounts the parent repo's `.git`
        directory at its exact host path so `git status`/`commit`/`diff` work seamlessly inside
        the container without broken absolute `gitdir:` pointers.
    """
    dot_git = cwd / ".git"
    if dot_git.is_file():
        try:
            line = dot_git.read_text(encoding="utf-8", errors="replace").strip()
            if line.startswith("gitdir:"):
                gitdir = Path(line.split(":", 1)[1].strip())
                if not gitdir.is_absolute():
                    gitdir = (cwd / gitdir).resolve()
                # gitdir is typically <repo>/.git/worktrees/<name>; mount <repo>/.git
                parent_git = gitdir.parent.parent if gitdir.parent.name == "worktrees" else gitdir
                if parent_git.is_dir():
                    host_cwd = str(cwd.resolve())
                    return host_cwd, [
                        "-v",
                        f"{host_cwd}:{host_cwd}",
                        "-v",
                        f"{parent_git}:{parent_git}",
                        "-w",
                        host_cwd,
                    ]
        except OSError:
            pass
    return "/work", ["-v", f"{cwd}:/work", "-w", "/work"]


def _claude_project_slug(path_str: str) -> str:
    """Claude Code stores per-project transcripts under `~/.claude/projects/<slug>/`."""
    return path_str.replace("/", "-")


class ClaudeAgent(AgentProvider):
    name = "claude"

    def __init__(
        self,
        claude_bin: str,
        mode: str = "docker",
        agent_image: str = "kanban-agent:latest",
        state_root: Path | None = None,
        max_turns: int = 100,
        proxy_port: int = 8788,
        proxy_url: str | None = None,
        docker_net: str | None = None,
        docker_user: str | None = None,
        model: str | None = None,
        auditor_model: str | None = None,
        base_env: dict[str, str] | None = None,
    ):
        self.claude_bin = claude_bin
        self.mode = (mode or "docker").strip().lower()
        if self.mode not in ("docker", "host"):
            raise ValueError(f"Invalid BRIDGE_CLAUDE_MODE {mode!r}; expected 'docker' or 'host'")
        self.needs_auth_proxy = self.mode == "docker"
        self.agent_image = agent_image
        self.state_root = state_root or Path("~/.local/share/kanban-bridge").expanduser()
        self.max_turns = max_turns
        self.proxy_port = proxy_port
        # Default to host networking on Linux (works reliably with rootless Docker),
        # and host.docker.internal bridge networking on macOS / Docker Desktop.
        self.docker_net = (docker_net or ("bridge" if sys.platform == "darwin" else "host")).strip().lower()
        if proxy_url:
            self.proxy_url = proxy_url.rstrip("/")
        elif self.docker_net == "host":
            self.proxy_url = f"http://127.0.0.1:{proxy_port}"
        else:
            self.proxy_url = f"http://host.docker.internal:{proxy_port}"
        # On Linux rootless Docker, --user 0:0 maps to host UID 1000 so files created in
        # bind-mounted task dirs are owned cleanly by the host user.
        self.docker_user = docker_user
        self.model = model or None
        self.auditor_model = auditor_model or None
        self.base_env = {
            k: v for k, v in (base_env or os.environ).items() if k not in BRIDGE_SECRET_KEYS
        }

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "ClaudeAgent":
        claude_bin = (
            env.get("CLAUDE_BIN")
            or shutil.which("claude")
            or str(Path("~/.local/bin/claude").expanduser())
        )
        return cls(
            claude_bin=claude_bin,
            mode=env.get("BRIDGE_CLAUDE_MODE", "docker"),
            agent_image=env.get("BRIDGE_AGENT_IMAGE", "kanban-agent:latest"),
            state_root=Path(env.get("BRIDGE_STATE_ROOT", "~/.local/share/kanban-bridge")).expanduser(),
            max_turns=int(env.get("BRIDGE_MAX_TURNS", "100")),
            proxy_port=int(env.get("BRIDGE_PROXY_PORT", "8788")),
            proxy_url=env.get("BRIDGE_PROXY_URL"),
            docker_net=env.get("BRIDGE_DOCKER_NET"),
            docker_user=env.get("BRIDGE_DOCKER_USER"),
            model=env.get("BRIDGE_CLAUDE_MODEL"),
            auditor_model=env.get("BRIDGE_AUDITOR_MODEL"),
            base_env=env,
        )

    def _claude_state_dir(self, task: Task, session_id: str, container_workdir: str) -> Path:
        """Per-task Claude config/transcripts, mounted into the container as ~/.claude."""
        d = self.state_root / "state" / f"task-{task.id}"
        slug = _claude_project_slug(container_workdir)
        projects = d / "projects" / slug
        projects.mkdir(parents=True, exist_ok=True)
        # Migrate any host-era or prior workdir transcript so --resume works seamlessly.
        target = projects / f"{session_id}.jsonl"
        if not target.exists():
            for root in (d / "projects", Path("~/.claude/projects").expanduser()):
                if not root.is_dir():
                    continue
                for old in root.glob(f"*/{session_id}.jsonl"):
                    if old.resolve() != target.resolve():
                        shutil.copy2(old, target)
                        return d
        return d

    def _run_streaming(
        self,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
        timeout: int,
        procs: set,
        stdin_text: str | None = None,
        on_progress: Callable[[int, int, str], None] | None = None,
        on_subtask: Callable[[str, str, bool], None] | None = None,
        log_path: Path | None = None,
        container: str | None = None,
        session_id: str | None = None,
    ) -> AgentResult:
        log_file = None
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = open(log_path, "a", encoding="utf-8")

        def emit_subtask(item_text: str, is_completed: bool) -> None:
            if on_subtask and item_text:
                try:
                    on_subtask(SUBTASK_LIST, item_text, is_completed)
                except Exception:
                    log.warning("on_subtask callback failed", exc_info=True)

        with tempfile.TemporaryFile("w+", encoding="utf-8") as err:
            # Own session: Ctrl-C in the bridge terminal must not kill running jobs directly.
            p = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=err,
                stdin=subprocess.PIPE if stdin_text else subprocess.DEVNULL,
                start_new_session=True,
            )
            procs.add(p)
            if stdin_text and p.stdin:
                p.stdin.write(stdin_text)
                p.stdin.close()

            timed_out = threading.Event()

            def on_timeout():
                timed_out.set()
                _kill_proc_group(p, container=container)

            timer = threading.Timer(timeout, on_timeout)
            timer.start()

            started = time.time()
            steps = 0
            last_step: str | None = None
            result_ev: dict | None = None
            subagent_by_id: dict[str, str] = {}  # tool_use_id -> item label
            subtask_state: dict[str, bool] = {}  # item_text -> is_completed
            stop_ticker = threading.Event()

            def track_subtask(label: str, done: bool) -> None:
                if subtask_state.get(label) == done:
                    return
                subtask_state[label] = done
                emit_subtask(label, done)

            def ticker():
                while not stop_ticker.wait(PROGRESS_EVERY):
                    try:
                        on_progress(int(time.time() - started), steps, last_step or "starting")
                    except Exception:
                        log.warning("progress update failed", exc_info=True)

            if on_progress:
                threading.Thread(target=ticker, daemon=True).start()

            try:
                assert p.stdout is not None
                for line in p.stdout:
                    if log_file:
                        log_file.write(line)
                        log_file.flush()
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ev_type = ev.get("type")
                    if ev_type == "assistant":
                        for item in (ev.get("message") or {}).get("content") or []:
                            if item.get("type") == "tool_use":
                                steps += 1
                                last_step = _describe_tool(item)
                                tname = str(item.get("name") or "")
                                inp = item.get("input") or {}
                                tid = str(item.get("id") or "")
                                if tname in ("Agent", "Task"):
                                    desc = str(inp.get("description") or inp.get("subagent_type") or "subagent").strip()
                                    stype = str(inp.get("subagent_type") or "").strip()
                                    label = f"[subagent] {desc[:120]}" + (f" ({stype})" if stype and stype.lower() not in desc.lower() else "")
                                    if tid:
                                        subagent_by_id[tid] = label[:160]
                                    track_subtask(label[:160], False)
                                elif tname == "TodoWrite":
                                    for todo in inp.get("todos") or []:
                                        content = str(todo.get("content") or "").strip()
                                        if content:
                                            track_subtask(content[:160], str(todo.get("status") or "").lower() == "completed")
                    elif ev_type == "user":
                        for item in (ev.get("message") or {}).get("content") or []:
                            if item.get("type") == "tool_result":
                                tid = str(item.get("tool_use_id") or "")
                                if tid in subagent_by_id and not item.get("is_error"):
                                    track_subtask(subagent_by_id.pop(tid), True)
                    elif ev_type == "result":
                        result_ev = ev
                p.wait()
            finally:
                procs.discard(p)
                stop_ticker.set()
                timer.cancel()
                if p.stdout:
                    p.stdout.close()
                if log_file:
                    log_file.close()

            if timed_out.is_set():
                raise subprocess.TimeoutExpired(cmd, timeout)

            if result_ev is None:
                err.seek(0)
                stderr_tail = err.read()[-2000:].strip()
                return AgentResult(
                    text=f"⚠️ Claude failed (exit {p.returncode}):\n\n```\n{stderr_tail}\n```",
                    session_id=session_id,
                    is_error=True,
                )

            text = (result_ev.get("result") or "").strip()
            subtype = result_ev.get("subtype", "success")
            is_err = bool(result_ev.get("is_error"))
            if subtype != "success":
                return AgentResult(
                    text=f"⚠️ Claude stopped early ({subtype}).\n\n{text}".strip(),
                    session_id=session_id,
                    is_error=True,
                )
            if is_err:
                return AgentResult(
                    text=f"⚠️ Claude ended with an error:\n\n{text}",
                    session_id=session_id,
                    is_error=True,
                )
            return AgentResult(text=text or "(no output)", session_id=session_id, is_error=False)

    def _build_docker_cmd(
        self,
        task: Task,
        session_id: str,
        prompt: str,
        system_instructions: str,
        resume: bool,
        cwd: Path,
        env_extra: dict[str, str],
        run_token: str,
        tmp_dir: Path,
        container_name: str,
    ) -> list[str]:
        container_workdir, work_mounts = _worktree_git_mount(cwd)
        state_dir = self._claude_state_dir(task, session_id, container_workdir)

        container_env: dict[str, str] = {
            "CLAUDE_CODE_OAUTH_TOKEN": run_token,
            "ANTHROPIC_BASE_URL": self.proxy_url,
            "GIT_TERMINAL_PROMPT": "0",
        }
        extra_mounts: list[str] = []
        for k, v in env_extra.items():
            if k == "AZURE_CONFIG_DIR":
                extra_mounts += ["-v", f"{v}:/home/agent/.azure"]
            elif k == "CARGO_TARGET_DIR":
                # Host Cargo target paths don't apply unless mounted; skip in container mode.
                continue
            else:
                container_env[k] = v

        env_file = tmp_dir / "env"
        env_file.write_text("".join(f"{k}={v}\n" for k, v in container_env.items()), encoding="utf-8")
        env_file.chmod(0o600)

        cmd = ["docker", "run", "--rm", "-i", "--name", container_name]
        if self.docker_net == "host":
            cmd += ["--net=host"]
        else:
            cmd += ["--add-host", "host.docker.internal:host-gateway"]
        if self.docker_user:
            cmd += ["--user", self.docker_user]

        cmd += [
            "--env-file",
            str(env_file),
            *work_mounts,
            "-v",
            f"{state_dir}:/home/agent/.claude",
            *extra_mounts,
            self.agent_image,
            "claude",
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--max-turns",
            str(self.max_turns),
            "--permission-mode",
            "bypassPermissions",
            "--append-system-prompt",
            system_instructions,
        ]
        if self.model:
            cmd += ["--model", self.model]
        cmd += ["--resume", session_id] if resume else ["--session-id", session_id]
        return cmd

    def _build_host_cmd(
        self,
        session_id: str,
        prompt: str,
        system_instructions: str,
        resume: bool,
    ) -> list[str]:
        cmd = [
            self.claude_bin,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--max-turns",
            str(self.max_turns),
            "--permission-mode",
            "bypassPermissions",
            "--append-system-prompt",
            system_instructions,
        ]
        if self.model:
            cmd += ["--model", self.model]
        cmd += ["--resume", session_id] if resume else ["--session-id", session_id]
        return cmd

    def run_task(
        self,
        task: Task,
        session_id: str | None,
        prompt: str,
        system_instructions: str,
        resume: bool,
        cwd: Path,
        env_extra: dict[str, str],
        on_progress: Callable[[int, int, str], None] | None,
        on_session_id: Callable[[str], None] | None,
        log_dir: Path,
        timeout: int,
        procs: set,
        on_subtask: Callable[[str, str, bool], None] | None = None,
    ) -> AgentResult:
        sid = session_id or str(uuid.uuid4())
        if not resume and on_session_id:
            on_session_id(sid)

        log_path = log_dir / f"{sid}.jsonl"

        if self.mode == "host":
            cmd = self._build_host_cmd(sid, prompt, system_instructions, resume)
            env = {**self.base_env, "GIT_TERMINAL_PROMPT": "0", **env_extra}
            log.info("task %s: running claude (host, %s) in %s", task.id, "resume" if resume else "new", cwd)
            return self._run_streaming(
                cmd=cmd,
                cwd=cwd,
                env=env,
                timeout=timeout,
                procs=procs,
                on_progress=on_progress,
                on_subtask=on_subtask,
                log_path=log_path,
                session_id=sid,
            )

        run_token = secrets.token_urlsafe(32)
        container = f"kanban-task-{task.id}-{run_token[:8].lower()}"
        with tempfile.TemporaryDirectory() as tmp:
            cmd = self._build_docker_cmd(
                task=task,
                session_id=sid,
                prompt=prompt,
                system_instructions=system_instructions,
                resume=resume,
                cwd=cwd,
                env_extra=env_extra,
                run_token=run_token,
                tmp_dir=Path(tmp),
                container_name=container,
            )
            log.info(
                "task %s: running claude (docker:%s, %s) in %s",
                task.id,
                container,
                "resume" if resume else "new",
                cwd,
            )
            register_proxy_token(run_token, task.id)
            try:
                return self._run_streaming(
                    cmd=cmd,
                    cwd=cwd,
                    env=self.base_env,
                    timeout=timeout,
                    procs=procs,
                    on_progress=on_progress,
                    on_subtask=on_subtask,
                    log_path=log_path,
                    container=container,
                    session_id=sid,
                )
            finally:
                unregister_proxy_token(run_token)

    def run_audit(
        self,
        task: Task,
        auditor_prompt: str,
        dossier: str,
        cwd: Path,
        timeout: int,
        procs: set,
    ) -> str:
        cmd = [
            self.claude_bin,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--allowedTools",
            "Read,Grep,Glob",
            "--append-system-prompt",
            auditor_prompt,
        ]
        if self.auditor_model:
            cmd += ["--model", self.auditor_model]
        res = self._run_streaming(
            cmd=cmd,
            cwd=cwd,
            env={**self.base_env, "GIT_TERMINAL_PROMPT": "0"},
            timeout=timeout,
            procs=procs,
            stdin_text=dossier,
        )
        return res.text

    def agent_workdir(self, cwd: Path) -> Path:
        if self.mode == "docker":
            workdir, _ = _worktree_git_mount(cwd)
            return Path(workdir)
        return cwd

    def _transcript_files(self, session_id: str, log_dir: Path, task_id: str | None = None) -> list[Path]:
        files: list[Path] = []
        primary = log_dir / f"{session_id}.jsonl"
        if primary.exists():
            files.append(primary)
        host_projects = Path("~/.claude/projects").expanduser()
        if host_projects.is_dir():
            files.extend(host_projects.glob(f"*/{session_id}.jsonl"))
        if task_id and self.state_root.is_dir():
            task_proj = self.state_root / f"task-{task_id}" / "projects"
            if task_proj.is_dir():
                files.extend(task_proj.glob(f"*/{session_id}.jsonl"))
        return files

    @staticmethod
    def _extract_result_text(raw_content) -> str:
        if isinstance(raw_content, str):
            return raw_content.strip()
        if isinstance(raw_content, list):
            parts: list[str] = []
            for blk in raw_content:
                if isinstance(blk, dict) and blk.get("type") == "text" and blk.get("text"):
                    parts.append(str(blk["text"]))
                elif isinstance(blk, str):
                    parts.append(blk)
            return "\n".join(parts).strip()
        return ""

    def extract_tool_calls(self, session_id: str, log_dir: Path) -> list[str]:
        files = self._transcript_files(session_id, log_dir)
        if not files:
            return ["(session transcript not found)"]

        calls: list[tuple[str, str, str]] = []
        errors: set[str] = set()
        for f in files:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Tool calls executed inside a child subagent belong under that subagent, not parent.
                if entry.get("parent_tool_use_id"):
                    continue
                content = (entry.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for item in content:
                    if item.get("type") == "tool_use":
                        calls.append(
                            (
                                str(item.get("id") or ""),
                                str(item.get("name") or "?"),
                                json.dumps(item.get("input") or {}, ensure_ascii=False)[:800],
                            )
                        )
                    elif item.get("type") == "tool_result" and item.get("is_error"):
                        errors.add(str(item.get("tool_use_id") or ""))

        unique: dict[str, tuple[str, str]] = {}
        order: list[str] = []
        for cid, name, inp in calls:
            if cid not in unique:
                order.append(cid)
            unique[cid] = (name, inp)

        if not order:
            return ["(no tool calls recorded)"]
        return [
            f"- {unique[cid][0]}{' [ERROR/DENIED]' if cid in errors else ''}: {unique[cid][1]}"
            for cid in order
        ]

    def extract_subagent_audits(
        self, session_id: str, log_dir: Path, task_id: str | None = None
    ) -> list[SubagentAudit]:
        files = self._transcript_files(session_id, log_dir, task_id=task_id)
        if not files:
            return []

        order: list[str] = []
        meta: dict[str, tuple[str, str]] = {}  # tool_use_id -> (name, prompt)
        reports: dict[str, str] = {}
        child_calls: dict[str, list[tuple[str, str, str]]] = {}
        child_errors: dict[str, set[str]] = {}

        for f in files:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = (entry.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                parent_tid = str(entry.get("parent_tool_use_id") or "")
                if parent_tid:
                    for item in content:
                        if item.get("type") == "tool_use":
                            child_calls.setdefault(parent_tid, []).append(
                                (
                                    str(item.get("id") or ""),
                                    str(item.get("name") or "?"),
                                    json.dumps(item.get("input") or {}, ensure_ascii=False)[:800],
                                )
                            )
                        elif item.get("type") == "tool_result" and item.get("is_error"):
                            child_errors.setdefault(parent_tid, set()).add(str(item.get("tool_use_id") or ""))
                else:
                    for item in content:
                        if item.get("type") == "tool_use" and item.get("name") in ("Agent", "Task"):
                            tid = str(item.get("id") or "")
                            if not tid:
                                continue
                            inp = item.get("input") or {}
                            desc = str(inp.get("description") or inp.get("subagent_type") or "subagent").strip()
                            stype = str(inp.get("subagent_type") or "").strip()
                            label = desc + (f" ({stype})" if stype and stype.lower() not in desc.lower() else "")
                            prompt = str(inp.get("prompt") or desc).strip()[:2000]
                            if tid not in meta:
                                order.append(tid)
                            meta[tid] = (label[:160], prompt)
                        elif item.get("type") == "tool_result":
                            tid = str(item.get("tool_use_id") or "")
                            if tid in meta:
                                txt = self._extract_result_text(item.get("content"))
                                if txt:
                                    reports[tid] = txt[:2500]

        # Also check on-disk Claude subagent JSONL files (Docker state_root or host ~/.claude)
        # when child tool calls weren't streamed with parent_tool_use_id.
        sub_dirs: list[Path] = []
        for base in [Path("~/.claude/projects").expanduser(), *(
            [self.state_root / f"task-{task_id}" / "projects"] if task_id else []
        )]:
            if base.is_dir():
                sub_dirs.extend(base.glob(f"*/{session_id}/subagents"))
                sub_dirs.extend(base.glob("*/subagents"))
        seen_subfiles: set[Path] = set()
        for sdir in sub_dirs:
            for sf in sorted(sdir.glob("agent-*.jsonl")):
                rpath = sf.resolve()
                if rpath in seen_subfiles:
                    continue
                seen_subfiles.add(rpath)
                sid_key = f"file:{sf.stem}"
                s_calls: list[tuple[str, str, str]] = []
                s_errs: set[str] = set()
                s_prompt = ""
                s_report = ""
                for line in sf.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = entry.get("message") or {}
                    role = msg.get("role") or entry.get("type")
                    content = msg.get("content")
                    if role == "user" and not s_prompt:
                        s_prompt = self._extract_result_text(content)[:2000]
                    if isinstance(content, list):
                        for item in content:
                            if item.get("type") == "tool_use":
                                s_calls.append(
                                    (
                                        str(item.get("id") or ""),
                                        str(item.get("name") or "?"),
                                        json.dumps(item.get("input") or {}, ensure_ascii=False)[:800],
                                    )
                                )
                            elif item.get("type") == "tool_result" and item.get("is_error"):
                                s_errs.add(str(item.get("tool_use_id") or ""))
                            elif role == "assistant" and item.get("type") == "text" and item.get("text"):
                                s_report = str(item["text"]).strip()[:2500]
                    elif role == "assistant" and isinstance(content, str) and content.strip():
                        s_report = content.strip()[:2500]

                # Match to an existing parent Agent/Task call without child_calls if prompts align
                matched_tid = None
                for tid in order:
                    if not child_calls.get(tid) and s_prompt and meta[tid][1] and (
                        s_prompt[:120] in meta[tid][1] or meta[tid][1][:120] in s_prompt
                    ):
                        matched_tid = tid
                        break
                if matched_tid:
                    child_calls[matched_tid] = s_calls
                    child_errors[matched_tid] = s_errs
                    if s_report and not reports.get(matched_tid):
                        reports[matched_tid] = s_report
                elif s_calls or s_report:
                    order.append(sid_key)
                    meta[sid_key] = (sf.stem, s_prompt)
                    child_calls[sid_key] = s_calls
                    child_errors[sid_key] = s_errs
                    if s_report:
                        reports[sid_key] = s_report

        audits: list[SubagentAudit] = []
        for tid in order:
            name, prompt = meta[tid]
            errs = child_errors.get(tid, set())
            unique_c: dict[str, tuple[str, str]] = {}
            c_order: list[str] = []
            for cid, cname, cinp in child_calls.get(tid, []):
                if cid not in unique_c:
                    c_order.append(cid)
                unique_c[cid] = (cname, cinp)
            formatted_calls = [
                f"- {unique_c[cid][0]}{' [ERROR/DENIED]' if cid in errs else ''}: {unique_c[cid][1]}"
                for cid in c_order
            ]
            audits.append(
                SubagentAudit(
                    id=tid,
                    name=name,
                    prompt=prompt,
                    tool_calls=formatted_calls,
                    final_report=reports.get(tid, ""),
                )
            )
        return audits

    def force_cleanup(self) -> None:
        if self.mode != "docker":
            return
        res = subprocess.run(
            ["docker", "ps", "-q", "--filter", "name=kanban-task-"],
            capture_output=True,
            text=True,
        )
        leftovers = res.stdout.split()
        if leftovers:
            subprocess.run(["docker", "kill", *leftovers], capture_output=True)
            log.info("killed %d leftover agent containers", len(leftovers))
