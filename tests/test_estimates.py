"""Tests for coach/estimates.py: reading Claude's estimate and report lines, pairing them,
the totals by model, and where they show (the digest, the recap plan, the live feed and
the coach page). Transcripts are synthetic and everything runs in temp folders."""
import datetime
import json
import os
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "coach"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import build_site  # noqa: E402
import estimates as ES  # noqa: E402
import extract  # noqa: E402
import recap  # noqa: E402
import test_site as TS  # noqa: E402
from test_live import Home, NOW, TODAY, at, user, usage, utc  # noqa: E402

FABLE = "claude-fable-5-2"
OPUS = "claude-opus-5-5"
YESTERDAY = "2026-11-22"


def say(mid, ep, text, model=FABLE, **extra):
    """An assistant message with one text block."""
    d = {"parentUuid": None, "isSidechain": False, "type": "assistant", "cwd": "/tmp/demo/alpha",
         "gitBranch": "main", "timestamp": utc(ep),
         "message": {"id": mid, "model": model, "role": "assistant",
                     "content": [{"type": "text", "text": text}], "usage": usage(10, 20, 1000, 100)}}
    d.update(extra)
    return d


def ev(ts, model, est, act=None):
    return (ts, model, est, act)


class Reading(unittest.TestCase):
    def test_estimate_phrasings(self):
        for text, want in (("Estimate: 15 minutes.", [(15, None)]),
                           ("Building it now. Estimate 25 min.", [(25, None)]),
                           ("**Estimate:** 10 minutes", [(10, None)]),
                           ("Estimate 20 minutes to live on uat.", [(20, None)]),
                           ("(estimate 10 minutes, I'll send the pull request)", [(10, None)]),
                           ("Estimate: 2 hours", [(120, None)]),
                           ("Estimate ~1.5 h for all of it", [(90, None)]),
                           ("Estimate: about 30 minutes", [(30, None)])):
            self.assertEqual(ES.find(text), want, text)

    def test_report_phrasings(self):
        for text, want in (("Live on uat: estimate 15 min, actual 11.", [(15, 11)]),
                           ("PR #160 merged. Estimate 25 min, actual 15.", [(25, 15)]),
                           ("open against uat; estimate 20, actual 4 minutes.", [(20, 4)]),
                           ("Estimate 20 minutes, actual about 95.", [(20, 95)]),
                           ("1.1.0 is built, estimate 45, actual 47: coach page", [(45, 47)]),
                           ("Estimate 2 h, actual 150 min", [(120, 150)]),
                           ("estimate 2 hours, actual 95", [(120, 95)]),     # a bare actual is minutes
                           ("Estimate 15 min (actual 11)", [(15, 11)]),
                           ("Estimate 20 min. Actual 35 min.", [(20, 35)])):
            self.assertEqual(ES.find(text), want, text)

    def test_a_report_is_not_also_an_estimate(self):
        self.assertEqual(ES.find("Done: estimate 15 minutes, actual 11 minutes."), [(15, 11)])

    def test_both_in_one_message_keep_their_order(self):
        self.assertEqual(ES.find("First is live: estimate 10, actual 6. Next one now. Estimate: 20 minutes."),
                         [(10, 6), (20, None)])

    def test_not_time_estimates(self):
        for text in ("cost estimate about 240 dollars a month",
                     "Estimated 12 to 16 weeks as a block",
                     "the estimate screenshot (1515px wide)",
                     "Estimate 20 to 30 minutes",
                     "168 estimated hours of catch-up",
                     "Estimate: http://localhost:8792/EstimateIndex.dc.html",
                     "logging the estimate and updating the notes",
                     "Estimate 0 minutes",
                     "", None):
            self.assertEqual(ES.find(text), [], text)

    def test_model_ids(self):
        self.assertEqual(ES.model_id("claude-opus-5-5-20261101"), OPUS)
        self.assertEqual(ES.model_id(FABLE), FABLE)
        self.assertIsNone(ES.model_id("<synthetic>"))
        self.assertIsNone(ES.model_id(None))
        self.assertEqual(ES.events("Estimate 5 min", "t", "<synthetic>"), [])


class Pairing(unittest.TestCase):
    def test_report_pairs_with_the_estimate_and_takes_its_model(self):
        pieces, opened = ES.pair([ev("2026-11-23T10:00:00Z", OPUS, 15),
                                  ev("2026-11-23T10:20:00Z", FABLE, 15, 11)])
        self.assertEqual(opened, [])
        self.assertEqual(len(pieces), 1)
        self.assertEqual((pieces[0]["model"], pieces[0]["est"], pieces[0]["act"], pieces[0]["paired"]),
                         (OPUS, 15, 11, True))
        self.assertEqual(pieces[0]["t"], "2026-11-23T10:20:00Z")

    def test_the_latest_estimate_of_the_same_minutes_wins(self):
        pieces, opened = ES.pair([ev("2026-11-23T09:00:00Z", FABLE, 20),
                                  ev("2026-11-23T09:05:00Z", OPUS, 30),
                                  ev("2026-11-23T09:10:00Z", OPUS, 20),
                                  ev("2026-11-23T09:40:00Z", FABLE, 20, 25)])
        self.assertEqual([p["model"] for p in pieces], [OPUS])
        self.assertEqual([(o["model"], o["est"]) for o in opened], [(FABLE, 20), (OPUS, 30)])

    def test_a_report_with_no_estimate_stands_alone(self):
        pieces, opened = ES.pair([ev("2026-11-23T09:00:00Z", FABLE, 45),
                                  ev("2026-11-23T10:00:00Z", OPUS, 30, 25)])
        self.assertEqual([(p["model"], p["est"], p["act"], p["paired"]) for p in pieces],
                         [(OPUS, 30, 25, False)])
        self.assertEqual([o["est"] for o in opened], [45])

    def test_a_repeated_report_counts_once(self):
        pieces, _ = ES.pair([ev("2026-11-23T05:38:00Z", FABLE, 15),
                             ev("2026-11-23T05:49:00Z", FABLE, 15, 11),
                             ev("2026-11-23T05:50:00Z", FABLE, 15, 11),
                             ev("2026-11-23T05:50:00Z", FABLE, 15, 11)])
        self.assertEqual(len(pieces), 1)

    def test_the_same_figures_later_are_another_piece(self):
        pieces, _ = ES.pair([ev("2026-11-23T05:49:00Z", FABLE, 15, 11),
                             ev("2026-11-23T09:00:00Z", FABLE, 15, 11)])
        self.assertEqual(len(pieces), 2)

    def test_a_repeat_does_not_take_a_later_open_estimate(self):
        pieces, opened = ES.pair([ev("2026-11-23T05:00:00Z", FABLE, 15),
                                  ev("2026-11-23T05:10:00Z", FABLE, 15, 11),
                                  ev("2026-11-23T05:11:00Z", FABLE, 15),          # the next piece
                                  ev("2026-11-23T05:12:00Z", FABLE, 15, 11)])     # the report again
        self.assertEqual(len(pieces), 1)
        self.assertEqual([o["t"] for o in opened], ["2026-11-23T05:11:00Z"])

    def test_events_are_put_in_time_order(self):
        pieces, opened = ES.pair([ev("2026-11-23T10:20:00Z", FABLE, 15, 11), ev("2026-11-23T10:00:00Z", OPUS, 15)])
        self.assertEqual((pieces[0]["model"], opened), (OPUS, []))

    def test_bad_events_are_skipped(self):
        self.assertEqual(ES.pair([None, (1, 2), ["2026-11-23T10:00:00Z", FABLE, 5, None]]),
                         ([], [{"t": "2026-11-23T10:00:00Z", "model": FABLE, "est": 5}]))


class Totals(unittest.TestCase):
    def pieces(self):
        return [{"t": "a", "model": OPUS, "est": 15, "act": 11, "paired": True},
                {"t": "b", "model": FABLE, "est": 20, "act": 30, "paired": True},
                {"t": "c", "model": FABLE, "est": 40, "act": 45, "paired": False},
                {"t": "d", "model": OPUS, "est": 25, "act": 15, "paired": True}]

    def test_rows_per_model_sorted_by_model(self):
        rows = ES.rows(self.pieces(), [{"t": "e", "model": OPUS, "est": 10}])
        self.assertEqual([r["model"] for r in rows], [FABLE, OPUS])
        fable, opus = rows
        self.assertEqual((fable["pieces"], fable["est_min"], fable["act_min"], fable["overrun_pct"],
                          fable["no_actual"]), (2, 60, 75, 25, 0))
        self.assertEqual((opus["pieces"], opus["est_min"], opus["act_min"], opus["overrun_pct"],
                          opus["no_actual"]), (2, 40, 26, -35, 1))
        self.assertEqual(fable["name"], "Claude Fable 5.2")

    def test_a_model_with_only_open_estimates(self):
        rows = ES.rows([], [{"t": "e", "model": OPUS, "est": 10}])
        self.assertEqual((rows[0]["pieces"], rows[0]["est_min"], rows[0]["overrun_pct"], rows[0]["no_actual"]),
                         (0, 0, None, 1))

    def test_overrun(self):
        self.assertEqual(ES.overrun_pct(20, 30), 50)
        self.assertEqual(ES.overrun_pct(30, 20), -33)
        self.assertEqual(ES.overrun_pct(10, 10), 0)
        self.assertIsNone(ES.overrun_pct(0, 5))

    def test_block_totals(self):
        b = ES.block(self.pieces(), [{"t": "e", "model": OPUS, "est": 10}])
        self.assertEqual((b["pieces"], b["est_min"], b["act_min"], b["overrun_pct"], b["no_actual"]),
                         (4, 100, 101, 1, 1))
        self.assertIsNone(ES.block([], []))

    def test_minutes_keep_one_decimal(self):
        b = ES.block([{"t": "a", "model": FABLE, "est": 90.0, "act": 7.5, "paired": True}], [])
        self.assertEqual((b["rows"][0]["est_min"], b["rows"][0]["act_min"]), (90, 7.5))

    def test_line(self):
        line = ES.line(ES.block(self.pieces(), [{"t": "e", "model": OPUS, "est": 10}]))
        self.assertEqual(line, "Estimates: Claude Fable 5.2: 2 pieces, 60 min estimated, 75 min actual, 25% over; "
                               "Claude Opus 5.5: 2 pieces, 40 min estimated, 26 min actual, 35% under. "
                               "Not counted: 1 estimate with no stated actual.")
        self.assertEqual(ES.line(ES.block([], [{"t": "e", "model": OPUS, "est": 10}])),
                         "Estimates: no piece of work with a stated actual. "
                         "Not counted: 1 estimate with no stated actual.")
        self.assertEqual(ES.line(ES.block([{"t": "a", "model": OPUS, "est": 10, "act": 10}], [])),
                         "Estimates: Claude Opus 5.5: 1 piece, 10 min estimated, 10 min actual, on estimate.")
        self.assertIsNone(ES.line(None))


class InTheDigest(Home):
    def setUp(self):
        super(InTheDigest, self).setUp()
        self._cfg = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")

    def tearDown(self):
        if self._cfg is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self._cfg
        super(InTheDigest, self).tearDown()

    def transcript(self, sid, records, **kw):
        """As Home writes it, but compact, as Claude Code does (extract.py looks for the
        timestamp key without a space)."""
        path = super(InTheDigest, self).transcript(sid, [], **kw)
        st = os.stat(path)
        with open(path, "w") as fh:
            for r in records:
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")
        os.utime(path, (st.st_atime, st.st_mtime))
        return path

    def sessions(self):
        self.transcript("s1", [user("build the filter", at(TODAY, 9)),
                               say("m1", at(TODAY, 9, 1), "Starting. Estimate: 20 minutes.", OPUS),
                               user("how is it going", at(TODAY, 9, 20)),
                               say("m2", at(TODAY, 9, 31), "Merged and live. Estimate 20, actual 30.", FABLE),
                               say("m3", at(TODAY, 9, 32), "Live on uat: estimate 20, actual 30.", FABLE),
                               say("m4", at(TODAY, 10), "Next: the docs. Estimate 10 min.", FABLE)])
        self.transcript("s2", [user("fix it", at(TODAY, 11)),
                               say("m5", at(TODAY, 11, 1), "Estimate 40 min, actual 35.", OPUS)])
        # a subagent's own estimate is not one the person was given
        self.transcript("s1", [say("sub1", at(TODAY, 9, 5), "Estimate: 5 minutes, actual 50.", FABLE,
                                   isSidechain=True)], sub="agent-1")
        # a resumed copy repeats the first session's messages: counted once
        self.transcript("s3", [user("build the filter", at(TODAY, 9)),
                               say("m1", at(TODAY, 9, 1), "Starting. Estimate: 20 minutes.", OPUS),
                               say("m2", at(TODAY, 9, 31), "Merged and live. Estimate 20, actual 30.", FABLE)],
                        mtime=NOW - 60)

    def test_totals_by_model(self):
        self.sessions()
        since, until = at(TODAY, 0), at(TODAY, 23, 59)
        digest, _blob = extract.run(since, until, projects_dir=self.projects, inventory=False)
        est = digest["totals"]["estimates"]
        self.assertEqual([(r["model"], r["pieces"], r["est_min"], r["act_min"], r["overrun_pct"], r["no_actual"])
                          for r in est["rows"]],
                         [(FABLE, 0, 0, 0, None, 1), (OPUS, 2, 60, 65, 8, 0)])
        self.assertEqual((est["pieces"], est["no_actual"]), (2, 1))

    def test_no_estimates_no_block(self):
        self.transcript("s1", [user("hi", at(TODAY, 9)), say("m1", at(TODAY, 9, 1), "Hello.")])
        digest, _ = extract.run(at(TODAY, 0), at(TODAY, 23, 59), projects_dir=self.projects, inventory=False)
        self.assertNotIn("estimates", digest["totals"])

    def test_the_recap_plan_carries_the_line(self):
        self.sessions()
        out = recap.build([TODAY], today=datetime.date(2026, 11, 24), projects_dir=self.projects,
                          root=os.path.join(self.tmp, "reports"))
        self.assertEqual(out["estimates_line"],
                         "Estimates: Claude Opus 5.5: 2 pieces, 60 min estimated, 65 min actual, 8% over. "
                         "Not counted: 1 estimate with no stated actual.")
        self.assertEqual(out["estimates_block"],
                         "Estimates:\n- Claude Opus 5.5: 2 pieces, 60 min estimated, 65 min actual, 8% over\n"
                         "- Not counted: 1 estimate with no stated actual")
        out = recap.build([YESTERDAY], today=datetime.date(2026, 11, 24), projects_dir=self.projects,
                          root=os.path.join(self.tmp, "reports"))
        self.assertIsNone(out["estimates_line"])
        self.assertIsNone(out["estimates_block"])

    def test_the_live_feed_has_them_per_period(self):
        self.sessions()
        self.transcript("s4", [user("old work", at(YESTERDAY, 15)),
                               say("m9", at(YESTERDAY, 15, 1), "Estimate 30 min, actual 60.", FABLE)])
        f = self.feed()
        today = f["periods"]["today"]["estimates"]
        self.assertEqual([(r["model"], r["pieces"], r["est_min"], r["act_min"]) for r in today["rows"]],
                         [(FABLE, 0, 0, 0), (OPUS, 2, 60, 65)])
        week = f["periods"]["week"]["estimates"]
        self.assertEqual([(r["model"], r["pieces"], r["est_min"], r["act_min"], r["overrun_pct"])
                          for r in week["rows"]],
                         [(FABLE, 1, 30, 60, 100), (OPUS, 2, 60, 65, 8)])
        self.assertEqual(week["no_actual"], 1)
        self.assertNotIn("meaning", week)
        json.dumps(f)

    def test_an_excluded_project_has_none(self):
        self.sessions()
        self.config(exclude=[{"match": ["alpha"]}])
        f = self.feed()
        self.assertIsNone(f["periods"]["today"]["estimates"])


DAILY = """# Coach: 2026-11-23

1. What happened: a thing (session 1a2b3c4d, turn 4, 10:00)
   Do instead: another thing.

Totals: 3 sessions, $12.
Estimates: Claude Opus 5.5: 2 pieces, 60 min estimated, 65 min actual, 8% over.
Checked: 1 item against the transcripts.
"""


class OnThePage(TS.SiteBase):
    # the Overview helpers of test_site, without running its tests a second time here
    write_feed = TS.OverviewTest.write_feed
    run_build = TS.OverviewTest.run_build
    run_script = TS.OverviewTest.run_script
    all_time = TS.OverviewTest.all_time

    def test_the_daily_line_is_read_apart(self):
        day = build_site.parse_daily(DAILY)
        self.assertEqual(day["estimates"],
                         "Claude Opus 5.5: 2 pieces, 60 min estimated, 65 min actual, 8% over.")
        self.assertEqual(day["totals"], "3 sessions, $12.")
        self.assertNotIn("Estimates", day["note"])
        self.assertEqual(build_site.parse_daily(DAILY.replace("Estimates:", "Estimate"))["estimates"], "")

    def test_live_estimates_are_cleaned(self):
        got = build_site.live_estimates({"rows": [
            {"model": OPUS, "name": "Claude Opus 5.5", "pieces": 2, "est_min": 60, "act_min": 65,
             "overrun_pct": 8, "no_actual": 0},
            {"model": FABLE, "name": "Claude Fable 5.2", "pieces": 1, "est_min": 30, "act_min": 60,
             "overrun_pct": 100, "no_actual": 2},
            {"model": "x", "name": "bad", "pieces": "many"}, "junk"], "no_actual": 2})
        self.assertEqual([r["name"] for r in got["rows"]], ["Claude Fable 5.2", "Claude Opus 5.5"])
        self.assertEqual([r["heat"] for r in got["rows"]], [50, 100])
        self.assertEqual(got["no_actual"], 2)
        self.assertIsNone(build_site.live_estimates(None))
        self.assertIsNone(build_site.live_estimates({"rows": []}))

    @unittest.skipUnless(shutil.which("node"), "node is not installed: the page's script is not run")
    def test_the_overview_draws_the_card(self):
        est = {"rows": [{"model": FABLE, "name": "Claude Fable 5.2", "pieces": 1, "est_min": 30,
                         "act_min": 60, "overrun_pct": 100, "no_actual": 0},
                        {"model": OPUS, "name": "Claude Opus 5.5", "pieces": 2, "est_min": 60,
                         "act_min": 45, "overrun_pct": -25, "no_actual": 1}],
               "pieces": 3, "est_min": 90, "act_min": 105, "overrun_pct": 17, "no_actual": 1}
        feed = self.all_time()
        feed["periods"]["week"]["estimates"] = est
        got = self.run_script(feed, "2026-11-23T11:43:00+02:00")
        week, today = got["periods"]["week"]["tree"], got["periods"]["today"]["tree"]
        for said in ('"Estimates by model"', '"Claude Fable 5.2"', '"Claude Opus 5.5"', '"30 min"',
                     '"60 min"', '"100% over"', '"25% under"',
                     '"1 estimate with no stated actual, not counted"'):
            self.assertIn(said, week, said)
        self.assertNotIn("Estimates by model", today)       # a period with none has no card


if __name__ == "__main__":
    unittest.main()
