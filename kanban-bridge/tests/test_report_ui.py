"""Unit tests for report_ui (interactive report & Q&A helpers)."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import report_ui


class ReportUiTests(unittest.TestCase):
    def test_extract_questions_parses_options_recommended_multi_and_freetext(self):
        src = (
            "## TL;DR\nProposed plan.\n\n"
            "QUESTIONS:\n"
            "1. Remove dead handlers?\n"
            "   - Remove both files [recommended]\n"
            "   - Keep and wire them\n"
            "2. Extra checks to run? [multi]\n"
            "   - clippy\n"
            "   - fmt\n"
            "3. Any deployment notes?\n"
            "\nTrailing text."
        )
        cleaned, qs = report_ui.extract_questions(src)
        self.assertNotIn("QUESTIONS:", cleaned)
        self.assertIn("## TL;DR", cleaned)
        self.assertIn("Trailing text.", cleaned)
        self.assertEqual(len(qs), 3)

        self.assertEqual(qs[0]["id"], "q1")
        self.assertFalse(qs[0]["multi"])
        self.assertEqual(
            qs[0]["options"],
            [
                {"label": "Remove both files", "recommended": True},
                {"label": "Keep and wire them", "recommended": False},
            ],
        )
        self.assertTrue(qs[1]["multi"])
        self.assertEqual(len(qs[1]["options"]), 2)
        self.assertEqual(qs[2]["options"], [])

    def test_sign_and_verify_report_link(self):
        url = report_ui.report_link("http://127.0.0.1:8787", "sec", "123", "456")
        self.assertIn("/report?", url)
        sig = url.split("s=")[-1]
        self.assertTrue(report_ui.verify_report_sig("sec", "123", "456", sig))
        self.assertFalse(report_ui.verify_report_sig("sec", "123", "999", sig))
        self.assertFalse(report_ui.verify_report_sig("wrong", "123", "456", sig))

    def test_save_and_load_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            rdir = Path(tmp) / "reports"
            report_ui.save_report(
                rdir,
                {
                    "task_id": "t1",
                    "comment_id": "c1",
                    "title": "Test card",
                    "raw_markdown": "## TL;DR\nDone.",
                    "questions": [],
                },
            )
            loaded = report_ui.load_report(rdir, "t1", "c1")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["title"], "Test card")
            latest = report_ui.load_report(rdir, "t1")
            self.assertIsNotNone(latest)
            self.assertEqual(latest["comment_id"], "c1")

    def test_format_planka_summary_short_vs_questions_vs_long(self):
        short = report_ui.format_planka_summary("All good.", [], "http://r/1", "DONE")
        self.assertIn("All good.", short)
        self.assertIn("[📊 Open Report](http://r/1)", short)

        qs = [{"id": "q1", "prompt": "Proceed with A?", "options": [], "multi": False}]
        with_q = report_ui.format_planka_summary("## TL;DR\nPlan.", qs, "http://r/2", "NEEDS_INPUT")
        self.assertIn("Open Report & Answer Questions (1)", with_q)
        self.assertIn("Proceed with A?", with_q)

        long_md = "\n".join(f"Line {i}" for i in range(40))
        long_sum = report_ui.format_planka_summary(long_md, [], "http://r/3", "DONE")
        self.assertIn("Open Full Report & Review Actions", long_sum)
        self.assertLess(len(long_sum), len(long_md))

    def test_markdown_to_html_tables_mermaid_and_xss_escape(self):
        md = (
            "# Title <script>alert(1)</script>\n\n"
            "| File | Status |\n"
            "| --- | --- |\n"
            "| `a.py` | **OK** |\n\n"
            "```mermaid\nflowchart LR\n  A --> B\n```\n"
        )
        out = report_ui.markdown_to_html(md)
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("<table>", out)
        self.assertIn("<code>a.py</code>", out)
        self.assertIn("<strong>OK</strong>", out)
        self.assertIn('<pre class="mermaid">', out)

    def test_format_qa_reply_and_render_report_page(self):
        qs = [
            {
                "id": "q1",
                "prompt": "Pick one",
                "options": [
                    {"label": "Alpha", "recommended": True},
                    {"label": "Beta", "recommended": False},
                ],
                "multi": False,
            },
            {"id": "q2", "prompt": "Notes?", "options": [], "multi": False},
        ]
        reply = report_ui.format_qa_reply(qs, {"action": ["submit"], "q1": ["Beta"], "q2_note": ["Ship it"]})
        self.assertEqual(reply, "1. Beta\n2. Ship it")

        defaults = report_ui.format_qa_reply(qs, {"action": ["proceed_defaults"]})
        self.assertIn("1. Alpha", defaults)

        html_page = report_ui.render_report_page(
            {
                "task_id": "t1",
                "title": "Card 1",
                "status": "NEEDS_INPUT",
                "raw_markdown": "## TL;DR\nHello",
                "questions": qs,
                "attachments": [{"name": "arch.png", "url": "http://localhost:3000/att/arch.png"}],
            },
            card_url="http://localhost:3000/cards/t1",
            still_open=True,
        )
        self.assertIn("Submit Answers", html_page)
        self.assertIn("arch.png", html_page)
        self.assertIn("Recommended", html_page)


if __name__ == "__main__":
    unittest.main()
