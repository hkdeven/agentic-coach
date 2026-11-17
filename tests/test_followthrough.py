"""Tests for follow-through tracking. Synthetic reports and digests only.

Run: python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "coach"))

import build_site  # noqa: E402
import followthrough as F  # noqa: E402

S1, S2, S3 = "aaaa1111", "bbbb2222", "cccc3333"


def session(sid, project="demo", peak=100000, active=60, turns=20, calls=100, **extra):
    s = {"id": sid + "-0000-0000-0000-000000000001", "project": project, "kind": "human",
         "start": "2026-11-03 09:00", "active_min": active, "turns": turns, "tool_calls": calls,
         "tokens": {"out": 5000, "peak_context": peak}}
    s.update(extra)
    return s


def digest(sessions, **totals):
    t = {"sessions": len(sessions), "turns": sum(s["turns"] for s in sessions),
         "active_min": sum(s["active_min"] for s in sessions),
         "tool_calls": sum(s["tool_calls"] for s in sessions),
         "errors": 0, "interruptions": 0, "corrections": 0, "compactions": 0}
    t.update(totals)
    return {"range": {"days": 7.0}, "totals": t, "sessions": sessions}


def finding(pattern, sid="", severity="medium"):
    return {"pattern": pattern, "severity": severity, "evidence": "e", "session": sid,
            "suggestion": "s"}


DAILY = """# Coach: %(day)s

1. What happened: %(sid)s, demo, 09:10: context peaked at 620k tokens after 70 prompts%(mark)s.
   Do instead: run /compact with a focus line, or start a new session from a handoff note.
2. What happened: %(sid)s, demo, 11:00: release notes were assembled by hand from 14 commits.
   Do instead: make the release notes routine a skill and call it by name.
"""

WEEKLY = """# Coach weekly: %(day)s

## Follow-through
- worse: "Run /compact" (given 2026-11-03). Sessions above 500k context per session went from 0.1 to 0.5.

## Week in numbers
3 sessions, 60 prompts.

## What cost the most
1. What happened: %(sid)s on 4 Nov: context peaked at 640k tokens.
   Do instead: at 300k type the handoff sentence (see P1) and open a new session.
   Trend: repeat since 2026-11-03, given 2 times; peaks are higher.
2. What happened: %(sid)s on 5 Nov: the same Edit failed 9 times in a subagent.
   Do instead: put a stop-after-two-failures line in every agent brief (P2).
   Trend: new.

## Proposed changes (not applied)
### P1: Handoff at 300k
Why: one line.
Change: add this line:
```
1. What happened: this is inside a fence and is not an item.
```
Where: the user CLAUDE.md

### P2: Stop after two failures
Why: one line.
Change: a line.
Where: the user CLAUDE.md

### P3: Allow read-only git
Why: one line.
Change: a rule.
Where: settings
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="coach-ft-")
        self.old = os.environ.get("COACH_HOME")
        os.environ["COACH_HOME"] = os.path.join(self.tmp, "home")
        self.root = os.path.join(self.tmp, "home", "reports")
        for name in ("daily", "weekly", "ranges", "digests"):
            os.makedirs(os.path.join(self.root, name))

    def tearDown(self):
        if self.old is None:
            os.environ.pop("COACH_HOME", None)
        else:
            os.environ["COACH_HOME"] = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def daily(self, day, sid=S1, mark="", findings=None, dig=None):
        if dig is not None:
            self.write("digests/daily-%s.json" % day, json.dumps(dig))
        if findings is not None:
            self.write("digests/daily-%s.findings.json" % day, json.dumps(findings))
        return self.write("daily/%s.md" % day, DAILY % {"day": day, "sid": sid, "mark": mark})

    def weekly(self, day, sid=S1, findings=None, dig=None):
        if dig is not None:
            self.write("digests/weekly-%s.json" % day, json.dumps(dig))
        if findings is not None:
            self.write("digests/weekly-%s.findings.json" % day, json.dumps(findings))
        return self.write("weekly/%s.md" % day, WEEKLY % {"day": day, "sid": sid})


class KeyTest(Base):
    def test_slug_ignores_ids_numbers_case_and_filler(self):
        a = F.advice_slug("Make the release notes routine a skill and call it by name.")
        b = F.advice_slug("make the Release Notes routine a SKILL, and call it by name "
                          "(aaaa1111, 14, 2026-11-03)")
        self.assertEqual(a, b)
        self.assertEqual(a, "advice:make-release-notes-routine-skill-call-name")
        self.assertRegex(a, F.KEY_RE)
        self.assertEqual(F.advice_slug("12 34"), "advice:unnamed")

    def test_pattern_key_takes_the_project_only_where_the_fix_is_per_project(self):
        self.assertEqual(F.make_key("context_bloat", "Demo App"), "context_bloat")
        self.assertEqual(F.make_key("repeated_failing_command", "Demo App"),
                         "repeated_failing_command@demo-app")
        self.assertEqual(F.make_key("repeated_failing_command", None), "repeated_failing_command")
        self.assertEqual(F.split_key("repeated_failing_command@demo-app"),
                         ("repeated_failing_command", "demo-app"))
        self.assertEqual(F.split_key("advice:some-words"), (None, None))
        dig = digest([session(S1, project="~/work/Demo App"), session(S2, project="(no folder)")])
        key, pattern, project = F.finding_key(finding("permission_denials", S1), dig)
        self.assertEqual((key, pattern, project),
                         ("permission_denials@demo-app", "permission_denials", "~/work/Demo App"))
        self.assertEqual(F.finding_key(finding("permission_denials", S2), dig)[0],
                         "permission_denials")
        self.assertTrue(F.same_advice("permission_denials@demo-app", "permission_denials"))
        self.assertFalse(F.same_advice("permission_denials@demo-app", "permission_denials@x"))
        self.assertFalse(F.same_advice("advice:a", "advice:b"))

    def test_item_across_two_projects_gets_the_bare_pattern_and_still_recurs(self):
        dig = digest([session(S1, project="~/work/one"), session(S2, project="~/work/two")])
        item = {"what": "%s and %s: the same Edit failed 9 times" % (S1, S2), "do": "stop",
                "ids": [S1, S2]}
        f = [finding("repeated_failing_command", S1), finding("repeated_failing_command", S2)]
        self.assertEqual(F.infer_pattern(item, f, dig)[0], "repeated_failing_command")
        self.assertEqual(F.infer_pattern(item, f[:1], dig)[0], "repeated_failing_command@one")
        log = [{"key": "repeated_failing_command@one", "date": "2026-11-03", "report": "daily/a"}]
        self.assertEqual(F.classify("repeated_failing_command", log, "2026-11-10")["status"],
                         "RECURRING")
        self.assertEqual(F.classify("repeated_failing_command@two", log, "2026-11-10")["status"],
                         "NEW")

    def test_same_advice_in_other_words_reuses_the_earlier_key(self):
        self.daily("2026-11-03")
        F.record_report(os.path.join(self.root, "daily", "2026-11-03.md"))
        log = F.load_log()
        first = [e for e in log if e["n"] == 2][0]["key"]
        again = F.text_key("Call the release notes routine by name: make it a skill.", log,
                           "2026-11-05")
        self.assertEqual(again, first)
        other = F.text_key("Allow the read-only git commands once in settings.", log, "2026-11-05")
        self.assertNotEqual(other, first)

    def test_feature_named(self):
        self.assertEqual(F.feature_named("switch to plan mode, then use a skill"), "plan mode")
        self.assertEqual(F.feature_named("add one CLAUDE.md line"), "CLAUDE.md rule")
        self.assertIsNone(F.feature_named("type less"))


class ParseTest(Base):
    def test_daily_lines_with_fixed_labels_and_the_marker(self):
        text = DAILY % {"day": "2026-11-05", "sid": S1,
                        "mark": " (repeat of 2026-11-03, given 3 times)"}
        items = F.parse_report(text, "daily")["items"]
        self.assertEqual([it["n"] for it in items], [1, 2])
        self.assertEqual(items[0]["ids"], [S1])
        self.assertEqual(items[0]["marker"], {"since": "2026-11-03", "times": 3})
        self.assertTrue(items[0]["do"].startswith("run /compact"))
        self.assertIsNone(items[1]["marker"])
        old = F.parse_marker("something happened (repeat of 2026-11-01).")
        self.assertEqual(old, {"since": "2026-11-01", "times": None})

    def test_the_page_parser_still_reads_the_extended_marker_and_the_block(self):
        text = DAILY % {"day": "2026-11-05", "sid": S1,
                        "mark": " (repeat of 2026-11-03, given 3 times)"}
        self.assertTrue(build_site.parse_daily(text)["items"][0]["repeat"])
        wk = build_site.parse_weekly(WEEKLY % {"day": "2026-11-10", "sid": S1})
        self.assertEqual(len(wk["items"]), 2)
        self.assertEqual(wk["items"][0]["state"], "repeat")
        self.assertEqual([p["id"] for p in wk["proposals"]], ["P1", "P2", "P3"])
        self.assertIn("3 sessions", wk["numbers"])

    def test_weekly_items_proposals_and_fences(self):
        got = F.parse_report(WEEKLY % {"day": "2026-11-10", "sid": S1}, "weekly")
        self.assertEqual(len(got["items"]), 2)
        self.assertEqual(got["items"][0]["proposals"], ["P1"])
        self.assertEqual(got["items"][0]["marker"], {"since": "2026-11-03", "times": 2})
        self.assertEqual([p["id"] for p in got["proposals"]], ["P1", "P2", "P3"])

    def test_nothing_notable_has_no_items(self):
        self.assertEqual(F.parse_report("# Coach: 2026-11-05\n\nNothing notable on 2026-11-05.\n",
                                        "daily")["items"], [])

    def test_report_meta(self):
        m = F.report_meta(os.path.join(self.root, "weekly", "2026-11-10.md"))
        self.assertEqual((m["first_day"], m["last_day"], m["date"], m["report"]),
                         ("2026-11-03", "2026-11-09", "2026-11-10", "weekly/2026-11-10.md"))
        m = F.report_meta(os.path.join(self.root, "ranges", "2026-11-01_to_2026-11-04.md"))
        self.assertEqual((m["mode"], m["first_day"], m["date"]),
                         ("range", "2026-11-01", "2026-11-04"))
        with self.assertRaises(ValueError):
            F.report_meta(os.path.join(self.root, "baseline", "SUMMARY.md"))


class RecordTest(Base):
    def test_new_then_recurring_with_since_and_count(self):
        f = [finding("context_bloat", S1)]
        for day in ("2026-11-03", "2026-11-04", "2026-11-05"):
            path = self.daily(day, findings=f, dig=digest([session(S1, peak=620000)]))
            got = F.record_report(path)
        self.assertEqual(got[0]["key"], "context_bloat")
        self.assertEqual(got[0]["key_source"], "finding")
        self.assertEqual(got[0]["given"], {"status": "RECURRING", "since": "2026-11-03", "times": 3})
        self.assertEqual(got[0]["metric"]["value"], 1)
        self.assertEqual(got[0]["numbers"]["peak_context"], 620000)
        self.assertEqual(got[0]["feature"], "/compact")
        self.assertEqual(got[1]["given"]["status"], "RECURRING")      # same words, same key
        first = [e for e in F.load_log() if e["date"] == "2026-11-03"]
        self.assertEqual([e["given"]["status"] for e in first], ["NEW", "NEW"])

        rows = F.annotate(f + [finding("multiple_compactions", S1)], date="2026-11-06")
        self.assertEqual([(r["key"], r["status"]) for r in rows],
                         [("context_bloat", "RECURRING"), ("multiple_compactions", "NEW")])
        self.assertEqual(rows[0]["marker"], "(repeat of 2026-11-03, given 4 times)")
        self.assertEqual(rows[0]["trend"], "repeat since 2026-11-03, given 4 times")
        self.assertEqual(rows[1]["marker"], "")
        self.assertRegex(rows[0]["marker"], build_site._REPEAT_RE)

    def test_annotate_text_candidates(self):
        F.record_report(self.daily("2026-11-03"))
        rows = F.annotate([], date="2026-11-04",
                          texts=["Make the release notes routine a skill, call it by name."])
        self.assertEqual(rows[0]["status"], "RECURRING")
        self.assertEqual(rows[0]["since"], "2026-11-03")

    def test_given_key_wins_and_bad_key_is_ignored(self):
        path = self.daily("2026-11-03")
        got = F.record_report(path, {1: "sprawling_session", 2: "Not A Key!"})
        self.assertEqual((got[0]["key"], got[0]["key_source"]), ("sprawling_session", "given"))
        self.assertEqual(got[1]["key_source"], "slug")

    def test_item_about_something_else_in_the_same_session_is_not_tied_to_the_finding(self):
        path = self.daily("2026-11-03", findings=[finding("context_bloat", S1)],
                          dig=digest([session(S1, peak=620000)]))
        got = F.record_report(path)
        self.assertTrue(got[1]["key"].startswith("advice:"))
        self.assertIsNone(got[1]["metric"])

    def test_record_twice_and_backfill_twice_change_nothing(self):
        self.daily("2026-11-04", findings=[finding("context_bloat", S1)],
                   dig=digest([session(S1, peak=620000)]))
        self.daily("2026-11-03")
        self.weekly("2026-11-10")
        self.write("ranges/2026-11-01_to_2026-11-02.md",
                   DAILY % {"day": "2026-11-01 to 2026-11-02", "sid": S2, "mark": ""})
        self.write("daily/notes.md", "not a report")
        one = F.backfill()
        with open(F.advice_file()) as fh:
            before = fh.read()
        two = F.backfill()
        with open(F.advice_file()) as fh:
            after = fh.read()
        self.assertEqual(before, after)
        self.assertEqual((one["reports"], one["items"]), (two["reports"], two["items"]))
        self.assertEqual(one["reports"], 4)
        F.record_report(os.path.join(self.root, "daily", "2026-11-03.md"))
        with open(F.advice_file()) as fh:
            self.assertEqual(fh.read(), before)
        log = F.load_log()
        self.assertEqual(len(set((e["report"], e["n"]) for e in log)), len(log))
        self.assertEqual([e["date"] for e in log], sorted(e["date"] for e in log))

    def test_weekly_proposals_link_to_keys(self):
        f = [finding("context_bloat", S1), finding("repeated_failing_command", S1)]
        F.record_report(self.weekly("2026-11-10", findings=f,
                                    dig=digest([session(S1, peak=640000)])))
        self.assertEqual(F.key_for_proposal("weekly/2026-11-10.md", "P1"), "context_bloat")
        self.assertEqual(F.key_for_proposal("weekly/2026-11-10.md", "p2"),
                         "repeated_failing_command@demo")
        self.assertEqual(F.key_for_proposal("weekly/2026-11-10.md", "P3"),
                         "proposal:allow-read-only-git")
        self.assertIsNone(F.key_for_proposal("weekly/2026-11-10.md", "P9"))


class MathsTest(unittest.TestCase):
    def test_measures_are_normalised(self):
        dig = digest([session(S1, peak=620000, active=120), session(S2, active=60),
                      session(S3, active=60)], compactions=6, corrections=5)
        m = F.measure("context_bloat", dig)
        self.assertEqual((m["value"], m["denom"], m["per"]), (1, 3, "session"))
        self.assertAlmostEqual(m["rate"], 0.3333, places=3)
        m = F.measure("multiple_compactions", dig)
        self.assertEqual((m["value"], m["rate"]), (6, 2.0))
        m = F.measure("correction_loop", dig)
        self.assertEqual((m["per"], m["denom"]), ("100 prompts", 60))
        self.assertAlmostEqual(m["rate"], 8.3333, places=3)
        self.assertIsNone(F.measure("advice:some-words", dig))
        self.assertIsNone(F.measure("context_bloat", None))

    def test_per_active_hour_and_project_scope(self):
        fails = {"total": 9, "repeated": [{"cmd": "x", "fails": 6}, {"cmd": "y", "fails": 2}]}
        dig = digest([session(S1, project="Demo", active=120, errors=fails),
                      session(S2, project="other", active=60,
                              errors={"total": 4, "repeated": [{"cmd": "z", "fails": 4}]})])
        m = F.measure("repeated_failing_command", dig)
        self.assertEqual((m["value"], m["denom"], m["rate"], m["per"]),
                         (10, 3.0, round(10 / 3.0, 4), "active hour"))
        m = F.measure("repeated_failing_command@demo", dig)
        self.assertEqual((m["value"], m["denom"], m["rate"], m["sessions"]), (6, 2.0, 3.0, 1))
        m = F.measure("repeated_failing_command@gone", dig)
        self.assertEqual((m["value"], m["sessions"], m["rate"]), (0, 0, None))

    def test_finding_counted_measures(self):
        dig = digest([session(S1), session(S2)])
        f = [finding("sprawling_session", S1), finding("sprawling_session", S2),
             finding("idle_then_resume", S1)]
        m = F.measure("sprawling_session", dig, f)
        self.assertEqual((m["value"], m["rate"]), (2, 1.0))
        self.assertEqual(F.measure("idle_then_resume", dig, f)["rate"], 0.5)

    def test_compare(self):
        def m(value, denom, sessions=5):
            return {"value": value, "denom": denom, "rate": value / float(denom) if denom else None,
                    "sessions": sessions, "per": "session", "name": "n"}
        self.assertEqual(F.compare(m(10, 10), m(5, 10)), "better")
        self.assertEqual(F.compare(m(10, 10), m(15, 10)), "worse")
        self.assertEqual(F.compare(m(10, 10), m(11, 10)), "same")
        # twice the raw count over four times the sessions is better, not worse
        self.assertEqual(F.compare(m(4, 10), m(8, 40)), "better")
        # the same raw count over half the sessions is worse, not the same
        self.assertEqual(F.compare(m(4, 10), m(4, 5)), "worse")
        self.assertEqual(F.compare(m(4, 10), m(0, 10)), "not seen again")
        self.assertEqual(F.compare(m(0, 10), m(0, 10)), "same")
        self.assertEqual(F.compare(m(0, 10), m(2, 10)), "worse")
        self.assertEqual(F.compare(m(4, 10), m(0, 0, sessions=0)), "no data")
        self.assertEqual(F.compare(None, m(4, 10)), "cannot measure")
        self.assertEqual(F.compare(m(4, 10), None), "cannot measure")
        # no rate (no active time logged): fall back to the raw counts
        self.assertEqual(F.compare(m(4, 0), m(2, 0)), "better")

    def test_combine_adds_days(self):
        days = [F.measure("context_bloat", digest([session(S1, peak=620000), session(S2)])),
                F.measure("context_bloat", digest([session(S3)])), None]
        m = F.combine(days)
        self.assertEqual((m["value"], m["denom"], m["sessions"]), (1, 3, 3))
        self.assertAlmostEqual(m["rate"], 0.3333, places=3)
        self.assertIsNone(F.combine([None]))


class WeeklyTest(Base):
    def seed(self, cur_sessions, cur_findings=None, **cur_totals):
        prev = digest([session(S1, peak=620000), session(S2, peak=700000), session(S3)],
                      compactions=3)
        f = [finding("context_bloat", S1), finding("multiple_compactions", S1)]
        text = ("# Coach weekly: 2026-11-10\n\n## What cost the most\n"
                "1. What happened: %s: context peaked at 620k tokens.\n"
                "   Do instead: start a new session from a handoff note.\n   Trend: new.\n"
                "2. What happened: %s: the session compacted 3 times.\n"
                "   Do instead: ask for a handoff at each natural break.\n   Trend: new.\n"
                "3. What happened: %s: notes were sent one at a time.\n"
                "   Do instead: send review notes as one batch and say go.\n   Trend: new.\n"
                % (S1, S1, S1))
        self.write("digests/weekly-2026-11-10.json", json.dumps(prev))
        self.write("digests/weekly-2026-11-10.findings.json", json.dumps(f))
        F.record_report(self.write("weekly/2026-11-10.md", text))
        self.write("digests/weekly-2026-11-17.json",
                   json.dumps(digest(cur_sessions, **cur_totals)))
        self.write("digests/weekly-2026-11-17.findings.json", json.dumps(cur_findings or []))

    def test_better_worse_and_cannot_measure_with_both_numbers(self):
        # context: 2 of 3 sessions, then 1 of 6. compactions: 3 in 3, then 12 in 6.
        cur = [session("dddd000%d" % i, peak=620000 if i == 0 else 1000) for i in range(6)]
        self.seed(cur, compactions=12)
        out = F.followthrough("2026-11-17")
        self.assertEqual(out["previous"], {"first_day": "2026-11-03", "last_day": "2026-11-09"})
        got = dict((it["key"], it) for it in out["items"])
        self.assertEqual(got["context_bloat"]["status"], "better")
        self.assertAlmostEqual(got["context_bloat"]["previous"]["rate"], 0.6667, places=3)
        self.assertAlmostEqual(got["context_bloat"]["current"]["rate"], 0.1667, places=3)
        self.assertEqual(got["multiple_compactions"]["status"], "worse")
        self.assertEqual((got["multiple_compactions"]["previous"]["rate"],
                          got["multiple_compactions"]["current"]["rate"]), (1.0, 2.0))
        written = [it for it in out["items"] if it["key"].startswith("advice:")][0]
        self.assertEqual(written["status"], "cannot measure")
        self.assertEqual([it["status"] for it in out["items"]],
                         ["worse", "better", "cannot measure"])
        self.assertTrue(out["block"].startswith("## Follow-through\n- worse: "))
        self.assertIn("went from 1 to 2 (3 in 3 sessions, then 12 in 6 sessions)", out["lines"][0])
        self.assertIn("went from 0.67 to 0.17", out["lines"][1])
        self.assertNotIn("better", out["lines"][2])
        for line in out["lines"]:
            self.assertNotIn(chr(0x2014), line)

    def test_not_seen_again_and_recurred(self):
        self.seed([session("dddd0001"), session("dddd0002")])
        self.daily("2026-11-12", sid="dddd0001")        # other advice, not a repeat
        path = self.write("daily/2026-11-13.md", "# Coach: 2026-11-13\n\n"
                          "1. What happened: dddd0001: notes one at a time again.\n"
                          "   Do instead: send the review notes as one batch, then say go.\n")
        F.record_report(path)
        out = F.followthrough("2026-11-17")
        got = dict((it["key"], it) for it in out["items"])
        self.assertEqual(got["context_bloat"]["status"], "not seen again")
        self.assertEqual(got["multiple_compactions"]["status"], "not seen again")
        written = [it for it in out["items"] if it["key"].startswith("advice:")][0]
        self.assertEqual((written["status"], written["again"]), ("recurred", "2026-11-13"))
        self.assertEqual(out["items"][0]["status"], "recurred")

    def test_empty_period_is_no_data_never_better(self):
        self.seed([])
        out = F.followthrough("2026-11-17")
        got = dict((it["key"], it["status"]) for it in out["items"])
        self.assertEqual(got["context_bloat"], "no data")
        self.assertEqual(got["multiple_compactions"], "no data")
        self.assertNotIn("better", out["block"])
        self.assertNotIn("not seen again", out["block"])

    def test_missing_current_digest_is_cannot_measure(self):
        self.seed([])
        os.remove(os.path.join(self.root, "digests", "weekly-2026-11-17.json"))
        out = F.followthrough("2026-11-17")
        self.assertEqual(set(it["status"] for it in out["items"]), set(["cannot measure"]))

    def test_previous_number_from_daily_digests_then_from_the_log(self):
        dig = digest([session(S1, peak=620000), session(S2)])
        F.record_report(self.daily("2026-11-05", findings=[finding("context_bloat", S1)], dig=dig))
        self.write("digests/daily-2026-11-06.json", json.dumps(digest([session(S3), session(S2)])))
        self.write("digests/weekly-2026-11-17.json", json.dumps(digest(
            [session("dddd0001", peak=900000), session("dddd0002", peak=900000)])))
        it = [i for i in F.followthrough("2026-11-17")["items"] if i["key"] == "context_bloat"][0]
        self.assertEqual((it["previous"]["source"], it["previous"]["value"],
                          it["previous"]["denom"]), ("daily digests", 1, 4))
        self.assertEqual((it["status"], it["current"]["rate"]), ("worse", 1.0))
        for name in ("daily-2026-11-05.json", "daily-2026-11-06.json"):
            os.remove(os.path.join(self.root, "digests", name))
        it = [i for i in F.followthrough("2026-11-17")["items"] if i["key"] == "context_bloat"][0]
        self.assertEqual((it["previous"]["source"], it["previous"]["rate"]), ("advice log", 0.5))

    def test_applied_line_and_at_most_five_lines(self):
        cur = [session("dddd000%d" % i) for i in range(3)]
        self.seed(cur, compactions=3)
        F.record_applied("context_bloat", "2026-11-11", note="handoff rule added")
        with open(F.applied_file(), "a") as fh:      # lines another tool wrote
            fh.write(json.dumps({"advice_key": "multiple_compactions",
                                 "ts": "2026-11-12T08:00:00"}) + "\n")
            fh.write("not json\n")
            fh.write(json.dumps({"key": "context_bloat", "date": "soon"}) + "\n")
        out = F.followthrough("2026-11-17")
        got = dict((it["key"], it) for it in out["items"])
        self.assertEqual(got["context_bloat"]["applied"], "2026-11-11")
        self.assertIn("not seen again, applied on 2026-11-11", got["context_bloat"]["line"])
        self.assertIn("Then sessions above 500k context per session moved from 0.67 to 0",
                      got["context_bloat"]["line"])
        self.assertEqual(got["multiple_compactions"]["applied"], "2026-11-12")
        self.assertIn("same, applied on 2026-11-12", got["multiple_compactions"]["line"])
        self.assertIn("stayed at 1 then 1", got["multiple_compactions"]["line"])
        self.assertEqual(len(F.followthrough("2026-11-17", limit=2)["lines"]), 2)
        self.assertEqual(F.followthrough("2026-11-17", limit=2)["omitted"], 1)
        self.assertLessEqual(len(out["lines"]), F.MAX_LINES)

    def test_no_earlier_advice_gives_no_block(self):
        out = F.followthrough("2026-11-17")
        self.assertEqual((out["items"], out["block"]), ([], ""))

    def test_applied_by_proposal_and_bad_input(self):
        F.record_report(self.weekly("2026-11-10", findings=[finding("context_bloat", S1)],
                                    dig=digest([session(S1, peak=640000)])))
        with open(F.applied_file(), "w") as fh:
            fh.write(json.dumps({"report": "weekly/2026-11-10.md", "proposal": "P1",
                                 "date": "2026-11-11"}) + "\n")
        self.assertEqual(F.applied_dates(), {"context_bloat": ["2026-11-11"]})
        with self.assertRaises(ValueError):
            F.record_applied("Bad Key")
        with self.assertRaises(ValueError):
            F.record_applied("context_bloat", "11 Nov")

    def test_page_data_and_escaped_block(self):
        self.seed([session("dddd0001")])
        path = self.write("daily/2026-11-12.md", "# Coach: 2026-11-12\n\n"
                          "1. What happened: dddd0001: a tag was typed.\n"
                          "   Do instead: write <b>less</b> & say \"go\".\n")
        F.record_report(path)
        data = F.page_data()
        json.dumps(data)
        self.assertEqual(data["followthrough"]["label"], "2026-11-17")
        self.assertEqual(sorted(r["status"] for r in data["advice"]), ["NEW"] * 4)
        block = F.page_block()
        text = json.dumps(block)
        self.assertNotIn("<b>", text)
        self.assertIn("&lt;b&gt;less&lt;/b&gt; &amp; say &quot;go&quot;", text.replace('\\"', '"'))


class ReviewFixTest(Base):
    def test_lines_this_module_cannot_read_survive_a_rewrite(self):
        with open(F.advice_file(), "w") as fh:
            fh.write('{"note": "from another tool"}\n{"key": "x", "date": "2026-11-01", "n": "1"\n')
            fh.write(json.dumps({"key": "x", "date": "2026-11-01", "report": "daily/a", "n": "1"})
                     + "\n")
            fh.write(json.dumps({"key": "x", "date": "2026-11-01", "report": "daily/a", "n": 2})
                     + "\n")
        F.record_report(self.daily("2026-11-03"))
        with open(F.advice_file()) as fh:
            text = fh.read()
        self.assertIn('{"note": "from another tool"}', text)
        self.assertIn('{"key": "x", "date": "2026-11-01", "n": "1"\n', text)
        self.assertEqual(len(F.load_log()), 4)

    def test_a_given_key_survives_backfill(self):
        path = self.daily("2026-11-03")
        F.record_report(path, {2: "sprawling_session"})
        F.backfill()
        got = [e for e in F.load_log() if e["n"] == 2][0]
        self.assertEqual((got["key"], got["key_source"]), ("sprawling_session", "given"))

    def test_proposals_after_one_line_and_same_line_fences(self):
        text = ("# Coach weekly: 2026-11-10\n\n## What cost the most\n"
                "1. What happened: x.\n   Do instead: y.\n   Trend: new.\n\n"
                "## Proposed changes (not applied)\n### P1: one\nChange: ```\nline\n```\n"
                "### P2: two\nChange: add\n```text```\n## not a section\n### P3: three\nWhy: z\n")
        got = F.parse_report(text, "weekly")
        self.assertEqual([p["id"] for p in got["proposals"]], ["P1", "P2", "P3"])
        self.assertEqual(len(got["items"]), 1)

    def test_long_slug_is_still_a_key(self):
        key = F.advice_slug(" ".join("src/components/some/very/long/path/number%d/file.tsx" % i
                                     for i in range(9)))
        self.assertRegex(key, F.KEY_RE)
        self.assertLessEqual(len(key), 100)

    def test_weekly_dated_d_comes_before_the_daily_for_d(self):
        f = [finding("context_bloat", S1)]
        dig = digest([session(S1, peak=640000)])
        F.record_report(self.weekly("2026-11-10", findings=f, dig=dig))
        got = F.record_report(self.daily("2026-11-10", findings=f, dig=dig))
        self.assertEqual(got[0]["given"], {"status": "RECURRING", "since": "2026-11-10", "times": 2})
        early = F.record_report(self.daily("2026-11-09", findings=f, dig=dig))
        self.assertEqual(early[0]["given"]["status"], "NEW")
        rows = F.annotate(f, date="2026-11-10", mode="weekly")
        self.assertEqual((rows[0]["status"], rows[0]["since"]), ("RECURRING", "2026-11-09"))

    def test_trimmed_digest_divides_by_the_sessions_it_can_see(self):
        dig = digest([session(S1, peak=620000, active=60)])
        dig["minor_sessions"] = [{"id": "eeee000%d" % i, "project": "demo", "turns": 5,
                                  "tool_calls": 9} for i in range(4)]
        dig["totals"]["sessions"] = 5
        m = F.measure("context_bloat", dig)
        self.assertEqual((m["value"], m["denom"], m["rate"], m["partial"]), (1, 5, 0.2, 4))
        dig["totals"]["compactions"] = 5
        self.assertNotIn("partial", F.measure("multiple_compactions", dig))   # from totals
        self.write("digests/weekly-2026-11-10.json", json.dumps(dig))
        f = [finding("context_bloat", S1)]
        self.write("digests/weekly-2026-11-10.findings.json", json.dumps(f))
        F.record_report(self.weekly("2026-11-10"))
        self.write("digests/weekly-2026-11-17.json", json.dumps(dig))
        line = [i for i in F.followthrough("2026-11-17")["items"]
                if i["key"] == "context_bloat"][0]["line"]
        self.assertIn("same: ", line)
        self.assertIn("8 small sessions are not in the digest detail", line)

    def test_first_backfill_already_has_the_final_order(self):
        f = [finding("context_bloat", S1)]
        dig = digest([session(S1, peak=640000)])
        self.weekly("2026-11-10", findings=f, dig=dig)
        self.daily("2026-11-10", findings=f, dig=dig)
        F.backfill()
        with open(F.advice_file()) as fh:
            before = fh.read()
        F.backfill()
        with open(F.advice_file()) as fh:
            self.assertEqual(fh.read(), before)
        got = [e for e in F.load_log() if e["report"] == "daily/2026-11-10.md"][0]
        self.assertEqual(got["given"]["status"], "RECURRING")

    def test_applied_before_the_advice_existed_adds_no_line(self):
        F.record_report(self.daily("2026-11-12", findings=[finding("context_bloat", S1)],
                                   dig=digest([session(S1, peak=640000)])))
        F.record_applied("context_bloat", "2026-11-05")
        self.write("digests/weekly-2026-11-17.json", json.dumps(digest([session(S1)])))
        self.assertEqual(F.followthrough("2026-11-17")["items"], [])

    def test_week_only_detectors_are_not_rebuilt_from_single_days(self):
        self.write("digests/daily-2026-11-05.json", json.dumps(digest([session(S1)])))
        self.assertIsNone(F.period_metric("unused_installed", self.root, "2026-11-03",
                                          "2026-11-09"))
        self.assertIsNotNone(F.period_metric("context_bloat", self.root, "2026-11-03",
                                             "2026-11-09"))

    def test_digest_that_is_not_an_object(self):
        path = self.daily("2026-11-03")
        self.write("digests/daily-2026-11-03.json", "[1, 2]")
        self.assertEqual(len(F.record_report(path)), 2)


class CliTest(Base):
    def run_cli(self, *args):
        p = subprocess.run([sys.executable, os.path.join(ROOT, "coach", "followthrough.py")]
                           + list(args), env=dict(os.environ), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
        return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")

    def test_every_subcommand(self):
        f = [finding("context_bloat", S1)]
        path = self.daily("2026-11-05", findings=f, dig=digest([session(S1, peak=620000)]))
        code, out, _ = self.run_cli(
            "annotate", "--findings", os.path.join(self.root, "digests",
                                                   "daily-2026-11-05.findings.json"))
        self.assertEqual((code, json.loads(out)[0]["status"]), (0, "NEW"))
        code, out, _ = self.run_cli("record", "--report", path, "--key", "2=sprawling_session")
        got = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual([i["key"] for i in got["items"]], ["context_bloat", "sprawling_session"])
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "home", "advice.jsonl")))
        code, out, _ = self.run_cli("applied", "--key", "context_bloat", "--date", "2026-11-12")
        self.assertEqual((code, json.loads(out)["key"]), (0, "context_bloat"))
        self.write("digests/weekly-2026-11-17.json", json.dumps(digest([session("dddd0001")])))
        code, out, _ = self.run_cli("weekly", "--label", "2026-11-17", "--format", "text")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("## Follow-through\n- "))
        self.assertIn("\n- not seen again, applied on 2026-11-12: ", out)
        code, out, _ = self.run_cli("weekly", "--label", "2026-11-17")
        self.assertEqual((code, json.loads(out)["period"]["first_day"]), (0, "2026-11-10"))
        code, out, _ = self.run_cli("backfill")
        self.assertEqual((code, json.loads(out)["reports"]), (0, 1))
        code, out, _ = self.run_cli("status")
        self.assertEqual((code, len(json.loads(out)["advice"])), (0, 2))

    def test_errors_exit_2_with_one_line(self):
        code, out, err = self.run_cli("record", "--report", os.path.join(self.tmp, "x.md"))
        self.assertEqual((code, out), (2, ""))
        self.assertTrue(err.startswith("coach followthrough: "))
        code, _, err = self.run_cli("applied")
        self.assertEqual(code, 2)
        code, _, _ = self.run_cli("record", "--report",
                                  self.daily("2026-11-05"), "--key", "one=two")
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
