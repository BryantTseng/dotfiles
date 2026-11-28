"""Interactive HTML report & Q&A page helpers for kanban-bridge."""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_QUESTIONS_HEAD_RE = re.compile(r"^\s*QUESTIONS\s*:\s*$", re.I)
_Q_ITEM_RE = re.compile(r"^\s*(?:(\d+)[.)]|[-*])\s+(.+?)\s*$")
_Q_OPT_RE = re.compile(r"^\s{2,}[-*]\s+(.+?)\s*$")
_REC_TAG_RE = re.compile(r"\s*[\[(]\s*recommended\s*[\])]\s*$", re.I)
_MULTI_TAG_RE = re.compile(r"\s*[\[(]\s*multi(?:-select)?\s*[\])]\s*", re.I)


def extract_questions(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Extract a ``QUESTIONS:`` block from an agent reply.

    Format::

        QUESTIONS:
        1. Question prompt?
           - Option A [recommended]
           - Option B
        2. Multi-select question? [multi]
           - Choice 1
           - Choice 2

    Returns ``(cleaned_text, questions)``.
    """
    lines = text.splitlines()
    kept: list[str] = []
    questions: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        if _QUESTIONS_HEAD_RE.match(lines[i]):
            parsed: list[dict[str, Any]] = []
            j = i + 1
            while j < len(lines):
                line = lines[j]
                if not line.strip():
                    j += 1
                    continue
                om = _Q_OPT_RE.match(line)
                if om and parsed:
                    raw_opt = om.group(1).strip()
                    rec = bool(_REC_TAG_RE.search(raw_opt))
                    label = _REC_TAG_RE.sub("", raw_opt).strip()
                    parsed[-1]["options"].append({"label": label, "recommended": rec})
                    j += 1
                    continue
                qm = _Q_ITEM_RE.match(line)
                if qm and not line.startswith("  "):
                    raw_q = qm.group(2).strip()
                    multi = bool(_MULTI_TAG_RE.search(raw_q))
                    prompt = _MULTI_TAG_RE.sub(" ", raw_q).strip()
                    parsed.append(
                        {
                            "id": f"q{len(parsed) + 1}",
                            "prompt": prompt,
                            "options": [],
                            "multi": multi,
                        }
                    )
                    j += 1
                    continue
                break
            if parsed:
                questions.extend(parsed)
                i = j
                continue
        kept.append(lines[i])
        i += 1
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return cleaned, questions


def sign_report(secret: str, task_id: str, comment_id: str) -> str:
    """Return an HMAC-SHA256 signature for a task report URL."""
    msg = f"report|{task_id}|{comment_id}"
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]


def verify_report_sig(secret: str, task_id: str, comment_id: str, sig: str) -> bool:
    """Constant-time verification of a report URL signature."""
    expected = sign_report(secret, task_id, comment_id)
    return hmac.compare_digest(sig or "", expected)


def report_link(base_url: str, secret: str, task_id: str, comment_id: str) -> str:
    """Build a signed ``/report?...`` URL."""
    q = urllib.parse.urlencode(
        {
            "t": task_id,
            "c": comment_id,
            "s": sign_report(secret, task_id, comment_id),
        }
    )
    return f"{base_url.rstrip('/')}/report?{q}"


def save_report(reports_dir: Path, report: dict[str, Any]) -> Path:
    """Persist a structured report JSON file for ``task_id`` + ``comment_id``."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    task_id = str(report["task_id"])
    comment_id = str(report["comment_id"])
    payload = {
        **report,
        "saved_at": report.get("saved_at") or datetime.now(timezone.utc).isoformat(),
    }
    path = reports_dir / f"{task_id}_{comment_id}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    latest = reports_dir / f"{task_id}_latest.json"
    latest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load_report(reports_dir: Path, task_id: str, comment_id: str = "") -> dict[str, Any] | None:
    """Load a saved report JSON by ``task_id`` and optional ``comment_id``."""
    if not reports_dir.exists():
        return None
    candidates = []
    if comment_id:
        candidates.append(reports_dir / f"{task_id}_{comment_id}.json")
    candidates.append(reports_dir / f"{task_id}_latest.json")
    for p in candidates:
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return None
    return None


def _extract_tldr(markdown_text: str, max_lines: int = 14, max_chars: int = 900) -> str:
    """Extract a concise executive summary from the top of a Markdown report."""
    lines = [ln for ln in markdown_text.strip().splitlines()]
    if not lines:
        return ""
    out: list[str] = []
    chars = 0
    for ln in lines:
        if len(out) >= max_lines or chars + len(ln) > max_chars:
            break
        out.append(ln)
        chars += len(ln) + 1
    return "\n".join(out).rstrip()


def format_planka_summary(
    reply: str,
    questions: list[dict[str, Any]],
    url: str,
    status: str,
    *,
    long_line_threshold: int = 24,
) -> str:
    """Build a concise Planka comment with a prominent link to the full report.

    Short replies without ``QUESTIONS:`` are kept inline with a compact report link.
    Long replies or replies with ``QUESTIONS:`` are condensed to an executive summary
    plus a clear action link so the Planka modal stays scannable.
    """
    lines = reply.splitlines()
    is_long = len(lines) > long_line_threshold or len(reply) > 1400
    if not questions and not is_long:
        return f"{reply.rstrip()}\n\n[📊 Open Report]({url})" if reply.strip() else f"[📊 Open Report]({url})"

    summary = _extract_tldr(reply)
    parts: list[str] = []
    if summary:
        parts.append(summary)
        if is_long:
            parts.append("…")

    if questions:
        q_lines = [f"- **{i}.** {q['prompt']}" for i, q in enumerate(questions, 1)]
        parts.append("**Questions awaiting your input:**\n" + "\n".join(q_lines))
        parts.append(f"👉 **[📝 Open Report & Answer Questions ({len(questions)})]({url})**")
    elif status == "DONE":
        parts.append(f"👉 **[📊 Open Full Report & Review Actions]({url})**")
    else:
        parts.append(f"👉 **[📊 Open Full Report]({url})**")
    return "\n\n".join(parts)


# ---------- lightweight, offline, XSS-safe Markdown -> HTML ----------

_SAFE_LINK_RE = re.compile(r"^(?:https?://|file://|/|#)", re.I)
_INLINE_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")


def _render_inline(text: str) -> str:
    """Escape HTML and render inline code, links, bold, and italic safely."""
    tokens: list[str] = []

    def _stash(html_snippet: str) -> str:
        idx = len(tokens)
        tokens.append(html_snippet)
        return f"\x00TOK{idx}\x00"

    def _code_sub(m: re.Match) -> str:
        return _stash(f"<code>{html.escape(m.group(1))}</code>")

    def _link_sub(m: re.Match) -> str:
        label = html.escape(m.group(1))
        href = m.group(2).strip()
        if not _SAFE_LINK_RE.match(href):
            return label
        return _stash(f'<a href="{html.escape(href, quote=True)}" target="_blank" rel="noopener">{label}</a>')

    work = _INLINE_CODE_RE.sub(_code_sub, text)
    work = _INLINE_LINK_RE.sub(_link_sub, work)
    work = html.escape(work)
    work = _BOLD_RE.sub(r"<strong>\1</strong>", work)
    work = _ITALIC_RE.sub(r"<em>\1</em>", work)
    for idx, tok in enumerate(tokens):
        work = work.replace(f"\x00TOK{idx}\x00", tok)
    return work


def _is_table_sep(line: str) -> bool:
    stripped = line.strip()
    if "|" not in stripped:
        return False
    cells = [c.strip() for c in stripped.strip("|").split("|")]
    return bool(cells) and all(re.match(r"^:?-{2,}:?$", c) for c in cells)


def _split_table_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def markdown_to_html(md: str) -> str:
    """Convert Markdown (headings, tables, lists, code blocks, quotes) to safe HTML."""
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # Fenced code block
        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # consume closing ```
            code_body = "\n".join(code_lines)
            if lang.lower() == "mermaid":
                out.append(
                    '<div class="mermaid-wrap">'
                    f'<pre class="mermaid">{html.escape(code_body)}</pre>'
                    "</div>"
                )
            else:
                cls = f' class="lang-{html.escape(lang)}"' if lang else ""
                out.append(f"<pre><code{cls}>{html.escape(code_body)}</code></pre>")
            continue

        # Horizontal rule
        if re.match(r"^(?:---|\*\*\*|___)\s*$", stripped):
            out.append("<hr>")
            i += 1
            continue

        # Headings
        hm = re.match(r"^(#{1,4})\s+(.+)$", stripped)
        if hm:
            lvl = len(hm.group(1))
            out.append(f"<h{lvl}>{_render_inline(hm.group(2))}</h{lvl}>")
            i += 1
            continue

        # Markdown table
        if "|" in stripped and i + 1 < len(lines) and _is_table_sep(lines[i + 1]):
            headers = _split_table_row(line)
            i += 2
            rows: list[list[str]] = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_split_table_row(lines[i]))
                i += 1
            ths = "".join(f"<th>{_render_inline(h)}</th>" for h in headers)
            trs = []
            for r in rows:
                padded = r + [""] * max(0, len(headers) - len(r))
                tds = "".join(f"<td>{_render_inline(c)}</td>" for c in padded[: len(headers)])
                trs.append(f"<tr>{tds}</tr>")
            out.append(f'<div class="table-wrap"><table><thead><tr>{ths}</tr></thead><tbody>{"".join(trs)}</tbody></table></div>')
            continue

        # Blockquote
        if stripped.startswith(">"):
            qlines: list[str] = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                qlines.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append(f"<blockquote>{_render_inline(' '.join(qlines))}</blockquote>")
            continue

        # Unordered / ordered list
        if re.match(r"^(?:[-*]|\d+[.)])\s+", stripped):
            ordered = bool(re.match(r"^\d+[.)]\s+", stripped))
            tag = "ol" if ordered else "ul"
            items: list[str] = []
            while i < len(lines):
                s = lines[i].strip()
                m = re.match(r"^(?:\d+[.)]|[-*])\s+(.+)$", s)
                if not m:
                    break
                items.append(f"<li>{_render_inline(m.group(1))}</li>")
                i += 1
            out.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue

        # Paragraph
        plines: list[str] = []
        while i < len(lines):
            s = lines[i].strip()
            if (
                not s
                or s.startswith("```")
                or s.startswith("#")
                or s.startswith(">")
                or re.match(r"^(?:---|\*\*\*|___)\s*$", s)
                or re.match(r"^(?:[-*]|\d+[.)])\s+", s)
                or ("|" in s and i + 1 < len(lines) and _is_table_sep(lines[i + 1]))
            ):
                break
            plines.append(s)
            i += 1
        out.append(f"<p>{_render_inline(' '.join(plines))}</p>")

    return "\n".join(out)


def format_qa_reply(questions: list[dict[str, Any]], form: dict[str, list[str]]) -> str:
    """Format submitted form answers into a clean numbered Planka user comment."""
    action = (form.get("action") or ["submit"])[0]
    if action == "proceed_defaults":
        lines = ["Proceeding with recommended defaults:"]
        for idx, q in enumerate(questions, 1):
            recs = [o["label"] for o in q.get("options", []) if o.get("recommended")]
            if recs:
                lines.append(f"{idx}. {', '.join(recs)}")
        if len(lines) == 1:
            return "proceed"
        return "\n".join(lines)

    if action == "free_reply":
        note = (form.get("free_reply_text") or [""])[0].strip()
        return note or "proceed"

    lines: list[str] = []
    for idx, q in enumerate(questions, 1):
        qid = q["id"]
        chosen = [v.strip() for v in form.get(qid, []) if v.strip()]
        custom = (form.get(f"{qid}_note") or [""])[0].strip()
        ans_parts = []
        if chosen:
            ans_parts.append(", ".join(chosen))
        if custom:
            ans_parts.append(custom)
        if ans_parts:
            lines.append(f"{idx}. {' — '.join(ans_parts)}")
    if not lines:
        return "proceed"
    return "\n".join(lines)


def render_report_page(
    report: dict[str, Any],
    *,
    card_url: str,
    still_open: bool,
    flash: str = "",
) -> str:
    """Render the interactive HTML Task Report & Q&A page."""
    title = str(report.get("title") or f"Task #{report.get('task_id', '')}")
    status = str(report.get("status") or "NEEDS_INPUT")
    mode = str(report.get("mode") or "")
    agent_name = str(report.get("agent_name") or "Agent")
    model = str(report.get("model") or "")
    git_footer = str(report.get("git_footer") or "")
    raw_md = str(report.get("raw_markdown") or "")
    questions = report.get("questions") or []

    badge_cls = "badge-done" if status == "DONE" else "badge-input"
    badge_txt = "Ready for Review" if status == "DONE" else "Needs Your Input"

    meta_bits = [f'<span class="badge {badge_cls}">{html.escape(badge_txt)}</span>']
    if mode:
        meta_bits.append(f'<span class="pill">{html.escape(mode)}</span>')
    if model:
        meta_bits.append(f'<span class="pill">{html.escape(agent_name)} · {html.escape(model)}</span>')
    if git_footer:
        meta_bits.append(f'<span class="pill">{html.escape(git_footer)}</span>')

    flash_html = f'<div class="flash">{html.escape(flash)}</div>' if flash else ""
    body_html = markdown_to_html(raw_md)
    attachments = report.get("attachments") or []
    att_html = ""
    if attachments:
        cards = []
        for att in attachments:
            name = str(att.get("name") or "attachment")
            aurl = str(att.get("url") or "")
            is_img = bool(re.search(r"\.(?:png|jpe?g|gif|webp|svg)$", name, re.I))
            if is_img and aurl:
                cards.append(
                    f'<figure class="att-card">'
                    f'<a href="{html.escape(aurl, quote=True)}" target="_blank" rel="noopener">'
                    f'<img src="{html.escape(aurl, quote=True)}" alt="{html.escape(name, quote=True)}" loading="lazy">'
                    f"</a><figcaption>{html.escape(name)}</figcaption></figure>"
                )
            elif aurl:
                cards.append(
                    f'<a class="pill" href="{html.escape(aurl, quote=True)}" target="_blank" rel="noopener">📎 {html.escape(name)}</a>'
                )
        if cards:
            att_html = f'<div class="att-section"><h3>Attachments &amp; Visuals</h3><div class="att-grid">{"".join(cards)}</div></div>'

    # Interactive Q&A / Review panel
    if not still_open:
        panel_html = (
            '<div class="panel muted-panel">'
            "<h3>Action status</h3>"
            "<p>This report has already been answered or superseded by a newer comment.</p>"
            f'<p><a class="btn-link" href="{html.escape(card_url, quote=True)}">Back to card</a></p>'
            "</div>"
        )
    elif questions:
        q_cards: list[str] = []
        for idx, q in enumerate(questions, 1):
            qid = html.escape(q["id"])
            qprompt = _render_inline(q["prompt"])
            opts = q.get("options") or []
            multi = bool(q.get("multi"))
            inp_type = "checkbox" if multi else "radio"
            has_rec = any(o.get("recommended") for o in opts)
            opt_html: list[str] = []
            for o_idx, opt in enumerate(opts):
                checked = " checked" if (opt.get("recommended") or (not has_rec and o_idx == 0 and not multi)) else ""
                rec_badge = ' <span class="rec-tag">Recommended</span>' if opt.get("recommended") else ""
                val = html.escape(opt["label"], quote=True)
                opt_html.append(
                    f'<label class="opt">'
                    f'<input type="{inp_type}" name="{qid}" value="{val}"{checked}>'
                    f'<span>{_render_inline(opt["label"])}{rec_badge}</span>'
                    f"</label>"
                )
            placeholder = "Your answer..." if not opts else "Optional note or override..."
            opt_html.append(
                f'<input class="note-input" type="text" name="{qid}_note" placeholder="{placeholder}">'
            )
            q_cards.append(
                f'<div class="q-card">'
                f'<div class="q-title"><span class="q-num">{idx}</span><div>{qprompt}</div></div>'
                f'<div class="q-opts">{"".join(opt_html)}</div>'
                f"</div>"
            )
        panel_html = (
            '<form method="post" class="panel">'
            "<h3>Your Decisions</h3>"
            + "".join(q_cards)
            + '<div class="actions">'
            + '<button type="submit" name="action" value="submit" class="btn-primary">Submit Answers &amp; Resume</button>'
            + '<button type="submit" name="action" value="proceed_defaults" class="btn-secondary">Proceed with Defaults</button>'
            + "</div></form>"
        )
    elif status == "DONE":
        panel_html = (
            '<form method="post" class="panel">'
            "<h3>Review Actions</h3>"
            '<textarea name="free_reply_text" rows="3" placeholder="Optional feedback or follow-up instructions..."></textarea>'
            '<div class="actions">'
            '<button type="submit" name="action" value="free_reply" class="btn-primary">Post Follow-up</button>'
            '<button type="submit" name="action" value="audit" class="btn-secondary">🔍 Send to Auditing</button>'
            '<button type="submit" name="action" value="mark_done" class="btn-success">✅ Mark Done</button>'
            "</div></form>"
        )
    else:
        panel_html = (
            '<form method="post" class="panel">'
            "<h3>Reply &amp; Resume</h3>"
            '<textarea name="free_reply_text" rows="4" placeholder="Type your reply (or leave blank to proceed)..."></textarea>'
            '<div class="actions">'
            '<button type="submit" name="action" value="free_reply" class="btn-primary">Send Reply &amp; Resume</button>'
            "</div></form>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} — Report</title>
<style>
  :root {{
    --bg: #151822; --surface: #1e2331; --card: #252b3b; --border: #333b50;
    --text: #e8ecf4; --muted: #9aa4bd; --accent: #5b82ff; --green: #2ea043;
    --amber: #d29922;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font: 15px/1.6 system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--text); }}
  .wrap {{ max-width: 1180px; margin: 0 auto; padding: 1.5rem; }}
  header {{ display: flex; flex-wrap: wrap; justify-content: space-between; align-items: flex-start; gap: 1rem;
            padding-bottom: 1rem; border-bottom: 1px solid var(--border); margin-bottom: 1.5rem; }}
  h1 {{ margin: 0 0 .5rem; font-size: 1.35rem; }}
  .meta {{ display: flex; flex-wrap: wrap; gap: .45rem; align-items: center; }}
  .badge, .pill {{ font-size: .78rem; padding: .2rem .55rem; border-radius: 999px; font-weight: 600; }}
  .badge-input {{ background: rgba(210,153,34,.18); color: #f2c96d; border: 1px solid rgba(210,153,34,.4); }}
  .badge-done {{ background: rgba(46,160,67,.18); color: #6fe387; border: 1px solid rgba(46,160,67,.4); }}
  .pill {{ background: var(--card); color: var(--muted); border: 1px solid var(--border); font-weight: 500; }}
  .btn-link {{ color: #9cb6ff; text-decoration: none; font-weight: 600; white-space: nowrap; }}
  .grid {{ display: grid; grid-template-columns: minmax(0, 1.45fr) minmax(320px, 1fr); gap: 1.5rem; align-items: start; }}
  @media (max-width: 900px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  .report-body {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 1.4rem 1.6rem; overflow-x: auto; }}
  .report-body h1, .report-body h2 {{ font-size: 1.15rem; margin: 1.2rem 0 .5rem; border-bottom: 1px solid var(--border); padding-bottom: .25rem; }}
  .report-body h3, .report-body h4 {{ font-size: 1rem; margin: 1rem 0 .4rem; color: #c7d5ff; }}
  .report-body h1:first-child, .report-body h2:first-child {{ margin-top: 0; }}
  .report-body p {{ margin: .55rem 0; }}
  .report-body ul, .report-body ol {{ margin: .5rem 0; padding-left: 1.4rem; }}
  .report-body li {{ margin: .25rem 0; }}
  .report-body code {{ background: #10131c; padding: .12rem .35rem; border-radius: 4px; font-size: .88em; }}
  .report-body pre {{ background: #10131c; padding: .85rem 1rem; border-radius: 8px; overflow-x: auto; border: 1px solid var(--border); }}
  .report-body pre code {{ background: none; padding: 0; }}
  .report-body blockquote {{ margin: .6rem 0; padding: .4rem .9rem; border-left: 3px solid var(--accent); background: rgba(91,130,255,.08); color: var(--muted); }}
  .report-body hr {{ border: 0; border-top: 1px solid var(--border); margin: 1rem 0; }}
  .table-wrap {{ overflow-x: auto; margin: .75rem 0; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .9rem; }}
  th, td {{ text-align: left; padding: .5rem .65rem; border: 1px solid var(--border); vertical-align: top; }}
  th {{ background: var(--card); color: #d6e0ff; font-weight: 600; }}
  .panel {{ position: sticky; top: 1rem; background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 1.25rem; }}
  .panel h3 {{ margin: 0 0 1rem; font-size: 1.05rem; }}
  .q-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: .85rem; margin-bottom: .85rem; }}
  .q-title {{ display: flex; gap: .6rem; align-items: flex-start; font-weight: 600; margin-bottom: .55rem; }}
  .q-num {{ flex: 0 0 auto; width: 1.5rem; height: 1.5rem; border-radius: 999px; background: var(--accent); color: #fff;
            display: inline-grid; place-items: center; font-size: .8rem; }}
  .q-opts {{ display: flex; flex-direction: column; gap: .4rem; }}
  .opt {{ display: flex; gap: .5rem; align-items: flex-start; cursor: pointer; padding: .35rem .45rem; border-radius: 6px; }}
  .opt:hover {{ background: rgba(255,255,255,.04); }}
  .opt input {{ margin-top: .25rem; }}
  .rec-tag {{ font-size: .72rem; background: rgba(46,160,67,.2); color: #7ee787; padding: .08rem .4rem; border-radius: 999px; margin-left: .35rem; }}
  .note-input, textarea {{ width: 100%; margin-top: .35rem; padding: .55rem .65rem; border-radius: 8px;
                           border: 1px solid var(--border); background: #10131c; color: var(--text); font: inherit; }}
  .actions {{ display: flex; flex-wrap: wrap; gap: .6rem; margin-top: 1rem; }}
  button {{ font: inherit; font-weight: 600; padding: .6rem 1rem; border: 0; border-radius: 8px; cursor: pointer; color: #fff; }}
  .btn-primary {{ background: var(--accent); }}
  .btn-secondary {{ background: #374057; }}
  .btn-success {{ background: var(--green); }}
  .flash {{ background: rgba(46,160,67,.18); border: 1px solid rgba(46,160,67,.45); color: #8af09e;
            padding: .75rem 1rem; border-radius: 10px; margin-bottom: 1rem; }}
  .mermaid-wrap {{ background: #10131c; border: 1px solid var(--border); border-radius: 10px; padding: .9rem; margin: .85rem 0; overflow-x: auto; }}
  .mermaid-wrap pre.mermaid {{ background: none; border: 0; margin: 0; padding: 0; }}
  .att-section {{ margin-top: 1.25rem; padding-top: 1rem; border-top: 1px solid var(--border); }}
  .att-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: .85rem; margin-top: .6rem; }}
  .att-card {{ margin: 0; background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: .5rem; }}
  .att-card img {{ width: 100%; height: auto; border-radius: 6px; display: block; }}
  .att-card figcaption {{ font-size: .8rem; color: var(--muted); margin-top: .35rem; word-break: break-word; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>{html.escape(title)}</h1>
      <div class="meta">{"".join(meta_bits)}</div>
    </div>
    <a class="btn-link" href="{html.escape(card_url, quote=True)}">← Back to Planka card</a>
  </header>
  {flash_html}
  <div class="grid">
    <section class="report-body">{body_html}{att_html}</section>
    <aside>{panel_html}</aside>
  </div>
</div>
<script type="module">
  if (document.querySelector('.mermaid')) {{
    try {{
      const {{ default: mermaid }} = await import('https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs');
      mermaid.initialize({{ startOnLoad: false, theme: 'dark' }});
      await mermaid.run({{ querySelector: '.mermaid' }});
    }} catch (_) {{ /* offline fallback: raw <pre class="mermaid"> stays visible */ }}
  }}
</script>
</body>
</html>"""
