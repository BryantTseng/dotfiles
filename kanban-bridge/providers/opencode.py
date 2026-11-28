"""OpenCode (`opencode`) agent provider — native OpenRouter / multi-provider CLI runtime.

Runs `opencode run --format json` in headless mode on the host:
  - Streams OpenCode JSONL events (`step_start`, `text`, `tool_use`, `step_finish`, `error`)
    into `~/.local/share/kanban-bridge/logs/<session>.jsonl` for tamper-proof auditing.
  - Persists session IDs (`ses_...`) via `--session <id>` for multi-turn card conversations.
  - Supports OpenRouter models natively via `OPENROUTER_API_KEY` and
    `BRIDGE_OPENCODE_MODEL=openrouter/<vendor>/<model>` (or any provider configured in OpenCode).
  - `BRIDGE_ORCHESTRATOR_MODEL`, `BRIDGE_AUDITOR_MODEL` and `BRIDGE_WORKER_MODEL`
    pin the coordinator, auditor and built-in subagent models independently.
  - Uses `OPENCODE_CONFIG_CONTENT` to auto-allow tool execution headlessly in `run_task` and
    enforce strict read-only permissions (`edit`/`write`/`bash` denied) in `run_audit`.
  - Runs agent subprocesses in their own session (`start_new_session=True`) so Ctrl-C drains
    cleanly and forced stop kills the process group.
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

from providers import (
    AgentProvider,
    AgentResult,
    ModelUsage,
    SubagentAudit,
    Task,
    UsageSummary,
    estimate_model_cost_usd,
    is_model_fallback_error,
    parse_model_list,
    resolve_model_candidates,
)

log = logging.getLogger("bridge.opencode")

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

# OpenCode's built-in subagents (used by orchestrator cards) get their model
# from the agent config, not from `opencode run --model`.
WORKER_AGENTS = ("general", "explore")

TASK_PERMISSION_CONFIG = json.dumps(
    {
        "$schema": "https://opencode.ai/config.json",
        "permission": "allow",
    }
)

AUDIT_PERMISSION_CONFIG = json.dumps(
    {
        "$schema": "https://opencode.ai/config.json",
        "permission": {
            "*": "deny",
            "read": "allow",
            "glob": "allow",
            "grep": "allow",
            "list": "allow",
        },
    }
)


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


def _normalize_model_label(model: str | None) -> str | None:
    """Strip OpenCode's `openrouter/` routing prefix for cleaner card labels and pricing lookup."""
    if not model:
        return None
    m = model.strip()
    if m.lower().startswith("openrouter/"):
        m = m[len("openrouter/") :]
    return m or None


def _describe_tool(tool_name: str, state: dict) -> str:
    inp = state.get("input") or {}
    title = str(state.get("title") or "").strip()
    detail = (
        title
        or inp.get("description")
        or inp.get("command")
        or inp.get("filePath")
        or inp.get("file_path")
        or inp.get("path")
        or inp.get("pattern")
        or inp.get("url")
        or ""
    )
    detail = str(detail).replace("\n", " ").strip()
    return f"{tool_name}: {detail[:80]}" if detail else tool_name


def _extract_error_text(ev: dict) -> str:
    err = ev.get("error")
    if isinstance(err, dict):
        data = err.get("data")
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])
        for k in ("message", "name", "detail"):
            if err.get(k):
                return str(err[k])
        return json.dumps(err)
    if isinstance(err, str) and err.strip():
        return err.strip()
    for k in ("message", "detail"):
        if ev.get(k):
            return str(ev[k])
    return "OpenCode returned an error event"


class OpenCodeAgent(AgentProvider):
    """Host-mode OpenCode CLI runtime (`opencode run --format json`)."""

    name = "opencode"
    needs_auth_proxy = False

    def __init__(
        self,
        *,
        opencode_bin: str,
        model: str | None = None,
        agent_name: str | None = None,
        auditor_model: str | None = None,
        worker_model: str | None = None,
        base_env: dict[str, str],
    ):
        self.opencode_bin = opencode_bin
        self.models = parse_model_list(model)
        self.model = self.models[0] if self.models else None
        self.agent_name = agent_name or None
        self.auditor_models = parse_model_list(auditor_model) or list(self.models)
        self.auditor_model = self.auditor_models[0] if self.auditor_models else None
        worker_models = parse_model_list(worker_model)
        self.worker_model = worker_models[0] if worker_models else None
        self.base_env = base_env

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "OpenCodeAgent":
        # Strip bridge-only secrets so agent subprocesses never inherit them,
        # while forwarding OPENROUTER_API_KEY (and any other provider keys) from env.
        base = {k: v for k, v in os.environ.items() if k not in BRIDGE_SECRET_KEYS}
        for key in ("OPENROUTER_API_KEY", "OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT"):
            if env.get(key):
                base[key] = env[key]
        opencode_bin = (
            env.get("BRIDGE_OPENCODE_BIN")
            or shutil.which("opencode")
            or "opencode"
        )
        return cls(
            opencode_bin=opencode_bin,
            model=env.get("BRIDGE_OPENCODE_MODEL"),
            agent_name=env.get("BRIDGE_OPENCODE_AGENT"),
            auditor_model=env.get("BRIDGE_AUDITOR_MODEL"),
            worker_model=env.get("BRIDGE_WORKER_MODEL"),
            base_env=base,
        )

    def _config_content(self, base: str | dict) -> str:
        """Build OPENCODE_CONFIG_CONTENT, pinning subagent models when configured."""
        cfg = json.loads(base) if isinstance(base, str) else dict(base)
        if self.worker_model:
            cfg["agents"] = {
                name: {"model": self.worker_model} for name in WORKER_AGENTS
            }
        return json.dumps(cfg)

    def _build_cmd(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        resume: bool = False,
        model: str | None = None,
        agent_name: str | None = None,
    ) -> list[str]:
        cmd = [self.opencode_bin, "run", "--format", "json"]
        if resume and session_id:
            cmd += ["--session", session_id]
        if model:
            cmd += ["--model", model]
        if agent_name:
            cmd += ["--agent", agent_name]
        cmd.append(prompt)
        return cmd

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
        default_model: str | None = None,
    ) -> AgentResult:
        session_id = known_session_id
        log_file = None

        def open_log(sid: str) -> None:
            nonlocal log_file
            if log_file is None and log_dir and sid:
                log_dir.mkdir(parents=True, exist_ok=True)
                log_file = open(log_dir / f"{sid}.jsonl", "a", encoding="utf-8")

        def emit_subtask(label: str, done: bool) -> None:
            if not on_subtask or not label:
                return
            try:
                on_subtask(SUBTASK_LIST, label, done)
            except Exception:
                log.exception("on_subtask callback failed")

        if session_id and log_dir:
            open_log(session_id)

        with tempfile.TemporaryFile("w+", encoding="utf-8") as err:
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
            step_idx = 0
            step_texts: dict[int, list[str]] = {}
            last_text_step: int | None = None
            all_texts: list[str] = []
            errors: list[str] = []
            detected_model: str | None = None
            saw_event = False
            stop_ticker = threading.Event()

            def ticker():
                while not stop_ticker.wait(PROGRESS_EVERY):
                    if on_progress:
                        try:
                            on_progress(int(time.time() - started), steps, last_step or "working…")
                        except Exception:
                            log.exception("on_progress failed")

            threading.Thread(target=ticker, daemon=True).start()

            try:
                assert p.stdout is not None
                for raw in p.stdout:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(ev, dict):
                        continue
                    saw_event = True

                    sid = ev.get("sessionID") or ev.get("session_id")
                    if sid and not session_id:
                        session_id = str(sid)
                        open_log(session_id)
                        if on_session_id:
                            try:
                                on_session_id(session_id)
                            except Exception:
                                log.exception("on_session_id callback failed")
                    elif sid and log_file is None:
                        open_log(str(sid))

                    if log_file is not None:
                        log_file.write(line + "\n")
                        log_file.flush()

                    ev_type = str(ev.get("type") or "").strip().lower()
                    part = ev.get("part") if isinstance(ev.get("part"), dict) else {}

                    # Capture model name if reported in stream events.
                    for m_candidate in (
                        part.get("modelID"),
                        part.get("model"),
                        ev.get("modelID"),
                        ev.get("model"),
                    ):
                        if isinstance(m_candidate, str) and m_candidate.strip():
                            detected_model = m_candidate.strip()
                            break

                    if ev_type == "step_start":
                        step_idx += 1
                    elif ev_type == "text":
                        txt = part.get("text")
                        if isinstance(txt, str) and txt:
                            step_texts.setdefault(step_idx, []).append(txt)
                            all_texts.append(txt)
                            if txt.strip():
                                last_text_step = step_idx
                    elif ev_type == "tool_use":
                        steps += 1
                        tool_name = str(part.get("tool") or part.get("name") or "tool").strip()
                        state = part.get("state") if isinstance(part.get("state"), dict) else {}
                        last_step = _describe_tool(tool_name, state)
                        status = str(state.get("status") or "completed").lower()
                        done = status in ("completed", "done")
                        inp = state.get("input") if isinstance(state.get("input"), dict) else {}
                        tlow = tool_name.lower()
                        if tlow == "task":
                            desc = str(inp.get("description") or inp.get("subagent_type") or "subagent").strip()
                            stype = str(inp.get("subagent_type") or "").strip()
                            lbl = f"[subagent] {desc}" + (
                                f" ({stype})" if stype and stype.lower() not in desc.lower() else ""
                            )
                            emit_subtask(lbl[:160], done)
                        elif tlow == "todowrite":
                            todos = inp.get("todos")
                            if isinstance(todos, list):
                                for item in todos:
                                    if not isinstance(item, dict):
                                        continue
                                    content = str(item.get("content") or "").strip()
                                    if not content:
                                        continue
                                    istatus = str(item.get("status") or "").lower()
                                    emit_subtask(content[:160], istatus in ("completed", "done"))
                    elif ev_type == "error":
                        errors.append(_extract_error_text(ev))
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

            model_label = _normalize_model_label(detected_model or default_model or self.model)

            if not saw_event and p.returncode != 0:
                err.seek(0)
                stderr_tail = err.read()[-2000:].strip()
                return AgentResult(
                    text=f"⚠️ OpenCode failed (exit {p.returncode}):\n\n```\n{stderr_tail}\n```",
                    session_id=session_id,
                    is_error=True,
                    model=model_label,
                )

            final_step_text = (
                "".join(step_texts.get(last_text_step, [])).strip()
                if last_text_step is not None
                else ""
            )
            text = final_step_text or "".join(all_texts).strip()

            if errors:
                detail = "\n".join(errors)
                return AgentResult(
                    text=f"⚠️ OpenCode ended with error:\n\n{detail}",
                    session_id=session_id,
                    is_error=True,
                    model=model_label,
                )

            if p.returncode != 0 and not text:
                err.seek(0)
                stderr_tail = err.read()[-2000:].strip()
                return AgentResult(
                    text=f"⚠️ OpenCode failed (exit {p.returncode}):\n\n```\n{stderr_tail}\n```",
                    session_id=session_id,
                    is_error=True,
                    model=model_label,
                )

            return AgentResult(
                text=text or "(no output)",
                session_id=session_id,
                is_error=False,
                model=model_label,
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
        model_override: str | None = None,
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

        env = {**self.base_env, "GIT_TERMINAL_PROMPT": "0", **env_extra}
        env.setdefault("OPENCODE_CONFIG_CONTENT", self._config_content(TASK_PERMISSION_CONFIG))
        candidates = resolve_model_candidates(model_override, self.models)
        attempt_session_id = session_id
        attempt_resume = resume
        last_res: AgentResult | None = None
        for idx, effective_model in enumerate(candidates):
            cmd = self._build_cmd(
                full_prompt,
                session_id=attempt_session_id,
                resume=attempt_resume,
                model=effective_model,
                agent_name=self.agent_name,
            )
            log.info(
                "task %s: running opencode (%s%s) in %s",
                task.id,
                f"resume {attempt_session_id[:12]}" if attempt_resume and attempt_session_id else "new",
                f", model={effective_model}" if effective_model else "",
                cwd,
            )
            res = self._run_streaming(
                cmd=cmd,
                cwd=cwd,
                env=env,
                timeout=timeout,
                procs=procs,
                on_progress=on_progress,
                on_session_id=on_session_id,
                on_subtask=on_subtask,
                log_dir=log_dir,
                known_session_id=attempt_session_id if attempt_resume else None,
                default_model=effective_model,
            )
            last_res = res
            if (
                res.is_error
                and idx + 1 < len(candidates)
                and is_model_fallback_error(res.text)
            ):
                next_model = candidates[idx + 1]
                log.warning(
                    "task %s: opencode model %r failed; retrying with %r",
                    task.id,
                    effective_model or "default",
                    next_model or "default",
                )
                if res.session_id and self.extract_tool_calls(res.session_id, log_dir):
                    attempt_session_id = res.session_id
                    attempt_resume = True
                continue
            return res
        assert last_res is not None
        return last_res

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
        env = {
            **self.base_env,
            "GIT_TERMINAL_PROMPT": "0",
            "OPENCODE_CONFIG_CONTENT": self._config_content(AUDIT_PERMISSION_CONFIG),
        }
        candidates: list[str | None] = list(self.auditor_models) or [None]
        last_res: AgentResult | None = None
        for idx, audit_model in enumerate(candidates):
            cmd = self._build_cmd(full_prompt, model=audit_model)
            res = self._run_streaming(
                cmd=cmd,
                cwd=cwd,
                env=env,
                timeout=timeout,
                procs=procs,
                default_model=audit_model,
            )
            last_res = res
            if (
                res.is_error
                and idx + 1 < len(candidates)
                and is_model_fallback_error(res.text)
            ):
                next_model = candidates[idx + 1]
                log.warning(
                    "task %s: opencode auditor model %r failed; retrying with %r",
                    task.id,
                    audit_model or "default",
                    next_model or "default",
                )
                continue
            return res.text
        assert last_res is not None
        return last_res.text

    def extract_tool_calls(self, session_id: str, log_dir: Path) -> list[str]:
        path = log_dir / f"{session_id}.jsonl"
        if not path.is_file():
            return []
        calls: list[str] = []
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if str(ev.get("type") or "").lower() != "tool_use":
                continue
            part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
            tool_name = str(part.get("tool") or part.get("name") or "?").strip()
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            status = str(state.get("status") or "").lower()
            suffix = f" [{status}]" if status and status not in ("completed", "done") else ""
            calls.append(_describe_tool(tool_name, state) + suffix)
        return calls

    def extract_subagent_audits(
        self, session_id: str, log_dir: Path, task_id: str | None = None
    ) -> list[SubagentAudit]:
        path = log_dir / f"{session_id}.jsonl"
        if not path.is_file():
            return []
        audits: list[SubagentAudit] = []
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if str(ev.get("type") or "").lower() != "tool_use":
                continue
            part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
            tool_name = str(part.get("tool") or part.get("name") or "").strip().lower()
            if tool_name != "task":
                continue
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            inp = state.get("input") if isinstance(state.get("input"), dict) else {}
            call_id = str(part.get("callID") or part.get("id") or f"sub-{len(audits) + 1}")
            desc = str(inp.get("description") or inp.get("subagent_type") or "subagent").strip()
            stype = str(inp.get("subagent_type") or "").strip()
            name = f"{desc} ({stype})" if stype and stype.lower() not in desc.lower() else desc
            prompt = str(inp.get("prompt") or "").strip()
            output = str(state.get("output") or "").strip()
            audits.append(
                SubagentAudit(
                    id=call_id,
                    name=name,
                    prompt=prompt[:800],
                    tool_calls=[],
                    final_report=output[:1500],
                )
            )
        return audits

    def extract_usage_summary(
        self, session_id: str, log_dir: Path, task_id: str | None = None
    ) -> UsageSummary:
        path = log_dir / f"{session_id}.jsonl"
        if not path.is_file():
            return UsageSummary()

        by_model: dict[str, ModelUsage] = {}
        reported_cost = 0.0
        has_reported_cost = False
        default_m = _normalize_model_label(self.model) or "opencode"

        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if str(ev.get("type") or "").lower() != "step_finish":
                continue
            part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
            raw_m = part.get("modelID") or part.get("model") or ev.get("modelID") or ev.get("model")
            m = _normalize_model_label(str(raw_m)) if raw_m else default_m
            m = m or default_m
            tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}

            inp_tok = int(tokens.get("input") or tokens.get("prompt") or 0)
            out_tok = int(tokens.get("output") or tokens.get("completion") or 0)
            cr_tok = int(cache.get("read") or tokens.get("cache_read") or 0)
            cw_tok = int(cache.get("write") or tokens.get("cache_write") or 0)

            u = by_model.setdefault(m, ModelUsage(model=m))
            u.input_tokens += inp_tok
            u.output_tokens += out_tok
            u.cache_read_tokens += cr_tok
            u.cache_write_tokens += cw_tok

            cost_val = part.get("cost")
            if isinstance(cost_val, (int, float)) and cost_val >= 0:
                has_reported_cost = True
                reported_cost += float(cost_val)
                u.reported_cost_usd = (u.reported_cost_usd or 0.0) + float(cost_val)

        models = list(by_model.values())
        est_parts = [estimate_model_cost_usd(u, env=self.base_env) for u in models]
        known_est = [c for c in est_parts if c is not None]
        estimated = round(sum(known_est), 6) if known_est else None
        return UsageSummary(
            models=models,
            subagent_models=[],
            reported_cost_usd=round(reported_cost, 6) if has_reported_cost else None,
            estimated_cost_usd=estimated,
            cost_note="reported by OpenCode" if has_reported_cost else "estimated via OpenRouter pricing",
        )
