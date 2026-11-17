"""Tests for coach/build_site.py. Run: python3 -m unittest discover -s tests -v"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "coach"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import build_site  # noqa: E402
from test_coach import Builder  # noqa: E402

SCRIPT = os.path.join(ROOT, "coach", "build_site.py")
EVIL = '<script>alert(1)</script> <img src=x "onload=alert(2)>'

BASELINE = """# Baseline: test patterns

Intro line with **bold** and `code` and %s.

## Patterns

1. Sessions run too long.
   - one: 51 percent above 600k.
   - Fix: one session per workstream.

2. Drip prompting.
   - Fix: one numbered list.

## Worked well

- Handoff doc.
""" % EVIL

DETAIL = """# Baseline: detail set

| Measure | Value |
|---|---|
| Sessions | 8 |
| Bad | %s |

```
echo "<b>raw</b>"
```
""" % EVIL

DAILY = """# Coach: 2026-11-16

1. What happened: abcd1234, demo project, 09:06: 9 agents started early %s.
   Do instead: hold the build until the owner answers, use `plan mode`.
2. What happened: abcd1234, demo, 12:14: 11 one-line edits (repeat of 2026-11-15).
   Do instead: type the notes as one numbered message.
"""

WEEKLY = """# Coach weekly: 2026-11-16

## Week in numbers
12 sessions (9 last week), 300 prompts, 14 active hours.

## What cost the most
1. What happened: abcd1234 on 16 Nov peaked at 450k context.
   Do instead: open a new session per topic.
   Trend: worse, 450k against 390k last week.
2. What happened: 5 refused publishes.
   Do instead: read the artifact first.
   Trend: new.

## Proposed changes (not applied)
### P1: Warn at 300k
Why: abcd1234 and two others passed 500k.
Change: adds a Stop hook.
```
{"hooks": {"Stop": [{"command": "warn.sh %s"}]}}
```
Where: `~/.claude/settings.json`

### P2: Batch rule
Why: 11 edits sent one at a time.
Change: `Send review notes as one numbered list.`
Where: ~/.claude/CLAUDE.md
"""

DIGEST = {
    "totals": {"sessions": 2, "scheduled_sessions": 1, "turns": 62, "active_min": 240,
               "errors": 14, "corrections": 1, "compactions": 0,
               "tokens": {"out": 1000}, "subagent_tokens": {"out": 500}},
    "sessions": [{"id": "abcd1234-0000-0000-0000-000000000001", "project": "~/demo <b>",
                  "title": "Demo", "kind": "interactive", "start": "2026-11-16 08:15",
                  "active_min": 200, "turns": 62,
                  "tokens": {"out": 1000, "peak_context": 450000},
                  "subagents": {"tokens": {"out": 500}}}],
}
FINDINGS = [
    {"pattern": "context_bloat", "severity": "medium", "session": "abcd1234",
     "evidence": "Context reached about 450k tokens " + EVIL + " " + "x" * 2000,
     "suggestion": "Run /compact."},
    {"pattern": "repeated_prompt", "severity": "low", "session": "",
     "evidence": "Same prompt in 3 sessions", "suggestion": "Make it a skill."},
]


def write(path, text):
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    with open(path, "w") as fh:
        fh.write(text)


class SiteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="coach-site-")
        self.reports = os.path.join(self.tmp, "reports")
        self.projects = os.path.join(self.tmp, "projects")
        self.out = os.path.join(self.tmp, "site", "index.html")
        os.makedirs(self.projects)
        write(os.path.join(self.reports, "baseline", "SUMMARY.md"), BASELINE)
        write(os.path.join(self.reports, "baseline", "detail.md"), DETAIL)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_reports(self):
        write(os.path.join(self.reports, "daily", "2026-11-16.md"), DAILY % EVIL)
        write(os.path.join(self.reports, "daily", "2026-11-15.md"),
              "# Coach: 2026-11-15\n\nNothing notable on 2026-11-15.\n")
        write(os.path.join(self.reports, "weekly", "2026-11-16.md"), WEEKLY % EVIL)
        write(os.path.join(self.reports, "digests", "daily-2026-11-16.json"), json.dumps(DIGEST))
        write(os.path.join(self.reports, "digests", "daily-2026-11-16.findings.json"),
              json.dumps(FINDINGS))

    def run_build(self, *extra):
        p = subprocess.run([sys.executable, SCRIPT, "--reports-dir", self.reports,
                            "--out", self.out, "--projects-dir", self.projects,
                            "--today", "2026-11-17"] + list(extra),
                           env=dict(os.environ, COACH_HOME=os.path.join(self.tmp, "coach-home"),
                                    CLAUDE_CONFIG_DIR=os.path.join(self.tmp, "claude")),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8"))
        with open(self.out, "rb") as fh:
            return fh.read().decode("utf-8"), p.stdout.decode("utf-8")

    def page_data(self, page):
        blob = page.split('<script type="application/json" id="coach-data">', 1)[1]
        return json.loads(blob.split("</script>", 1)[0])

    def check_contract(self, page):
        self.assertTrue(page.startswith("<title>Agentic Coach</title>"))
        self.assertIn('<meta name="claude:viewport-fit-sides" content="cover">', page)
        low = page.lower()
        low = low.replace("alert(1)", "").replace("alert(2)", "")
        for banned in (r"<!doctype", r"<html[\s>]", r"<head[\s>]", r"<body[\s>]", r"<iframe",
                       r"mailto:", r"\balert\(", r"\bconfirm\(", r"window\.print", r"\bfetch\("):
            self.assertIsNone(re.search(banned, low), banned)
        self.assertEqual(page.count("<style"), 1)
        self.assertEqual(page.count("<script"), 2)
        self.assertNotIn(chr(0x2013), page)
        self.assertNotIn(chr(0x2014), page)
        self.assertLess(len(page.encode("utf-8")), 2000000)

    def test_builds_with_only_baseline(self):
        page, out = self.run_build()
        self.assertEqual(out, "")
        self.check_contract(page)
        data = self.page_data(page)
        self.assertEqual(data["dailies"], [])
        self.assertEqual(data["weeklies"], [])
        self.assertIn("No daily recap yet.", page)
        self.assertIn("after the next daily run.", page)
        self.assertIn("after the next weekly run.", page)
        self.assertIsNone(re.search(r"\b\d\d:\d\d\b", page.split("<script", 1)[0].split("</style>")[-1]))
        self.assertIn("No trend data yet.", page)
        self.assertIn("Sessions run too long.", page)
        self.assertIn('<ol class="patterns">', page)
        self.assertIn('<span class="pill fix">Fix</span> one session per workstream.', page)
        self.assertIn("<th>Measure</th>", page)
        self.assertIn("<strong>bold</strong>", page)
        self.assertNotIn('class="onething"', page)

    def test_builds_with_reports(self):
        self.add_reports()
        page, _ = self.run_build()
        self.check_contract(page)
        data = self.page_data(page)
        self.assertEqual([d["date"] for d in data["dailies"]], ["2026-11-16", "2026-11-15"])
        day = data["dailies"][0]
        self.assertEqual(len(day["items"]), 2)
        self.assertEqual(day["items"][0]["ids"], ["abcd1234"])
        self.assertFalse(day["items"][0]["repeat"])
        self.assertTrue(day["items"][1]["repeat"])
        self.assertTrue(day["items"][0]["do"].startswith("Hold the build"))
        self.assertEqual(day["sessions"]["abcd1234"]["peak"], 450000)
        self.assertEqual(day["sessions"]["abcd1234"]["sub_out"], 500)
        self.assertEqual(day["findings"]["abcd1234"][0]["pattern"], "context_bloat")
        self.assertLessEqual(len(day["findings"]["abcd1234"][0]["evidence"]), 320)
        self.assertEqual(len(day["other"]), 1)
        self.assertEqual(data["dailies"][1]["items"], [])
        self.assertIn("Nothing notable", data["dailies"][1]["note"])
        week = data["weeklies"][0]
        self.assertIn("12 sessions", week["numbers"])
        self.assertEqual([i["state"] for i in week["items"]], ["worse", "new"])
        self.assertEqual([p["id"] for p in week["proposals"]], ["P1", "P2"])
        self.assertTrue(week["proposals"][0]["change"].startswith('{"hooks"'))
        self.assertEqual(week["proposals"][0]["note"], "adds a Stop hook.")
        self.assertEqual(week["proposals"][0]["where"], "`~/.claude/settings.json`")
        self.assertEqual(week["proposals"][1]["change"], "Send review notes as one numbered list.")
        self.assertIn('class="onething"', page)
        self.assertIn("Hold the build until the owner answers, use <code>plan mode</code>", page)

    def test_report_text_is_escaped(self):
        self.add_reports()
        page, _ = self.run_build()
        self.assertNotIn("<script>alert", page)
        self.assertNotIn('"onload=', page)
        self.assertNotIn("<img", page)
        self.assertNotIn("<b>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        day = self.page_data(page)["dailies"][0]
        self.assertIn(EVIL, day["items"][0]["what"])

    def test_card_output(self):
        _, out = self.run_build("--card")
        self.assertEqual(json.loads(out), {"date": None, "items": [], "counts": {}})
        self.add_reports()
        _, out = self.run_build("--card")
        self.assertEqual(out.count("\n"), 1)
        card = json.loads(out)
        self.assertEqual(sorted(card.keys()), ["counts", "date", "items"])
        self.assertEqual(card["date"], "2026-11-16")
        self.assertEqual(len(card["items"]), 2)
        for line in card["items"]:
            self.assertIsInstance(line, str)
            self.assertNotIn("\n", line)
            self.assertLessEqual(len(line), 160)
        self.assertEqual(card["counts"]["prompts"], 62)
        self.assertEqual(card["counts"]["items"], 2)

    def test_trend_cache_and_charts(self):
        b = Builder()
        b.prompt("add a logout button to the header").bash("npm test", "ok")
        b.prompt("ok go").prompt("no that is wrong")
        b.write(self.projects)
        page, _ = self.run_build()
        path = os.path.join(self.reports, "digests", "trend.json")
        with open(path) as fh:
            cache = json.load(fh)
        self.assertEqual(len(cache["days"]), 30)
        self.assertNotIn("2026-11-17", cache["days"])
        total = lambda k: sum(d[k] for d in cache["days"].values())  # noqa: E731
        self.assertEqual(total("prompts"), 3)
        self.assertEqual(total("short"), 1)
        self.assertEqual(total("corrections"), 1)
        self.assertEqual(total("sessions"), 1)
        self.assertIn("<svg", page)
        self.assertIn("300k line", page)
        self.assertIn("Too few days to compare yet", page)
        before = os.stat(path).st_mtime_ns
        page2, _ = self.run_build()
        self.assertEqual(page, page2)
        self.assertEqual(os.stat(path).st_mtime_ns, before)
        page3, _ = self.run_build("--no-trends")
        self.assertEqual(page, page3)

    def test_verdict_compares_first_week(self):
        days = {}
        for i in range(1, 15):
            d = build_site.empty_day()
            d.update(sessions=1, prompts=100, active_min=120,
                     peak=800000 if i <= 7 else 250000,
                     short=10 if i <= 7 else 40, corrections=5)
            days["2026-11-%02d" % i] = d
        self.assertEqual(build_site.verdict(days, build_site.HEADLINES[0][2])[0], "better")
        self.assertEqual(build_site.verdict(days, build_site.HEADLINES[1][2])[0], "worse")
        state, _word, text = build_site.verdict(days, build_site.HEADLINES[2][2])
        self.assertEqual(state, "same")
        self.assertIn("1 Nov to 7 Nov", text)

    def test_placeholders_in_report_text_are_not_expanded(self):
        self.add_reports()
        write(os.path.join(self.reports, "baseline", "detail.md"),
              "# D\n\n[here](https://e.x/{{DATA}}) and {{BASELINE}} and {{TRENDS}}\n")
        page, _ = self.run_build()
        self.check_contract(page)
        self.assertIn('href="https://e.x/{{DATA}}"', page)
        self.assertEqual(page.count('id="coach-data"'), 1)

    def test_headings_inside_a_change_block_stay_in_the_proposal(self):
        text = ("# Coach weekly: 2026-11-16\n\n## Proposed changes (not applied)\n"
                "### P1: A skill\nWhy: asked three times.\nChange: a new skill.\n```\n# Title\n## Steps\n1. go\n```\n"
                "Where: a skills folder\n\n### P2: Second\nWhy: w.\nChange: `x`\nWhere: y\n")
        props = build_site.parse_weekly(text)["proposals"]
        self.assertEqual([p["id"] for p in props], ["P1", "P2"])
        self.assertIn("## Steps", props[0]["change"])
        self.assertEqual(props[0]["where"], "a skills folder")
        # a fence left open, or opened on the Change line, does not swallow what follows
        text = ("## Week in numbers\n```10 sessions```\n\n## What cost the most\n1. What happened: x\n"
                "   Do instead: y\n\n## Proposed changes (not applied)\n### P1: One\nWhy: w\n"
                "Change: ```\nline\n\n### P2: Two\nWhy: w\nChange: `z`\nWhere: here\n")
        week = build_site.parse_weekly(text)
        self.assertEqual(len(week["items"]), 1)
        self.assertEqual([p["id"] for p in week["proposals"]], ["P1", "P2"])
        self.assertEqual(week["proposals"][1]["where"], "here")

    def test_malformed_inputs_do_not_stop_the_build(self):
        self.add_reports()
        write(os.path.join(self.reports, "daily", "2026-13-40.md"), "# Coach\n\n1. What happened: x\n")
        write(os.path.join(self.reports, "digests", "daily-2026-11-16.json"), '["not", "a", "digest"]')
        write(os.path.join(self.reports, "digests", "weekly-2026-11-16.json"), '{"totals": "x"}')
        write(os.path.join(self.reports, "digests", "trend.json"),
              '{"version": 1, "days": {"2026-11-10": 5, "2026-11-11": {"sessions": 2, "peak": NaN, '
              '"out": 1e999}, "bad": {}}, "scheduled": "x"}')
        page, _ = self.run_build("--no-trends")
        self.check_contract(page)
        data = self.page_data(page)
        self.assertEqual([d["date"] for d in data["dailies"]], ["2026-11-16", "2026-11-15"])

    def test_markdown_subset(self):
        html = build_site.md_to_html("## H\n\ntext [x](javascript:alert(1)) [y](https://a.b/c)\n\n"
                                     "- a\n  - b\n\n1. one\n2. two\n")
        self.assertIn('<h4 class="md-h2">H</h4>', html)
        self.assertNotIn("javascript:", html.replace("(javascript:", ""))
        self.assertNotIn('href="javascript', html)
        self.assertIn('<a href="https://a.b/c" target="_blank" rel="noopener noreferrer">y</a>', html)
        self.assertIn("<ul><li>a<ul><li>b</li></ul></li></ul>", html)
        self.assertIn('<li value="2">two</li>', html)

    def test_no_long_dashes_in_sources(self):
        for name in ("coach/build_site.py", "tests/test_site.py"):
            with open(os.path.join(ROOT, name), "rb") as fh:
                text = fh.read().decode("utf-8")
            self.assertNotIn(chr(0x2013), text, name)
            self.assertNotIn(chr(0x2014), text, name)


if __name__ == "__main__":
    unittest.main()
