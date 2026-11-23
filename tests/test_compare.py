"""Tests for coach/compare.py with small synthetic transcripts.

Run: python3 -m unittest discover -s tests -v
"""
import contextlib
import datetime
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "coach"))

import compare  # noqa: E402

D = datetime.date
TODAY = D(2026, 11, 17)          # a Tuesday
USAGE = {"input_tokens": 10, "output_tokens": 100, "cache_read_input_tokens": 1000,
         "cache_creation_input_tokens": 50}


def write_session(projects, sid, day, prompts=3, calls=10, errors=0, first=None,
                  usage=None, project="-tmp-demo", cwd="/tmp/demo"):
    """One session at midday local time on `day`: prompts, then Bash calls 2 minutes apart."""
    t = [time.mktime((day.year, day.month, day.day, 12, 0, 0, 0, 0, -1))]
    lines = []

    def add(rec, step=120):
        t[0] += step
        rec.update({"uuid": "%s-%d" % (sid[:8], len(lines)), "sessionId": sid, "cwd": cwd,
                    "isSidechain": False,
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t[0])) + ".000Z"})
        lines.append(json.dumps(rec, separators=(",", ":")))

    if first:
        add({"type": "user", "message": {"role": "user", "content": first}})
    for i in range(prompts):
        add({"type": "user", "origin": {"kind": "human"},
             "message": {"role": "user", "content": "please do step %d of the %s job" % (i, sid[:4])}})
    for i in range(calls):
        tid = "toolu_%s_%d" % (sid[:8], i)
        add({"type": "assistant", "message": {
            "id": "msg_%s_%d" % (sid[:8], i), "model": "claude-test-1", "role": "assistant",
            "usage": usage or USAGE,
            "content": [{"type": "tool_use", "id": tid, "name": "Bash",
                         "input": {"command": "make step%d" % i}}]}})
        bad = i < errors
        add({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "is_error": bad,
             "content": "Exit code 1" if bad else "ok"}]}})
    folder = os.path.join(projects, project)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    with open(os.path.join(folder, sid + ".jsonl"), "w") as fh:
        fh.write("\n".join(lines) + "\n")


def sid(n):
    return "%08d-0000-0000-0000-000000000000" % n


def fake(days=7, findings=None, **counts):
    """A period's numbers without transcripts, for the rule tests."""
    c = dict((k, 0) for k in compare.COUNT_KEYS)
    c.update({"cost": None, "permission_prompts": None, "sessions": 10, "turns": 100,
              "active_min": 600, "tool_calls": 1000, "tokens_total": 1000000})
    c.update(counts)
    return {"version": compare.VERSION, "counts": c, "findings": findings or {}, "projects": {},
            "days": days, "first_day": "2026-11-01", "last_day": "2026-11-07",
            "sessions_seen": c["sessions"], "excluded": {"scheduled": 0, "coach": 0},
            "include_scheduled": False, "context_threshold": 500000, "source": "transcripts",
            "partial_sessions": 0}


def row(result, key):
    for r in result["metrics"] + result["findings"]:
        if r["key"] == key:
            return r
    raise KeyError(key)


class PlanTest(unittest.TestCase):
    def test_presets(self):
        before, after, notes = compare.plan([], TODAY)
        self.assertEqual((before["first"], before["end"]), (D(2026, 11, 3), D(2026, 11, 10)))
        self.assertEqual((after["first"], after["end"]), (D(2026, 11, 10), TODAY))
        self.assertEqual(notes, [])
        self.assertEqual(compare.plan(["last7"], TODAY)[:2], (before, after))
        before, after, _ = compare.plan(["last14"], TODAY)
        self.assertEqual((before["first"], after["first"], after["end"]),
                         (D(2026, 10, 20), D(2026, 11, 3), TODAY))
        # this week so far (Monday to today, today included) versus last week
        before, after, notes = compare.plan(["week"], TODAY)
        self.assertEqual((before["first"], before["end"]), (D(2026, 11, 9), D(2026, 11, 16)))
        self.assertEqual((after["first"], after["end"]), (D(2026, 11, 16), D(2026, 11, 18)))
        self.assertIn("2 of 7 days", notes[0])
        self.assertIn("today, which is still running", notes[1])
        words = ["2026-11-01", "2026-11-07", "2026-11-08", "2026-11-17"]
        self.assertIn("still running", compare.plan(words, TODAY)[2][0])

    def test_around_a_pivot_date(self):
        before, after, notes = compare.plan(["around", "2026-11-05", "5"], TODAY)
        self.assertEqual((before["first"], before["end"]), (D(2026, 10, 31), D(2026, 11, 5)))
        self.assertEqual((after["first"], after["end"]), (D(2026, 11, 5), D(2026, 11, 10)))
        self.assertEqual(notes, [])
        # the after side cannot run past today, and the plan says so
        before, after, notes = compare.plan(["around", "2026-11-14"], TODAY)
        self.assertEqual((before["first"], before["end"]), (D(2026, 11, 7), D(2026, 11, 14)))
        self.assertEqual(after["end"], D(2026, 11, 18))
        self.assertIn("4 days instead of 7", notes[0])

    def test_explicit_ranges(self):
        want = ({"first": D(2026, 11, 1), "end": D(2026, 11, 8)},
                {"first": D(2026, 11, 8), "end": D(2026, 11, 15)})
        words = ["2026-11-01", "2026-11-07", "2026-11-08", "2026-11-14"]
        self.assertEqual(compare.plan(words, TODAY)[:2], want)
        # later period first, filler words and commas are fine
        self.assertEqual(compare.plan(["2026-11-08", "to", "2026-11-14,", "vs", "2026-11-07",
                                       "2026-11-01"], TODAY)[:2], want)

    def test_against_a_snapshot(self):
        before, after, _ = compare.plan(["against", "Pre-Hooks", "last3"], TODAY)
        self.assertEqual(before, {"snapshot": "pre-hooks"})
        self.assertRaises(compare.UsageError, compare.plan, ["against", "to", "last7"], TODAY)
        self.assertEqual((after["first"], after["end"]), (D(2026, 11, 14), TODAY))

    def test_one_period(self):
        p = compare.one_period
        self.assertEqual(p([], TODAY), {"first": D(2026, 11, 10), "end": TODAY})
        self.assertEqual(p(["lastweek"], TODAY), {"first": D(2026, 11, 9), "end": D(2026, 11, 16)})
        self.assertEqual(p(["2026-11-03"], TODAY), {"first": D(2026, 11, 3), "end": D(2026, 11, 4)})
        self.assertEqual(p(["2026-11-09", "2026-11-03"], TODAY),
                         {"first": D(2026, 11, 3), "end": D(2026, 11, 10)})

    def test_bad_input_is_refused(self):
        for bad in (["tomorrow"], ["last0"], ["last999"], ["around"], ["around", "2026-13-45"],
                    ["around", "2027-01-01"], ["around", "2026-11-05", "x"],
                    ["2026-11-01", "2026-11-07"], ["against"], ["against", "../x"],
                    ["2026-11-01", "2026-11-07", "2026-11-05", "2026-11-12"],     # overlap
                    ["2026-11-01", "2026-11-07", "2026-11-08", "2026-12-30"],     # future
                    ["2026-11-01; rm -rf x"]):
            self.assertRaises(compare.UsageError, compare.plan, bad, TODAY)


class RulesTest(unittest.TestCase):
    def test_percent_change_edges(self):
        self.assertEqual(compare.pct_change(10, 15), 50.0)
        self.assertEqual(compare.pct_change(10, 0), -100.0)
        self.assertEqual(compare.pct_change(3, 4), 33.3)
        self.assertIsNone(compare.pct_change(0, 5))        # zero before: no percent
        self.assertIsNone(compare.pct_change(0, 0))
        self.assertIsNone(compare.pct_change(None, 5))
        self.assertIsNone(compare.pct_change(5, None))

    def test_normalisation(self):
        self.assertEqual(compare.normalise(30, 10), 3.0)
        self.assertEqual(compare.normalise(5, 200, 100), 2.5)
        self.assertIsNone(compare.normalise(5, 0))
        self.assertIsNone(compare.normalise(None, 4))
        # twice the work with twice the errors is no change once normalised
        r = compare.compare(fake(tool_errors=20), fake(tool_errors=40, tool_calls=2000,
                                                       turns=200, active_min=1200, sessions=20))
        e = row(r, "tool_errors")
        self.assertEqual((e["before"], e["after"], e["change"], e["pct"]), (20, 40, 20, 100.0))
        self.assertEqual((e["basis"], e["norm_before"], e["norm_after"], e["norm_pct"]),
                         ("per 100 tool calls", 2.0, 2.0, 0.0))
        self.assertEqual(e["state"], "no_real_change")
        t = row(r, "turns")
        self.assertEqual((t["basis"], t["norm_before"], t["norm_after"]), ("per session", 10.0, 10.0))

    def test_threshold_decides_the_verdict(self):
        r = compare.compare(fake(tool_errors=50, corrections=20, tokens_out=100000),
                            fake(tool_errors=59, corrections=10, tokens_out=150000))
        self.assertEqual(row(r, "tool_errors")["state"], "no_real_change")     # +18%
        self.assertEqual(row(r, "corrections")["state"], "improvement")        # -50%
        self.assertEqual(row(r, "tokens_out")["state"], "up")                  # neutral metric
        self.assertEqual(r["highlights"][0], "corrections")                    # judged rows first
        r = compare.compare(fake(tool_errors=50), fake(tool_errors=60))
        self.assertEqual(row(r, "tool_errors")["state"], "regression")         # +20% clears it
        r = compare.compare(fake(tool_errors=5000, tool_calls=100000),
                            fake(tool_errors=5998, tool_calls=100000))
        e = row(r, "tool_errors")                # 19.96% shows as 20.0 but does not clear it
        self.assertEqual((e["norm_pct"], e["state"]), (20.0, "no_real_change"))
        r = compare.compare(fake(tool_errors=5), fake(tool_errors=6))     # exactly 20%
        self.assertEqual(row(r, "tool_errors")["state"], "regression")
        r = compare.compare(fake(findings={"x": 5}, sessions=3), fake(findings={"x": 6}, sessions=3))
        self.assertEqual(row(r, "finding:x")["state"], "regression")
        r = compare.compare(fake(tokens_out=150000), fake(tokens_out=100000))
        self.assertEqual(row(r, "tokens_out")["state"], "down")
        # a share moves in points: 10 points is real, 5 is not
        r = compare.compare(fake(subagent_tokens=100000), fake(subagent_tokens=150000))
        s = row(r, "subagent_share")
        self.assertEqual((s["before"], s["after"], s["pct"], s["state"]),
                         (10.0, 15.0, None, "no_real_change"))
        r = compare.compare(fake(subagent_tokens=100000), fake(subagent_tokens=300000))
        self.assertEqual(row(r, "subagent_share")["state"], "up")

    def test_zero_before(self):
        r = compare.compare(fake(compactions=0), fake(compactions=8))
        c = row(r, "compactions")
        self.assertEqual((c["pct"], c["norm_pct"], c["from_zero"], c["state"]),
                         (None, None, True, "regression"))
        self.assertIn("from 0", compare.text_table(r))
        r = compare.compare(fake(compactions=8), fake(compactions=0))
        c = row(r, "compactions")
        self.assertEqual((c["pct"], c["norm_pct"], c["state"]), (-100.0, -100.0, "improvement"))
        r = compare.compare(fake(), fake())
        self.assertEqual(row(r, "compactions")["state"], "too_little_data")    # 0 and 0

    def test_small_counts_get_no_percent(self):
        r = compare.compare(fake(compactions=1), fake(compactions=4))
        c = row(r, "compactions")
        self.assertEqual((c["state"], c["pct"], c["norm_pct"]), ("too_little_data", None, None))
        self.assertEqual((c["before"], c["after"]), (1, 4))                    # numbers stay
        self.assertNotIn("compactions", r["highlights"])

    def test_too_little_data_in_either_period(self):
        for thin in (fake(sessions=2, tool_errors=50), fake(active_min=20, tool_errors=50)):
            for before, after in ((thin, fake(tool_errors=10)), (fake(tool_errors=10), thin)):
                r = compare.compare(before, after)
                self.assertTrue(r["too_little_data"])
                self.assertIn("Too little data", r["notes"][0])
                self.assertEqual(r["highlights"], [])
                for m in r["metrics"]:
                    self.assertIn(m["state"], ("too_little_data", "not_available"))
                    self.assertIsNone(m["pct"])
                    self.assertIsNone(m["norm_pct"])
                self.assertIsNone(re.search(r"[+-]\d+%", compare.text_table(r)))

    def test_missing_figures_are_not_available(self):
        r = compare.compare(fake(), fake(cost=12.5, permission_prompts=9))
        self.assertEqual(row(r, "cost")["state"], "not_available")
        self.assertEqual(row(r, "permission_prompts")["state"], "not_available")
        r = compare.compare(fake(cost=10.0), fake(cost=20.0, active_min=600))
        c = row(r, "cost")
        self.assertEqual((c["basis"], c["norm_before"], c["norm_after"], c["state"]),
                         ("per active hour", 1.0, 2.0, "up"))

    def test_volume_and_length_are_called_out(self):
        r = compare.compare(fake(), fake(turns=250, active_min=1500))
        self.assertTrue(r["volume"]["differs"])
        self.assertIn("differ a lot in volume", r["volume"]["text"])
        self.assertIn(r["volume"]["text"], r["notes"])
        r = compare.compare(fake(), fake(turns=150, active_min=900))
        self.assertFalse(r["volume"]["differs"])
        r = compare.compare(fake(days=7), fake(days=3))
        self.assertTrue(any("differ in length" in n for n in r["notes"]))

    def test_findings_per_pattern(self):
        r = compare.compare(fake(findings={"correction_loop": 6, "context_bloat": 1}),
                            fake(findings={"correction_loop": 2}))
        f = row(r, "finding:correction_loop")
        self.assertEqual((f["before"], f["after"], f["basis"], f["state"]),
                         (6, 2, "per session", "improvement"))
        self.assertEqual(row(r, "finding:context_bloat")["state"], "too_little_data")


class TranscriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="coach-compare-test-")
        self.projects = os.path.join(self.tmp, "projects")
        self.old_env = dict(os.environ)
        os.environ["COACH_HOME"] = os.path.join(self.tmp, "home")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        n = [0]

        def some(day, count, **kw):
            for _ in range(count):
                n[0] += 1
                write_session(self.projects, sid(n[0]), day, **kw)

        # before: 2026-11-03 to 2026-11-09, after: 2026-11-10 to 2026-11-16
        some(D(2026, 11, 4), 2, errors=4)
        some(D(2026, 11, 6), 2, errors=4)
        some(D(2026, 11, 11), 2, errors=1)
        some(D(2026, 11, 13), 2, errors=1, project="-tmp-other", cwd="/tmp/<b>other")
        some(D(2026, 11, 12), 2, prompts=0, calls=3,
             first='<scheduled-task name="agentic-coach-daily">run it</scheduled-task>')
        some(D(2026, 11, 12), 1, prompts=1, calls=3, first="<command-name>/coach</command-name>")
        some(D(2026, 11, 12), 1, prompts=1, calls=3,
             first="<command-name>/agentic-coach:coach</command-name>")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_words(self, words, **kw):
        return compare.run_words(words, today=TODAY, projects_dir=self.projects, **kw)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = compare.main(list(args) + ["--projects-dir", self.projects], today=TODAY)
        return code, out.getvalue(), err.getvalue()

    def test_scheduled_and_coach_sessions_are_left_out(self):
        r = self.run_words([])
        self.assertEqual(r["after"]["excluded"], {"scheduled": 2, "coach": 2})
        self.assertEqual(r["before"]["excluded"], {"scheduled": 0, "coach": 0})
        s = row(r, "sessions")
        self.assertEqual((s["before"], s["after"]), (4, 4))
        self.assertEqual(row(r, "turns")["after"], 12)
        self.assertFalse(r["after"]["include_scheduled"])
        r = self.run_words([], include_scheduled=True)
        self.assertEqual(row(r, "sessions")["after"], 8)
        self.assertEqual(r["after"]["excluded"], {"scheduled": 0, "coach": 0})
        self.assertTrue(r["after"]["include_scheduled"])
        # a long working session that also ran the coach is kept
        long_one = {"kind": "interactive", "turns": 12, "slash_commands": {"/coach": 1}}
        self.assertIsNone(compare.exclusion(long_one))
        self.assertEqual(compare.exclusion(dict(long_one, turns=1)), "coach")
        self.assertEqual(compare.exclusion({"kind": "scheduled", "turns": 40}), "scheduled")

    def test_numbers_from_transcripts(self):
        r = self.run_words([])
        self.assertEqual((r["before"]["first_day"], r["before"]["last_day"], r["before"]["days"]),
                         ("2026-11-03", "2026-11-09", 7))
        self.assertEqual(r["before"]["source"], "transcripts")
        self.assertFalse(r["too_little_data"])
        e = row(r, "tool_errors")       # 16 of 40 calls, then 4 of 40
        self.assertEqual((e["before"], e["after"], e["norm_before"], e["norm_after"]),
                         (16, 4, 40.0, 10.0))
        self.assertEqual((e["pct"], e["norm_pct"], e["state"]), (-75.0, -75.0, "improvement"))
        out = row(r, "tokens_out")      # 10 calls of 100 output tokens in each of 4 sessions
        self.assertEqual((out["before"], out["after"], out["norm_before"]), (4000, 4000, 333.3333))
        total = row(r, "tokens_total")
        self.assertEqual(total["before"], 4 * 10 * (10 + 100 + 1000 + 50))
        self.assertEqual(row(r, "peak_context")["before"], 1060)
        self.assertNotEqual(row(r, "cost")["state"], "not_available")   # digests carry cost
        self.assertEqual(row(r, "permission_prompts")["state"], "not_available")
        self.assertIn("tool_errors", r["highlights"])
        self.assertLessEqual(len(r["highlights"]), 6)
        names = [p["project"] for p in r["projects"]]
        self.assertEqual(sorted(names), ["<b>other", "demo"])       # resolved project names
        demo = [p for p in r["projects"] if p["project"] == "demo"][0]
        self.assertEqual((demo["sessions"]["before"], demo["sessions"]["after"]), (4, 2))
        self.assertTrue(demo["too_little_data"])        # 2 sessions after: no percent
        self.assertIsNone(demo["tokens_per_prompt"]["pct"])
        self.assertEqual(sorted(demo["tool_errors"]), ["after", "before"])   # no percent on raw counts
        json.dumps(r)                                    # serialisable as it is

    def test_cost_and_context_mark_from_a_digest(self):
        digest = {"sessions": [
            {"id": "a" * 36, "kind": "interactive", "turns": 5, "active_min": 40, "tool_calls": 9,
             "project": "~/demo", "project_name": "Demo", "cost_usd": 1.5,
             "tokens": {"in": 1, "out": 2, "cache_read": 3, "cache_write": 4, "peak_context": 900},
             "subagents": {"tokens": {"in": 10, "out": 20, "cache_read": 30, "cache_write": 40,
                                      "cost": 0.5},
                           "repeated_fails": [{"cmd": "x", "fails": 3, "err": "e"}]},
             "errors": {"total": 7, "repeated": [{"cmd": "y", "fails": 2, "err": "e"},
                                                 {"cmd": "z", "fails": 5, "err": "e"}]},
             "denials": {"user-rejected": 2, "automode-blocked": 1, "top": []},
             "idle_gaps": {"count": 2, "longest": []}, "compactions_total": 9,
             "compactions": [{}, {}], "interruptions": 3, "corrections": 4},
            {"id": "b" * 36, "kind": "interactive", "turns": 1, "active_min": 5, "tool_calls": 1,
             "project": "~/demo", "tokens": {"in": 0, "out": 5, "cache_read": 0, "cache_write": 0,
                                             "peak_context": 100}}],
            "minor_sessions": [{"id": "cccccccc", "kind": "interactive", "turns": 2,
                                "tool_calls": 3, "out_tokens": 50},
                               {"id": "dddddddd", "kind": "scheduled", "turns": 0,
                                "tool_calls": 3, "out_tokens": 50}]}
        m = compare.period_metrics(digest, context_threshold=500)
        c = m["counts"]
        self.assertEqual((c["sessions"], c["turns"], c["tool_calls"]), (3, 8, 13))
        self.assertEqual((c["tokens_in"], c["tokens_out"], c["tokens_cache_read"],
                          c["tokens_cache_write"]), (11, 77, 33, 44))
        self.assertEqual((c["tokens_total"], c["subagent_tokens"]), (165, 100))
        self.assertEqual(c["cost"], 2.0)
        self.assertEqual((c["tool_errors"], c["repeated_failing_commands"]), (7, 2))
        self.assertEqual((c["permission_denials"], c["idle_resumes"], c["compactions"]), (3, 2, 9))
        self.assertEqual((c["peak_context"], c["sessions_over_context"]), (900, 1))
        self.assertEqual((m["sessions_seen"], m["partial_sessions"]), (4, 1))
        self.assertEqual(m["excluded"], {"scheduled": 1, "coach": 0})
        self.assertEqual(list(m["projects"]), ["Demo", "~/demo"])      # a resolved name wins
        self.assertEqual(m["findings"].get("multiple_compactions"), 1)
        self.assertEqual(c["findings_total"], sum(m["findings"].values()))

    def test_permission_prompts_from_the_log(self):
        home = os.environ["COACH_HOME"]
        os.makedirs(home)
        noon = time.mktime((2026, 11, 12, 12, 0, 0, 0, 0, -1))
        rows = [{"ts": noon, "session": sid(1), "tool": "Bash", "cmd": "ls"}] * 6
        rows.append({"ts": noon, "session": sid(9), "tool": "Bash", "cmd": "ls"})  # scheduled run
        rows.append({"ts": noon - 30 * 86400, "session": sid(1), "tool": "Bash", "cmd": "ls"})
        with open(os.path.join(home, "permission-prompts.jsonl"), "w") as fh:
            fh.write("not json\n" + "\n".join(json.dumps(x) for x in rows) + "\n")
        r = self.run_words([])
        p = row(r, "permission_prompts")
        self.assertEqual((p["before"], p["after"], p["state"]), (0, 6, "regression"))
        self.assertEqual(row(self.run_words([], include_scheduled=True),
                             "permission_prompts")["after"], 7)
        # a period from before the log began has no figure, not a zero
        with open(os.path.join(home, "permission-prompts.jsonl"), "w") as fh:
            fh.write("\n".join(json.dumps(x) for x in rows[:7]) + "\n")
        shutil.rmtree(os.path.join(home, "compare"))     # no earlier count to fall back on
        p = row(self.run_words([], store=False), "permission_prompts")
        self.assertEqual((p["before"], p["after"], p["state"]), (None, 6, "not_available"))
        self.run_words([])
        with open(os.path.join(home, "permission-prompts.jsonl"), "w") as fh:
            fh.write("\n".join(json.dumps(x) for x in rows) + "\n")
        self.assertEqual(row(self.run_words([]), "permission_prompts")["before"], 0)
        # the log rotates away: the count an earlier run stored for the same sessions is kept
        with open(os.path.join(home, "permission-prompts.jsonl"), "w") as fh:
            fh.write("not json\n")
        p = row(self.run_words([]), "permission_prompts")
        self.assertEqual((p["before"], p["after"], p["state"]), (0, 6, "regression"))
        # a log that now holds fewer of the period's prompts does not lower the stored count
        with open(os.path.join(home, "permission-prompts.jsonl"), "w") as fh:
            fh.write("\n".join(json.dumps(x) for x in rows[:2]) + "\n")
        self.assertEqual(row(self.run_words([]), "permission_prompts")["after"], 6)
        # the log does not name a project, so a project filter gets no figure
        p = row(self.run_words([], project="demo"), "permission_prompts")
        self.assertEqual(p["state"], "not_available")

    def test_snapshot_round_trip(self):
        code, out, _ = self.cli("snapshot", "--label", "Pre-Hooks", "2026-11-03", "2026-11-09")
        self.assertEqual(code, 0)
        saved = json.loads(out)
        self.assertEqual((saved["snapshot"], saved["sessions"]), ("pre-hooks", 4))
        want = os.path.join(os.environ["COACH_HOME"], "compare", "snapshots", "pre-hooks.json")
        self.assertEqual(saved["file"], want)
        with open(want) as fh:
            blob = fh.read()
        self.assertNotIn("please do step", blob)         # numbers only, no prompt text
        self.assertNotIn("skip_ids", blob)
        self.assertEqual([s["label"] for s in compare.list_snapshots()], ["pre-hooks"])
        live = self.run_words([])
        shutil.rmtree(self.projects)                     # the transcripts are gone
        shutil.rmtree(os.path.join(os.environ["COACH_HOME"], "compare", "periods"))
        back = compare.compare(compare.load_snapshot("pre-hooks"),
                               compare.load_snapshot("PRE-HOOKS"))
        self.assertEqual(row(back, "tool_errors")["before"], 16)
        self.assertEqual(back["before"]["source"], "snapshot pre-hooks")
        write_session(self.projects, sid(90), D(2026, 11, 11), errors=1)
        write_session(self.projects, sid(91), D(2026, 11, 12), errors=1)
        write_session(self.projects, sid(92), D(2026, 11, 13), errors=2)
        r = self.run_words(["against", "pre-hooks", "last7"])
        e = row(r, "tool_errors")
        self.assertEqual((e["before"], e["after"]), (row(live, "tool_errors")["before"], 4))
        self.assertEqual(r["before"]["snapshot"], "pre-hooks")
        self.assertTrue(any("snapshot pre-hooks" in n for n in r["notes"]))
        # a thinner rerun, or another period, does not replace it without --force
        code, _, err = self.cli("snapshot", "--label", "pre-hooks", "2026-11-03", "2026-11-09")
        self.assertEqual(code, 2)
        self.assertIn("left as it is", err)
        code, _, err = self.cli("snapshot", "--label", "pre-hooks", "last7")
        self.assertEqual(code, 2)
        self.assertIn("already holds", err)
        self.assertEqual(compare.load_snapshot("pre-hooks")["counts"]["sessions"], 4)
        code, _, err = self.cli("snapshot", "--label", "pre-hooks", "2026-11-03", "2026-11-09",
                                "--include-scheduled")
        self.assertEqual(code, 2)
        self.assertIn("other options", err)
        # a snapshot must end before the period it is compared with, and covers all projects
        self.assertRaises(compare.UsageError, self.run_words,
                          ["against", "pre-hooks", "2026-11-09", "2026-11-12"])
        self.assertRaises(compare.UsageError, self.run_words, ["against", "pre-hooks", "last7"],
                          project="demo")
        self.assertEqual(self.cli("snapshot", "--label", "x", "last7", "--no-store")[0], 2)
        self.assertEqual(self.cli("snapshot", "--label", "pre-hooks", "last7", "--force")[0], 0)
        forced = compare.load_snapshot("pre-hooks")
        self.assertEqual((forced["first_day"], forced["last_day"]), ("2026-11-10", "2026-11-16"))
        self.assertEqual(forced["counts"]["sessions"], 3)
        for bad in ("", "../x", "a/b", "x" * 41, ".hidden", "a b", "vs", "to"):
            self.assertRaises(compare.UsageError, compare.snapshot_name, bad)
        self.assertRaises(compare.UsageError, compare.load_snapshot, "nope")

    def test_stored_numbers_are_not_replaced_by_thinner_ones(self):
        first = self.run_words([])
        path = os.path.join(os.environ["COACH_HOME"], "compare", "periods",
                            "2026-11-03_to_2026-11-09.json")
        with open(path) as fh:
            stored = fh.read()
        os.remove(os.path.join(self.projects, "-tmp-demo", sid(1) + ".jsonl"))
        again = self.run_words([])
        self.assertEqual(row(again, "sessions")["before"], row(first, "sessions")["before"])
        self.assertEqual(again["before"]["source"], "stored numbers")
        self.assertTrue(any("stored numbers" in n for n in again["notes"]))
        with open(path) as fh:
            self.assertEqual(fh.read(), stored)
        # with nothing stored the thinner scan is all there is
        thin = self.run_words([], include_scheduled=True, store=False)
        self.assertEqual(thin["before"]["source"], "transcripts")
        self.assertEqual(row(thin, "sessions")["before"], 3)

    def test_fuller_recap_digest_is_reused_and_left_alone(self):
        import extract
        lo = compare.C.parse_when("2026-11-03")
        hi = compare.C.parse_when("2026-11-10")
        _digest, blob = extract.run(lo, hi, projects_dir=self.projects, inventory=False)
        folder = os.path.join(os.environ["COACH_HOME"], "reports", "digests")
        os.makedirs(folder)
        path = os.path.join(folder, "range-2026-11-03_to_2026-11-09.json")
        with open(path, "w") as fh:
            fh.write(blob + "\n")
        shutil.rmtree(os.path.join(self.projects, "-tmp-demo"))
        write_session(self.projects, sid(80), D(2026, 11, 5))
        r = self.run_words([], store=False)
        self.assertEqual(r["before"]["source"], "stored digest")
        self.assertEqual(row(r, "tool_errors")["before"], 16)
        with open(path) as fh:
            self.assertEqual(fh.read(), blob + "\n")
        self.assertEqual(os.listdir(folder), [os.path.basename(path)])
        # a damaged or shrunk digest never beats what is on disk
        for bad in ({"sessions": [1, 2, 3, 4, 5, 6]}, {"sessions": [{}], "minor_sessions": 5},
                    {"sessions": [{"id": "a"}], "minor_sessions": [{"id": "b"}] * 9}):
            with open(path, "w") as fh:
                json.dump(bad, fh)
            r = self.run_words([], store=False)
            self.assertEqual((r["before"]["source"], row(r, "sessions")["before"]),
                             ("transcripts", 1))

    def test_damaged_stored_numbers_are_ignored(self):
        good = self.run_words([])
        folder = os.path.join(os.environ["COACH_HOME"], "compare", "periods")
        path = os.path.join(folder, "2026-11-03_to_2026-11-09.json")
        with open(path) as fh:
            stored = json.load(fh)
        stored["sessions_seen"] = 99
        for key, value in (("days", "7"), ("partial_sessions", "x"), ("context_threshold", "x"),
                           ("excluded", []), ("projects", {"p": 3})):
            with open(path, "w") as fh:
                json.dump(dict(stored, **{key: value}), fh)
            r = self.run_words([], store=False)
            self.assertEqual(r["before"]["source"], "transcripts")
        with open(path, "w") as fh:
            json.dump(dict(stored, counts=dict(stored["counts"], sessions="4")), fh)
        self.assertEqual(self.run_words([], store=False)["before"]["source"], "transcripts")
        self.assertEqual(row(good, "sessions")["before"], 4)

    def test_cli_outputs_and_where_it_writes(self):
        code, out, err = self.cli("2026-11-03", "2026-11-09", "2026-11-10", "2026-11-16")
        self.assertEqual((code, err), (0, ""))
        r = json.loads(out)
        self.assertEqual(sorted(r), ["after", "before", "findings", "generated", "highlights",
                                     "metrics", "notes", "projects", "rules", "scope_sig",
                                     "too_little_data", "version", "volume"])
        self.assertEqual(r["rules"]["threshold_pct"], compare.RULES["threshold_pct"])
        code, text, _ = self.cli("--before", "2026-11-03", "2026-11-09", "--after", "2026-11-10",
                                 "2026-11-16", "--format", "text")
        self.assertEqual(code, 0)
        self.assertIn("Before: 2026-11-03 to 2026-11-09 (7 days), transcripts, left out 0 "
                      "scheduled and 0 coach sessions", text)
        self.assertIn("left out 2 scheduled and 2 coach sessions", text)
        line = [x for x in text.split("\n") if x.startswith("Tool errors")][0].split()
        self.assertEqual(line[2:6] + line[-4:], ["16", "4", "-12", "-75%", "40", "10",
                                                 "-75%", "improvement"])
        self.assertTrue(all(len(x) < 200 for x in text.split("\n")))
        code, out, _ = self.cli("--around", "2026-11-10", "--days", "7")
        self.assertEqual((code, json.loads(out)["before"]["first_day"]), (0, "2026-11-03"))
        code, out, _ = self.cli("snapshots")
        self.assertEqual((code, json.loads(out)), (0, {"snapshots": []}))
        for bad in (["nonsense"], ["--days", "3"], ["--around", "2026-11-10", "week"],
                    ["--before", "2026-11-03", "2026-11-09"], ["snapshot"],
                    ["--context-threshold", "0"]):
            code, out, err = self.cli(*bad)
            self.assertEqual((code, out), (2, ""))
            self.assertIn("coach compare:", err)
        # everything written sits under <coach home>/compare
        self.assertEqual(os.listdir(os.environ["COACH_HOME"]), ["compare"])
        self.assertEqual(sorted(os.listdir(os.path.join(os.environ["COACH_HOME"], "compare"))),
                         ["latest.json", "periods"])
        self.assertFalse(os.path.exists(os.environ["CLAUDE_CONFIG_DIR"]))

    def test_no_store_writes_nothing(self):
        code, _, _ = self.cli("last7", "--no-store")
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(os.environ["COACH_HOME"]))
        self.assertIsNone(compare.page_block())

    def test_page_block_is_safe_to_embed(self):
        self.assertEqual(self.cli("2026-11-03", "2026-11-09", "2026-11-10", "2026-11-16")[0], 0)
        block = compare.page_block()
        self.assertEqual(block["id"], "coach-compare-data")
        for ch in "<>&'":
            self.assertNotIn(ch, block["json"])
        self.assertTrue(all(ord(ch) < 128 for ch in block["json"]))
        data = json.loads(block["json"])
        self.assertEqual(data, block["data"])
        self.assertIn("<b>other", [p["project"] for p in data["projects"]])    # resolved names

    def test_weekly_file(self):
        path = compare.weekly_file("2026-11-17", today=TODAY, projects_dir=self.projects)
        self.assertEqual(path, os.path.join(os.environ["COACH_HOME"], "compare",
                                            "weekly-2026-11-17.json"))
        self.assertIsNone(compare.weekly_file("../x", today=TODAY, projects_dir=self.projects))
        with open(path) as fh:
            self.assertEqual(json.load(fh)["after"]["last_day"], "2026-11-16")


class ShippedFilesTest(unittest.TestCase):
    def test_reference_states_the_rules_in_force(self):
        with open(os.path.join(ROOT, "skills", "coach", "references", "compare.md")) as fh:
            text = fh.read()
        for want in ("Too little data", "At most six lines", "--include-scheduled",
                     "rules.threshold_pct", "rules.min_sessions", "compare.py"):
            self.assertIn(want, text)
        with open(os.path.join(ROOT, "coach", "compare.py")) as fh:
            code = fh.read()
        for text in (text, code):
            self.assertNotIn(chr(0x2013), text)
            self.assertNotIn(chr(0x2014), text)


if __name__ == "__main__":
    unittest.main()
