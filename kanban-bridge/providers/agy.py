"""Google Antigravity CLI (`agy`) agent provider.

Runs headless `agy -p --output-format stream-json --sandbox` in each task's isolated
working directory (`~/workspace/kanban-tasks/task-<id>-<slug>/`).

- Uses `agy`'s built-in Linux kernel-namespace `--sandbox` so terminal commands stay
  contained to the task directory without copying OAuth tokens into containers.
- Strips all bridge secrets from the child environment and injects only user-approved
  access credentials (GH_TOKEN, AWS_*, AZURE_CONFIG_DIR).
- Captures `conversation_id` from the `init` NDJSON event and attaches it immediately
  via `on_session_id` so subsequent task comments resume with `--conversation <id>`.
- Streams every NDJSON event into a host-side audit log (`~/.local/share/kanban-bridge/logs/`).
- Runs the auditor read-only (`--mode plan` without `--dangerously-skip-permissions`).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from providers import AgentProvider, AgentResult, Task

log = logging.getLogger("bridge.agy")

BRIDGE_SECRET_KEYS = {
    "PLANKA_API_KEY",
    "PLANKA_BOT_API_KEY",
    "WEBHOOK_SECRET",
    "BOT_TOKEN",
    "BOT_PASSWORD",
    "CLAUDE_OAUTH_TOKEN",
    "TEAMS_WEBHOOK_URL",
}

PROGRESS_EVERY = 30


def _describe_tool(tool_name: str, params: dict | None) -> str:
    inp = params or {}
    detail = (
        inp.get("CommandLine")
        or inp.get("Path")
        or inp.get("TargetFile")
        or inp.get("Query")
        or inp.get("Pattern")
        or inp.get("url")
        or inp.get("command")
        or inp.get("file_path")
        or ""
    )
    detail = str(detail).replace("\n", " ").strip()
    return f"{tool_name}: {detail[:80]}" if detail else (tool_name or "?")


def _kill_proc_group(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


class AgyAgent(AgentProvider):
    name = "agy"
    needs_auth_proxy = False

    def __init__(
        self,
        agy_bin: str,
        model: str | None = None,
        effort: str | None = None,
        auditor_model: str | None = None,
        base_env: dict[str, str] | None = None,
    ):
        self.agy_bin = agy_bin
        self.model = model or None
        self.effort = effort or None
        self.auditor_model = auditor_model or model or None
        self.base_env = {
            k: v for k, v in (base_env or os.environ).items() if k not in BRIDGE_SECRET_KEYS
        }

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "AgyAgent":
        agy_bin = env.get("AGY_BIN") or shutil.which("agy") or "/usr/bin/agy"
        return cls(
            agy_bin=agy_bin,
            model=env.get("BRIDGE_AGY_MODEL"),
            effort=env.get("BRIDGE_AGY_EFFORT"),
            auditor_model=env.get("BRIDGE_AUDITOR_MODEL"),
            base_env=env,
        )

    def _run_streaming(
        self,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
        timeout: int,
        procs: set,
        on_progress: Callable[[int, int, str], None] | None = None,
        on_session_id: Callable[[str], None] | None = None,
        log_dir: Path | None = None,
        known_session_id: str | None = None,
    ) -> AgentResult:
        buffered_lines: list[str] = []
        session_id = known_session_id
        log_file = None

        def open_log(sid: str):
            nonlocal log_file
            if log_dir and log_file is None:
                log_dir.mkdir(parents=True, exist_ok=True)
                log_file = open(log_dir / f"{sid}.jsonl", "a", encoding="utf-8")
                for line in buffered_lines:
                    log_file.write(line)
                buffered_lines.clear()
                log_file.flush()

        if session_id and log_dir:
            open_log(session_id)

        with tempfile.TemporaryFile("w+", encoding="utf-8") as err:
            # Own session: Ctrl-C in the bridge terminal must not kill running jobs directly.
            p = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=err,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            procs.add(p)

            timed_out = threading.Event()

            def on_timeout():
                timed_out.set()
                _kill_proc_group(p)

            timer = threading.Timer(timeout, on_timeout)
            timer.start()

            started = time.time()
            steps = 0
            last_step: str | None = None
            result_payload: dict | None = None
            text_chunks: list[str] = []
            stop_ticker = threading.Event()

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
                    elif log_dir:
                        buffered_lines.append(line)

                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    ev_type = ev.get("event")
                    if ev_type == "init":
                        cid = ev.get("conversation_id")
                        if cid and not session_id:
                            session_id = cid
                            open_log(cid)
                            if on_session_id:
                                try:
                                    on_session_id(cid)
                                except Exception:
                                    log.exception("on_session_id callback failed")
                    elif ev_type == "step_update":
                        su = ev.get("step_update") or {}
                        cid = su.get("conversation_id")
                        if cid and not session_id:
                            session_id = cid
                            open_log(cid)
                            if on_session_id:
                                try:
                                    on_session_id(cid)
                                except Exception:
                                    log.exception("on_session_id callback failed")
                        if su.get("step_type") == "tool" and su.get("state") == "ACTIVE":
                            steps += 1
                            tinfo = su.get("tool_info") or {}
                            tname = su.get("tool_name") or tinfo.get("name") or "?"
                            last_step = _describe_tool(tname, tinfo.get("parameters"))
                        elif su.get("step_type") == "agent_response" and su.get("text_delta"):
                            text_chunks.append(su["text_delta"])
                    elif ev_type == "result":
                        result_payload = ev.get("result") or ev
                        cid = result_payload.get("conversation_id")
                        if cid and not session_id:
                            session_id = cid
                            open_log(cid)
                            if on_session_id:
                                try:
                                    on_session_id(cid)
                                except Exception:
                                    log.exception("on_session_id callback failed")
                p.wait()
            finally:
                procs.discard(p)
                stop_ticker.set()
                timer.cancel()
                if log_file:
                    log_file.close()

            if timed_out.is_set():
                raise subprocess.TimeoutExpired(cmd, timeout)

            if result_payload is None:
                err.seek(0)
                stderr_tail = err.read()[-2000:].strip()
                return AgentResult(
                    text=f"⚠️ Agy failed (exit {p.returncode}):\n\n```\n{stderr_tail}\n```",
                    session_id=session_id,
                    is_error=True,
                )

            status = str(result_payload.get("status") or "SUCCESS").upper()
            text = (result_payload.get("response") or "".join(text_chunks)).strip()
            denied = result_payload.get("denied_actions") or []
            err_msg = result_payload.get("error")

            if status != "SUCCESS":
                detail = err_msg or text or "(no details)"
                return AgentResult(
                    text=f"⚠️ Agy ended with status `{status}`:\n\n{detail}",
                    session_id=session_id,
                    is_error=True,
                )
            if denied and not text:
                names = ", ".join(d.get("display_name") or d.get("action") or "?" for d in denied)
                return AgentResult(
                    text=f"⚠️ Agy could not proceed because headless permissions denied: {names}.",
                    session_id=session_id,
                    is_error=True,
                )
            return AgentResult(text=text or "(no output)", session_id=session_id, is_error=False)

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
    ) -> AgentResult:
        if resume:
            full_prompt = (
                f"<bridge_context>\n{system_instructions}\n</bridge_context>\n\n"
                f"{prompt}"
            )
        else:
            full_prompt = (
                f"<bridge_instructions>\n{system_instructions}\n</bridge_instructions>\n\n"
                f"{prompt}"
            )

        cmd = [
            self.agy_bin,
            "-p",
            full_prompt,
            "--output-format",
            "stream-json",
            "--sandbox",
            "--dangerously-skip-permissions",
            "--disable-slash-commands",
        ]
        if resume and session_id:
            cmd += ["--conversation", session_id]
        if self.model:
            cmd += ["--model", self.model]
        if self.effort:
            cmd += ["--effort", self.effort]

        env = {**self.base_env, "GIT_TERMINAL_PROMPT": "0", **env_extra}
        log.info(
            "task %s: running agy (%s) in %s",
            task.id,
            f"resume {session_id[:8]}" if resume and session_id else "new",
            cwd,
        )
        return self._run_streaming(
            cmd=cmd,
            cwd=cwd,
            env=env,
            timeout=timeout,
            procs=procs,
            on_progress=on_progress,
            on_session_id=on_session_id,
            log_dir=log_dir,
            known_session_id=session_id if resume else None,
        )

    def run_audit(
        self,
        task: Task,
        auditor_prompt: str,
        dossier: str,
        cwd: Path,
        timeout: int,
        procs: set,
    ) -> str:
        full_prompt = (
            f"<auditor_instructions>\n{auditor_prompt}\n</auditor_instructions>\n\n"
            f"{dossier}"
        )
        # Read-only audit: --mode plan + --sandbox, NO --dangerously-skip-permissions.
        # Keep slash-command expansion enabled so --mode plan takes effect.
        cmd = [
            self.agy_bin,
            "-p",
            full_prompt,
            "--output-format",
            "stream-json",
            "--sandbox",
            "--mode",
            "plan",
        ]
        if self.auditor_model:
            cmd += ["--model", self.auditor_model]

        env = {**self.base_env, "GIT_TERMINAL_PROMPT": "0"}
        res = self._run_streaming(
            cmd=cmd,
            cwd=cwd,
            env=env,
            timeout=timeout,
            procs=procs,
            log_dir=None,
        )
        return res.text

    def extract_tool_calls(self, session_id: str, log_dir: Path) -> list[str]:
        log_file = log_dir / f"{session_id}.jsonl"
        if not log_file.exists():
            return ["(session tool log not found)"]

        calls: dict[tuple[str, int], tuple[str, str, bool]] = {}
        order: list[tuple[str, int]] = []
        for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("event") != "step_update":
                continue
            su = ev.get("step_update") or {}
            if su.get("step_type") != "tool":
                continue
            cid = str(su.get("conversation_id") or session_id)
            idx = int(su.get("step_index") or 0)
            key = (cid, idx)
            tinfo = su.get("tool_info") or {}
            tname = su.get("tool_name") or tinfo.get("name") or "?"
            params = json.dumps(tinfo.get("parameters") or {}, ensure_ascii=False)[:800]
            state = str(su.get("state") or "").upper()
            is_err = state in ("ERROR", "FAILED", "DENIED") or bool(su.get("error"))
            if key not in calls:
                order.append(key)
            prev_err = calls[key][2] if key in calls else False
            calls[key] = (tname, params, prev_err or is_err)

        if not order:
            return ["(no tool calls recorded)"]
        return [
            f"- {calls[k][0]}{' [ERROR/DENIED]' if calls[k][2] else ''}: {calls[k][1]}"
            for k in order
        ]
