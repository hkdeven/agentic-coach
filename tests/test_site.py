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


class SiteBase(unittest.TestCase):
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



class SiteTest(SiteBase):
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
        self.assertIn("600k line", page)
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


NEW_DAILY = """# Coach: 2026-11-16

1. What happened: 14 one-line edits sent one at a time (session 1a2b3c4d, turn 1, 13:39) (repeat of 2026-11-10, given 3 times)
   Do instead: type the notes as one numbered message.
   Evidence: python3 <plugin>/coach/evidence.py show 1a2b3c4d --turn 1
2. What happened: a long build, 9 agents (session 1a2b3c4d, turns 2 to 3, 13:39 to 14:02) (repeat of 2026-11-12)
   Do instead: hold the build until the owner answers.
3. What happened: 3 compactions (session 0b1c2d3e, evidence no longer on disk)
   Do instead: ask for a handoff at each natural break.
4. What happened: no tag on this one, 5 retries.
   Do instead: read the error first.

Totals: 12.3M tokens (1.1M output), about $41 API-equivalent. Top projects: demo $20, quagga $12, other $9. Week to date: $80. All time since 2026-11-01: $300.
Checked: 4 items against the transcripts, 2 candidates dropped on verification.
"""

NEW_WEEKLY = """# Coach weekly: 2026-11-16

## Follow-through
- worse: "ask for a handoff at each natural break." (given 2026-11-10). Compactions per session went from 1 to 2.
- not seen again, applied on 2026-11-11: "start a new session from a handoff note." (given 2026-11-10). Then it moved from 0.67 to 0.
- recurred: "read the error first." (given 2 times since 2026-11-03). No number tracks this advice, and it was given again.
- cannot measure: "say what done looks like." (given 2026-11-10). No number tracks this advice.
- no data: "use plan mode." (given 2026-11-10). No sessions this period to judge it by.

## Week in numbers
12 sessions (9 last week), 300 prompts, 14 active hours.
Checked: 5 items against the transcripts, 1 candidate dropped on verification.

## Cost by project
| Project | Week tokens | Week cost | All-time cost |
|---|---|---|---|
| demo | 8.0M | $20 | $90 |
| quagga | 3.0M | $12 | $40 |
| other | 1.3M | $9 | $170 |
| Total | 12.3M | $41 | $300 |
All time is since 2026-11-01. API-equivalent at list prices, not a bill.

## Before and after
Last 7 days against the 7 before.
Tool errors per 100 tool calls went from 6.1 to 2.4, an improvement.
Corrections per 100 prompts: no real change.

## What cost the most
1. What happened: a session peaked at 450k context (session 1a2b3c4d, turn 2, 13:40).
   Do instead: open a new session per topic.
   Trend: repeat since 2026-11-03, given 4 times; worse, 450k against 390k.
2. What happened: 5 refused publishes.
   Do instead: read the artifact first.
   Trend: new.

## Set up but unused
- skill deploy-helper (user): about 1.2k tokens per turn (estimate), never used. Action: remove it.
- mcp tracker (user): about 3k tokens per turn (estimate), last used 2026-10-01. Action: turn it off.
Always on: about 21k tokens on every turn, about $9 over 7 days at the cache read rate (estimate).

## Proposed changes (not applied)
### P1: Batch rule
Why: 14 edits sent one at a time.
Change: `Send review notes as one numbered list.`
Where: ~/.claude/CLAUDE.md
"""

COMPARE = {
    "version": 1, "generated": "2026-11-16 08:00",
    "before": {"first_day": "2026-11-03", "last_day": "2026-11-09", "days": 7, "sessions_seen": 9},
    "after": {"first_day": "2026-11-10", "last_day": "2026-11-16", "days": 7, "sessions_seen": 12},
    "notes": ["The periods differ a lot in volume. " + EVIL],
    "metrics": [
        {"key": "tool_errors", "label": "Tool errors", "kind": "count", "direction": "lower",
         "before": 61, "after": 24, "pct": -60.7, "basis": "per 100 tool calls",
         "norm_before": 6.1, "norm_after": 2.4, "norm_pct": -60.7, "state": "improvement"},
        {"key": "corrections", "label": "Corrections", "kind": "count", "direction": "lower",
         "before": 4, "after": 9, "pct": 125.0, "basis": "per 100 prompts",
         "norm_before": 1.3, "norm_after": 3.0, "norm_pct": 130.8, "state": "regression"},
        {"key": "cost", "label": "Cost", "kind": "cost", "direction": "neutral",
         "before": 30.5, "after": 41.0, "pct": None, "basis": None, "state": "too_little_data"}],
    "findings": [
        {"key": "finding:context_bloat " + EVIL, "label": "context_bloat " + EVIL, "kind": "count",
         "before": 1, "after": 5, "pct": 400.0, "basis": "per session", "norm_before": 0.11,
         "norm_after": 0.42, "norm_pct": 281.8, "state": "regression"}],
    "projects": [{"project": "demo " + EVIL}],
    "highlights": ["tool_errors", "corrections", "finding:context_bloat " + EVIL, "not_a_key"],
}

AUDIT = {
    "generated": "2026-11-16T08:00:00", "findings": {},
    "window": {"since": "2026-11-09", "until": "2026-11-16", "real_days": 7, "turns": 300,
               "standard": True},
    "always_on": {"every_session_tokens_per_turn": 21000, "window_tokens": 6300000,
                  "usd_cache_read": 9.45, "usd_first_turn_write": 1.2, "by_kind": {"skill": 4000}},
    "weekly_block": ["- skill deploy-helper (user): about 1.2k tokens per turn (estimate), never used. "
                     "Action: remove it.", "- mcp tracker " + EVIL + " (user): load unknown."],
    "items": [
        {"kind": "skill", "name": "deploy-helper", "scope": "user", "tokens_per_turn": 1200,
         "window_tokens": 360000, "usd": 0.5, "uses": 0, "last_used": None},
        {"kind": "mcp", "name": "tracker " + EVIL, "scope": "user", "tokens_per_turn": 3000,
         "window_tokens": 900000, "usd": 1.4, "uses": 2, "last_used": "2026-10-01"},
        {"kind": "skill", "name": "quagga-notes", "scope": "project quagga", "tokens_per_turn": 800,
         "window_tokens": 240000, "usd": 0.3, "uses": 0, "last_used": None}],
}


def applied_lines(home):
    rows = [{"action": "apply", "key": "p1-batch", "date": "2026-11-12T09:00:00+0000",
             "type": "claude_md_rule", "target": os.path.join(home, ".claude", "CLAUDE.md"),
             "summary": "Adds the batch rule " + EVIL, "reason": "14 edits sent one at a time"},
            {"action": "apply", "key": "p2-hook", "date": "2026-11-13T09:00:00+0000",
             "type": "hook", "target": "/srv/quagga/.claude/settings.json",
             "summary": "Adds a Stop hook", "reason": "context passed 500k twice"},
            {"action": "undo", "key": "p2-hook", "date": "2026-11-14T09:00:00+0000",
             "target": "/srv/quagga/.claude/settings.json"}]
    return "".join(json.dumps(r) + "\n" for r in rows)


class NewSectionsTest(SiteBase):
    """Release 1.2.0: new report lines, the module sections and the exclude rules."""

    def setUp(self):
        SiteBase.setUp(self)
        self.home = os.path.join(self.tmp, "coach-home")
        self.reports = os.path.join(self.home, "reports")      # modules read the coach home
        write(os.path.join(self.reports, "baseline", "SUMMARY.md"),
              BASELINE + "- Quagga migration ran long.\n  - nested under it.\n- Kept line.\n")
        self.env = dict(os.environ, COACH_HOME=self.home,
                        CLAUDE_CONFIG_DIR=os.path.join(self.tmp, "claude"))

    def add_new_reports(self, modules=True):
        write(os.path.join(self.reports, "daily", "2026-11-16.md"), NEW_DAILY)
        write(os.path.join(self.reports, "weekly", "2026-11-16.md"), NEW_WEEKLY)
        digest = json.loads(json.dumps(DIGEST))
        digest["totals"]["sessions"] = 3
        digest["sessions"][0]["id"] = "1a2b3c4d-0000-0000-0000-000000000001"
        digest["totals"]["cost"] = {"usd": 41.0}
        digest["sessions"].append(
            {"id": "5e4d3c2b-0000-0000-0000-000000000002", "project": "~/quagga", "title": "Port",
             "project_name": "quagga", "kind": "interactive", "start": "2026-11-16 09:00",
             "active_min": 40, "turns": 12, "corrections": 1, "errors": {"total": 4},
             "tokens": {"out": 300, "peak_context": 90000}, "cost": {"usd": 12.0}})
        digest["sessions"][0]["cost"] = {"usd": 29.0}
        digest["projects"] = [{"name": "quagga", "usd": 12.0}, {"name": "demo", "usd": 29.0}]
        write(os.path.join(self.reports, "digests", "daily-2026-11-16.json"), json.dumps(digest))
        write(os.path.join(self.reports, "digests", "daily-2026-11-16.findings.json"), json.dumps(
            FINDINGS + [{"pattern": "high_error_rate", "severity": "high", "session": "5e4d3c2b",
                         "evidence": "4 errors in 12 calls", "suggestion": "Read the error."}]))
        b = Builder(sid="1a2b3c4d-0000-0000-0000-000000000001")
        b.prompt("add a logout button " + EVIL).bash("npm test", "ok")
        b.prompt("now the footer").prompt("and the header")
        b.write(self.projects)
        if modules:
            write(os.path.join(self.home, "compare", "latest.json"), json.dumps(COMPARE))
            write(os.path.join(self.reports, "config-audit", "2026-11-16.json"), json.dumps(AUDIT))
            write(os.path.join(self.home, "applied.jsonl"), applied_lines(os.path.expanduser("~")))
            for mode in ("backfill",):
                p = subprocess.run([sys.executable, os.path.join(ROOT, "coach", "followthrough.py"),
                                    mode], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8"))

    def exclude(self, rules, ledger_ids=()):
        write(os.path.join(self.home, "config.json"), json.dumps({"exclude": rules}))
        if ledger_ids:
            write(os.path.join(self.home, "ledger.json"), json.dumps(
                {"version": 1, "first_day": None, "days": {}, "excluded": list(ledger_ids)}))

    def body(self, page):
        """The markup between the style block and the data script."""
        return page.split("</style>", 1)[1].split('<script type="application/json"', 1)[0]

    def test_daily_lines_tag_marker_evidence_and_checked(self):
        day = build_site.parse_daily(NEW_DAILY)
        one, two, three, four = day["items"]
        self.assertEqual(one["tag"], "session 1a2b3c4d, turn 1, 13:39")
        self.assertIn("(session 1a2b3c4d, turn 1, 13:39)", one["what"])
        self.assertEqual((one["repeat_since"], one["times"]), ("2026-11-10", 3))
        self.assertTrue(one["repeat"])
        self.assertEqual(one["evidence_cmd"], "python3 <plugin>/coach/evidence.py show 1a2b3c4d --turn 1")
        self.assertEqual(one["do"], "Type the notes as one numbered message.")
        self.assertEqual(two["tag"], "session 1a2b3c4d, turns 2 to 3, 13:39 to 14:02")
        self.assertEqual((two["repeat_since"], two["times"]), ("2026-11-12", None))
        self.assertIsNone(two["evidence_cmd"])
        self.assertEqual(three["tag"], "session 0b1c2d3e, evidence no longer on disk")
        self.assertEqual((four["tag"], four["repeat_since"], four["times"]), (None, None, None))
        self.assertFalse(four["repeat"])
        self.assertEqual(day["checked"],
                         "4 items against the transcripts, 2 candidates dropped on verification.")
        self.assertTrue(day["totals"].startswith("12.3M tokens"))
        self.assertNotIn("Checked", day["totals"])
        self.assertEqual(len(day["items"]), 4)
        self.assertNotIn("Checked", four["do"])

    def test_weekly_sections_are_parsed_in_any_mix(self):
        week = build_site.parse_weekly(NEW_WEEKLY)
        self.assertEqual([f["status"] for f in week["followthrough"]],
                         ["worse", "not seen again", "recurred", "cannot measure", "no data"])
        self.assertEqual([f["applied"] for f in week["followthrough"]],
                         [None, "2026-11-11", None, None, None])
        self.assertTrue(week["followthrough"][0]["text"].startswith('"ask for a handoff'))
        self.assertEqual(week["checked"],
                         "5 items against the transcripts, 1 candidate dropped on verification.")
        self.assertEqual(week["numbers"], "12 sessions (9 last week), 300 prompts, 14 active hours.")
        self.assertEqual(len(week["compare_lines"]), 3)
        self.assertEqual(week["compare_lines"][0], "Last 7 days against the 7 before.")
        self.assertEqual(len(week["unused"]["lines"]), 2)
        self.assertTrue(week["unused"]["lines"][0].startswith("skill deploy-helper"))
        self.assertTrue(week["unused"]["total"].startswith("Always on: about 21k"))
        self.assertEqual(len(week["cost"]["rows"]), 4)
        item = week["items"][0]
        self.assertEqual((item["repeat_since"], item["times"]), ("2026-11-03", 4))
        self.assertEqual(item["state"], "repeat")
        self.assertEqual(item["tag"], "session 1a2b3c4d, turn 2, 13:40")
        self.assertEqual([p["id"] for p in week["proposals"]], ["P1"])
        # a report from before these sections existed reads as it always did
        old = build_site.parse_weekly(WEEKLY % "x")
        self.assertEqual((old["followthrough"], old["compare_lines"], old["checked"]), ([], [], ""))
        self.assertEqual(old["unused"], {"lines": [], "total": ""})
        self.assertEqual([i["state"] for i in old["items"]], ["worse", "new"])
        self.assertEqual([p["id"] for p in old["proposals"]], ["P1", "P2"])
        self.assertIn("12 sessions", old["numbers"])
        # a heading inside a pasted change does not start a section
        text = NEW_WEEKLY + "### P2: A skill\nWhy: w\nChange: s\n```\n## Follow-through\n- worse: no\n```\nWhere: x\n"
        self.assertEqual(len(build_site.parse_weekly(text)["followthrough"]), 5)

    def test_new_sections_render(self):
        self.add_new_reports()
        page, _ = self.run_build()
        self.check_contract(page)
        body = self.body(page)
        for tab in ("progress", "setup"):
            self.assertIn('id="tab-%s"' % tab, body)
            self.assertIn('id="view-%s"' % tab, body)
        for title in ("Follow-through", "Before and after", "Set up but unused", "Fixes applied",
                      "Status line"):
            self.assertIn('<h3 class="sub">%s</h3>' % title, body, title)
        # follow-through: the status word is always printed, colour only repeats it
        self.assertIn('<span class="pill worse">worse</span>', body)
        self.assertIn('<span class="pill worse">recurred</span>', body)
        self.assertIn('<span class="pill better">not seen again</span>', body)
        self.assertIn('<span class="pill">cannot measure</span>', body)
        self.assertIn('<span class="pill">no data</span>', body)
        self.assertIn("applied on 11 Nov 2026", body)
        self.assertIn("Advice given so far", body)
        self.assertIn("Type the notes as one numbered message.", body)
        # before and after
        self.assertIn("<b>Before</b> 3 Nov 2026 to 9 Nov 2026 (7 days, 9 sessions)", body)
        self.assertIn("6.10 to 2.40 per 100 tool calls", body)
        self.assertIn('<span class="pill better">improvement</span>', body)
        self.assertIn('<span class="pill worse">regression</span>', body)
        self.assertIn("<summary>Every measure as a table</summary>", body)
        self.assertIn("too little data", body)
        # set up but unused
        self.assertIn("Always on: about 21k tokens on every turn", body)
        self.assertIn("2026-11-09 to 2026-11-16, 7 days", body)
        self.assertIn("Every figure is an estimate", body)
        self.assertIn('deploy-helper <span class="pill warn">unused</span>', body)
        # fixes applied: the home folder shows as ~
        self.assertIn("<code>~/.claude/CLAUDE.md</code>", body)
        self.assertNotIn(os.path.expanduser("~") + os.sep, page)
        self.assertIn('<span class="pill better">applied</span>', body)
        self.assertIn('<span class="pill">undone</span>', body)
        # status line: off here, with the legend and the thresholds
        self.assertIn('<span class="pill">off</span>', body)
        self.assertIn("Off: no status line is set.", body)
        self.assertIn("<code>ctx 182k 18%</code>", body)
        self.assertIn("Context marker !!", body)
        # the weekly view carries the new sections as data
        week = self.page_data(page)["weeklies"][0]
        self.assertEqual(len(week["followthrough"]), 5)
        self.assertEqual(len(week["compare_lines"]), 3)
        self.assertEqual(len(week["unused"]["lines"]), 2)
        self.assertTrue(week["checked"].startswith("5 items"))

    def test_item_evidence(self):
        self.add_new_reports()
        page, _ = self.run_build()
        data = self.page_data(page)
        items = data["dailies"][0]["items"]
        ev = items[0]["ev"]
        self.assertEqual(ev["tag"], "session 1a2b3c4d, turn 1, 13:39")
        self.assertEqual(ev["open"], "python3 <plugin>/coach/evidence.py show 1a2b3c4d --turn 1")
        self.assertEqual(ev["tpl"], "ev-d0-1")
        self.assertIn('<template id="ev-d0-1"><details class="evidence-slice">', page)
        self.assertIn("ev-focus", page)
        # the excerpt is the module's own escaped markup, inserted once and not escaped again
        self.assertIn("add a logout button &lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("&amp;lt;script", page)
        self.assertNotIn("<script>alert", page)
        self.assertEqual(items[2]["ev"], {"tag": "session 0b1c2d3e, evidence no longer on disk",
                                          "open": "", "tpl": None})
        self.assertNotIn("ev", items[3])
        self.assertEqual(data["weeklies"][0]["items"][0]["ev"]["tpl"], "ev-w0-1")
        for cls in ("evidence-slice", "evidence-note", "evidence-rows", "ev-row", "ev-meta",
                    "ev-text", "ev-focus", "evidence-open"):
            self.assertIn("." + cls, page.split("</style>")[0], cls)
        # with excerpts turned off the tag and the command stay, the excerpt goes
        write(os.path.join(self.home, "config.json"), json.dumps({"evidence_on_page": False}))
        page, _ = self.run_build()
        self.assertNotIn("<template", page)
        self.assertNotIn("logout button", page)
        ev = self.page_data(page)["dailies"][0]["items"][0]["ev"]
        self.assertEqual((ev["tag"], ev["tpl"]), ("session 1a2b3c4d, turn 1, 13:39", None))
        self.assertTrue(ev["open"].endswith("show 1a2b3c4d --turn 1"))

    def test_evidence_only_on_the_newest_reports(self):
        self.add_new_reports(modules=False)
        for i in range(1, 9):
            write(os.path.join(self.reports, "daily", "2026-11-%02d.md" % i), NEW_DAILY)
        write(os.path.join(self.reports, "weekly", "2026-11-09.md"), NEW_WEEKLY)
        data = self.page_data(self.run_build("--no-trends")[0])
        have = ["ev" in d["items"][0] for d in data["dailies"]]
        self.assertEqual(have, [True] * 7 + [False] * 2)
        self.assertTrue(all(d["items"][0]["tag"] for d in data["dailies"]))
        self.assertEqual(["ev" in w["items"][0] for w in data["weeklies"]], [True, False])

    def test_new_sections_are_absent_without_data(self):
        page, _ = self.run_build()
        self.check_contract(page)
        body = self.body(page)
        for title in ("Follow-through", "Before and after", "Set up but unused", "Fixes applied"):
            self.assertNotIn(title, body, title)
        self.assertIn("Nothing to follow up yet.", body)
        self.assertIn('<h3 class="sub">Status line</h3>', body)
        self.assertNotIn("<template", page)
        # reports from before 1.2.0: no new section, no empty heading, same items
        self.add_reports()
        page, _ = self.run_build()
        body = self.body(page)
        for title in ("Follow-through", "Before and after", "Set up but unused", "Fixes applied"):
            self.assertNotIn(title, body, title)
        day = self.page_data(page)["dailies"][0]
        self.assertEqual((day["checked"], len(day["items"])), ("", 2))
        self.assertEqual([it["tag"] for it in day["items"]], [None, None])
        self.assertEqual(day["items"][1]["repeat_since"], "2026-11-15")

    def test_a_broken_module_or_file_leaves_the_page_standing(self):
        self.add_new_reports()
        for path in (os.path.join(self.home, "compare", "latest.json"),
                     os.path.join(self.reports, "config-audit", "2026-11-16.json"),
                     os.path.join(self.home, "applied.jsonl"), os.path.join(self.home, "advice.jsonl")):
            write(path, '{"version": 1, "items": [[[\n{{DATA}} ' + EVIL)
        page, _ = self.run_build()
        self.check_contract(page)
        self.assertEqual(len(self.page_data(page)["dailies"][0]["items"]), 4)
        self.assertIn('<h3 class="sub">Status line</h3>', page)
        self.assertNotIn("Fixes applied", self.body(page))
        self.assertNotIn("<script>alert", page)

        class Broken(object):
            def __getattr__(self, name):
                raise RuntimeError("module is broken")

        old_env = dict((k, os.environ.get(k)) for k in ("COACH_HOME", "CLAUDE_CONFIG_DIR"))
        old = (build_site.optional, build_site.EV)
        os.environ.update(COACH_HOME=self.home, CLAUDE_CONFIG_DIR=os.path.join(self.tmp, "claude"))
        try:
            for stand_in in (lambda name: Broken(), lambda name: None):
                build_site.optional, build_site.EV = stand_in, stand_in("evidence")
                page, card = build_site.build_page(self.reports, self.projects, "2026-11-17", False)
                self.assertTrue(page.startswith("<title>Agentic Coach</title>"))
                self.assertEqual(len(card["items"]), 4)
                self.assertIn("Nothing to follow up yet.", page)
                self.assertNotIn("{{", page.split("<script", 1)[0])
        finally:
            build_site.optional, build_site.EV = old
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_module_and_report_text_cannot_inject_markup(self):
        self.add_new_reports()
        weekly = NEW_WEEKLY.replace("Compactions per session went", EVIL + " went") \
            .replace("Tool errors per 100", EVIL + " per 100") \
            .replace("skill deploy-helper", "skill " + EVIL) \
            .replace("Always on: about 21k", "Always on: " + EVIL) \
            .replace("5 items against", EVIL + " against")
        write(os.path.join(self.reports, "weekly", "2026-11-16.md"), weekly)
        write(os.path.join(self.reports, "daily", "2026-11-16.md"),
              NEW_DAILY.replace("4 items against", EVIL + " against")
              .replace("show 1a2b3c4d --turn 1", "show " + EVIL)
              .replace("(session 0b1c2d3e, evidence", "(session 0b1c2d3e, " + EVIL + " evidence"))
        with open(os.path.join(self.home, "advice.jsonl")) as fh:
            log = fh.read()
        self.assertIn("handoff at each natural break", log)
        write(os.path.join(self.home, "advice.jsonl"),
              log.replace("handoff at each natural break", "handoff " + json.dumps(EVIL)[1:-1]))
        page, _ = self.run_build()
        self.check_contract(page)
        self.assertEqual(page.count("</script>"), 2)
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<img", page)
        self.assertNotIn('"onload=', page)
        body = self.body(page)
        self.assertGreaterEqual(body.count("&lt;script&gt;alert(1)&lt;/script&gt;"), 6)
        data = self.page_data(page)
        self.assertIn(EVIL, data["weeklies"][0]["followthrough"][0]["text"])
        self.assertIn(EVIL, data["weeklies"][0]["checked"])
        self.assertIn(EVIL, data["dailies"][0]["checked"])
        self.assertIn(EVIL, data["dailies"][0]["items"][0]["evidence_cmd"])

    # ---------- exclude rules ----------

    RULES = [{"match": ["Quagga"]}, {"sessions": ["9f8e7d6c-1111-2222-3333-444444444444"]}]

    def add_old_reports_naming_hidden_work(self):
        self.add_new_reports()
        daily = NEW_DAILY.replace(
            "4. What happened: no tag on this one, 5 retries.",
            "4. What happened: quagga port, 40 minutes lost.\n   Do instead: split it.\n"
            "5. What happened: 9f8e7d6c ran 3 hours.\n   Do instead: stop sooner.\n"
            "6. What happened: no tag on this one, 5 retries.")
        daily = daily.replace("2. What happened: a long build", "2. What happened: session 7a6b5c4d, a long build")
        write(os.path.join(self.reports, "daily", "2026-11-16.md"), daily)
        weekly = NEW_WEEKLY.replace(
            "- recurred:", '- same: "keep the Quagga notes short." (given 2026-11-10). Stayed.\n- recurred:')
        weekly = weekly.replace("Corrections per 100 prompts: no real change.",
                                "Corrections per 100 prompts: no real change.\nQuagga alone doubled.")
        weekly = weekly.replace("1. What happened: a session peaked",
                                "1. What happened: 9f8e7d6c idled.\n   Do instead: close it.\n   Trend: new.\n"
                                "2. What happened: quagga rebuilt twice.\n   Do instead: cache it.\n   Trend: new.\n"
                                "3. What happened: a session peaked")
        weekly = weekly.replace("2. What happened: 5 refused publishes.", "4. What happened: 5 refused publishes.")
        weekly = weekly.replace("- mcp tracker (user)", "- skill quagga-notes (project): never used.\n- mcp tracker (user)")
        weekly += ("\n### P2: Quagga hook\nWhy: two rebuilds.\nChange: `x`\nWhere: the project file\n"
                   "\n### P3: Close idle sessions\nWhy: session 9f8e7d6c idled.\nChange: `y`\nWhere: a rule\n")
        write(os.path.join(self.reports, "weekly", "2026-11-16.md"), weekly)
        write(os.path.join(self.reports, "baseline", "quagga.md"), "# Quagga detail\n\nAll of it.\n")
        Builder(sid="7a6b5c4d-0000-0000-0000-000000000009", cwd="/tmp/quagga-app") \
            .prompt("port the importer to the new api").bash("make", "ok") \
            .write(self.projects, project="-tmp-quagga-app")

    def test_excluded_work_is_shown_nowhere(self):
        self.add_old_reports_naming_hidden_work()
        self.exclude(self.RULES, ledger_ids=["7a6b5c4d"])
        page, out = self.run_build("--card")
        self.check_contract(page)
        for hidden in ("quagga", "9f8e7d6c", "7a6b5c4d", "5e4d3c2b"):
            self.assertNotIn(hidden, page.lower(), hidden)
            self.assertNotIn(hidden, out.lower(), hidden)
        data = self.page_data(page)
        day = data["dailies"][0]
        # the other items remain, numbered without a gap
        self.assertEqual([it["n"] for it in day["items"]], [1, 2, 3])
        self.assertTrue(day["items"][0]["what"].startswith("14 one-line edits"))
        self.assertTrue(day["items"][1]["what"].startswith("3 compactions"))
        self.assertTrue(day["items"][2]["what"].startswith("no tag on this one"))
        # the report named excluded work, so its totals were added up with that work
        # in them: the line is left out rather than shown with the name cut
        self.assertEqual(day["totals"], "")
        # digest numbers: worked out again without the hidden session, or not printed
        self.assertEqual(sorted(day["sessions"]), ["1a2b3c4d"])
        self.assertEqual(day["counts"]["sessions"], 2)
        self.assertEqual(day["counts"]["prompts"], 50)
        self.assertEqual(day["counts"]["active_min"], 200)
        self.assertEqual(day["counts"]["errors"], 10)
        self.assertEqual(day["counts"]["corrections"], 0)
        self.assertEqual(day["counts"]["out_tokens"], 1200)
        self.assertEqual(day["cost"]["usd"], 29.0)
        self.assertEqual(day["cost"]["top_project"]["name"], "demo")
        self.assertEqual([f["pattern"] for f in day["other"]], ["context_bloat", "repeated_prompt"])
        card = json.loads(out)
        self.assertEqual(len(card["items"]), 3)
        self.assertEqual(card["cost"]["top_project"]["name"], "demo")
        week = data["weeklies"][0]
        self.assertEqual([it["n"] for it in week["items"]], [1, 2])
        self.assertTrue(week["items"][0]["what"].startswith("a session peaked"))
        self.assertEqual([p["id"] for p in week["proposals"]], ["P1"])
        self.assertEqual(len(week["followthrough"]), 5)
        self.assertEqual(len(week["compare_lines"]), 3)
        self.assertEqual(len(week["unused"]["lines"]), 2)
        self.assertEqual([r[0] for r in week["cost"]["rows"]], ["demo", "other"])
        # baseline: the bullet goes with what is nested under it, the detail report goes whole
        self.assertNotIn("nested under it", page)
        self.assertIn("Kept line.", page)
        self.assertIn("Sessions run too long.", page)
        # module rows
        body = self.body(page)
        self.assertIn("deploy-helper", body)
        self.assertIn("<code>~/.claude/CLAUDE.md</code>", body)
        self.assertNotIn("Adds a Stop hook", body)
        # the trend scan leaves the session out, and nothing says that anything was left out
        with open(os.path.join(self.reports, "digests", "trend.json")) as fh:
            cache = json.load(fh)
        self.assertEqual(sum(d["sessions"] for d in cache["days"].values()), 1)
        self.assertNotIn("7a6b5c4d", json.dumps(cache))
        for word in ("exclud", "omitted", "hidden", "not shown"):
            self.assertNotIn(word, self.body(page).split("evidence-slice")[0].lower(), word)

    def test_rules_reach_the_trend_scan_and_a_cache_made_before_them(self):
        self.add_old_reports_naming_hidden_work()
        self.run_build()
        path = os.path.join(self.reports, "digests", "trend.json")
        with open(path) as fh:
            cache = json.load(fh)
        self.assertEqual(sum(d["sessions"] for d in cache["days"].values()), 2)
        self.assertNotIn("exclude_sig", cache)
        self.exclude([{"match": ["quagga"]}])            # by folder: no id is listed anywhere
        page, _ = self.run_build("--no-trends")           # a stale cache is not drawn
        self.assertIn("No trend data yet.", page)
        self.run_build()
        with open(path) as fh:
            cache = json.load(fh)
        self.assertEqual(sum(d["sessions"] for d in cache["days"].values()), 1)
        self.assertEqual(sum(d["prompts"] for d in cache["days"].values()), 3)
        self.assertTrue(cache["exclude_sig"])
        self.assertNotIn("quagga", json.dumps(cache).lower())
        self.exclude([])
        self.run_build()
        with open(path) as fh:
            cache = json.load(fh)
        self.assertEqual(sum(d["sessions"] for d in cache["days"].values()), 2)
        self.assertNotIn("exclude_sig", cache)

    def test_no_rules_changes_nothing(self):
        self.add_old_reports_naming_hidden_work()
        plain, card = self.run_build("--card")
        self.assertIn("quagga", plain.lower())
        self.assertIn("9f8e7d6c", plain)
        data = self.page_data(plain)
        self.assertEqual([it["n"] for it in data["dailies"][0]["items"]], [1, 2, 3, 4, 5])
        self.assertEqual(data["dailies"][0]["counts"]["sessions"], 3)
        self.assertEqual(len(data["weeklies"][0]["cost"]["rows"]), 4)
        write(os.path.join(self.home, "config.json"), json.dumps({"exclude": []}))
        self.assertEqual(self.run_build("--card"), (plain, card))

    def test_unusable_leave_out_settings_stop_the_build(self):
        self.add_old_reports_naming_hidden_work()
        before, _ = self.run_build()
        site = self.out
        for text in ("{broken", "[]", json.dumps({"exclude": "quagga"}),
                     json.dumps({"exclude": [{"match": []}, "x"]}),
                     json.dumps({"only_account": "nope"}), json.dumps({"exclude_dates": ["soon"]}),
                     json.dumps({"unknown_account": "maybe"})):
            write(os.path.join(self.home, "config.json"), text)
            p = subprocess.run([sys.executable, SCRIPT, "--reports-dir", self.reports,
                                "--out", self.out, "--projects-dir", self.projects],
                               env=dict(os.environ, COACH_HOME=self.home),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(p.returncode, 2, text)
            self.assertIn(b"Nothing was built", p.stderr)
            with open(site) as fh:
                self.assertEqual(fh.read(), before)      # the page on disk is left as it was

    def test_hidden_totals_line_is_cut_cleanly_or_left_out(self):
        hide = build_site.Hide([{"match": ["quagga"]}])
        line = "1M tokens (1k output), about $41 API-equivalent. Top projects: quagga $20. Week to date: $80."
        self.assertEqual(build_site.visible_totals(line, hide),
                         "1M tokens (1k output), about $41 API-equivalent. Week to date: $80.")
        # a name that cannot be cut out as one clause takes the line with it
        for bad in ("1M tokens. Top projects: a, quagga b $20, c $3.", "Quagga day: 1M tokens.",
                    "1M tokens. Top projects: quagga, inc $20, c $3."):
            self.assertEqual(build_site.visible_totals(bad, hide), "", bad)
        self.assertEqual(build_site.visible_totals(line, build_site.NO_HIDE), line)
        self.assertFalse(build_site.Hide([], []).on)
        self.assertTrue(build_site.Hide([], ["9f8e7d6c"]).text("see 9F8E7D6C here"))


if __name__ == "__main__":
    unittest.main()
