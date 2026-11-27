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
        # every chart on Trends is a line graph in the Overview's style, none a bar chart
        trends = page.split('id="view-trends"', 1)[1].split('id="view-progress"', 1)[0]
        self.assertEqual(trends.count('<svg class="lg"'), len(build_site.SERIES))
        self.assertEqual(trends.count('<figure class="chart trend">'), len(build_site.SERIES))
        for gone in ('class="ch-bar', 'class="ch-zero"', '<svg class="ch"', "highlighted bar"):
            self.assertNotIn(gone, trends)
        # the 600k threshold is kept on the peak context graph, as a dashed line with its label
        self.assertEqual(trends.count('class="lg-limit"'), 1)
        self.assertEqual(trends.count('class="lg-limit-lab"'), 1)
        self.assertIn("The ringed point is the latest day with data.", trends)
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


LIST_DAILY = """# Coach: 2026-11-26

1. What happened: CRM sync build session was picked up after a 156 minute break while holding about 318k context (repeat of 2026-11-16, given 8 times)
   Do instead: after a break of more than an hour, start the next task in a fresh session.
   Evidence: session 1a2b3c4d, turns 2 to 3, 13:39 to 14:02
2. What happened: an Artifact publish was refused 3 times
   Do instead: merge onto the live version, then publish once.
   Evidence: session 0b1c2d3e, evidence no longer on disk

Totals:
- Tokens: 2B (8.5M output), about $1,259 API-equivalent
- Top projects: A360 $1,048, quagga $162, ACV $44
- Week to date: $3,729
- All time since 2026-08-29: $14,540
Estimates:
- Claude Fable 5.2: 71 pieces, 2496 min estimated, 1715 min actual, 31% under
- Claude Opus 5.5: 26 pieces, 763 min estimated, 623 min actual, 18% under
- Not counted: 69 estimates with no stated actual
Checked: 2 items against the transcripts, 3 candidates dropped on verification.
"""

OLD_INLINE_DAILY = """# Coach: 2026-11-26

1. What happened: CRM sync project build session was picked up after a 156 minute break (15:39 to 18:15) while holding about 318k context; about $40 plus $122 in subagents (session 1a2b3c4d, turns 38 to 39, 15:39 to 18:15) (repeat of 2026-11-16, given 8 times)
   Do instead: After a break of more than an hour, start the next task in a fresh session.
2. What happened: In A360, a publish was refused 3 times, the last try resending the same content; session about $31 (session 0b1c2d3e, turns 1 to 12, 14:02 to 19:14)
   Do instead: merge onto the live version first.

Totals: 2B tokens (8.5M output), about $1,259 API-equivalent. Top projects: A360 $1,048, Other one-offs $162, ACV $44. Week to date: $3,729. All time since 2026-08-29: $14,540.
Estimates: Claude Fable 5.2: 71 pieces, 2496 min estimated, 1715 min actual, 31% under; Claude Opus 5.5: 26 pieces, 763 min estimated, 623 min actual, 18% under. Not counted: 69 estimates with no stated actual.
Checked: 2 items against the transcripts, 3 candidates dropped on verification.
"""

LIST_WEEKLY = """# Coach weekly: 2026-11-26

## Week in numbers
- Sessions: 53 (last week 20)
- Output tokens: 21.7M (14.0M), $3,196 (last week $1,765)
Estimates:
- Claude Opus 5.5: 26 pieces, 763 min estimated, 623 min actual, 18% under
Checked: 3 items against the transcripts.

## What cost the most
1. What happened: ACV subagents called Edit on files they had not read, 41 times on 20 Nov
   Do instead: brief agents to Read before Edit. (P1)
   Trend: repeat since 2026-11-20, given 5 times; 0.25 to 0.34 per session, worse.
   Evidence: session 1a2b3c4d, turn 2, 13:40

## Proposed changes (not applied)
### P1: Read first
Why: Edit on unread files failed 41 times (20 Nov, $306 in subagents) and in 9 more sessions.
Change: `Read a file before editing it.`
Where: ~/.claude/CLAUDE.md
"""


class TipFormatTest(SiteBase):
    """Release 1.5.1: no cost or evidence tag in a tip, totals and estimates as lists."""

    def test_list_totals_estimates_and_evidence_line(self):
        day = build_site.parse_daily(LIST_DAILY)
        self.assertEqual(day["totals_list"], [
            "Tokens: 2B (8.5M output), about $1,259 API-equivalent",
            "Top projects: A360 $1,048, quagga $162, ACV $44",
            "Week to date: $3,729", "All time since 2026-08-29: $14,540"])
        self.assertTrue(day["totals"].startswith("Tokens: 2B (8.5M output)"))
        self.assertEqual(day["estimates_list"], [
            "Claude Fable 5.2: 71 pieces, 2496 min estimated, 1715 min actual, 31% under",
            "Claude Opus 5.5: 26 pieces, 763 min estimated, 623 min actual, 18% under",
            "Not counted: 69 estimates with no stated actual"])
        self.assertEqual(day["checked"], "2 items against the transcripts, 3 candidates dropped on verification.")
        one, two = day["items"]
        self.assertEqual(one["tag"], "session 1a2b3c4d, turns 2 to 3, 13:39 to 14:02")
        self.assertIsNone(one["evidence_cmd"])                  # a tag, not a command
        self.assertEqual(one["ids"], ["1a2b3c4d"])
        self.assertEqual((one["repeat_since"], one["times"]), ("2026-11-16", 8))
        self.assertEqual(one["what"], "CRM sync build session was picked up after a 156 minute break "
                                      "while holding about 318k context (repeat of 2026-11-16, given 8 times)")
        self.assertEqual(two["tag"], "session 0b1c2d3e, evidence no longer on disk")
        for it in day["items"]:
            self.assertNotIn("Tokens", it["do"])                # the totals are not glued onto an item
            self.assertNotIn("Evidence", it["do"])
        self.assertEqual(day["note"], "")

    def test_old_inline_report_still_reads_and_shows_no_tag_or_cost(self):
        day = build_site.parse_daily(OLD_INLINE_DAILY)
        one, two = day["items"]
        self.assertEqual(one["what"], "CRM sync project build session was picked up after a 156 minute break "
                                      "while holding about 318k context (repeat of 2026-11-16, given 8 times)")
        self.assertEqual(one["tag"], "session 1a2b3c4d, turns 38 to 39, 15:39 to 18:15")
        self.assertEqual((one["repeat_since"], one["times"]), ("2026-11-16", 8))
        self.assertEqual(two["what"], "In A360, a publish was refused 3 times, the last try resending the same content")
        self.assertEqual(two["tag"], "session 0b1c2d3e, turns 1 to 12, 14:02 to 19:14")
        self.assertTrue(day["totals"].startswith("2B tokens (8.5M output)"))
        self.assertEqual(day["totals_list"], [
            "Tokens: 2B (8.5M output), about $1,259 API-equivalent",
            "Top projects: A360 $1,048, Other one-offs $162, ACV $44",
            "Week to date: $3,729", "All time since 2026-08-29: $14,540"])
        self.assertEqual(day["estimates_list"][-1], "Not counted: 69 estimates with no stated actual")
        self.assertEqual(len(day["estimates_list"]), 3)
        self.assertTrue(day["estimates"].startswith("Claude Fable 5.2: 71 pieces"))

    def test_tip_text_leaves_plain_text_alone(self):
        for text in ("plain text, no change; ok.", 'hit "No .claude/launch.json found" 6 times',
                     "a run at 10:05 (repeat of 2026-11-10)", "", None):
            self.assertEqual(build_site.tip_text(text), text)
        self.assertEqual(build_site.tip_text("x, 3.0M output tokens, about $45 (session 1a2b3c4d, turn 4, 13:39)"),
                         "x, 3.0M output tokens")
        self.assertEqual(build_site.tip_text("41 times (20 Nov, $306 in subagents) and more"),
                         "41 times (20 Nov) and more")
        self.assertEqual(build_site.tip_text("ran `awk '{print $1}'`, 9 failures"),
                         "ran `awk '{print $1}'`, 9 failures")

    def test_hidden_project_in_list_totals(self):
        hide = build_site.Hide([{"match": ["quagga"]}])
        items = ["Tokens: 1M (1k output), about $41 API-equivalent", "Top projects: demo $20, quagga $12",
                 "Week to date: $80"]
        self.assertEqual(build_site.visible_totals_list(items, hide),
                         ["Tokens: 1M (1k output), about $41 API-equivalent", "Top projects: demo $20",
                          "Week to date: $80"])
        self.assertEqual(build_site.visible_totals_list(["Quagga day: 1M tokens", "Week to date: $80"], hide), [])
        self.assertEqual(build_site.visible_totals_list(items, build_site.NO_HIDE), items)
        # a report that names excluded work anywhere shows no totals at all
        self.assertEqual(build_site.parse_daily(LIST_DAILY, hide)["totals_list"], [])

    def test_weekly_lists_and_why_without_cost(self):
        week = build_site.parse_weekly(LIST_WEEKLY)
        self.assertEqual(week["numbers_list"], ["Sessions: 53 (last week 20)",
                                                "Output tokens: 21.7M (14.0M), $3,196 (last week $1,765)"])
        self.assertEqual(week["estimates_list"],
                         ["Claude Opus 5.5: 26 pieces, 763 min estimated, 623 min actual, 18% under"])
        self.assertEqual(week["checked"], "3 items against the transcripts.")
        item = week["items"][0]
        self.assertEqual(item["tag"], "session 1a2b3c4d, turn 2, 13:40")
        self.assertEqual(item["state"], "repeat")
        self.assertEqual((item["repeat_since"], item["times"]), ("2026-11-20", 5))
        self.assertNotIn("1a2b3c4d", item["what"] + item["trend"] + item["do"])
        self.assertEqual(week["proposals"][0]["why"],
                         "Edit on unread files failed 41 times (20 Nov) and in 9 more sessions.")
        # a weekly from before 1.5.1 keeps its paragraph
        old = build_site.parse_weekly(NEW_WEEKLY)
        self.assertEqual((old["numbers"], old["numbers_list"], old["estimates_list"]),
                         ("12 sessions (9 last week), 300 prompts, 14 active hours.", [], []))


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
        # the tag is read, and not shown as part of the tip (1.5.1)
        self.assertEqual(one["what"], "14 one-line edits sent one at a time (repeat of 2026-11-10, given 3 times)")
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
        # (the graphs' axis labels carry aria-hidden, which is markup, not a word on the page)
        shown = self.body(page).split("evidence-slice")[0].replace('aria-hidden="true"', "").lower()
        for word in ("exclud", "omitted", "hidden", "not shown"):
            self.assertNotIn(word, shown, word)

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


def live_period(label, unit, series, **over):
    p = {"label": label, "cost_usd": 27.8, "prev_cost_usd": 145.3, "calls": 403, "sessions": 4,
         "tokens": 57300000, "series_unit": unit, "series": series,
         "peak": {"t": series[-1]["t"], "tokens": 14600000, "usd": 6.9},
         "first_try": {"rate": 0.82, "prev_rate": 0.76, "edit_turns": 61},
         "cache_hit": {"rate": 0.96, "prev_rate": 0.96},
         "corrections": {"count": 3, "prev_count": 2},
         "activities": [{"name": "Coding", "usd": 13.09, "share": 0.47, "first_try": 0.86, "turns": 40},
                        {"name": "Conversation", "usd": 13.05, "share": 0.47, "first_try": None,
                         "turns": 31},
                        {"name": "Feature Dev", "usd": 0.92, "share": 0.03, "first_try": 0.5, "turns": 4},
                        {"name": "Debugging", "usd": 0.5, "share": 0.02, "first_try": 1.0, "turns": 2},
                        {"name": "Planning", "usd": 0.24, "share": 0.01, "first_try": None, "turns": 1}],
         "projects": [{"name": "agentic-coach", "sessions": 2, "usd": 20.1, "share": 0.72},
                      {"name": "Other", "sessions": 2, "usd": 7.7, "share": 0.28}],
         "mcp_servers": [{"name": "zoho-crm", "calls": 41, "tools": 6, "errors": 2},
                         {"name": "browser", "calls": 10, "tools": 3, "errors": 0}]}
    p.update(over)
    return p


def live_feed():
    """A full feed, as the contract for ~/.claude/coach/live.json lays it out."""
    hours = [{"t": "2026-11-23T%02d:00:00+02:00" % i, "tokens": 1000 * i, "usd": v}
             for i, v in enumerate([0.11, 0, 0.4, 2.1, 6.9, 5.74])]
    days = [{"t": "2026-11-%02dT00:00:00+02:00" % d, "tokens": 1000000 * d, "usd": float(v)}
            for d, v in zip(range(17, 24), [88, 131, 96, 1420.5, 120, 107.7, 27.8])]
    return {
        "version": 1, "updated": "2026-11-23T11:41:00+02:00", "page_url": None,
        "periods": {"today": live_period("Today, Nov 23", "hour", hours),
                    "week": live_period("Last 7 days", "day", days, cost_usd=1991.0),
                    "month": live_period("Last 30 days", "day", days, cost_usd=7509.94)},
        "limits": {"as_of": "2026-11-23T11:40:10+02:00", "rows": [
            {"key": "five_hour", "label": "5-hour", "used_pct": 7,
             "resets_at": "2026-11-23T14:24:00+02:00", "pace_pct": -39, "runs_out_at": None,
             "level": "ok"},
            {"key": "weekly", "label": "Weekly", "used_pct": 94,
             "resets_at": "2026-11-26T05:41:00+02:00", "pace_pct": 33,
             "runs_out_at": "2026-11-23T17:45:00+02:00", "level": "crit"}]},
        "running": [{"project": "agentic-coach", "branch": "main", "model": "Fable 5.2",
                     "started": "2026-11-23T10:29:00+02:00", "context_pct": 64,
                     "context_left_tokens": 359000, "level": "warn"}],
        "tips": [{"title": "agentic-coach, main has passed 600k of context.",
                  "advice": "Compact or hand off before the next big step.",
                  "pattern": "context_bloat"}],
    }


class FakeLive(object):
    """Stands in for coach/live.py, which lives on another branch until the pool."""

    def __init__(self, result):
        self.result = result

    def snapshot(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class OverviewTest(SiteBase):
    def write_feed(self, feed):
        write(os.path.join(self.tmp, "coach-home", "live.json"), json.dumps(feed))

    def run_build(self, *extra):
        """Build in this process with the feed taken from live.json: live.py, once it
        is pooled in, would otherwise answer first with a feed of its own making."""
        real = build_site.optional
        old = dict((k, os.environ.get(k)) for k in ("COACH_HOME", "CLAUDE_CONFIG_DIR"))
        os.environ["COACH_HOME"] = os.path.join(self.tmp, "coach-home")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        build_site.optional = lambda name: None if name == "live" else real(name)
        try:
            page, _card = build_site.build_page(self.reports, self.projects, "2026-11-17")
        finally:
            build_site.optional = real
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return page, ""

    def test_line_graph(self):
        svg = build_site.line_svg([0, 5, 10, 2.5], ["a: $0", 'b "x" <b>', "c", "d"],
                                  'Spend <by> "day"', mark=2)
        self.assertTrue(svg.startswith('<svg class="lg" viewBox="0 0 1000 210" preserveAspectRatio="none"'))
        self.assertIn('aria-label="Spend &lt;by&gt; &quot;day&quot;"', svg)
        self.assertIn('tabindex="0"', svg)
        # one scale: the top value sits on the top grid line, zero on the base line
        self.assertIn('<polyline class="lg-line" points="4.0,200.0 334.7,108.0 665.3,16.0 996.0,154.0"/>', svg)
        self.assertIn('<polygon class="lg-area" points="4.0,200.0 4.0,200.0 ', svg)
        self.assertIn(' 996.0,154.0 996.0,200.0"/>', svg)
        self.assertEqual(svg.count('class="lg-grid"'), 2)            # dashed, top and middle
        self.assertEqual(svg.count('class="lg-base"'), 1)
        self.assertEqual(svg.count('<g class="b"'), 4)               # a readout target per point
        self.assertIn('data-r="b &quot;x&quot; &lt;b&gt;"', svg)
        self.assertNotIn("<b>", svg)
        # a ring on the last point and one on the marked point
        self.assertIn('<path class="lg-ring" d="M996.0 154.0h0"/>', svg)
        self.assertIn('<path class="lg-ring peak" d="M665.3 16.0h0"/>', svg)
        self.assertEqual(svg.count("lg-hole"), 2)
        # the marked point is the last one: one ring, the marked one
        both = build_site.line_svg([1, 2, 4], mark=2)
        self.assertEqual(both.count('class="lg-ring'), 1)
        self.assertIn('class="lg-ring peak"', both)
        # the small before-and-now line: no grid, no area, no targets, its own scale
        small = build_site.line_svg([76, 82], title="t", size=(200, 40), grid=False, area=False,
                                    hits=False, zero=False)
        for gone in ("lg-grid", "lg-area", "<g ", "tabindex"):
            self.assertNotIn(gone, small)
        self.assertIn('points="4.0,33.0 196.0,7.0"', small)
        flat = build_site.line_svg([5, 5], size=(200, 40), grid=False, hits=False, zero=False)
        self.assertIn('points="4.0,20.0 196.0,20.0"', flat)
        # one point: a ring and no line. No points, gaps and all zeros: still a drawing
        one = build_site.line_svg([3], ["x"])
        self.assertNotIn("polyline", one)
        self.assertIn('d="M500.0 16.0h0"', one)
        for values in ([], [None, None], [0, 0, 0], [None, 4, float("nan"), 2]):
            got = build_site.line_svg(values, mark=7)
            self.assertTrue(got.endswith("</svg>"), values)
            self.assertNotIn("nan", got)
        self.assertNotIn("tabindex", build_site.line_svg([]))
        self.assertIn('points="4.0,200.0 500.0,200.0 996.0,200.0"', build_site.line_svg([0, 0, 0]))
        self.assertEqual(build_site.fmt_usd2(7509.94), "$7,509.94")
        self.assertEqual(build_site.fmt_usd2(None), "$0.00")
        self.assertEqual(build_site.axis_marks(3), [0, 1, 2])
        self.assertEqual(build_site.axis_marks(7), [0, 2, 4, 6])
        self.assertEqual(build_site.axis_marks(30), [0, 7, 15, 22, 29])

    def test_line_graph_ceiling_and_limit(self):
        # a ceiling lifts the top of the scale; a limit is a dashed line on the same scale
        svg = build_site.line_svg([0, 50], ceiling=200, limit=100)
        self.assertIn('points="4.0,200.0 996.0,154.0"', svg)
        self.assertIn('<line class="lg-limit" x1="0" y1="108.0" x2="1000" y2="108.0"/>', svg)
        # a ceiling below the data does not cut it; a limit off the scale is not drawn
        self.assertIn('points="4.0,200.0 996.0,16.0"', build_site.line_svg([0, 50], ceiling=10))
        for odd in (None, -5, 900, float("nan"), "x", True):
            self.assertNotIn("lg-limit", build_site.line_svg([0, 50], ceiling=200, limit=odd), odd)

    def test_trend_graph(self):
        days = ["2026-11-%02d" % i for i in range(1, 8)]
        values = [None, 200000, 0, 450000, None, 700000, 520000]
        html = build_site.trend_graph(days, values, "tok", "Peak context, tokens", 600000)
        svg = html.split("<svg", 1)[1].split("</svg>", 1)[0]
        self.assertIn('aria-label="Line graph of Peak context, tokens, last 7 days"', html)
        # a line through the days with data, a readout target on each, a ring on the latest
        self.assertIn('class="lg-line"', svg)
        self.assertEqual(svg.count('<g class="b"'), 5)
        self.assertIn('data-r="6 Nov: 700k"', svg)
        self.assertIn('data-r="3 Nov: 0"', svg)
        self.assertEqual(svg.count('class="lg-ring"'), 1)
        self.assertNotIn("ch-bar", html)
        # the scale is a round figure above the data and the threshold: 800k, 400k, 0
        self.assertEqual(re.findall(r'<span style="top:[\d.]+%">([^<]+)</span>', html), ["800k", "400k", "0"])
        # the threshold: a dashed line and its label at the same height (600k of 800k)
        y = 200.0 - 184.0 * 600000 / 800000
        self.assertIn('<line class="lg-limit" x1="0" y1="%.1f"' % y, svg)
        self.assertIn('class="lg-limit-lab" aria-hidden="true" style="top:%.1f%%">600k line</span>'
                      % (100.0 * y / 210.0), html)
        # the first and last day under the graph
        self.assertEqual(re.findall(r'<span style="left:[\d.]+%">([^<]+)</span>', html), ["1 Nov", "7 Nov"])
        # no threshold: no dashed line; a small count keeps a scale of at least 4
        plain = build_site.trend_graph(days, [0, 1, 0, 2, 1, 0, 1], "int", "Corrections")
        self.assertNotIn("lg-limit", plain)
        self.assertEqual(re.findall(r'<span style="top:[\d.]+%">([^<]+)</span>', plain), ["4", "2", "0"])
        # no data at all still draws an empty graph
        self.assertIn("</svg>", build_site.trend_graph(days, [None] * 7, "dec", "Active hours"))

    def test_every_tab_shares_one_column(self):
        page, _ = self.run_build()
        css = page.split("<style", 1)[1].split("</style>", 1)[0]
        # one width rule for the header and every tab: the Overview's 1320px column
        self.assertIn(".wrap { max-width: 1320px; margin-inline: auto;", css)
        self.assertNotIn("max-width: 980px", page)
        self.assertNotIn("ov-on", page)
        self.assertEqual(re.findall(r"\.wrap \{ max-width: (\d+)px", css), ["1320"])

    def test_overview_tiles_fill_whole_rows(self):
        page, _ = self.run_build()
        css = page.split("<style", 1)[1].split("</style>", 1)[0]
        # two columns on a phone, three from 600px, six (one row of figures) from 1180px
        self.assertIn(".ov-tiles { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));", css)
        self.assertIn("@media (min-width: 600px) {\n  .ov-tiles { grid-template-columns: repeat(3, minmax(0, 1fr)); }", css)
        self.assertIn("@media (min-width: 1180px) {\n  .ov-tiles { grid-template-columns: repeat(6, minmax(0, 1fr)); }", css)
        self.assertNotIn("auto-fit, minmax(170px", css)
        # the limits share the tiles' grid on a row of their own, spanning whole columns to fill it
        self.assertEqual(page.count('h("div", { class: "ov-tiles" })'), 2)
        self.assertEqual(page.count("limitRow("), 3)
        self.assertIn(".ov-tiles.lims-2 .ov-lim { grid-column: span 3; }", css)
        self.assertIn(".ov-tiles.lims-1 .ov-lim { grid-column: span 6; }", css)

    def test_trend_scale_labels_are_exact(self):
        top = build_site.scale_top
        self.assertEqual(top(5, "int"), 8)            # 5 would label its middle 2
        self.assertEqual(top(2, "int"), 4)            # a count scale is at least 4
        self.assertEqual(top(25, "int"), 40)
        self.assertEqual(top(700000, "tok"), 800000)
        self.assertEqual(top(2200000, "tok"), 4000000)   # 2.5M would label its middle 1.2M
        self.assertEqual(top(0, "pct"), 2)
        self.assertEqual(top(85, "pct"), 100)
        self.assertEqual(top(0, "dec"), 1)
        self.assertEqual(top(14.3, "dec"), 20)
        for kind in ("int", "tok", "pct", "dec"):
            for v in (0, 0.3, 1, 3, 5, 7, 12, 25, 99, 140, 999, 2500, 61000, 660000, 1.3e6, 7.7e6):
                t = top(v, kind)
                self.assertGreaterEqual(t, v, (kind, v))
                self.assertTrue(build_site._clean(t, kind) and build_site._clean(t / 2.0, kind), (kind, v, t))

    def test_trend_line_from_the_measure_series(self):
        feed = live_feed()
        week = feed["periods"]["week"]
        week["first_try"]["series"] = [0.7, None, 0.76, "x", 0.8, 0.82]
        week["cache_hit"]["series"] = [None, 0.96, None]            # one step of data: not a line
        week["corrections"]["series"] = list(range(20, 0, -1))      # more than 12 steps: cut to 12
        view = build_site.live_view(feed)
        got = view["periods"]["week"]
        self.assertEqual(got["first_try"]["series"], [0.7, None, 0.76, None, 0.8, 0.82])
        self.assertEqual(got["corrections"]["series"], list(range(20, 8, -1)))
        self.assertIsNone(view["periods"]["today"]["first_try"]["series"])
        graphs = build_site.overview_graphs(view)

        def spark(period, m):
            return graphs.split('<template id="ov-s-%s-%s">' % (period, m), 1)[1].split("</template>")[0]

        ft = spark("week", "ft")
        self.assertIn('aria-label="First-try edits over the period: from 70% to 82%"', ft)
        # nulls are skipped and the points that remain keep their place along the line
        self.assertIn('points="4.0,33.0 80.8,20.0 157.6,11.3 196.0,7.0"', ft)
        self.assertIn('<path class="lg-ring" d="M196.0 7.0h0"/>', ft)
        self.assertEqual(spark("week", "co").count(","), 12)
        self.assertIn("Corrections over the period: from 20 to 9", spark("week", "co"))
        # too little in the series, or no series: the two points, the period before and now
        self.assertIn("Cache hit: 96% the period before, 96% now", spark("week", "ch"))
        self.assertIn("First-try edits: 76% the period before, 82% now", spark("today", "ft"))
        self.assertIn('points="4.0,33.0 196.0,7.0"', spark("today", "ft"))
        # a series alone is enough: no figure for the period before is needed
        week["first_try"].update(prev_rate=None)
        self.assertIn("from 70% to 82%", build_site.overview_graphs(build_site.live_view(feed)))
        for odd in ("soon", {"a": 1}, [], [None], ["a", "b"], [5]):
            week["first_try"] = {"rate": None, "prev_rate": None, "series": odd}
            self.assertNotIn("ov-s-week-ft", build_site.overview_graphs(build_site.live_view(feed)), odd)

    def test_heat_bar_width(self):
        heat = build_site.heat_pct
        self.assertEqual(heat(50, 50), 100)              # the largest row fills the track
        self.assertEqual(heat(25, 50), 50)
        self.assertEqual(heat(1, 3), 33)
        self.assertEqual(heat(0.01, 500), 2)             # anything at all keeps a sliver
        self.assertEqual(heat(80, 50), 100)              # never past the track
        for value, top in ((0, 50), (-3, 50), (None, 50), (5, 0), (5, None), ("5", 50),
                           (float("nan"), 50), (True, 50)):
            self.assertEqual(heat(value, top), 0, (value, top))
        rows = build_site.add_heat([{"usd": 20.0}, {"usd": 5.0}, {"usd": None}, {"usd": 0}], "usd")
        self.assertEqual([r["heat"] for r in rows], [100, 25, 0, 0])
        self.assertEqual(build_site.add_heat([], "usd"), [])
        self.assertEqual(build_site.add_heat([{"usd": None}], "usd")[0]["heat"], 0)

    def test_full_feed_is_embedded_and_drawn(self):
        feed = live_feed()
        feed["tips"][0]["title"] = "Tip " + EVIL
        feed["periods"]["today"]["projects"][0]["name"] = "proj " + EVIL
        self.write_feed(feed)
        self.add_reports()
        page, _out = self.run_build()
        self.check_contract(page)
        self.assertNotIn("<script>alert(1)", page)
        self.assertNotIn('"onload=alert(2)>', page)
        # Overview is the first tab, with its own address, and the others are still there
        tabs = re.findall(r'<a href="#(\w+)" id="tab-(\w+)">', page)
        self.assertEqual([t[0] for t in tabs], ["overview", "today", "week", "cost", "trends",
                                                "progress", "setup", "baseline"])
        self.assertIn('<section class="view" id="view-overview" aria-labelledby="tab-overview">', page)
        for label in (">Today</button>", ">7 days</button>", ">30 days</button>"):
            self.assertIn(label, page)
        live = self.page_data(page)["live"]
        self.assertEqual(live["version"], 1)
        self.assertEqual(live["updated"], "2026-11-23T11:41:00+02:00")
        self.assertEqual(sorted(live["periods"]), ["month", "today", "week"])
        today = live["periods"]["today"]
        self.assertEqual(today["label"], "Today, Nov 23")
        self.assertEqual((today["cost_usd"], today["prev_cost_usd"], today["calls"]), (27.8, 145.3, 403))
        self.assertEqual(today["series_unit"], "hour")
        self.assertEqual(len(today["series"]), 6)
        self.assertEqual(today["first_try"], {"rate": 0.82, "prev_rate": 0.76, "edit_turns": 61,
                                              "series": None})
        # every activity is listed, each with its turns and its heat bar length
        self.assertEqual([a["name"] for a in today["activities"]],
                         ["Coding", "Conversation", "Feature Dev", "Debugging", "Planning"])
        self.assertEqual([a["heat"] for a in today["activities"]], [100, 100, 7, 4, 2])
        self.assertEqual(today["activities"][0]["turns"], 40)
        self.assertIsNone(today["activities"][1]["first_try"])
        self.assertEqual([p["heat"] for p in today["projects"]], [100, 38])
        self.assertEqual(today["mcp_servers"], [
            {"name": "zoho-crm", "calls": 41, "tools": 6, "errors": 2, "heat": 100},
            {"name": "browser", "calls": 10, "tools": 3, "errors": 0, "heat": 24}])
        self.assertEqual(live["periods"]["week"]["series_unit"], "day")
        self.assertEqual(live["limits"]["as_of"], "2026-11-23T11:40:10+02:00")
        self.assertEqual([r["key"] for r in live["limits"]["rows"]], ["five_hour", "weekly"])
        self.assertEqual(live["limits"]["rows"][1]["runs_out_at"], "2026-11-23T17:45:00+02:00")
        self.assertEqual(live["running"][0]["context_left_tokens"], 359000)
        self.assertEqual(live["tips"][0]["advice"], "Compact or hand off before the next big step.")
        self.assertNotIn("page_url", live)
        # the graphs: spend for each period, the hour as the unit for today, the peak marked
        for key in ("today", "week", "month"):
            self.assertEqual(page.count('<template id="ov-g-%s">' % key), 1)
            for m in ("ft", "ch", "co"):
                self.assertEqual(page.count('<template id="ov-s-%s-%s">' % (key, m)), 1)
        self.assertIn('aria-label="Line graph of spend by hour, Today, Nov 23, peaking at 04:00"', page)
        self.assertIn('aria-label="Line graph of spend by day, Last 7 days, peaking on Nov 20"', page)
        self.assertIn('data-r="Nov 20: $1,420.50"', page)
        self.assertIn('data-r="04:00: $6.90"', page)
        self.assertEqual(page.count("lg-ring peak"), 3)
        self.assertIn('<span style="left:99.6%">Today</span>', page)
        self.assertIn('aria-label="First-try edits: 76% the period before, 82% now"', page)
        self.assertIn('aria-label="Corrections: 2 the period before, 3 now"', page)
        # the words the script prints (they are in the page with or without a feed)
        for said in ("API-equivalent, not a bill", "Limit used, ", "Spend by hour", "Spend by day",
                     "Spend by week", "Spend today", "Calls", "Tokens", "First-try edits", "Cache hit",
                     "Corrections", "Coach tips", "By activity", "Projects", "MCP servers", "Running now",
                     "7-day average", "No MCP calls in this period", "Update the coach"):
            self.assertIn(said, page)
        # the restyled page: system fonts only (no web font is asked for), sentence case,
        # the dashboard's colours in light and dark, tabs and periods as one kind of control
        self.assertNotIn("fonts.googleapis", page)
        self.assertNotIn("IBM Plex", page)
        self.assertNotIn("text-transform: uppercase; letter-spacing", page)
        self.assertIn('--sans: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif;', page)
        self.assertIn("font-variant-numeric: tabular-nums", page)
        self.assertIn("--bg: #f3f2ee; --surface: #fcfcfb;", page)          # light
        self.assertEqual(page.count("--bg: #111110; --surface: #1a1a19;"), 2)   # dark, by setting and by choice
        self.assertIn('.tabs a[aria-current="page"] { background: var(--pill); color: var(--pill-text); }', page)
        self.assertIn('.seg button[aria-pressed="true"] { background: var(--pill); color: var(--pill-text); }', page)
        self.assertIn(".brand h1 { font-size: 17px; font-weight: 650; }", page)
        self.assertIn(".ov-card { background: var(--surface); border: 1px solid var(--line); border-radius: 10px;", page)
        self.assertIn(".hb { width: 110px; height: 4px;", page)
        # the figure a screen reader hears is rounded as the page rounds it
        feed["periods"]["today"]["first_try"] = {"rate": 0.825, "prev_rate": 0.765}
        self.assertIn("First-try edits: 77% the period before, 83% now",
                      build_site.overview_graphs(build_site.live_view(feed)))

    def test_minimal_feed_with_nulls_and_no_limits(self):
        feed = {"version": 1, "updated": "2026-11-23T11:41:00+02:00", "page_url": None,
                "periods": {"today": {
                    "label": "Today, Nov 23", "cost_usd": 0.42, "prev_cost_usd": None, "calls": 3,
                    "sessions": 1, "tokens": 120000, "series_unit": "hour",
                    "series": [{"t": "2026-11-23T11:00:00+02:00", "tokens": 120000, "usd": 0.42}],
                    "peak": None,
                    "first_try": {"rate": None, "prev_rate": None, "edit_turns": 0},
                    "cache_hit": {"rate": 0.5, "prev_rate": None},
                    "corrections": {"count": 0, "prev_count": None},
                    "activities": [{"name": "Conversation", "usd": 0.42, "share": 1.0,
                                    "first_try": None}],
                    "projects": [], "mcp_servers": []}},
                "limits": {"as_of": None, "rows": []}, "running": [], "tips": []}
        self.write_feed(feed)
        page, _out = self.run_build()
        self.check_contract(page)
        live = self.page_data(page)["live"]
        self.assertEqual(sorted(live["periods"]), ["today"])       # a missing period is just absent
        today = live["periods"]["today"]
        self.assertIsNone(today["prev_cost_usd"])
        self.assertIsNone(today["peak"])
        self.assertEqual(today["first_try"], {"rate": None, "prev_rate": None, "edit_turns": 0,
                                              "series": None})
        self.assertEqual(today["cache_hit"], {"rate": 0.5, "prev_rate": None, "series": None})
        self.assertEqual(today["corrections"], {"count": 0, "prev_count": None, "series": None})
        self.assertEqual(today["activities"], [{"name": "Conversation", "usd": 0.42, "turns": None,
                                                "share": 1.0, "first_try": None, "heat": 100}])
        self.assertEqual((today["projects"], today["mcp_servers"]), ([], []))
        self.assertEqual(live["limits"], {"as_of": None, "rows": []})
        self.assertEqual((live["running"], live["tips"]), ([], []))
        self.assertEqual(page.count('<template id="ov-g-today">'), 1)
        self.assertNotIn('<template id="ov-s-', page)              # nothing before to draw a line from
        self.assertNotIn("ov-g-week", page.split("<script>")[0])
        # keys left out, and values of the wrong kind, are tolerated the same way
        for odd in ({}, {"version": 1}, {"version": "1", "periods": [], "limits": "x", "running": {},
                                         "tips": None, "updated": 7},
                    {"periods": {"today": {"series": [1, {"t": "soon"}, {"t": None}],
                                           "activities": "none", "first_try": 3,
                                           "cost_usd": float("inf")}},
                     "limits": {"rows": [{"label": "5-hour"}, {"used_pct": 95}, 4]},
                     "running": [{"branch": "main"}, {"project": "p", "started": "yesterday"}],
                     "tips": [{"advice": "a"}, "t"]}):
            view = build_site.live_view(odd)
            self.assertEqual(view["version"], 1)
            json.dumps(view, allow_nan=False)
            self.assertIsInstance(build_site.overview_graphs(view), str)
        view = build_site.live_view(odd)
        self.assertEqual(view["periods"]["today"]["series"], [])
        self.assertIsNone(view["periods"]["today"]["cost_usd"])
        self.assertEqual(view["periods"]["today"]["label"], "Today")
        self.assertEqual(view["limits"]["rows"], [
            {"key": "", "label": "Limit", "used_pct": 95, "resets_at": None, "pace_pct": None,
             "runs_out_at": None, "level": "crit"}])
        self.assertEqual([r["project"] for r in view["running"]], ["p"])
        self.assertIsNone(view["running"][0]["started"])
        self.assertEqual(view["tips"], [])
        # a number no float can hold is no number, and the rest of the feed still shows
        huge = live_feed()
        huge["periods"]["today"]["cost_usd"] = 10 ** 400
        huge["periods"]["today"]["calls"] = 10 ** 400
        view = build_site.live_view(huge)
        self.assertIsNone(view["periods"]["today"]["cost_usd"])
        self.assertEqual(len(view["periods"]), 3)

    def test_no_feed_leaves_an_empty_overview(self):
        page, _out = self.run_build()
        self.check_contract(page)
        self.assertIsNone(self.page_data(page)["live"])
        self.assertIn('id="tab-overview"', page)
        self.assertIn('<p class="empty-title">No live numbers yet.</p>', page)
        self.assertNotIn('<template id="ov-', page)
        # the script opens on Today then, as it did before there was an Overview
        self.assertIn('v = liveOk ? "overview" : "today";', page)
        # a file that is not a feed is no feed
        for text in ("{broken", "[1, 2]", '"live"'):
            write(os.path.join(self.tmp, "coach-home", "live.json"), text)
            page, _out = self.run_build()
            self.assertIsNone(self.page_data(page)["live"], text)

    def all_time(self, **over):
        """A feed with an All time period of 12 weekly points, Sep 7 to Nov 23."""
        import datetime
        feed = live_feed()
        mondays = [(datetime.date(2026, 9, 7) + datetime.timedelta(days=7 * i)).isoformat() for i in range(12)]
        weeks = [{"t": d + "T00:00:00+02:00", "tokens": 1000000, "usd": float(10 + i)}
                 for i, d in enumerate(mondays)]
        weeks[3]["usd"] = 900.5
        p = live_period("All time, since Sep 9", "week", weeks, cost_usd=10811.0, prev_cost_usd=None,
                        since="2026-09-09T00:00:00+02:00", detail_since="2026-11-09T00:00:00+02:00")
        p["first_try"]["prev_rate"] = p["cache_hit"]["prev_rate"] = p["corrections"]["prev_count"] = None
        p.update(over)
        feed["periods"]["all"] = p
        return feed

    def test_all_time_is_the_fourth_period(self):
        view = build_site.live_view(self.all_time())
        self.assertEqual(sorted(view["periods"]), ["all", "month", "today", "week"])
        a = view["periods"]["all"]
        self.assertEqual((a["label"], a["series_unit"], len(a["series"])), ("All time, since Sep 9", "week", 12))
        self.assertIsNone(a["prev_cost_usd"])
        self.assertEqual(a["detail_from"], "Nov 9")
        self.write_feed(self.all_time())
        page, _ = self.run_build()
        self.assertIn('<button type="button" id="ov-p-all" aria-pressed="false">All time</button>', page)
        self.assertIn('var PERIODS = ["today", "week", "month", "all"]', page)
        self.assertEqual(page.count('<template id="ov-g-all">'), 1)
        self.assertIn('"Detail from " + p.detail_from', page)
        self.assertIn("#overview/all", page)
        # no period before: nothing to draw a before-and-now line from
        for m in ("ft", "ch", "co"):
            self.assertNotIn('<template id="ov-s-all-%s">' % m, page)

    def test_detail_line_only_when_the_detail_starts_later(self):
        def note(**over):
            return build_site.live_view(self.all_time(**over))["periods"]["all"]["detail_from"]
        self.assertEqual(note(), "Nov 9")
        self.assertIsNone(note(detail_since="2026-09-09T00:00:00+02:00"))     # the whole period has detail
        self.assertIsNone(note(detail_since="2026-09-09T09:30:00+02:00"))     # the same day counts as the start
        self.assertIsNone(note(detail_since=None))
        self.assertIsNone(note(since=None))
        self.assertIsNone(note(detail_since="soon"))
        self.assertIsNone(note(detail_since="2026-08-01T00:00:00+02:00"))     # earlier than the start: nothing to say
        # the other periods carry no such line
        self.assertIsNone(build_site.live_view(self.all_time())["periods"]["month"]["detail_from"])

    def test_weekly_points_read_like_days_under_the_graph(self):
        graphs = build_site.overview_graphs(build_site.live_view(self.all_time()))
        g = graphs.split('<template id="ov-g-all">', 1)[1].split("</template>")[0]
        self.assertIn('aria-label="Line graph of spend by week, All time, since Sep 9, peaking in the week of Sep 28"', g)
        self.assertIn('data-r="Week of Sep 28: $900.50"', g)
        axis = re.findall(r'<span style="left:[0-9.]+%">([^<]+)</span>', g)
        self.assertEqual(axis[0], "Sep 7")
        self.assertEqual(axis[-1], "Today")                # the last week is the one today is in
        self.assertNotIn("Week of", "".join(axis))
        # a last week that starts after the feed was written is not called Today
        late = self.all_time()
        late["updated"] = "2026-11-20T11:41:00+02:00"
        g = build_site.overview_graphs(build_site.live_view(late)).split('<template id="ov-g-all">', 1)[1]
        self.assertIn(">Nov 23</span>", g.split("</template>")[0])

    def test_a_long_all_time_graph_keeps_its_points(self):
        import datetime
        weeks = [{"t": (datetime.date(2024, 1, 1) + datetime.timedelta(days=7 * i)).isoformat() + "T00:00:00+02:00",
                  "tokens": 5, "usd": 1.0} for i in range(151)]
        a = build_site.live_view(self.all_time(series=weeks))["periods"]["all"]
        self.assertEqual(len(a["series"]), 151)
        self.assertEqual(build_site.live_period({"series_unit": "fortnight"}, "x")["series_unit"], "day")

    def test_spend_graph_average_line_and_money_scale(self):
        self.assertEqual(build_site.running_average([7, 7, 7]), [7.0, 7.0, 7.0])
        self.assertEqual(build_site.running_average([0, 14]), [0.0, 7.0])
        # each point: the mean of it and up to six before it
        self.assertEqual(build_site.running_average(list(range(1, 10)))[-1], 6.0)        # 3..9
        self.assertEqual(build_site.running_average([10, 20, 30], span=2), [10.0, 15.0, 25.0])
        self.assertEqual(build_site.running_average([]), [])
        self.assertEqual([build_site.axis_usd(v) for v in (0, 4.5, 12.4, 450, 999.6, 1000, 1240, 9960, 12400, None)],
                         ["$0", "$4.50", "$12", "$450", "$1000", "$1k", "$1.2k", "$10.0k".replace(".0k", "k"), "$12k", "$0"])
        # the average is a dashed line on the graph's own scale, and no target or ring of its own
        svg = build_site.line_svg([0, 10, 20], avg=[0, 5, 10])
        self.assertIn('<polyline class="lg-avg" points="4.0,200.0 500.0,154.0 996.0,108.0"/>', svg)
        self.assertEqual(svg.count("<g class="), 3)
        self.assertNotIn("lg-avg", build_site.line_svg([0, 10, 20]))
        self.assertNotIn("lg-avg", build_site.line_svg([0, 10, 20], avg=[None, 5, None]))   # one point is no line

        feed = live_feed()
        days = [{"t": "2026-11-%02dT00:00:00+02:00" % d, "tokens": 1, "usd": float(v)}
                for d, v in zip(range(14, 24), [10, 20, 30, 40, 50, 60, 70, 80, 900, 100])]
        feed["periods"]["month"] = live_period("Last 30 days", "day", days)
        graphs = build_site.overview_graphs(build_site.live_view(feed))

        def graph(key):
            return graphs.split('<template id="ov-g-%s">' % key, 1)[1].split("</template>")[0]
        month = graph("month")
        self.assertIn("lg-avg", month)                      # by day and more than a week of days
        self.assertIn(", with a dashed 7-day average", month)
        self.assertNotIn("lg-avg", graph("week"))          # seven days: nothing to average over
        self.assertNotIn("lg-avg", graph("today"))         # by hour
        # the scale: the top line is the highest point, the middle half of it, the base $0
        self.assertIn('<div class="lg-y" aria-hidden="true"><span style="top:7.6%">$900</span>'
                      '<span style="top:51.4%">$450</span><span style="top:95.2%">$0</span></div>', month)
        self.assertIn('<span style="top:7.6%">$6.90</span>', graph("today"))
        weekly = build_site.overview_graphs(build_site.live_view(self.all_time()))
        self.assertNotIn("lg-avg", weekly.split('<template id="ov-g-all">', 1)[1].split("</template>")[0])

    def run_script(self, feed, now):
        """Build the page from the feed and run its own script (tests/page_dom.js, under node):
        the live line, and for each period the Overview's class and its text as a tree."""
        self.write_feed(feed)
        page, _ = self.run_build()
        path = os.path.join(self.tmp, "page.html")
        write(path, page)
        done = subprocess.run([shutil.which("node"), os.path.join(ROOT, "tests", "page_dom.js"), path, now],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        return json.loads(done.stdout)

    @unittest.skipUnless(shutil.which("node"), "node is not installed: the page's script is not run")
    def test_today_and_week_show_tips_without_tag_or_cost_and_totals_as_lists(self):
        write(os.path.join(self.reports, "daily", "2026-11-16.md"), OLD_INLINE_DAILY)
        write(os.path.join(self.reports, "daily", "2026-11-17.md"), LIST_DAILY.replace("2026-11-26", "2026-11-17"))
        write(os.path.join(self.reports, "weekly", "2026-11-17.md"), LIST_WEEKLY)
        got = self.run_script(self.all_time(), "2026-11-23T11:43:00+02:00")
        today, week = got["today"], got["weekly"]
        self.assertIn("ul.totals-list", today)
        self.assertIn('"Totals:"', today)
        self.assertIn('"Estimates:"', today)
        self.assertIn('li "Top projects: A360 $1,048, quagga $162, ACV $44"', today)
        self.assertIn('li "Not counted: 69 estimates with no stated actual"', today)
        self.assertIn('"CRM sync build session was picked up after a 156 minute break while holding about '
                      '318k context (repeat of 2026-11-16, given 8 times)"', today)
        self.assertNotIn('"Evidence: session', today)            # the fold's summary names no session
        self.assertIn('summary "Evidence: 1 session, 0 detector findings"', today)
        self.assertIn("ul.totals-list", week)
        self.assertIn('li\n      "Sessions: 53 (last week 20)"', week)
        self.assertIn('"Edit on unread files failed 41 times (20 Nov) and in 9 more sessions."', week)

    @unittest.skipUnless(shutil.which("node"), "node is not installed: the page's script is not run")
    def test_the_page_script_draws_every_period(self):
        got = self.run_script(self.all_time(), "2026-11-23T11:43:00+02:00")
        self.assertEqual(got["live"], "conn on | Live · updated 11:41:00")
        for key, p in got["periods"].items():
            self.assertEqual((key, p["cls"], p["pressed"]), (key, "ov", "true"))
            tree = p["tree"]
            self.assertNotIn("Update the coach", tree, key)         # what a script error would leave
            for said in ("div.ov-tiles", "div.ov-row2", "div.ov-row3", '"API-equivalent, not a bill"',
                         '"Calls"', '"Tokens"', '"First-try edits"', '"Cache hit"', '"Corrections"',
                         '"Limit used, 5-hour"', '"Limit used, Weekly, near limit"', '"Running now"',
                         '"agentic-coach, main"', '"64% context"', '"Coach tips"', '"By activity"',
                         '"Projects"', '"MCP servers"', '"Activity"', '"Project"', '"Server"', "div.hb"):
                self.assertIn(said, tree, (key, said))
            # the two limits tell the tiles' grid their count, so they span and fill their row
            self.assertIn("div.ov-tiles.lims-2", tree, key)
        today, week, all_time =(got["periods"][k]["tree"] for k in ("today", "week", "all"))
        self.assertIn('"Spend today"', today)
        self.assertIn('"$27.80"', today)
        self.assertIn('"Spend by hour"', today)
        self.assertIn('b.is-good "Up 6"', today)                    # first-try: up is good
        self.assertIn('" on yesterday"', today)
        self.assertIn('"same as yesterday"', today)                 # cache hit did not move
        self.assertIn("section.ov-card.ov-tile.ov-how.warning", today)   # corrections went up
        self.assertIn('b.is-warn "Up 1"', today)
        self.assertIn('"Spend, last 7 days"', week)
        self.assertIn('"Spend by day"', week)
        self.assertNotIn("7-day average", week)                     # seven days: no average line
        # All time: its own label, a point a week, where the detail starts, and no change lines
        self.assertIn('"Spend, all time, since Sep 9"', all_time)
        self.assertIn('"Spend by week"', all_time)
        self.assertIn("in the week of Sep 28", all_time)
        self.assertEqual(all_time.count('"Detail from Nov 9"'), 6)   # calls, three figures, two tables
        for word in ("Up ", "Down ", "same as", " on the ", "yesterday"):
            self.assertNotIn(word, all_time, word)
        self.assertNotIn("Detail from", today)

    @unittest.skipUnless(shutil.which("node"), "node is not installed: the page's script is not run")
    def test_the_page_script_with_an_old_or_thin_feed(self):
        # over 15 minutes old: an orange dot and the words say so
        got = self.run_script(live_feed(), "2026-11-23T14:00:00+02:00")
        self.assertEqual(got["live"], "conn on stale | Not live · updated Nov 23 at 11:41")
        self.assertEqual(self.run_script(live_feed(), "2026-11-23T11:56:00+02:00")["live"].split(" | ")[0], "conn on")
        # a feed without All time: that period says so, and the limits and what is running still show
        thin = got["periods"]["all"]["tree"]
        self.assertIn('"No data for this period."', thin)
        self.assertIn('"Limit used, 5-hour"', thin)
        self.assertIn("div.ov-tiles.lims-2", thin)
        self.assertIn('"Running now"', thin)
        self.assertNotIn("div.ov-row3", thin)
        # a feed with nothing but a period: no limits, nothing running, no tips, empty tables
        bare = live_feed()
        bare.update(limits={"as_of": None, "rows": []}, running=[], tips=[])
        bare["periods"]["today"].update(activities=[], projects=[], mcp_servers=[], series=[], peak=None)
        tree = self.run_script(bare, "2026-11-23T11:43:00+02:00")["periods"]["today"]["tree"]
        self.assertNotIn("Update the coach", tree)
        for said in ('"Nothing is running."', '"No tips right now."', '"No activity in this period"',
                     '"No projects in this period"', '"No MCP calls in this period"',
                     '"No spend recorded in this period."'):
            self.assertIn(said, tree, said)
        self.assertNotIn("Limit used", tree)
        self.assertNotIn("lims-", tree)

    def test_a_newer_feed_asks_for_an_update(self):
        self.write_feed(dict(live_feed(), version=2))
        page, _out = self.run_build()
        self.check_contract(page)
        self.assertEqual(self.page_data(page)["live"], {"version": 2})
        self.assertNotIn('<template id="ov-', page)
        self.assertNotIn("agentic-coach, main", page)
        # anything above 1 is newer, however it is written; 1 is this version
        self.assertEqual(build_site.live_view(dict(live_feed(), version=1.5)), {"version": 1.5})
        self.assertEqual(build_site.live_view(dict(live_feed(), version="2")), {"version": 2.0})
        for newer, shown in (("2.0.0", 2.0), ("v3", 3.0), (float("inf"), 2), (10 ** 400, 2), (1e16, 2)):
            self.assertEqual(build_site.live_view(dict(live_feed(), version=newer)),
                             {"version": shown}, newer)
        for same in (1, 1.0, "1", "1.0", None, "one", 0, True, [2]):
            self.assertEqual(len(build_site.live_view(dict(live_feed(), version=same))["periods"]), 3)

    def test_feed_comes_from_the_module_first_then_the_file(self):
        home = os.path.join(self.tmp, "coach-home")
        self.write_feed({"version": 1, "updated": "2026-11-23T09:00:00+02:00",
                         "periods": {"today": {"sessions": 1}}})
        real, env = build_site.optional, os.environ.get("COACH_HOME")
        os.environ["COACH_HOME"] = home
        try:
            def with_live(mod):
                build_site.optional = lambda name: mod if name == "live" else real(name)
                return build_site.load_live()

            snap = {"version": 1, "updated": "2026-11-23T11:41:00+02:00",
                    "periods": {"today": {"sessions": 1}}}
            self.assertEqual(with_live(FakeLive(snap)), snap)                # the module wins
            for bad in (FakeLive(RuntimeError("boom")), FakeLive(None), FakeLive([1]), object(), None):
                # it raises, hands back no feed, has no snapshot(), or is not there: the file
                self.assertEqual(with_live(bad)["updated"], "2026-11-23T09:00:00+02:00", bad)
            os.remove(os.path.join(home, "live.json"))
            self.assertIsNone(with_live(None))                               # neither: no feed
            self.assertIsNone(with_live(FakeLive(RuntimeError("boom"))))
            self.assertEqual(with_live(FakeLive(snap)), snap)
        finally:
            build_site.optional = real
            if env is None:
                del os.environ["COACH_HOME"]
            else:
                os.environ["COACH_HOME"] = env

    def test_feed_that_names_excluded_work(self):
        feed = live_feed()
        feed["periods"]["week"]["projects"][0]["name"] = "quagga-app"
        feed["running"].append({"project": "Quagga", "branch": "main"})
        feed["tips"].append({"title": "quagga ran long.", "advice": "Stop."})
        view = build_site.live_view(feed, build_site.Hide([{"match": ["quagga"]}]))
        self.assertEqual(sorted(view["periods"]), ["month", "today"])    # counted with it: left out
        self.assertEqual([r["project"] for r in view["running"]], ["agentic-coach"])
        self.assertEqual(len(view["tips"]), 1)
        self.assertNotIn("quagga", json.dumps(view).lower())
        self.assertNotIn("quagga", build_site.overview_graphs(view).lower())
        self.assertEqual(len(build_site.live_view(feed)["periods"]), 3)
        # the rules reach the page through the build, from config.json
        self.write_feed(feed)
        write(os.path.join(self.tmp, "coach-home", "config.json"),
              json.dumps({"exclude": [{"match": ["quagga"]}]}))
        page, _out = self.run_build()
        self.assertNotIn("quagga", page.lower())
        live = self.page_data(page)["live"]
        self.assertEqual(sorted(live["periods"]), ["month", "today"])
        self.assertEqual(len(live["running"]), 1)
        # a rule is matched against names only: the feed's own words, an activity
        # type or a server that happens to hold the rule's text hide nothing
        for word in ("zoho", "session", "tool", "cod", "plan", "debug", "today", "last 7", "label",
                     "other"):
            view = build_site.live_view(live_feed(), build_site.Hide([{"match": [word]}]))
            self.assertEqual(len(view["periods"]), 3, word)
            self.assertEqual((len(view["running"]), len(view["tips"])), (1, 1), word)
        view = build_site.live_view(live_feed(), build_site.Hide([{"match": ["agentic"]}]))
        self.assertEqual((view["periods"], view["running"], view["tips"]), ({}, [], []))
        # an MCP server is named by the person: its row goes, the others are measured again
        view = build_site.live_view(live_feed(), build_site.Hide([{"match": ["zoho"]}]))
        self.assertEqual(view["periods"]["today"]["mcp_servers"], [
            {"name": "browser", "calls": 10, "tools": 3, "errors": 0, "heat": 100}])


class EmptyFeedTest(unittest.TestCase):
    """A feed with nothing in it (fresh install, 30 quiet days) counts as no feed."""

    def feed(self, **over):
        quiet = {"sessions": 0, "cost_usd": 0}
        base = {"version": 1, "periods": {"today": dict(quiet), "week": dict(quiet), "month": dict(quiet)},
                "limits": {"as_of": None, "rows": []}, "running": []}
        base.update(over)
        return base

    def test_rule(self):
        self.assertTrue(build_site.feed_is_empty(self.feed()))
        self.assertTrue(build_site.feed_is_empty(self.feed(periods={})))
        busy = self.feed()
        busy["periods"]["month"]["sessions"] = 1
        self.assertFalse(build_site.feed_is_empty(busy))
        spent = self.feed()
        spent["periods"]["week"]["cost_usd"] = 0.01
        self.assertFalse(build_site.feed_is_empty(spent))
        self.assertFalse(build_site.feed_is_empty(self.feed(limits={"rows": [{"key": "weekly"}]})))
        self.assertFalse(build_site.feed_is_empty(self.feed(running=[{"project": "x"}])))
        for newer in (2, 1.5, "2", None):           # never hide "Update the coach"
            self.assertFalse(build_site.feed_is_empty(self.feed(version=newer)), newer)


M = 1000000


def ledger_session(project, model, out_main, out_sub=0):
    """A ledger row with output tokens only, so its cost is easy to work out."""
    return {"project": project, "scheduled": False,
            "models": {model: [[0, out_main, 0, 0, 0], [0, out_sub, 0, 0, 0]]}}


def cost_ledger():
    """Three days: one in August (All time then spans over 60 days), six projects on Nov 16,
    one session on Nov 17, the build day. Output prices per M: Fable 5.2 $40, Opus 5.5 $20,
    Sonnet 5.5 $10, Haiku 5.5 $0.50."""
    return {"version": build_site.L.VERSION, "days": {
        "2026-08-03": {"sessions": {"old00001": ledger_session("alpha", "claude-opus-5-5", M)}},
        "2026-11-16": {"sessions": {
            "s1aaaaaa": ledger_session("alpha", "claude-fable-5-2", M, M // 2),     # $40 + $20
            "s2bbbbbb": ledger_session("beta", "claude-opus-5-5", M // 2),          # $10
            "s3cccccc": ledger_session("gamma", "claude-sonnet-5-5", M // 2),       # $5
            "s4dddddd": ledger_session("delta", "claude-haiku-5-5", 4 * M),         # $2
            "s5eeeeee": ledger_session("epsilon", "claude-opus-5-5", M // 20),      # $1
            "s6ffffff": ledger_session("zeta", "claude-opus-5-5", M // 40)}},       # $0.50
        "2026-11-17": {"sessions": {"s7gggggg": ledger_session("alpha", "claude-opus-5-5", M // 4)}}}}


COST_DIGEST = {"totals": {"sessions": 2}, "sessions": [
    {"id": "s1aaaaaa-0000", "project_name": "alpha", "title": "Build the cost tab",
     "cost": {"usd": 40.0}, "subagents": {"cost": {"usd": 20.0}}},
    {"id": "s2bbbbbb-0000", "project_name": "beta", "title": "", "cost": {"usd": 10.0}}]}


def model_rows():
    return [
        {"name": "Claude Fable 5.2", "usd": 4982.10, "share": 0.66, "main_usd": 1120.40, "sub_usd": 3861.70,
         "first_try": 0.76, "edit_turns": 410, "tool_calls": 2340, "tool_errors": 96, "error_rate": 0.041,
         "redone_edits": 44, "corrections": 21, "where_most": "Testing, Debugging, Coding",
         "where": [{"name": "Bash", "kind": "tool", "errors": 58, "redone_edits": 31, "corrections": 6},
                   {"name": "Edit", "kind": "tool", "errors": 17, "redone_edits": 13, "corrections": 9},
                   {"name": "zoho-crm", "kind": "mcp", "errors": 3, "redone_edits": 0, "corrections": 4}]},
        {"name": "Claude Opus 5.5", "usd": 2401.40, "share": 0.32, "first_try": 0.88, "edit_turns": 120,
         "tool_calls": 1000, "tool_errors": 20, "error_rate": None, "redone_edits": 9, "corrections": 17,
         "where_most": "Delegation", "where": []},
        {"name": "Claude Haiku 5.5 (long prompt)", "usd": 0.42, "share": 0.0001, "where_most": None},
        {"name": "Claude Sonnet 5.5", "usd": 186.32, "share": 0.02, "first_try": None, "edit_turns": 0,
         "tool_calls": 750, "tool_errors": 3, "error_rate": 0.004, "redone_edits": 0, "corrections": 0,
         "where_most": None, "where": None},
        {"name": "Claude Haiku 5.5", "usd": 20.13, "share": 0.003, "first_try": None, "tool_calls": 0,
         "tool_errors": 0, "error_rate": None, "redone_edits": 0, "corrections": 0, "where_most": None},
        {"name": "Claude Sonnet 5", "usd": 3.10, "share": None},
    ]


class CostTabTest(SiteBase):
    write_feed = OverviewTest.write_feed
    run_build = OverviewTest.run_build
    run_script = OverviewTest.run_script

    def setUp(self):
        SiteBase.setUp(self)
        write(os.path.join(self.tmp, "coach-home", "ledger.json"), json.dumps(cost_ledger()))
        write(os.path.join(self.reports, "digests", "daily-2026-11-16.json"), json.dumps(COST_DIGEST))

    def test_split_parts_fold_past_four_into_other(self):
        rows = [{"name": n, "usd": v} for n, v in
                (("a", 60), ("b", 10), ("c", 5), ("d", 2), ("e", 1), ("f", 0.5), ("g", 0))]
        parts = build_site.split_parts(rows)
        self.assertEqual([(p["name"], p["usd"], p["folded"]) for p in parts],
                         [("a", 60, 0), ("b", 10, 0), ("c", 5, 0), ("Other", 3.5, 3)])
        self.assertAlmostEqual(sum(p["share"] for p in parts), 1.0, places=3)
        four = build_site.split_parts(rows[:4])                 # four fit: no Other
        self.assertEqual([p["name"] for p in four], ["a", "b", "c", "d"])
        self.assertEqual(build_site.split_parts([{"name": "x", "usd": 0}, {"name": "y", "usd": None}]), [])
        self.assertEqual(build_site.split_parts([{"name": "y", "usd": 2}, {"name": "x", "usd": 6}])[0]["name"], "x")

    def test_split_comes_from_the_ledger_for_each_period(self):
        page, _ = self.run_build()
        cost = self.page_data(page)["cost"]
        self.assertEqual(sorted(cost["periods"]), ["all", "month", "today", "week"])
        week = cost["periods"]["week"]["t"]
        self.assertAlmostEqual(week["usd"], 83.5, places=2)
        split = week["split"]
        self.assertEqual([(p["name"], p["usd"]) for p in split["project"]],
                         [("alpha", 65.0), ("beta", 10.0), ("gamma", 5.0), ("Other", 3.5)])
        self.assertEqual(split["project"][-1]["folded"], 3)
        self.assertEqual([(p["name"], p["usd"]) for p in split["model"]],
                         [("Claude Fable 5.2", 60.0), ("Claude Opus 5.5", 16.5),
                          ("Claude Sonnet 5.5", 5.0), ("Claude Haiku 5.5", 2.0)])
        self.assertEqual([(p["name"], p["usd"]) for p in split["agent"]],
                         [("Main sessions", 63.5), ("Subagents", 20.0)])
        for key in ("model", "project", "agent"):
            self.assertAlmostEqual(sum(p["usd"] for p in split[key]), week["usd"], places=1)
        today = cost["periods"]["today"]["t"]
        self.assertEqual((today["days"], today["usd"]), (1, 5.0))
        self.assertEqual(cost["periods"]["all"]["t"]["days"], 3)
        self.assertAlmostEqual(cost["periods"]["all"]["t"]["usd"], 103.5, places=2)
        # By token type: the price range of the models used, and the note on the costliest type
        out = dict((r["type"], r) for r in week["by_type"])["Output"]
        self.assertEqual(out["price"], "$0.50 to $40 per M")
        self.assertEqual(week["type_note"], build_site.TYPE_NOTES["Output"])
        # Top sessions: from the ledger, every day; a title where a digest has one
        sess = cost["periods"]["week"]["sessions"]
        self.assertEqual([(s["id"], s["title"], s["day"], s["when"], s["usd"], s["sub_usd"]) for s in sess],
                         [("s1aaaaaa", "Build the cost tab", "2026-11-16", "Nov 16", 60.0, 20.0),
                          ("s2bbbbbb", "", "2026-11-16", "Nov 16", 10.0, 0.0),
                          ("s3cccccc", "", "2026-11-16", "Nov 16", 5.0, 0.0),      # more tokens than s7
                          ("s7gggggg", "", "2026-11-17", "Nov 17", 5.0, 0.0),
                          ("s4dddddd", "", "2026-11-16", "Nov 16", 2.0, 0.0),
                          ("s5eeeeee", "", "2026-11-16", "Nov 16", 1.0, 0.0),
                          ("s6ffffff", "", "2026-11-16", "Nov 16", 0.5, 0.0)])
        self.assertEqual(cost["periods"]["week"]["session_count"], 7)
        self.assertEqual([s["id"] for s in cost["periods"]["today"]["sessions"]], ["s7gggggg"])
        self.assertEqual(cost["periods"]["all"]["session_count"], 8)

    def test_cost_graph_per_period(self):
        page, _ = self.run_build()
        graphs = dict((k, page.split('<template id="cg-%s">' % k, 1)[1].split("</template>", 1)[0])
                      for k in ("today", "week", "month", "all"))
        week = graphs["week"]
        self.assertEqual(week.count('<g class="b"'), 7)                  # a stem and dot a day
        self.assertEqual(week.count('class="cg-dot pk"'), 1)
        self.assertIn('data-r="Mon Nov 16" data-v="$78.50" data-n="Above your average"', week)
        self.assertIn('data-n="Below your average"', week)
        self.assertIn('class="cg-peak"', week)
        self.assertIn(">Peak $79<", week)
        self.assertIn('class="cg-avg"', week)
        self.assertIn(">average $12 a day<", week)
        self.assertIn('data-unit="day">Average $12 a day, peak <b>$79</b>', week)
        self.assertIn(">Today</span>", week)
        # All time over 60 days: a point a week, each on its Monday
        allt = graphs["all"]
        self.assertIn('data-unit="week"', allt)
        self.assertIn('data-r="Week of Aug 3"', allt)
        self.assertIn(">This week</span>", allt)
        self.assertEqual(allt.count('<g class="b"'), 16)
        # the scale's top is round and so is its half
        self.assertIn(">$100</span>", week)
        self.assertIn(">$50</span>", week)
        # the feed's series wins where it has one (here Today, by the hour)
        feed = live_feed()
        self.write_feed(feed)
        page, _ = self.run_build()
        today = page.split('<template id="cg-today">', 1)[1].split("</template>", 1)[0]
        self.assertIn('data-unit="hour"', today)
        self.assertIn('data-r="04:00" data-v="$6.90"', today)
        self.assertEqual(build_site.cost_graph("week", [], "day", "2026-11-17"), "")
        g = build_site.cost_graph("week", [("2026-11-15T00:00", 1), ("2026-11-16T00:00", 8),
                                            ("2026-11-17T00:00", 10)], "day", "2026-11-17")
        self.assertIn('class="cg-dot hi"', g)                         # above the average, not the peak
        self.assertIn('class="cg-dot pk last"', g)
        self.assertIn('class="cg-dot lo"', g)

    def test_removed_tables_and_the_period_hash(self):
        page, _ = self.run_build()
        for gone in ('costBlock("By project"', '"By model", sortable', "function sortable", "costIdx",
                     'class="chart cost-chart"', "By session, top", "All time means since"):
            self.assertNotIn(gone, page, gone)
        self.assertNotIn('<h2 class="view-title">Cost</h2>', page)
        # the summary row as it was
        self.assertIn('[[usd(t.usd), "API-equivalent, not a bill"], [tok(t.tokens), "tokens"], '
                      '[tok(t.out), "output tokens"],', page)
        # the period buttons drive the Cost tab too, and its address keeps the period
        self.assertIn('var at = view === "cost" ? "cost" : "overview";', page)
        self.assertIn('history.replaceState(null, "", "#" + at + "/" + p)', page)
        self.assertIn('(v === "overview" || v === "cost") && hash[0] === v && PERIODS.indexOf(hash[1]) >= 0', page)
        self.assertIn('(v === "cost" && !!D.cost)', page)
        self.assertIn('localStorage.setItem("coach.split", split)', page)

    def test_series_colours_in_both_themes(self):
        page, _ = self.run_build()
        css = page.split("<style", 1)[1].split("</style>", 1)[0]
        self.assertEqual(css.count("--s1: #6366f1; --s2: #a78bfa; --s3: #2dd4bf; --s4: #38bdf8;"), 2)
        light = re.search(r"--s1: (#\w+); --s2: (#\w+); --s3: (#\w+); --s4: (#\w+);", css).groups()

        def lum(c):
            v = [int(c[i:i + 2], 16) / 255.0 for i in (1, 3, 5)]
            v = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in v]
            return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]
        for c in light:                                   # 3:1 on the light card, for bars
            self.assertGreaterEqual((lum("#fcfcfb") + 0.05) / (lum(c) + 0.05), 3.0, c)
        # series colours stay off status: no warn, crit or good colour among them
        for status in ("#fab219", "#d03b3b", "#0ca30c", "#ec6a6a"):
            self.assertNotIn(status, light)

    def test_model_rows_levels_and_fold(self):
        self.assertEqual([build_site.error_level(r) for r in (None, 0.0, 0.0199, 0.02, 0.0399, 0.04, 0.2)],
                         [None, None, None, "warn", "warn", "crit", "crit"])
        rows = build_site.live_models(model_rows())
        self.assertEqual([m["name"] for m in rows],
                         ["Claude Fable 5.2", "Claude Opus 5.5", "Claude Sonnet 5.5", "Claude Haiku 5.5"])
        fable, opus, sonnet, haiku = rows
        self.assertEqual((fable["error_level"], fable["rework_level"]), ("crit", "warn"))
        self.assertEqual((opus["error_rate"], opus["error_level"], opus["rework_level"]), (0.02, "warn", None))
        self.assertEqual((sonnet["error_level"], sonnet["where"], sonnet["first_try"]), (None, [], None))
        self.assertIsNone(haiku["error_rate"])                # no tool calls: nothing to divide by
        self.assertEqual([w["heat"] for w in fable["where"]], [100, 29, 5])
        self.assertEqual(fable["heat"], 100)
        self.assertEqual(build_site.smaller_models(model_rows()), {"count": 2, "usd": 3.52})
        self.assertIsNone(build_site.smaller_models(model_rows()[:4]))
        self.assertIsNone(build_site.live_models(None))
        self.assertIsNone(build_site.live_models({"name": "x"}))
        # rework: redone edits 9 and 10 either side of the amber line
        edge = build_site.live_models([{"name": "a", "redone_edits": 9}, {"name": "b", "redone_edits": 10}])
        self.assertEqual([m["rework_level"] for m in edge], [None, "warn"])
        # a server name the rules leave out is not shown under a model
        hide = build_site.Hide([{"match": ["zoho"]}], [])
        self.assertEqual([w["name"] for w in build_site.live_models(model_rows(), hide)[0]["where"]], ["Bash", "Edit"])

    def test_rework_note(self):
        note = build_site.rework_note(build_site.live_models(model_rows()))
        self.assertEqual(note["lead"], "Most rework: Claude Fable 5.2.")
        self.assertEqual(note["rest"], "31 of its 44 redone edits were in Bash. "
                                       "Its Bash calls fail most: 58 of 96 errors.")
        plain = build_site.rework_note(build_site.live_models([{"name": "Opus", "redone_edits": 1, "corrections": 3}]))
        self.assertEqual(plain["rest"], "1 redone edit and 3 corrections in all.")
        self.assertIsNone(build_site.rework_note(build_site.live_models([{"name": "Haiku", "redone_edits": 0}])))
        self.assertIsNone(build_site.rework_note(None))

    def test_sessions_over_midnight_count_once_and_years_show(self):
        led = cost_ledger()
        led["days"]["2026-11-17"]["sessions"]["s1aaaaaa"] = ledger_session(
            "alpha", "claude-opus-5-5", M // 4, M // 20)                        # $5 + $1
        write(os.path.join(self.tmp, "coach-home", "ledger.json"), json.dumps(led))
        page, _ = self.run_build()
        week = self.page_data(page)["cost"]["periods"]["week"]
        self.assertEqual(week["session_count"], 7)
        first = week["sessions"][0]
        self.assertEqual((first["title"], first["day"], first["usd"], first["sub_usd"]),
                         ("Build the cost tab", "2026-11-17", 66.0, 21.0))   # under the latest day it ran
        self.assertEqual([s["id"] for s in week["sessions"]].count("s1aaaaaa"), 1)
        self.assertEqual(build_site.year_day("2025-12-29", "2026-01-02"), "Dec 29, 2025")
        self.assertEqual(build_site.year_day("2026-01-02", "2026-01-02"), "Jan 2")
        self.assertEqual(build_site._point_names("2025-12-29T00:00", "week", "2026-01-02"),
                         "Week of Dec 29, 2025")
        # a ledger day from a wrong clock does not stretch All time past ten years
        days = build_site.cost_days("all", "2026-11-17", "1970-01-01")
        self.assertEqual((len(days), days[-1]), (build_site.COST_MAX_DAYS, "2026-11-17"))

    def test_today_without_a_ledger_day_shows_the_hours(self):
        led = cost_ledger()
        del led["days"]["2026-11-17"]
        write(os.path.join(self.tmp, "coach-home", "ledger.json"), json.dumps(led))
        self.write_feed(live_feed())
        page, _ = self.run_build()
        today = self.page_data(page)["cost"]["periods"]["today"]
        self.assertEqual((today["t"]["days"], today["live_hours"]), (0, True))
        self.assertIn("Today's totals and split are recorded at the next recap.", page)
        # the 7-day graph is drawn from the ledger, so it adds up to the figures beside it
        week = page.split('<template id="cg-week">', 1)[1].split("</template>", 1)[0]
        self.assertIn('data-unit="day"', week)
        self.assertIn('data-r="Mon Nov 16" data-v="$78.50"', week)

    def test_the_feed_hours_are_not_used_past_a_date_or_account_rule(self):
        path = os.path.join(self.tmp, "coach-home", "ledger.json")
        live = build_site.live_view(live_feed())
        data, _g = build_site.load_cost(self.reports, "2026-11-17", path, build_site.NO_HIDE, live)
        self.assertTrue(data["periods"]["today"]["live_hours"])
        for cfg in ({"exclude_dates": [("2026-11-17", None)]}, {"only_account": "11aa22"}):
            hide = build_site.Hide(scope=build_site.SC.Scope(cfg=cfg))
            data, graphs = build_site.load_cost(self.reports, "2026-11-17", path, hide, live)
            self.assertFalse(data["periods"]["today"]["live_hours"], cfg)
            self.assertNotIn('data-unit="hour"', graphs, cfg)

    def test_where_most_naming_a_left_out_server_is_dropped(self):
        rows = model_rows()
        rows[0]["where_most"] = "zoho-crm, Testing"
        hide = build_site.Hide([{"match": ["zoho"]}], [])
        self.assertIsNone(build_site.live_models(rows, hide)[0]["where_most"])
        self.assertEqual(build_site.live_models(rows)[0]["where_most"], "zoho-crm, Testing")

    def test_feed_without_models_hides_the_card(self):
        view = build_site.live_view(live_feed())
        self.assertIsNone(view["periods"]["today"]["models"])
        self.assertIsNone(view["periods"]["today"]["models_more"])
        feed = live_feed()
        feed["periods"]["week"]["models"] = model_rows()
        week = build_site.live_view(feed)["periods"]["week"]
        self.assertEqual(len(week["models"]), 4)
        self.assertEqual(week["model_note"]["model"], "Claude Fable 5.2")

    @unittest.skipUnless(shutil.which("node"), "node is not installed: the page's script is not run")
    def test_the_page_script_draws_the_cost_tab_and_by_model(self):
        feed = live_feed()
        feed["periods"]["week"]["models"] = model_rows()
        got = self.run_script(feed, "2026-11-23T11:43:00+02:00")
        week = got["periods"]["week"]
        self.assertNotIn("Update the coach", week["tree"] + week["cost"])
        for said in ('"By model"', '"Errors and rework are counted on the model that made the call"',
                     '"Mostly Testing, Debugging, Coding"', '"66% of spend"', '"Under 1% of spend"',
                     'div.fig.is-crit "96"', 'div.fig.is-warn "44 edits"', '"and 21 corrections"',
                     '"4.1% of tool calls"', '"no tool calls"', '"No edits"', 'div.fig.is-good "None"',
                     '"Where the errors were"', '"zoho-crm, MCP"', '"2 smaller models, $3.52"',
                     '"Most rework: Claude Fable 5.2."', 'title="Tool calls this model made that failed'):
            self.assertIn(said, week["tree"], said)
        for key in ("today", "month", "all"):
            self.assertNotIn('"By model"', got["periods"][key]["tree"], key)   # no models: no card
        cost = week["cost"]
        for said in ('"$84"', '"API-equivalent, not a bill"', '"2"',
                     '"days with data"', '"Split by"', 'aria-pressed="true"', '"Model"', '"Project"',
                     '"Main or subagent"', '"Claude Fable 5.2"', '"72%"', 'section.cs-card.cs-top',
                     '"Top sessions"', '"Build the cost tab"', '"Session s2bbbbbb"', '"Nov 16"',
                     '"By token type"', '"$0.50 to $40 per M"', "div.tt-bar",
                     '"Output costs the most: the answers and the files Claude writes. List prices as of Nov 17."'):
            self.assertIn(said, cost, said)
        for gone in ('"By project"', '"By session', "Period"):
            self.assertNotIn(gone, cost, gone)
        today = got["periods"]["today"]["cost"]
        self.assertIn('"Session s7gggggg"', today)                 # the ledger's today
        self.assertIn('"$5.00"', today)
        self.assertIn('"day with data"', today)
        # a period with cost but no session to list still says when sessions come
        page, _ = self.run_build()
        self.assertIn('text: "Sessions are listed once the day\'s recap has run."', page)


class TopSessionsTest(unittest.TestCase):
    """Top sessions on the Cost tab come from the ledger, so every period lists its own days,
    whatever digests there are and whatever settings they were counted under."""
    TODAY = "2026-11-17"
    RULES = [{"match": ["secret"]}]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "coach-home")
        self.reports = os.path.join(self.home, "reports")
        old = os.environ.get("COACH_HOME")
        os.environ["COACH_HOME"] = self.home
        self.addCleanup(lambda: os.environ.__setitem__("COACH_HOME", old) if old is not None
                        else os.environ.pop("COACH_HOME", None))
        opus = "claude-opus-5-5"                                            # output $20 per M
        led = {"version": build_site.L.VERSION, "days": {
            "2026-08-29": {"sessions": {"aaaa0001-x": ledger_session("alpha", opus, M)}},           # $20
            "2026-11-01": {"sessions": {"bbbb0002-x": ledger_session("beta", opus, M // 2)}},       # $10
            "2026-11-10": {"sessions": {"cccc0003-x": ledger_session("gamma", opus, M // 4)}},      # $5
            "2026-11-12": {"sessions": {"cccc0003-x": ledger_session("gamma", opus, M // 4, M // 20)}},  # $5 + $1
            "2026-11-16": {"sessions": {
                "dddd0004-x": ledger_session("delta", opus, M // 10),                               # $2
                "ffff0006-x": ledger_session("secret-work", opus, M),          # an exclude rule's project
                "gggg0007-x": ledger_session("delta", opus, M),               # a digest says it is left out
                "hhhh0008-x": ledger_session("theta", opus, M)}},             # the live cache says it is hidden
            "2026-11-17": {"sessions": {"eeee0005-x": ledger_session("epsilon", opus, M // 20),   # $1
                                        "iiii0009-x": {"project": "iota", "scheduled": True,
                                                       "models": {opus: [[0] * 5, [0] * 5]}}}}}}
        self.hide = build_site.Hide(self.RULES, [], build_site.SC.Scope(
            cfg={"exclude": self.RULES, "only_account": "11aa22"}))
        led["exclude_sig"] = self.hide.scope.sig()           # written under the settings in force
        self.led = led
        self.ledger = os.path.join(self.home, "ledger.json")
        write(self.ledger, json.dumps(led))
        # the only digest: Nov 16, counted under older account rules (another scope_sig)
        write(os.path.join(self.reports, "digests", "daily-2026-11-16.json"), json.dumps({
            "scope_sig": "0123456789abcdef", "sessions": [
                {"id": "dddd0004-x", "project_name": "delta", "title": "Delta work", "cost": {"usd": 2.0}},
                {"id": "gggg0007-x", "project_name": "delta", "title": "The secret plan", "cost": {"usd": 20.0}},
                {"id": "zzzz9999-x", "project_name": "other", "title": "Other account job",
                 "cost": {"usd": 99.0}}]}))
        # the live feed's cache: a custom title for one session, a hidden one for another
        write(os.path.join(self.home, "live-cache.json"), json.dumps({"sig": self.hide.scope.sig(), "files": {
            "/p/b.jsonl": {"sid": "bbbb0002-x", "sub": False, "project": "beta",
                           "meta": {"titles": ["beta ai title"], "ctitles": ["Beta custom"]}},
            "/p/h.jsonl": {"sid": "hhhh0008-x", "sub": False, "hidden": True, "project": "",
                           "meta": {"titles": ["Not to be shown"], "ctitles": []}}}}))

    def periods(self, hide=None):
        data, _g = build_site.load_cost(self.reports, self.TODAY, self.ledger, hide or self.hide)
        return data["periods"]

    def test_each_period_lists_its_own_days(self):
        p = self.periods()
        got = dict((k, [(s["id"], s["usd"]) for s in p[k]["sessions"]]) for k in p)
        self.assertEqual(got["today"], [("eeee0005", 1.0), ("iiii0009", 0.0)])   # no tokens: $0, not "no price"
        self.assertEqual(got["week"], [("cccc0003", 6.0), ("dddd0004", 2.0), ("eeee0005", 1.0),
                                       ("iiii0009", 0.0)])
        self.assertEqual(got["month"], [("cccc0003", 11.0), ("bbbb0002", 10.0), ("dddd0004", 2.0),
                                        ("eeee0005", 1.0), ("iiii0009", 0.0)])
        # All time reaches back past the first digest (Nov 16) to the ledger's first day
        self.assertEqual(got["all"], [("aaaa0001", 20.0), ("cccc0003", 11.0), ("bbbb0002", 10.0),
                                      ("dddd0004", 2.0), ("eeee0005", 1.0), ("iiii0009", 0.0)])
        self.assertEqual(p["all"]["sessions"][0]["day"], "2026-08-29")
        self.assertEqual(p["all"]["sessions"][0]["when"], "Aug 29")
        self.assertEqual(p["today"]["sessions"][1]["kind"], "scheduled")
        self.assertEqual([p[k]["session_count"] for k in ("today", "week", "month", "all")], [2, 4, 5, 6])

    def test_a_hidden_flag_from_older_cache_settings_hides_no_row(self):
        path = os.path.join(self.home, "live-cache.json")
        with open(path) as fh:
            cache = json.load(fh)
        cache["sig"] = "0123456789abcdef"                     # a rule since removed
        write(path, json.dumps(cache))
        rows = dict((s["id"], s["title"]) for s in self.periods()["all"]["sessions"])
        self.assertEqual(rows.get("hhhh0008"), "")             # listed again, still without that title

    def test_a_ledger_written_under_other_settings_lists_no_session(self):
        self.led["exclude_sig"] = "0123456789abcdef"          # e.g. before only_account changed
        write(self.ledger, json.dumps(self.led))
        p = self.periods()
        self.assertEqual([(p[k]["sessions"], p[k]["session_count"]) for k in p], [([], 0)] * 4)

    def test_a_session_over_several_days_is_one_row(self):
        month = self.periods()["month"]["sessions"]
        rows = [s for s in month if s["id"] == "cccc0003"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["usd"], rows[0]["sub_usd"], rows[0]["day"], rows[0]["project"], rows[0]["kind"]),
                         (11.0, 1.0, "2026-11-12", "gamma", "interactive"))

    def test_left_out_and_other_account_work_never_shows(self):
        p = self.periods()
        blob = json.dumps(p)
        for gone in ("ffff0006", "gggg0007", "hhhh0008", "zzzz9999", "secret", "Other account job",
                     "Not to be shown"):
            self.assertNotIn(gone, blob, gone)
        self.assertTrue(all(s["id"] != "gggg0007" for k in p for s in p[k]["sessions"]))

    def test_titles_from_old_digests_the_cache_or_the_id(self):
        titles = dict((s["id"], s["title"]) for s in self.periods()["all"]["sessions"])
        self.assertEqual(titles, {"dddd0004": "Delta work",      # a digest under older account rules
                                  "bbbb0002": "Beta custom",     # the live cache's custom title
                                  "cccc0003": "", "aaaa0001": "", "eeee0005": "",
                                  "iiii0009": ""})
        # a digest's title wins over the cache's
        write(os.path.join(self.reports, "digests", "daily-2026-11-01.json"), json.dumps({
            "sessions": [{"id": "bbbb0002-x", "title": "Beta from the digest"}]}))
        titles = dict((s["id"], s["title"]) for s in self.periods()["all"]["sessions"])
        self.assertEqual(titles["bbbb0002"], "Beta from the digest")


if __name__ == "__main__":
    unittest.main()
