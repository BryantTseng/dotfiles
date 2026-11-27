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
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from providers import (
    AgentProvider,
    AgentResult,
    ModelUsage,
    SubagentAudit,
    Task,
    UsageSummary,
    estimate_model_cost_usd,
)

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
SUBTASK_LIST = "Subagents & Tasks"
BG_CMD_THRESHOLD_SEC = 2.0
NONZERO_EXIT_RE = re.compile(r"exited with code\s+(?!0\b)\d+", re.IGNORECASE)


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
        disable_slash_commands: bool = False,
        base_env: dict[str, str] | None = None,
    ):
        self.agy_bin = agy_bin
        self.model = model or None
        self.effort = effort or None
        self.auditor_model = auditor_model or model or None
        self.disable_slash_commands = disable_slash_commands
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
            disable_slash_commands=env.get("BRIDGE_AGY_DISABLE_SLASH_COMMANDS", "0") == "1",
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
        on_subtask: Callable[[str, str, bool], None] | None = None,
        log_dir: Path | None = None,
        known_session_id: str | None = None,
    ) -> AgentResult:
        buffered_lines: list[str] = []
        session_id = known_session_id
        log_file = None

        def emit_subtask(item_text: str, is_completed: bool) -> None:
            if on_subtask and item_text:
                try:
                    on_subtask(SUBTASK_LIST, item_text, is_completed)
                except Exception:
                    log.warning("on_subtask callback failed", exc_info=True)

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
            step_texts: dict[int, list[str]] = {}
            last_text_step: int | None = None
            subtask_lock = threading.Lock()
            open_subtasks: dict[str, bool] = {}  # item_text -> is_completed
            failed_subtasks: set[str] = set()
            swarm_members: dict[str, str] = {}   # roster name -> item_text
            workflow_steps: dict[int, str] = {}  # step_index -> item_text
            cmd_steps: dict[int, str] = {}       # step_index -> item_text
            cmd_timers: dict[int, threading.Timer] = {}
            stop_ticker = threading.Event()

            def track_subtask(label: str, done: bool) -> None:
                with subtask_lock:
                    if open_subtasks.get(label) == done:
                        return
                    open_subtasks[label] = done
                emit_subtask(label, done)

            def _format_subagent_label(role: str, ttype: str) -> str:
                r = (role or ttype or "subagent").strip()
                t = (ttype or "").strip()
                label = f"[subagent] {r}" + (f" ({t})" if t and t.lower() not in r.lower() else "")
                return label[:160]

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
                        step_type = str(su.get("step_type") or "").lower()
                        tinfo = su.get("tool_info") or {}
                        tname = str(su.get("tool_name") or tinfo.get("name") or "")
                        params = tinfo.get("parameters") or {}
                        tstate = str(su.get("state") or "").upper()
                        sidx = int(su.get("step_index") or 0)

                        if step_type == "subagent" or tname in ("invoke_subagent", "browser_subagent"):
                            subagents = ((su.get("subagent_info") or {}).get("subagents") or [])
                            if tstate == "ACTIVE":
                                steps += 1
                                labels: list[str] = []
                                for sub in subagents:
                                    labels.append(
                                        _format_subagent_label(
                                            str(sub.get("role") or ""),
                                            str(sub.get("type_name") or ""),
                                        )
                                    )
                                if not labels:
                                    labels.append(
                                        _format_subagent_label(
                                            str(params.get("Role") or params.get("TaskName") or ""),
                                            str(params.get("TypeName") or ""),
                                        )
                                    )
                                last_step = labels[0]
                                for lbl in labels:
                                    track_subtask(lbl, False)
                        elif step_type == "tool" or tname in ("swarm", "run_workflow", "manage_subagents", "run_command"):
                            if tstate == "ACTIVE":
                                steps += 1
                                last_step = _describe_tool(tname or "?", params)
                                if tname == "swarm":
                                    act = str(params.get("action") or "").lower()
                                    if act == "add":
                                        base = str(params.get("name") or "worker").strip()
                                        model = str(params.get("model") or "").strip()
                                        tasks_arg = params.get("tasks") or []
                                        n_arg = int(params.get("n") or 1)
                                        count = len(tasks_arg) if isinstance(tasks_arg, list) and tasks_arg else n_arg
                                        names = [f"{base}-{i}" for i in range(1, count + 1)] if count > 1 else [base]
                                        for mname in names:
                                            label = f"[swarm] {mname}" + (f" ({model})" if model else "")
                                            swarm_members[mname] = label[:160]
                                            track_subtask(label[:160], False)
                                    elif act == "kill":
                                        mname = str(params.get("name") or "").strip()
                                        if mname in swarm_members:
                                            track_subtask(swarm_members[mname], True)
                                elif tname == "run_workflow":
                                    desc = str(params.get("Description") or "workflow").strip()
                                    label = f"[workflow] {desc[:120]}"
                                    workflow_steps[sidx] = label
                                    track_subtask(label, False)
                                elif tname == "run_command":
                                    raw_cmd = str(params.get("toolSummary") or params.get("CommandLine") or "command")
                                    cmd_label = f"[cmd] {' '.join(raw_cmd.split())[:120]}"
                                    cmd_steps[sidx] = cmd_label

                                    def _on_long_cmd(step_i: int = sidx, lbl: str = cmd_label) -> None:
                                        with subtask_lock:
                                            if step_i not in cmd_timers:
                                                return
                                        track_subtask(lbl, False)

                                    t = threading.Timer(BG_CMD_THRESHOLD_SEC, _on_long_cmd)
                                    t.daemon = True
                                    with subtask_lock:
                                        cmd_timers[sidx] = t
                                    t.start()
                            elif tstate in ("DONE", "ERROR", "FAILED", "DENIED"):
                                out_text = str(tinfo.get("output") or "")
                                if tname == "run_workflow" and sidx in workflow_steps:
                                    if "background" not in out_text.lower() and tstate == "DONE":
                                        track_subtask(workflow_steps[sidx], True)
                                elif tname == "run_command":
                                    with subtask_lock:
                                        t = cmd_timers.pop(sidx, None)
                                        if t is not None:
                                            t.cancel()
                                    raw_cmd = str(params.get("toolSummary") or params.get("CommandLine") or "command")
                                    cmd_label = cmd_steps.get(sidx) or f"[cmd] {' '.join(raw_cmd.split())[:120]}"
                                    dur = float(su.get("duration_seconds") or 0.0)
                                    if "background task" in out_text.lower():
                                        track_subtask(cmd_label, False)
                                    elif cmd_label in open_subtasks or dur >= BG_CMD_THRESHOLD_SEC:
                                        cmd_failed = tstate != "DONE" or bool(NONZERO_EXIT_RE.search(out_text))
                                        if cmd_failed:
                                            failed_subtasks.add(cmd_label)
                                            track_subtask(cmd_label, False)
                                        else:
                                            failed_subtasks.discard(cmd_label)
                                            track_subtask(cmd_label, True)
                                elif tname == "manage_subagents" and str(params.get("Action") or "").lower() == "kill_all":
                                    for lbl in list(open_subtasks):
                                        if lbl.startswith("[subagent]") and not open_subtasks[lbl]:
                                            track_subtask(lbl, True)
                        elif step_type == "agent_response" and su.get("text_delta"):
                            delta = su["text_delta"]
                            step_texts.setdefault(sidx, []).append(delta)
                            if delta.strip():
                                last_text_step = sidx
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
                with subtask_lock:
                    for t in cmd_timers.values():
                        t.cancel()
                    cmd_timers.clear()
                if p.stdout:
                    p.stdout.close()
                if log_file:
                    log_file.close()

            if timed_out.is_set():
                raise subprocess.TimeoutExpired(cmd, timeout)

            detected_model = self._detect_model(session_id) or self.model

            if result_payload is None:
                err.seek(0)
                stderr_tail = err.read()[-2000:].strip()
                return AgentResult(
                    text=f"⚠️ Agy failed (exit {p.returncode}):\n\n```\n{stderr_tail}\n```",
                    session_id=session_id,
                    is_error=True,
                    model=detected_model,
                )

            status = str(result_payload.get("status") or "SUCCESS").upper()
            if status == "SUCCESS":
                # Any subagents, swarm workers, workflows, or background tasks launched during
                # a clean turn have finished by the time the turn ends; mark non-failed ones checked.
                for lbl, is_done in list(open_subtasks.items()):
                    if not is_done and lbl not in failed_subtasks:
                        track_subtask(lbl, True)
            # Use only the final agent_response step so intermediate background-wait chatter
            # (e.g. "Running cargo test... waiting for build to finish.") doesn't pollute the reply.
            final_step_text = "".join(step_texts.get(last_text_step, [])).strip() if last_text_step is not None else ""
            text = final_step_text or (result_payload.get("response") or "").strip()
            denied = result_payload.get("denied_actions") or []
            err_msg = result_payload.get("error")

            if status != "SUCCESS":
                detail = err_msg or text or "(no details)"
                return AgentResult(
                    text=f"⚠️ Agy ended with status `{status}`:\n\n{detail}",
                    session_id=session_id,
                    is_error=True,
                    model=detected_model,
                )
            if denied and not text:
                names = ", ".join(d.get("display_name") or d.get("action") or "?" for d in denied)
                return AgentResult(
                    text=f"⚠️ Agy could not proceed because headless permissions denied: {names}.",
                    session_id=session_id,
                    is_error=True,
                    model=detected_model,
                )
            return AgentResult(
                text=text or "(no output)",
                session_id=session_id,
                is_error=False,
                model=detected_model,
            )

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
        ]
        if self.disable_slash_commands:
            cmd.append("--disable-slash-commands")
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
            on_subtask=on_subtask,
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
            stype = str(su.get("step_type") or "").lower()
            if stype not in ("tool", "subagent"):
                continue
            cid = str(su.get("conversation_id") or session_id)
            idx = int(su.get("step_index") or 0)
            key = (cid, idx)
            tinfo = su.get("tool_info") or {}
            tname = su.get("tool_name") or tinfo.get("name") or stype or "?"
            raw_params = (
                su.get("subagent_info") if stype == "subagent" else tinfo.get("parameters")
            ) or {}
            params = json.dumps(raw_params, ensure_ascii=False)[:800]
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

    def extract_subagent_audits(
        self, session_id: str, log_dir: Path, task_id: str | None = None
    ) -> list[SubagentAudit]:
        log_file = log_dir / f"{session_id}.jsonl"
        if not log_file.exists():
            return []

        order: list[str] = []
        subs: dict[str, dict] = {}
        for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("event") != "step_update":
                continue
            su = ev.get("step_update") or {}
            if str(su.get("step_type") or "").lower() != "subagent":
                continue
            sidx = int(su.get("step_index") or 0)
            for idx, sub in enumerate((su.get("subagent_info") or {}).get("subagents") or []):
                cid = str(sub.get("conversation_id") or "")
                role = str(sub.get("role") or sub.get("type_name") or "subagent").strip()
                ttype = str(sub.get("type_name") or "").strip()
                key = cid or f"step:{sidx}:{idx}:{role}"
                # Upgrade placeholder step key once DONE provides conversation_id
                step_key = f"step:{sidx}:{idx}:{role}"
                if cid and step_key in subs and cid not in subs:
                    subs[cid] = subs.pop(step_key)
                    order = [cid if k == step_key else k for k in order]
                if key not in subs:
                    order.append(key)
                    subs[key] = {}
                entry = subs[key]
                entry["id"] = cid or key
                entry["role"] = role
                entry["type_name"] = ttype
                if sub.get("initial_prompt"):
                    entry["prompt"] = str(sub["initial_prompt"]).strip()[:2000]
                if sub.get("log_uri"):
                    entry["log_uri"] = str(sub["log_uri"])

        audits: list[SubagentAudit] = []
        for key in order:
            info = subs[key]
            role = info.get("role") or "subagent"
            ttype = info.get("type_name") or ""
            label = role + (f" ({ttype})" if ttype and ttype.lower() not in role.lower() else "")
            prompt = info.get("prompt") or ""

            log_uri = info.get("log_uri") or ""
            transcript_path: Path | None = None
            if log_uri.startswith("file://"):
                cand = Path(log_uri[len("file://") :])
                if cand.is_file():
                    transcript_path = cand
            if transcript_path is None and info.get("id") and not str(info["id"]).startswith("step:"):
                cand = (
                    Path.home()
                    / ".gemini"
                    / "antigravity-cli"
                    / "brain"
                    / str(info["id"])
                    / ".system_generated"
                    / "logs"
                    / "transcript.jsonl"
                )
                if cand.is_file():
                    transcript_path = cand

            tool_calls: list[str] = []
            final_report = ""
            if transcript_path is not None:
                steps_raw: list[dict] = []
                for tline in transcript_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        steps_raw.append(json.loads(tline))
                    except json.JSONDecodeError:
                        continue
                for i, st in enumerate(steps_raw):
                    if st.get("type") != "PLANNER_RESPONSE":
                        continue
                    next_err = (
                        i + 1 < len(steps_raw)
                        and str(steps_raw[i + 1].get("status") or "").upper() in ("ERROR", "FAILED", "DENIED")
                    )
                    for tc in st.get("tool_calls") or []:
                        tc_name = str(tc.get("name") or "?")
                        tc_args = tc.get("args") or {}
                        args_s = json.dumps(tc_args, ensure_ascii=False)[:800]
                        tool_calls.append(f"- {tc_name}{' [ERROR/DENIED]' if next_err else ''}: {args_s}")
                        if tc_name == "send_message" and tc_args.get("Message"):
                            msg = str(tc_args["Message"]).strip()
                            if len(msg) >= 2 and msg[0] == '"' and msg[-1] == '"':
                                try:
                                    msg = json.loads(msg)
                                except Exception:
                                    pass
                            final_report = str(msg).strip()[:2500]
                    if st.get("content") and not final_report:
                        final_report = str(st["content"]).strip()[:2500]

            audits.append(
                SubagentAudit(
                    id=str(info.get("id") or key),
                    name=label[:160],
                    prompt=prompt,
                    tool_calls=tool_calls,
                    final_report=final_report,
                )
            )
        return audits

    @staticmethod
    def _transcript_path(session_id: str | None) -> Path | None:
        if not session_id:
            return None
        cand = (
            Path.home()
            / ".gemini"
            / "antigravity-cli"
            / "brain"
            / session_id
            / ".system_generated"
            / "logs"
            / "transcript.jsonl"
        )
        return cand if cand.is_file() else None

    def _detect_model(self, session_id: str | None) -> str | None:
        tpath = self._transcript_path(session_id)
        if tpath is None:
            return None
        last_model: str | None = None
        for line in tpath.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                st = json.loads(line)
            except json.JSONDecodeError:
                continue
            if st.get("type") == "PLANNER_RESPONSE" and st.get("model"):
                last_model = str(st["model"]).strip()
        return last_model

    @staticmethod
    def _usage_from_transcript(tpath: Path) -> dict[str, ModelUsage]:
        by_model: dict[str, ModelUsage] = {}
        for line in tpath.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                st = json.loads(line)
            except json.JSONDecodeError:
                continue
            if st.get("type") != "PLANNER_RESPONSE":
                continue
            m = str(st.get("model") or "agy").strip() or "agy"
            u = by_model.setdefault(m, ModelUsage(model=m))
            u.input_tokens += int(st.get("input_tokens") or 0)
            u.output_tokens += int(st.get("output_tokens") or 0)
            u.cache_read_tokens += int(st.get("cache_read_tokens") or 0)
        return by_model

    def extract_usage_summary(
        self, session_id: str, log_dir: Path, task_id: str | None = None
    ) -> UsageSummary:
        parent_by_model: dict[str, ModelUsage] = {}
        tpath = self._transcript_path(session_id)
        if tpath is not None:
            parent_by_model = self._usage_from_transcript(tpath)

        log_file = log_dir / f"{session_id}.jsonl"
        sub_paths: list[Path] = []
        seen_sub_paths: set[Path] = set()
        if log_file.exists():
            last_result_usage: dict | None = None
            for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("event") == "result":
                    usage = (ev.get("result") or ev).get("usage")
                    if isinstance(usage, dict):
                        last_result_usage = usage
                elif ev.get("event") == "step_update":
                    su = ev.get("step_update") or {}
                    if str(su.get("step_type") or "").lower() == "subagent":
                        for sub in (su.get("subagent_info") or {}).get("subagents") or []:
                            luri = str(sub.get("log_uri") or "")
                            cand: Path | None = None
                            if luri.startswith("file://"):
                                p = Path(luri[len("file://") :])
                                if p.is_file():
                                    cand = p
                            if cand is None and sub.get("conversation_id"):
                                cand = self._transcript_path(str(sub["conversation_id"]))
                            if cand is not None:
                                r = cand.resolve()
                                if r not in seen_sub_paths:
                                    seen_sub_paths.add(r)
                                    sub_paths.append(r)

            if not parent_by_model and last_result_usage:
                m = self.model or "agy"
                parent_by_model[m] = ModelUsage(
                    model=m,
                    input_tokens=int(last_result_usage.get("input_tokens") or 0),
                    output_tokens=int(last_result_usage.get("output_tokens") or 0),
                    cache_read_tokens=int(last_result_usage.get("cache_read_tokens") or 0),
                )

        # Recursively discover any nested grandchild subagents referenced inside child transcripts.
        sub_by_model: dict[str, ModelUsage] = {}
        queue_paths = list(sub_paths)
        while queue_paths:
            sp = queue_paths.pop(0)
            for m, u in self._usage_from_transcript(sp).items():
                dst = sub_by_model.setdefault(m, ModelUsage(model=m))
                dst.input_tokens += u.input_tokens
                dst.output_tokens += u.output_tokens
                dst.cache_read_tokens += u.cache_read_tokens
            for line in sp.read_text(encoding="utf-8", errors="replace").splitlines():
                if "logAbsoluteUri" not in line:
                    continue
                for uri_m in re.finditer(r'file://([^"\\\s]+transcript\.jsonl)', line):
                    cand = Path(uri_m.group(1))
                    if cand.is_file():
                        r = cand.resolve()
                        if r not in seen_sub_paths and (tpath is None or r != tpath.resolve()):
                            seen_sub_paths.add(r)
                            queue_paths.append(r)

        models = list(parent_by_model.values())
        sub_models = list(sub_by_model.values())
        est_parts = [estimate_model_cost_usd(u, env=self.base_env) for u in models + sub_models]
        known = [c for c in est_parts if c is not None]
        est_total = sum(known) if known else None
        return UsageSummary(
            models=models,
            subagent_models=sub_models,
            estimated_cost_usd=est_total,
            cost_note="(estimated via OpenRouter pricing)" if est_total is not None else "",
        )
