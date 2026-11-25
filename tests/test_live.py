"""Tests for coach/activity.py, coach/live.py, the limit capture in coach/statusline.py and
the live feed runner in coach/setup_check.py. Transcripts are synthetic and everything
runs in temp folders: no real transcript, setting or LaunchAgent is touched."""
import io
import json
import os
import plistlib
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "coach"))

import activity as A  # noqa: E402
import common as C  # noqa: E402
import extract as E  # noqa: E402
import ledger as L  # noqa: E402
import live as LV  # noqa: E402
import pricing as P  # noqa: E402
import setup_check as S  # noqa: E402
import statusline as SL  # noqa: E402

MODEL = "claude-fable-5-2"
TODAY = "2026-11-23"
NOW = time.mktime((2026, 11, 23, 12, 30, 0, 0, 0, -1))      # local 12:30 on Nov 23


def at(day, hh, mm=0, ss=0):
    """A local day and time as epoch seconds."""
    return time.mktime((int(day[0:4]), int(day[5:7]), int(day[8:10]), hh, mm, ss, 0, 0, -1))


def utc(ep):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ep)) + ".000Z"


def usage(inp=0, out=0, read=0, write=0):
    return {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": write}


def user(text, ep, cwd="/tmp/demo/alpha", **extra):
    d = {"parentUuid": None, "isSidechain": False, "type": "user", "cwd": cwd, "gitBranch": "main",
         "message": {"role": "user", "content": text}, "timestamp": utc(ep),
         "origin": {"kind": "human"}}
    d.update(extra)
    return d


def result(tool_id, ep, error=False, **extra):
    d = {"parentUuid": None, "isSidechain": False, "type": "user", "cwd": "/tmp/demo/alpha",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": tool_id, "content": "x", "is_error": error}]},
         "timestamp": utc(ep)}
    d.update(extra)
    return d


def asst(mid, ep, use=None, tools=(), model=MODEL, cwd="/tmp/demo/alpha", **extra):
    content = [{"type": "tool_use", "id": t[2] if len(t) > 2 else "tu_%s_%d" % (mid, i),
                "name": t[0], "input": t[1]} for i, t in enumerate(tools)]
    d = {"parentUuid": None, "isSidechain": False, "type": "assistant", "cwd": cwd,
         "gitBranch": "main", "timestamp": utc(ep),
         "message": {"id": mid, "model": model, "role": "assistant", "content": content,
                     "usage": use if use is not None else usage(10, 20, 1000, 100)}}
    d.update(extra)
    return d


class Home(unittest.TestCase):
    """A temp coach home and a temp transcripts folder."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="coach-live-test-")
        self.home = os.path.join(self.tmp, "coach")
        self.projects = os.path.join(self.tmp, "projects")
        os.makedirs(self.home)
        os.makedirs(self.projects)
        self._env = {k: os.environ.get(k) for k in ("COACH_HOME", "COACH_LAUNCH_AGENTS_DIR")}
        os.environ["COACH_HOME"] = self.home
        os.environ["COACH_LAUNCH_AGENTS_DIR"] = os.path.join(self.tmp, "LaunchAgents")

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def transcript(self, sid, records, pdir="-tmp-demo-alpha", mtime=None, sub=None, append=False):
        folder = os.path.join(self.projects, pdir)
        path = os.path.join(folder, sid + ".jsonl")
        if sub:
            path = os.path.join(folder, sid, "subagents", sub + ".jsonl")
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        with open(path, "a" if append else "w") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")
        mtime = NOW - 3600 if mtime is None else mtime
        os.utime(path, (mtime, mtime))
        return path

    def config(self, **keys):
        with open(os.path.join(self.home, "config.json"), "w") as fh:
            json.dump(keys, fh)

    def feed(self, now=NOW, stats=None):
        return LV.snapshot(now=now, projects_dir=self.projects, stats=stats)


# ---------- activity.py ----------

class ActivityRules(unittest.TestCase):
    def test_thirteen_types(self):
        self.assertEqual(len(A.CATEGORIES), 13)
        self.assertEqual(set(A.CATEGORIES), set([
            "Coding", "Debugging", "Feature Dev", "Refactoring", "Testing", "Exploration",
            "Planning", "Delegation", "Git Ops", "Build/Deploy", "Brainstorming", "Conversation",
            "General"]))

    def test_plan_mode_wins(self):
        self.assertEqual(A.classify(["EnterPlanMode", "Agent", "Edit"], "fix the bug"), "Planning")

    def test_agent_is_delegation(self):
        self.assertEqual(A.classify(["Agent", "Edit", "Bash"], "add a page"), "Delegation")
        self.assertEqual(A.classify(["Task"], ""), "Delegation")

    def test_shell_without_edits_reads_the_prompt(self):
        self.assertEqual(A.classify(["Bash"], "run the test suite"), "Testing")
        # whole words only, as in CodeBurn: the plural is not a test word
        self.assertEqual(A.classify(["Bash"], "run the tests"), "Coding")
        self.assertEqual(A.classify(["Bash", "Read"], "git push it"), "Git Ops")
        self.assertEqual(A.classify(["Bash"], "deploy to uat"), "Build/Deploy")
        self.assertEqual(A.classify(["Bash"], "npm install lodash"), "Build/Deploy")

    def test_test_word_does_not_win_over_an_edit(self):
        self.assertEqual(A.classify(["Bash", "Edit"], "get the test to pass"), "Coding")

    def test_edits_are_coding_then_refined(self):
        self.assertEqual(A.classify(["Edit"], "tidy it"), "Coding")
        self.assertEqual(A.classify(["Write"], "rename the helper"), "Refactoring")
        self.assertEqual(A.classify(["Edit"], "add a search box"), "Feature Dev")
        self.assertEqual(A.classify(["Edit"], "the page is broken"), "Debugging")

    def test_first_keyword_wins(self):
        # "add" comes before "error": a feature, not a bug hunt
        self.assertEqual(A.classify(["Edit"], "add error handling"), "Feature Dev")
        self.assertEqual(A.classify(["Edit"], "fix it then add a test"), "Debugging")

    def test_tie_goes_to_refactoring_then_feature(self):
        self.assertEqual(A._first([(4, "Refactoring"), (4, "Feature Dev"), (4, "Debugging")]),
                         "Refactoring")
        self.assertEqual(A._first([(-1, "Refactoring"), (2, "Feature Dev"), (2, "Debugging")]),
                         "Feature Dev")

    def test_shell_and_reads(self):
        self.assertEqual(A.classify(["Bash", "Read"], "what happened"), "Exploration")
        self.assertEqual(A.classify(["Bash"], "do it"), "Coding")

    def test_exploration_kinds(self):
        self.assertEqual(A.classify(["WebSearch"], "go"), "Exploration")
        self.assertEqual(A.classify(["mcp__zoho__getRecord"], "go"), "Exploration")
        self.assertEqual(A.classify(["Read", "Grep"], "go"), "Exploration")

    def test_exploration_becomes_debugging(self):
        self.assertEqual(A.classify(["Read"], "why is this failing"), "Debugging")
        # a research word keeps it Exploration even with a debugging word
        self.assertEqual(A.classify(["Read"], "investigate the error"), "Exploration")

    def test_task_tools_and_skill(self):
        self.assertEqual(A.classify(["TodoWrite"], "go"), "Planning")
        self.assertEqual(A.classify(["Skill"], "go"), "General")

    def test_no_tools_reads_the_prompt(self):
        self.assertEqual(A.classify([], "what if we used a queue"), "Brainstorming")
        self.assertEqual(A.classify([], "explain this"), "Exploration")
        self.assertEqual(A.classify([], "add a button"), "Feature Dev")
        self.assertEqual(A.classify([], "it is broken"), "Debugging")
        self.assertEqual(A.classify([], "see main.py"), "Coding")
        self.assertEqual(A.classify([], "call the endpoint"), "Coding")
        self.assertEqual(A.classify([], "https://example.com/x"), "Exploration")
        self.assertEqual(A.classify([], "thanks"), "Conversation")

    def test_unknown_tool_falls_back_to_the_prompt(self):
        self.assertEqual(A.classify(["SomeNewTool"], "thanks"), "Conversation")

    def test_flags_hold_no_text(self):
        flags = A.prompt_flags("please add a secret thing to main.py")
        self.assertEqual(len(flags), len(A.NO_FLAGS))
        self.assertTrue(all(isinstance(x, int) for x in flags))
        self.assertEqual(A.classify("E", flags), A.classify(["Edit"], "please add a secret thing"))

    def test_every_result_is_a_known_type(self):
        for tools in ([], ["Edit"], ["Bash"], ["Read"], ["Agent"], ["Skill"], ["X"], ["TodoWrite"]):
            for text in ("", "fix", "add", "rename", "test it", "git commit", "deploy", "idea"):
                self.assertIn(A.classify(tools, text), A.CATEGORIES)


class FirstTry(unittest.TestCase):
    def test_same_file_after_shell_is_a_retry(self):
        calls = [("Edit", "foo.py", None), ("Bash", None, "pytest"), ("Edit", "foo.py", None)]
        self.assertEqual(A.count_retries(calls), 1)

    def test_other_file_after_shell_is_not(self):
        calls = [("Edit", "foo.py", None), ("Bash", None, "pytest"), ("Edit", "bar.py", None)]
        self.assertEqual(A.count_retries(calls), 0)

    def test_same_file_with_no_shell_between_is_not(self):
        calls = [("Edit", "foo.py", None), ("Read", "foo.py", None), ("Edit", "foo.py", None)]
        self.assertEqual(A.count_retries(calls), 0)

    def test_a_read_only_shell_command_is_not_a_check(self):
        calls = [("Edit", "foo.py", None), ("Bash", None, "grep -n x foo.py | head"),
                 ("Edit", "foo.py", None)]
        self.assertEqual(A.count_retries(calls), 0)

    def test_each_further_round_counts(self):
        calls = [("Edit", "f", None), ("Bash", None, "make"), ("Edit", "f", None),
                 ("Bash", None, "make"), ("Write", "f", None)]
        self.assertEqual(A.count_retries(calls), 2)

    def test_calls_of_one_message_run_side_by_side(self):
        # edit, then one message with a shell command and the edit together: no check in between
        calls = [("Edit", "f", None, "m1"), ("Bash", None, "make", "m2"), ("Edit", "f", None, "m2")]
        self.assertEqual(A.count_retries(calls), 0)

    def test_read_commands(self):
        self.assertTrue(A.is_read_command("cat a.txt"))
        self.assertTrue(A.is_read_command("git log --oneline | head -5"))
        self.assertTrue(A.is_read_command("FOO=1 rg 'x && y' src"))
        self.assertFalse(A.is_read_command("cat a && sed -i s/a/b/ f"))
        self.assertFalse(A.is_read_command("git branch -D x"))
        self.assertFalse(A.is_read_command("npm test"))
        self.assertFalse(A.is_read_command(""))

    def test_rate(self):
        self.assertIsNone(A.first_try_rate(0, 0))
        self.assertEqual(A.first_try_rate(4, 3), 0.75)
        self.assertEqual(A.first_try_rate(2, 2), 1.0)


# ---------- live.py: counting ----------

class Counting(Home):
    def test_schema(self):
        self.transcript("s1", [user("add a page", at(TODAY, 10)), asst("msg_a1", at(TODAY, 10, 1))])
        f = self.feed()
        self.assertEqual(f["version"], 1)
        self.assertEqual(sorted(f), ["limits", "page_url", "periods", "running", "tips", "updated",
                                     "version"])
        self.assertEqual(sorted(f["periods"]), ["all", "month", "today", "week"])
        keys = ["activities", "cache_hit", "calls", "corrections", "cost_usd", "first_try", "label",
                "mcp_servers", "models", "peak", "prev_cost_usd", "projects", "series", "series_unit",
                "sessions", "tokens"]
        for name, p in f["periods"].items():
            # All time also says where it starts and where its detail starts
            self.assertEqual(sorted(p), sorted(keys + ["detail_since", "since"]) if name == "all" else keys)
        self.assertEqual(f["periods"]["today"]["label"], "Today, Nov 23")
        self.assertEqual(f["periods"]["week"]["label"], "Last 7 days")
        self.assertEqual(f["periods"]["month"]["label"], "Last 30 days")
        self.assertIsNone(f["page_url"])
        self.assertEqual(f["limits"], {"as_of": None, "rows": []})
        json.dumps(f)

    def test_times_are_local_iso_with_offset(self):
        self.transcript("s1", [user("go", at(TODAY, 10)), asst("msg_a1", at(TODAY, 10, 1))])
        f = self.feed()
        for text in (f["updated"], f["periods"]["today"]["series"][0]["t"],
                     f["periods"]["week"]["series"][0]["t"]):
            self.assertRegex(text, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
        self.assertEqual(f["updated"][:19], "2026-11-23T12:30:00")

    def test_series_lengths_and_units(self):
        self.transcript("s1", [user("go", at(TODAY, 10)), asst("msg_a1", at(TODAY, 10, 1))])
        p = self.feed()["periods"]
        self.assertEqual(p["today"]["series_unit"], "hour")
        self.assertEqual(len(p["today"]["series"]), 13)             # 00:00 to the 12:00 hour
        self.assertEqual(p["today"]["series"][0]["t"][:19], "2026-11-23T00:00:00")
        self.assertEqual((p["week"]["series_unit"], len(p["week"]["series"])), ("day", 7))
        self.assertEqual((p["month"]["series_unit"], len(p["month"]["series"])), ("day", 30))
        self.assertEqual(p["week"]["series"][0]["t"][:10], "2026-11-17")
        self.assertEqual(p["week"]["series"][-1]["t"][:10], TODAY)
        self.assertEqual(p["month"]["series"][0]["t"][:10], "2026-10-25")

    def test_tokens_cost_calls_and_peak(self):
        self.transcript("s1", [
            user("go", at(TODAY, 9)),
            asst("msg_a1", at(TODAY, 9, 1), usage(100, 200, 3000, 400)),
            asst("msg_a2", at(TODAY, 10, 5), usage(1000, 2000, 30000, 4000)),
            asst("msg_a3", at(TODAY, 10, 6), usage(1, 1, 1, 1))])
        t = self.feed()["periods"]["today"]
        self.assertEqual(t["calls"], 3)
        self.assertEqual(t["tokens"], 3700 + 37000 + 4)             # in + out + cache read + write
        rates = P.rates(MODEL)
        want = (1101 * rates[0] + 2201 * rates[1] + 33001 * rates[2] + 4401 * rates[3]) / 1e6
        self.assertAlmostEqual(t["cost_usd"], round(want, 2), places=2)
        self.assertEqual(t["series"][9]["tokens"], 3700)
        self.assertEqual(t["series"][10]["tokens"], 37004)
        self.assertEqual(t["peak"]["tokens"], 37004)
        self.assertEqual(t["peak"]["t"][:13], "2026-11-23T10")
        self.assertEqual(sum(pt["tokens"] for pt in t["series"]), t["tokens"])

    def test_peak_is_null_with_no_tokens(self):
        self.assertIsNone(self.feed()["periods"]["today"]["peak"])

    def test_one_message_counts_once_and_the_last_usage_wins(self):
        # Claude Code writes one record per content block, each with the message id
        self.transcript("s1", [
            user("go", at(TODAY, 9)),
            asst("msg_a1", at(TODAY, 9, 1), usage(10, 5, 0, 0)),
            asst("msg_a1", at(TODAY, 9, 1, 2), usage(10, 50, 0, 0)),
            asst("msg_a1", at(TODAY, 9, 1, 3), usage(10, 70, 0, 0), tools=[("Read", {})])])
        t = self.feed()["periods"]["today"]
        self.assertEqual(t["calls"], 1)
        self.assertEqual(t["tokens"], 80)

    def test_a_resumed_copy_counts_once(self):
        first = [user("go", at(TODAY, 9)), asst("msg_a1", at(TODAY, 9, 1), usage(10, 10, 0, 0))]
        a = self.transcript("s1", first, mtime=NOW - 7200)
        os.utime(a, (NOW - 7200, NOW - 7200))
        self.transcript("s2", first + [user("more", at(TODAY, 10)),
                                       asst("msg_b1", at(TODAY, 10, 1), usage(1, 1, 0, 0))])
        t = self.feed()["periods"]["today"]
        self.assertEqual(t["calls"], 2)
        self.assertEqual(t["tokens"], 22)

    def test_a_prompt_between_two_blocks_of_one_message(self):
        # the answer's last block, with its final usage, is written after a queued prompt
        self.transcript("s1", [user("go", at(TODAY, 9)),
                               asst("msg_a1", at(TODAY, 9, 1), usage(10, 1, 0, 0)),
                               user("and then this", at(TODAY, 9, 2)),
                               asst("msg_a1", at(TODAY, 9, 3), usage(10, 3000, 0, 0)),
                               asst("msg_a1", at(TODAY, 9, 4), usage(10, 5000, 0, 0))])
        t = self.feed()["periods"]["today"]
        self.assertEqual((t["tokens"], t["calls"]), (5010, 1))
        self.assertAlmostEqual(sum(a["usd"] for a in t["activities"]), t["cost_usd"], places=2)

    def test_a_late_block_with_nothing_new_files_no_turn(self):
        self.transcript("s1", [user("tidy", at(TODAY, 9)),
                               asst("msg_a1", at(TODAY, 9, 1), usage(0, 5000, 0, 0)),
                               user("thanks", at(TODAY, 9, 2)),
                               asst("msg_a1", at(TODAY, 9, 3), usage(0, 5000, 0, 0))])
        rows = self.feed()["periods"]["today"]["activities"]
        self.assertEqual(sum(r["turns"] for r in rows), 1)

    def test_the_hour_the_clocks_skip(self):
        tot = LV.Totals()
        tot.hours = {"2026-11-23T01": {MODEL: [0, 5, 0, 0, 0]}, "2026-11-23T02": {MODEL: [0, 7, 0, 0, 0]}}
        real = LV._hour_iso
        LV._hour_iso = lambda key: real(key[:11] + ("01" if key.endswith("T02") else key[11:]))
        try:
            pts = LV.hour_series(tot, TODAY, at(TODAY, 2, 30), P.load_prices())
        finally:
            LV._hour_iso = real
        self.assertEqual([p["tokens"] for p in pts], [0, 12])               # nothing is dropped

    def test_sidechain_records_in_a_main_transcript_are_skipped(self):
        self.transcript("s1", [user("go", at(TODAY, 9)),
                               asst("msg_a1", at(TODAY, 9, 1), usage(1, 1, 0, 0)),
                               asst("msg_x", at(TODAY, 9, 2), usage(9, 9, 0, 0), isSidechain=True)])
        self.assertEqual(self.feed()["periods"]["today"]["tokens"], 2)

    def test_subagent_transcripts_count_for_the_session(self):
        self.transcript("s1", [user("go", at(TODAY, 9)),
                               asst("msg_a1", at(TODAY, 9, 1), usage(100, 100, 0, 0),
                                    tools=[("Agent", {"prompt": "x"})])])
        self.transcript("s1", [user("add the page", at(TODAY, 9, 2), isSidechain=True),
                               asst("msg_s1", at(TODAY, 9, 3), usage(100, 100, 0, 0),
                                    tools=[("Edit", {"file_path": "a.py"})], isSidechain=True)],
                        sub="agent-1")
        t = self.feed()["periods"]["today"]
        self.assertEqual((t["sessions"], t["calls"], t["tokens"]), (1, 2, 400))
        names = dict((a["name"], a) for a in t["activities"])
        self.assertEqual(sorted(names), ["Delegation", "Feature Dev"])   # the agent's work is filed as what it did
        self.assertEqual(names["Feature Dev"]["first_try"], 1.0)
        self.assertIsNone(names["Delegation"]["first_try"])

    def test_cache_hit_rate(self):
        self.assertEqual(LV.cache_hit_rate([40, 999, 960, 0, 0]), 0.96)
        self.assertEqual(LV.cache_hit_rate([40, 999, 900, 40, 20]), 0.9)    # writes are misses
        self.assertEqual(LV.cache_hit_rate([0, 10, 0, 50, 0]), 0.0)
        self.assertIsNone(LV.cache_hit_rate([0, 10, 0, 0, 0]))
        self.transcript("s1", [user("go", at(TODAY, 9)),
                               asst("msg_a1", at(TODAY, 9, 1), usage(25, 7, 75, 900))])
        # 75 read of 25 input + 75 read + 900 written
        self.assertEqual(self.feed()["periods"]["today"]["cache_hit"]["rate"], 0.075)

    def test_sessions_are_counted_once_across_days(self):
        self.transcript("s1", [user("go", at("2026-11-21", 9)), asst("msg_a1", at("2026-11-21", 9, 1)),
                               user("go on", at("2026-11-22", 9)), asst("msg_a2", at("2026-11-22", 9, 1))])
        self.transcript("s2", [user("go", at("2026-11-22", 9)), asst("msg_b1", at("2026-11-22", 9, 1))])
        self.assertEqual(self.feed()["periods"]["week"]["sessions"], 2)

    def test_previous_period(self):
        self.transcript("s1", [user("no, wrong", at("2026-11-22", 9)),
                               asst("msg_y1", at("2026-11-22", 9, 1), usage(0, 1000000, 0, 0)),
                               user("go", at(TODAY, 9)),
                               asst("msg_t1", at(TODAY, 9, 1), usage(0, 500000, 0, 0)),
                               user("go", at("2026-11-12", 9)),
                               asst("msg_w1", at("2026-11-12", 9, 1), usage(0, 250000, 0, 0))])
        p = self.feed()["periods"]
        out = P.rates(MODEL)[1]
        self.assertEqual(p["today"]["cost_usd"], round(0.5 * out, 2))
        self.assertEqual(p["today"]["prev_cost_usd"], round(1.0 * out, 2))      # yesterday
        self.assertEqual((p["today"]["corrections"]["count"], p["today"]["corrections"]["prev_count"]),
                         (0, 1))
        self.assertEqual(p["week"]["cost_usd"], round(1.5 * out, 2))
        self.assertEqual(p["week"]["prev_cost_usd"], round(0.25 * out, 2))      # the 7 days before
        self.assertIsNone(p["month"]["prev_cost_usd"])                          # nothing on record there
        self.assertIsNone(p["month"]["corrections"]["prev_count"])
        self.assertIsNone(p["month"]["first_try"]["prev_rate"])
        self.assertIsNone(p["month"]["cache_hit"]["prev_rate"])

    def test_corrections_match_extract(self):
        prompts = ["add a page", "no, that is wrong", "thanks", "you forgot the title", "still broken",
                   "<task-notification>x</task-notification>", "undo that"]
        recs = []
        for i, text in enumerate(prompts):
            recs.append(user(text, at(TODAY, 8, i * 3), origin=None))
            recs.append(asst("msg_c%d" % i, at(TODAY, 8, i * 3 + 1)))
        recs.append({"parentUuid": None, "isSidechain": False, "type": "attachment",
                     "timestamp": utc(at(TODAY, 9)),
                     "attachment": {"type": "queued_command", "prompt": "wrong again",
                                    "origin": {"kind": "human"}}})
        self.transcript("s1", recs)
        acc = E.SessionAcc("s1")
        for r in recs:
            acc.feed(r)
        self.assertEqual(acc.corrections, 5)
        self.assertEqual(self.feed()["periods"]["today"]["corrections"]["count"], acc.corrections)

    def test_bookkeeping_lines_are_not_read(self):
        path = self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_a1", at(TODAY, 9, 1),
                                                                     usage(1, 1, 0, 0))])
        with open(path, "a") as fh:
            fh.write('{"type":"api-request","message":{"usage":{"output_tokens":999}},'
                     '"timestamp":"%s"}\n' % utc(at(TODAY, 9, 2)))
            fh.write("not json\n")
            fh.write('{"type":"assistant","timestamp":"%s","message":{"id":"msg_k","model":"%s",'
                     '"usage":{"output_tokens":3}}}\n' % (utc(at(TODAY, 9, 3)), MODEL))
        os.utime(path, (NOW - 60, NOW - 60))
        self.assertEqual(self.feed()["periods"]["today"]["tokens"], 5)


class Activities(Home):
    def build(self):
        recs = []
        # Coding turn with a retry: Edit a, Bash, Edit a
        recs += [user("tidy this", at(TODAY, 8)),
                 asst("msg_1", at(TODAY, 8, 1), usage(0, 300000, 0, 0),
                      tools=[("Edit", {"file_path": "/x/a.py"})]),
                 asst("msg_2", at(TODAY, 8, 2), usage(0, 0, 0, 0), tools=[("Bash", {"command": "pytest"})]),
                 asst("msg_3", at(TODAY, 8, 3), usage(0, 0, 0, 0),
                      tools=[("Edit", {"file_path": "/x/a.py"})])]
        # Coding turn, clean: Edit a, Bash, Edit b
        recs += [user("tidy that", at(TODAY, 8, 10)),
                 asst("msg_4", at(TODAY, 8, 11), usage(0, 100000, 0, 0),
                      tools=[("Edit", {"file_path": "/x/a.py"})]),
                 asst("msg_5", at(TODAY, 8, 12), usage(0, 0, 0, 0), tools=[("Bash", {"command": "pytest"})]),
                 asst("msg_6", at(TODAY, 8, 13), usage(0, 0, 0, 0),
                      tools=[("Edit", {"file_path": "/x/b.py"})])]
        # Testing turn, no edits
        recs += [user("run the test suite", at(TODAY, 9)),
                 asst("msg_7", at(TODAY, 9, 1), usage(0, 500000, 0, 0), tools=[("Bash", {"command": "pytest"})])]
        # Conversation, no tools
        recs += [user("thanks", at(TODAY, 9, 30)), asst("msg_8", at(TODAY, 9, 31), usage(0, 100000, 0, 0))]
        # A turn with no spend at all: never listed
        recs += [user("what if we wait", at(TODAY, 9, 40)), asst("msg_9", at(TODAY, 9, 41), usage(0, 0, 0, 0))]
        self.transcript("s1", recs)

    def test_every_type_with_spend_highest_first(self):
        self.build()
        t = self.feed()["periods"]["today"]
        rows = t["activities"]
        self.assertEqual([r["name"] for r in rows], ["Testing", "Coding", "Conversation"])
        self.assertEqual(sorted(rows[0]), ["first_try", "name", "share", "turns", "usd"])
        self.assertEqual([r["turns"] for r in rows], [1, 2, 1])
        self.assertEqual([r["share"] for r in rows], [0.5, 0.4, 0.1])
        self.assertAlmostEqual(sum(r["usd"] for r in rows), t["cost_usd"], places=2)
        for r in rows:
            self.assertIn(r["name"], A.CATEGORIES)

    def test_first_try(self):
        self.build()
        t = self.feed()["periods"]["today"]
        rows = dict((r["name"], r) for r in t["activities"])
        self.assertEqual(rows["Coding"]["first_try"], 0.5)          # one of two edit turns had a retry
        self.assertIsNone(rows["Testing"]["first_try"])             # no edit turns: readers show "No edits"
        self.assertEqual((t["first_try"]["rate"], t["first_try"]["prev_rate"], t["first_try"]["edit_turns"]),
                         (0.5, None, 2))

    def test_not_cut_to_three(self):
        recs = []
        prompts = [("tidy", "Edit"), ("add a thing", "Edit"), ("rename it", "Edit"),
                   ("it is broken", "Edit"), ("run the test", "Bash"), ("look", "Read"),
                   ("go", "TodoWrite"), ("go", "Agent"), ("git status please", "Bash"),
                   ("deploy it", "Bash"), ("what if", None), ("thanks", None), ("go", "Skill")]
        for i, (text, tool) in enumerate(prompts):
            recs.append(user(text + " %d" % i, at(TODAY, 1, i * 2)))
            recs.append(asst("msg_%d" % i, at(TODAY, 1, i * 2 + 1), usage(0, 1000 * (i + 1), 0, 0),
                             tools=[(tool, {})] if tool else ()))
        self.transcript("s1", recs)
        rows = self.feed()["periods"]["today"]["activities"]
        self.assertEqual(len(rows), 13)
        self.assertEqual(set(r["name"] for r in rows), set(A.CATEGORIES))
        self.assertEqual([r["usd"] for r in rows], sorted((r["usd"] for r in rows), reverse=True))
        self.assertAlmostEqual(sum(r["share"] for r in rows), 1.0, places=3)

    def test_the_running_turn_is_counted(self):
        # a turn with no prompt after it yet still shows
        self.transcript("s1", [user("add a page", at(TODAY, 9)),
                               asst("msg_1", at(TODAY, 9, 1), usage(0, 1000, 0, 0),
                                    tools=[("Write", {"file_path": "p"})])])
        rows = self.feed()["periods"]["today"]["activities"]
        self.assertEqual([(r["name"], r["turns"]) for r in rows], [("Feature Dev", 1)])

    def test_no_prompt_text_in_the_cache(self):
        self.transcript("s1", [user("add the zebra-unicorn page", at(TODAY, 9)),
                               asst("msg_1", at(TODAY, 9, 1), tools=[("Bash", {"command": "echo zebra-cmd"})])])
        self.feed()
        for name in ("live-cache.json", "live-mids.json"):
            with open(os.path.join(self.home, name)) as fh:
                self.assertNotIn("zebra", fh.read())


class Trends(Home):
    def build(self):
        recs = []
        # 08:00 an edit turn with a retry; a correction at 08:30 starting a clean edit turn
        recs += [user("tidy this", at(TODAY, 8)),
                 asst("m1", at(TODAY, 8, 1), usage(20, 5, 80, 0), tools=[("Edit", {"file_path": "a"})]),
                 asst("m2", at(TODAY, 8, 2), usage(0, 5, 0, 0), tools=[("Bash", {"command": "make"})]),
                 asst("m3", at(TODAY, 8, 3), usage(0, 5, 0, 0), tools=[("Edit", {"file_path": "a"})]),
                 user("no, the other file", at(TODAY, 8, 30)),
                 asst("m4", at(TODAY, 8, 31), usage(0, 5, 0, 0), tools=[("Edit", {"file_path": "b"})])]
        # 11:00 no edits, cache hit 1 of 4
        recs += [user("thanks", at(TODAY, 11)), asst("m5", at(TODAY, 11, 1), usage(75, 5, 25, 0))]
        # Nov 20: one clean edit turn and two corrections; Nov 5: a retry
        recs += [user("wrong, tidy it", at("2026-11-20", 9)),
                 asst("m6", at("2026-11-20", 9, 1), usage(10, 5, 90, 0), tools=[("Edit", {"file_path": "a"})]),
                 user("still broken here", at("2026-11-20", 9, 30)),
                 asst("m7", at("2026-11-20", 9, 31), usage(0, 5, 0, 0)),
                 user("tidy", at("2026-11-05", 9)),
                 asst("m8", at("2026-11-05", 9, 1), usage(0, 5, 0, 0), tools=[("Edit", {"file_path": "a"})]),
                 asst("m9", at("2026-11-05", 9, 2), usage(0, 5, 0, 0), tools=[("Bash", {"command": "make"})]),
                 asst("m10", at("2026-11-05", 9, 3), usage(0, 5, 0, 0), tools=[("Edit", {"file_path": "a"})])]
        self.transcript("s1", recs)
        return self.feed()["periods"]

    def test_groups(self):
        self.assertEqual(LV.thin(list(range(7))), [[i] for i in range(7)])
        self.assertEqual(LV.thin(list(range(13))), [[0], [1], [2], [3], [4], [5], [6], [7], [8], [9],
                                                    [10], [11, 12]])
        groups = LV.thin(list(range(30)))
        self.assertEqual(len(groups), 12)
        self.assertEqual([x for g in groups for x in g], list(range(30)))    # every step, in order
        self.assertTrue(all(len(g) in (2, 3) for g in groups))
        self.assertEqual(LV.thin([]), [])

    def test_today_by_hour(self):
        t = self.build()["today"]
        # 13 hours (00:00 to 12:00) in 12 groups: one per hour, the last two together
        for key in ("first_try", "cache_hit", "corrections"):
            self.assertEqual(len(t[key]["series"]), 12)
        self.assertEqual(t["first_try"]["series"], [None] * 8 + [0.5] + [None] * 3)
        self.assertEqual(t["cache_hit"]["series"], [None] * 8 + [0.8, None, None, 0.25])
        self.assertEqual(t["corrections"]["series"], [None] * 8 + [1, None, None, 0])

    def test_week_by_day(self):
        w = self.build()["week"]
        # Nov 17 to Nov 23, one value a day, oldest first
        self.assertEqual(w["first_try"]["series"], [None, None, None, 1.0, None, None, 0.5])
        self.assertEqual(w["cache_hit"]["series"], [None, None, None, 0.9, None, None, 0.525])
        self.assertEqual(w["corrections"]["series"], [None, None, None, 2, None, None, 1])
        self.assertEqual(w["first_try"]["rate"], round(2 / 3.0, 4))

    def test_month_is_thinned_to_twelve(self):
        m = self.build()["month"]
        for key in ("first_try", "cache_hit", "corrections"):
            self.assertEqual(len(m[key]["series"]), 12)
        # 30 days from Oct 25 in groups of 2 or 3: Nov 5 is in group 4, Nov 20 in 10, Nov 23 in 11
        self.assertEqual(m["first_try"]["series"], [None] * 4 + [0.0] + [None] * 5 + [1.0, 0.5])
        self.assertEqual(m["corrections"]["series"], [None] * 4 + [0] + [None] * 5 + [2, 1])
        self.assertEqual(m["cache_hit"]["series"][10:], [0.9, 0.525])

    def test_empty(self):
        p = self.feed()["periods"]
        self.assertEqual(p["week"]["corrections"]["series"], [None] * 7)
        self.assertEqual(p["month"]["first_try"]["series"], [None] * 12)

    def test_snapshot_needs_no_arguments(self):
        import inspect
        for name, par in inspect.signature(LV.snapshot).parameters.items():
            self.assertIsNot(par.default, inspect.Parameter.empty, name)


class Projects(Home):
    def test_top_three_and_other(self):
        for i, name in enumerate(["alpha", "beta", "gamma", "delta", "epsilon"]):
            self.transcript("s%d" % i, [
                user("go", at(TODAY, 9), cwd="/tmp/demo/" + name),
                asst("msg_p%d" % i, at(TODAY, 9, 1), usage(0, 100000 * (5 - i), 0, 0),
                     cwd="/tmp/demo/" + name)], pdir="-tmp-demo-" + name)
        rows = self.feed()["periods"]["today"]["projects"]
        self.assertEqual([r["name"] for r in rows], ["alpha", "beta", "gamma", "Other"])
        self.assertEqual(rows[3]["sessions"], 2)
        self.assertAlmostEqual(sum(r["share"] for r in rows), 1.0, places=3)
        self.assertEqual(rows[0]["share"], round(5 / 15.0, 4))
        self.assertEqual(sorted(rows[0]), ["name", "sessions", "share", "usd"])

    def test_three_or_fewer_has_no_other(self):
        self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1))])
        rows = self.feed()["periods"]["today"]["projects"]
        self.assertEqual([r["name"] for r in rows], ["alpha"])
        self.assertEqual(rows[0]["share"], 1.0)


class McpServers(Home):
    def test_rows(self):
        self.transcript("s1", [
            user("go", at(TODAY, 9)),
            asst("msg_1", at(TODAY, 9, 1), tools=[("mcp__zoho-crm__getRecord", {}, "t1"),
                                                  ("mcp__zoho-crm__getRecord", {}, "t2"),
                                                  ("mcp__zoho-crm__searchRecords", {}, "t3"),
                                                  ("mcp__Claude_Browser__navigate", {}, "t4"),
                                                  ("Read", {}, "t5")]),
            result("t1", at(TODAY, 9, 2), error=True),
            result("t2", at(TODAY, 9, 2)),
            result("t5", at(TODAY, 9, 2), error=True),
            user("go", at("2026-11-20", 9)),
            asst("msg_2", at("2026-11-20", 9, 1), tools=[("mcp__Claude_Browser__find", {}, "t6"),
                                                         ("mcp__Claude_Browser__navigate", {}, "t7"),
                                                         ("mcp__Claude_Browser__navigate", {}, "t8")]),
            result("t8", at("2026-11-20", 9, 2), error=True)])
        p = self.feed()["periods"]
        self.assertEqual(p["today"]["mcp_servers"], [
            {"name": "zoho crm", "calls": 3, "tools": 2, "errors": 1},
            {"name": "Claude browser", "calls": 1, "tools": 1, "errors": 0}])
        self.assertEqual(p["week"]["mcp_servers"], [
            {"name": "Claude browser", "calls": 4, "tools": 2, "errors": 1},
            {"name": "zoho crm", "calls": 3, "tools": 2, "errors": 1}])

    def test_mcp_labels(self):
        uid = "3a708f37-448d-402b-9f30-a046245c22fa"
        self.assertEqual(LV.mcp_label(uid, {"executeCOQLQuery", "getRecord"}), "Zoho CRM")
        self.assertEqual(LV.mcp_label(uid, {"outlook_email_search"}), "Microsoft 365")
        self.assertEqual(LV.mcp_label(uid, {"get_live_results"}), "Certified queries")
        self.assertEqual(LV.mcp_label(uid, {"list_meetings"}), "Claude Meetings")
        self.assertEqual(LV.mcp_label(uid, {"guide", "batch"}), "Claude Docs")
        self.assertEqual(LV.mcp_label(uid, {"something_else"}), "Connector 3a708f")
        self.assertEqual(LV.mcp_label("ccd_turn", {"wrap_up"}), "Claude app: task list")
        self.assertEqual(LV.mcp_label("Claude_Browser"), "Claude browser")
        self.assertEqual(LV.mcp_label("my-server_x"), "my server x")

    def test_none_and_at_most_eight(self):
        self.assertEqual(self.feed()["periods"]["today"]["mcp_servers"], [])
        tools = [("mcp__srv%d__t" % i, {}) for i in range(10) for _ in range(i + 1)]
        self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1), tools=tools)])
        rows = self.feed()["periods"]["today"]["mcp_servers"]
        self.assertEqual(len(rows), 8)
        self.assertEqual(rows[0], {"name": "srv9", "calls": 10, "tools": 1, "errors": 0})

    def test_excluded_work_is_left_out(self):
        self.config(exclude=[{"match": ["secret"]}])
        self.transcript("s1", [user("go", at(TODAY, 9), cwd="/tmp/demo/secret"),
                               asst("msg_1", at(TODAY, 9, 1), tools=[("mcp__hidden__t", {})],
                                    cwd="/tmp/demo/secret")], pdir="-tmp-demo-secret")
        self.assertEqual(self.feed()["periods"]["today"]["mcp_servers"], [])


OPUS = "claude-opus-5-5"


def denied(tool_id, ep, kind=None, text="x"):
    """A tool result marked as an error that is a refusal."""
    d = result(tool_id, ep, error=True)
    d["message"]["content"][0]["content"] = text
    if kind:
        d["toolDenialKind"] = kind
    return d


class Models(Home):
    def rows(self, period="today"):
        return dict((r["name"], r) for r in self.feed()["periods"][period]["models"])

    def test_rows_by_cost_with_the_main_and_subagent_split(self):
        self.transcript("s1", [user("go", at(TODAY, 9)),
                               asst("msg_1", at(TODAY, 9, 1), usage(0, 1000000, 0, 0),
                                    tools=[("Agent", {"prompt": "x"})])])
        self.transcript("s1", [user("add the page", at(TODAY, 9, 2), isSidechain=True),
                               asst("msg_s1", at(TODAY, 9, 3), usage(0, 2000000, 0, 0), model=OPUS,
                                    isSidechain=True),
                               asst("msg_s2", at(TODAY, 9, 4), usage(0, 500000, 0, 0), isSidechain=True)],
                        sub="agent-1")
        t = self.feed()["periods"]["today"]
        rows = t["models"]
        fable, opus = P.rates(MODEL)[1], P.rates(OPUS)[1]
        want = sorted([("Claude Fable 5.2", 1.5 * fable), ("Claude Opus 5.5", 2.0 * opus)],
                      key=lambda r: -r[1])
        self.assertEqual([r["name"] for r in rows], [w[0] for w in want])
        self.assertEqual(list(rows[0]), ["name", "usd", "share", "main_usd", "sub_usd", "first_try",
                                         "edit_turns", "tool_calls", "tool_errors", "error_rate",
                                         "redone_edits", "corrections", "where_most", "where"])
        by = dict((r["name"], r) for r in rows)
        f = by["Claude Fable 5.2"]
        self.assertEqual((f["main_usd"], f["sub_usd"], f["usd"]),
                         (round(1.0 * fable, 2), round(0.5 * fable, 2), round(1.5 * fable, 2)))
        o = by["Claude Opus 5.5"]
        self.assertEqual((o["main_usd"], o["sub_usd"]), (0.0, round(2.0 * opus, 2)))
        self.assertAlmostEqual(sum(r["share"] for r in rows), 1.0, places=3)
        self.assertAlmostEqual(sum(r["usd"] for r in rows), t["cost_usd"], places=1)
        self.assertEqual(f["share"], round(1.5 * fable / (1.5 * fable + 2.0 * opus), 4))
        json.dumps(rows)

    def test_a_dated_id_is_the_same_model(self):
        self.transcript("s1", [user("go", at(TODAY, 9)),
                               asst("msg_1", at(TODAY, 9, 1), usage(0, 100, 0, 0)),
                               asst("msg_2", at(TODAY, 9, 2), usage(0, 100, 0, 0), model=MODEL + "-20261101")])
        self.assertEqual(list(self.rows()), ["Claude Fable 5.2"])

    def test_no_spend_no_row(self):
        self.assertEqual(self.feed()["periods"]["today"]["models"], [])
        self.transcript("s1", [user("go", at(TODAY, 9)),
                               asst("msg_1", at(TODAY, 9, 1), usage(0, 0, 0, 0), tools=[("Bash", {}, "t1")]),
                               asst("msg_2", at(TODAY, 9, 2), usage(0, 50, 0, 0), model="claude-unknown-9")])
        self.assertEqual(self.feed()["periods"]["today"]["models"], [])

    def test_tool_calls_and_errors_belong_to_the_calling_model(self):
        self.transcript("s1", [
            user("look", at(TODAY, 9)),
            asst("msg_1", at(TODAY, 9, 1), tools=[("Bash", {"command": "make"}, "t1"), ("Read", {}, "t2"),
                                                  ("Read", {}, "t3"), ("Read", {}, "t4")]),
            result("t1", at(TODAY, 9, 2), error=True),
            denied("t2", at(TODAY, 9, 2), kind="user-rejected"),                  # a refusal: no error
            denied("t3", at(TODAY, 9, 2), text="The user doesn't want to proceed with this tool use."),
            result("t4", at(TODAY, 9, 2)),
            asst("msg_2", at(TODAY, 9, 3), model=OPUS, tools=[("Bash", {"command": "make"}, "t5"),
                                                             ("mcp__zoho-crm__getRecord", {}, "t6")]),
            result("t5", at(TODAY, 9, 4), error=True),
            result("t6", at(TODAY, 9, 4), error=True),
            user("look again", at("2026-11-20", 9)),
            asst("msg_3", at("2026-11-20", 9, 1), tools=[("Grep", {}, "t7")]),
            result("t7", at("2026-11-20", 9, 2), error=True)])
        rows = self.rows()
        f, o = rows["Claude Fable 5.2"], rows["Claude Opus 5.5"]
        self.assertEqual((f["tool_calls"], f["tool_errors"], f["error_rate"]), (4, 1, 0.25))
        self.assertEqual((o["tool_calls"], o["tool_errors"], o["error_rate"]), (2, 2, 1.0))
        self.assertEqual(f["where"], [{"name": "Bash", "kind": "tool", "errors": 1, "redone_edits": 0,
                                       "corrections": 0}])
        self.assertEqual([(w["name"], w["kind"], w["errors"]) for w in o["where"]],
                         [("Bash", "tool", 1), ("zoho crm", "mcp", 1)])
        self.assertIsNone(f["first_try"])                       # no edits
        self.assertEqual((f["edit_turns"], f["redone_edits"]), (0, 0))
        w = self.rows("week")["Claude Fable 5.2"]               # the error on Nov 20 is in the week only
        self.assertEqual((w["tool_calls"], w["tool_errors"]), (5, 2))

    def test_redone_edits_go_to_the_model_that_made_the_retrying_edit(self):
        self.transcript("s1", [
            # one turn, two models: Fable edits a, a check runs, Opus edits a again
            user("tidy", at(TODAY, 9)),
            asst("msg_1", at(TODAY, 9, 1), tools=[("Edit", {"file_path": "/x/a.py"})]),
            asst("msg_2", at(TODAY, 9, 2), tools=[("Bash", {"command": "pytest"})]),
            asst("msg_3", at(TODAY, 9, 3), model=OPUS, tools=[("Edit", {"file_path": "/x/a.py"})]),
            # Fable alone retries, twice in the turn: still one redone edit turn
            user("tidy more", at(TODAY, 10)),
            asst("msg_4", at(TODAY, 10, 1), tools=[("Write", {"file_path": "/x/b.py"})]),
            asst("msg_5", at(TODAY, 10, 2), tools=[("Bash", {"command": "make"})]),
            asst("msg_6", at(TODAY, 10, 3), tools=[("Write", {"file_path": "/x/b.py"})]),
            asst("msg_7", at(TODAY, 10, 4), tools=[("Bash", {"command": "make"})]),
            asst("msg_8", at(TODAY, 10, 5), tools=[("Edit", {"file_path": "/x/b.py"})]),
            # Fable, clean
            user("tidy the rest", at(TODAY, 11)),
            asst("msg_9", at(TODAY, 11, 1), tools=[("Edit", {"file_path": "/x/c.py"})])])
        rows = self.rows()
        f, o = rows["Claude Fable 5.2"], rows["Claude Opus 5.5"]
        self.assertEqual((f["edit_turns"], f["redone_edits"], f["first_try"]), (3, 1, 0.6667))
        self.assertEqual((o["edit_turns"], o["redone_edits"], o["first_try"]), (1, 1, 0.0))
        self.assertEqual(f["where"], [{"name": "Write", "kind": "tool", "errors": 0, "redone_edits": 1,
                                       "corrections": 0}])
        self.assertEqual(o["where"], [{"name": "Edit", "kind": "tool", "errors": 0, "redone_edits": 1,
                                       "corrections": 0}])
        # the whole period's first-try rule is unchanged: 3 edit turns, 1 clean
        self.assertEqual(self.feed()["periods"]["today"]["first_try"]["edit_turns"], 3)

    def test_corrections_go_to_the_last_answer_and_its_last_tool(self):
        self.transcript("s1", [
            user("add a page", at(TODAY, 9)),
            asst("msg_1", at(TODAY, 9, 1), tools=[("Read", {})]),
            asst("msg_2", at(TODAY, 9, 2), tools=[("Bash", {"command": "make"})]),
            user("no, that is wrong", at(TODAY, 9, 10)),
            asst("msg_3", at(TODAY, 9, 11), model=OPUS),
            user("you forgot the title", at(TODAY, 9, 20)),
            asst("msg_4", at(TODAY, 9, 21), model=OPUS, tools=[("mcp__Claude_Browser__navigate", {})]),
            user("thanks", at(TODAY, 9, 30)),
            asst("msg_5", at(TODAY, 9, 31)),
            user("undo that", at(TODAY, 9, 40))])
        t = self.feed()["periods"]["today"]
        rows = dict((r["name"], r) for r in t["models"])
        f, o = rows["Claude Fable 5.2"], rows["Claude Opus 5.5"]
        self.assertEqual((f["corrections"], o["corrections"]), (2, 1))
        self.assertEqual(f["corrections"] + o["corrections"], t["corrections"]["count"])
        self.assertEqual([(w["name"], w["kind"], w["corrections"]) for w in f["where"]],
                         [("Bash", "tool", 1), ("No tool", "none", 1)])
        self.assertEqual([(w["name"], w["kind"], w["corrections"]) for w in o["where"]],
                         [("No tool", "none", 1)])

    def test_a_correction_with_no_answer_before_it_has_no_model(self):
        self.transcript("s1", [user("no, that is wrong", at(TODAY, 9)),
                               asst("msg_1", at(TODAY, 9, 1))])
        t = self.feed()["periods"]["today"]
        self.assertEqual(t["corrections"]["count"], 1)
        self.assertEqual(t["models"][0]["corrections"], 0)

    def test_where_most_is_the_top_three_activities_by_cost(self):
        self.transcript("s1", [
            user("run the test suite", at(TODAY, 8)),
            asst("msg_1", at(TODAY, 8, 1), usage(0, 400000, 0, 0), tools=[("Bash", {"command": "pytest"})]),
            user("tidy", at(TODAY, 9)),
            asst("msg_2", at(TODAY, 9, 1), usage(0, 300000, 0, 0), tools=[("Edit", {"file_path": "a"})]),
            user("thanks", at(TODAY, 10)),
            asst("msg_3", at(TODAY, 10, 1), usage(0, 200000, 0, 0)),
            user("look", at(TODAY, 11)),
            asst("msg_4", at(TODAY, 11, 1), usage(0, 100000, 0, 0), tools=[("Read", {})]),
            user("look more", at(TODAY, 11, 5)),
            asst("msg_5", at(TODAY, 11, 6), usage(0, 900000, 0, 0), tools=[("Read", {})], model=OPUS)])
        rows = self.rows()
        self.assertEqual(rows["Claude Fable 5.2"]["where_most"], "Testing, Coding, Conversation")
        self.assertEqual(rows["Claude Opus 5.5"]["where_most"], "Exploration")

    def test_where_has_at_most_six_rows_most_errors_first(self):
        recs = [user("go", at(TODAY, 9))]
        tools, results = [], []
        names = ["Tool%d" % i for i in range(7)] + ["mcp__Claude_Browser__navigate"] * 9
        for i, name in enumerate(names):
            n = (int(name[4:]) + 1) if name.startswith("Tool") else 1
            for k in range(n):
                tid = "t%d_%d" % (i, k)
                tools.append((name, {}, tid))
                results.append(result(tid, at(TODAY, 9, 2), error=True))
        recs.append(asst("msg_1", at(TODAY, 9, 1), tools=tools))
        recs += results
        self.transcript("s1", recs)
        where = self.rows()["Claude Fable 5.2"]["where"]
        self.assertEqual([(w["name"], w["errors"]) for w in where],
                         [("Claude browser", 9), ("Tool6", 7), ("Tool5", 6), ("Tool4", 5), ("Tool3", 4),
                          ("Tool2", 3)])
        self.assertEqual(where[0]["kind"], "mcp")

    def test_a_resumed_copy_counts_its_tool_calls_once(self):
        first = [user("go", at(TODAY, 9)), asst("msg_a1", at(TODAY, 9, 1), tools=[("Bash", {}, "t1")]),
                 result("t1", at(TODAY, 9, 2), error=True)]
        self.transcript("s1", first, mtime=NOW - 7200)
        self.transcript("s2", first + [user("more", at(TODAY, 10)),
                                       asst("msg_b1", at(TODAY, 10, 1), tools=[("Read", {}, "t2")])])
        f = self.rows()["Claude Fable 5.2"]
        self.assertEqual((f["tool_calls"], f["tool_errors"]), (2, 1))

    def test_exclusions_and_the_account_filter_apply(self):
        self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1), usage(0, 10, 0, 0))])
        self.transcript("s2", [user("go", at(TODAY, 9), cwd="/tmp/demo/secret-client"),
                               asst("msg_2", at(TODAY, 9, 1), usage(0, 5000, 0, 0), model=OPUS,
                                    cwd="/tmp/demo/secret-client", tools=[("Bash", {}, "t1")]),
                               result("t1", at(TODAY, 9, 2), error=True)],
                        pdir="-tmp-demo-secret-client")
        self.transcript("s3", [user("go", at("2026-11-22", 9)),
                               asst("msg_3", at("2026-11-22", 9, 1), usage(0, 10, 0, 0), model=OPUS)])
        self.assertEqual(sorted(self.rows("week")), ["Claude Fable 5.2", "Claude Opus 5.5"])
        self.config(exclude=[{"match": ["secret-client"]}], exclude_dates=["2026-11-22"])
        self.assertEqual(sorted(self.rows("week")), ["Claude Fable 5.2"])
        self.config(only_account="aaaaaa")
        import accounts
        accounts.record("s1", when=at(TODAY, 8), who={"account": "bbbbbb", "org": "cccccc"})
        self.assertEqual(sorted(self.rows("week")), ["Claude Opus 5.5"])

    def test_ledger_days_give_cost_by_model_only(self):
        led = {"version": 1, "days": {"2026-11-10": {"closed": True, "sessions": {
            "old-session-1": {"project": "acme", "scheduled": False,
                              "models": {MODEL: [[0, 2000000, 0, 0, 0], [0, 1000000, 0, 0, 0]]}}}}}}
        L.save(L._clean(led))
        rows = self.feed()["periods"]["month"]["models"]
        out = P.rates(MODEL)[1]
        self.assertEqual(rows, [{"name": "Claude Fable 5.2", "usd": round(3 * out, 2), "share": 1.0,
                                 "main_usd": round(2 * out, 2), "sub_usd": round(out, 2),
                                 "first_try": None, "edit_turns": 0, "tool_calls": 0, "tool_errors": 0,
                                 "error_rate": None, "redone_edits": 0, "corrections": 0,
                                 "where_most": None, "where": []}])

    def test_no_prompt_text_in_the_feed_or_the_cache(self):
        self.transcript("s1", [user("add the zebra-unicorn page", at(TODAY, 9)),
                               asst("msg_1", at(TODAY, 9, 1), tools=[("Bash", {"command": "echo zebra-cmd"})]),
                               user("no, zebra is wrong", at(TODAY, 9, 5)),
                               asst("msg_2", at(TODAY, 9, 6))])
        f = self.feed()
        self.assertEqual(f["periods"]["today"]["models"][0]["corrections"], 1)
        self.assertNotIn("zebra", json.dumps(f))
        for name in ("live-cache.json", "live-mids.json"):
            with open(os.path.join(self.home, name)) as fh:
                self.assertNotIn("zebra", fh.read())

    def test_odd_content_keeps_the_message_usage(self):
        bad = asst("msg_2", at(TODAY, 9, 2), usage(0, 70, 0, 0))
        bad["message"]["content"] = 5
        self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1), usage(0, 30, 0, 0)), bad])
        t = self.feed()["periods"]["today"]
        self.assertEqual((t["tokens"], t["calls"]), (100, 2))
        self.assertEqual(t["models"][0]["tool_calls"], 0)

    def test_an_older_cache_is_rebuilt(self):
        self.assertGreaterEqual(LV.CACHE_VERSION, 7)
        self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1), tools=[("Bash", {})])])
        self.feed()
        with open(LV.cache_file()) as fh:
            cache = json.load(fh)
        cache["v"] = 6                          # a cache written before figures per model
        for e in cache["files"].values():
            e["agg"].pop("mod", None)
        with open(LV.cache_file(), "w") as fh:
            json.dump(cache, fh)
        st = {}
        f = self.feed(stats=st)
        self.assertEqual(st["read"], 1)
        self.assertEqual(f["periods"]["today"]["models"][0]["tool_calls"], 1)


class Exclusions(Home):
    def two(self):
        self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1), usage(0, 10, 0, 0))])
        self.transcript("s2", [user("go", at(TODAY, 9), cwd="/tmp/demo/secret-client"),
                               asst("msg_2", at(TODAY, 9, 1), usage(0, 5000, 0, 0),
                                    cwd="/tmp/demo/secret-client",
                                    tools=[("mcp__x__y", {})])],
                        pdir="-tmp-demo-secret-client", mtime=NOW - 30)
        self.transcript("s2", [user("go", at(TODAY, 9, 2), isSidechain=True),
                               asst("msg_3", at(TODAY, 9, 3), usage(0, 7000, 0, 0), isSidechain=True)],
                        pdir="-tmp-demo-secret-client", sub="agent-1")

    def test_exclude_rule(self):
        self.two()
        self.assertEqual(self.feed()["periods"]["today"]["tokens"], 12010)
        self.config(exclude=[{"match": ["secret-client"]}])
        f = self.feed()
        t = f["periods"]["today"]
        self.assertEqual((t["tokens"], t["sessions"], t["calls"]), (10, 1, 1))
        self.assertEqual([r["name"] for r in t["projects"]], ["alpha"])
        self.assertEqual(f["running"], [])
        with open(LV.cache_file()) as fh:
            cache = json.load(fh)
        for path, e in cache["files"].items():
            if "secret" in path:                # no figure, folder or title is kept for it
                self.assertTrue(e.get("skip") or e.get("hidden"))
                self.assertEqual(e.get("agg", {}).get("h", {}), {})
                self.assertEqual(e.get("agg", {}).get("mcp", {}), {})
                self.assertIsNone(e.get("open"))
                self.assertEqual(e.get("project", ""), "")
                if "meta" in e:
                    self.assertEqual(e["meta"], LV.new_entry()["meta"])

    def test_a_hidden_session_stays_hidden_however_many_titles_follow(self):
        self.config(exclude=[{"match": ["payroll"]}])
        recs = [{"type": "ai-title", "aiTitle": "Payroll export"},
                user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1), usage(0, 100, 0, 0))]
        path = self.transcript("s1", recs)
        self.assertEqual(self.feed()["periods"]["today"]["tokens"], 0)
        more = [{"type": "ai-title", "aiTitle": "Title %d" % i} for i in range(8)]
        more += [user("go", at(TODAY, 10)), asst("msg_2", at(TODAY, 10, 1), usage(0, 1400, 0, 0))]
        self.transcript("s1", more, append=True, mtime=NOW - 30)
        f = self.feed()
        self.assertEqual((f["periods"]["today"]["tokens"], f["running"]), (0, []))
        os.remove(LV.cache_file())
        os.remove(LV.mids_file())
        self.assertEqual(self.feed()["periods"]["today"]["tokens"], 0)      # cold: all titles are tested
        self.assertTrue(os.path.exists(path))

    def test_the_ledger_is_not_used_under_other_settings(self):
        # the ledger was written before the rule: it still holds the session by folder
        led = {"version": 1, "days": {"2026-11-10": {"closed": True, "sessions": {
            "client-session-1": {"project": "acme", "scheduled": False,
                                 "models": {MODEL: [[0, 2000000, 0, 0, 0], [0, 0, 0, 0, 0]]}}}}}}
        L.save(L._clean(led))
        self.assertEqual(self.feed()["periods"]["month"]["tokens"], 2000000)
        self.config(exclude=[{"match": ["/clients/"]}])
        m = self.feed()["periods"]["month"]
        self.assertEqual((m["tokens"], m["projects"]), (0, []))

    def test_another_account_seen_later_is_left_out(self):
        import accounts
        self.config(only_account="aaaaaa")
        self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1), usage(0, 6000, 0, 0))])
        self.assertEqual(self.feed()["periods"]["today"]["tokens"], 6000)   # no evidence yet: shown
        accounts.record("s1", when=at(TODAY, 8), who={"account": "bbbbbb", "org": "cccccc"})
        st = {}
        f = self.feed(stats=st)
        self.assertEqual(st["read"], 0)                                     # from the cache, and still left out
        self.assertEqual(f["periods"]["today"]["tokens"], 0)

    def test_exclude_dates(self):
        self.transcript("s1", [user("go", at("2026-11-22", 9)),
                               asst("msg_1", at("2026-11-22", 9, 1), usage(0, 10, 0, 0)),
                               user("go", at(TODAY, 9)),
                               asst("msg_2", at(TODAY, 9, 1), usage(0, 3, 0, 0))])
        self.config(exclude_dates=["2026-11-22"])
        p = self.feed()["periods"]
        self.assertEqual(p["week"]["tokens"], 3)
        self.assertIsNone(p["today"]["prev_cost_usd"])

    def test_a_session_left_out_by_date_keeps_no_figures(self):
        self.config(exclude_dates=[TODAY])
        self.transcript("s1", [user("go", at(TODAY, 9), cwd="/tmp/demo/zebra"),
                               asst("msg_1", at(TODAY, 9, 1), usage(0, 4321, 0, 0), cwd="/tmp/demo/zebra",
                                    tools=[("mcp__srv__tool", {})])], pdir="-p1")
        self.assertEqual(self.feed()["periods"]["today"]["tokens"], 0)
        with open(LV.cache_file()) as fh:
            text = fh.read()
        for word in ("4321", "zebra", "srv"):
            self.assertNotIn(word, text)

    def test_a_hidden_session_stays_hidden_across_a_day_that_is_left_out(self):
        self.config(exclude=[{"match": ["payroll"]}], exclude_dates=["2026-11-22"])
        self.transcript("s1", [{"type": "ai-title", "aiTitle": "Payroll export"},
                               user("go", at("2026-11-21", 9)),
                               asst("msg_1", at("2026-11-21", 9, 1), usage(0, 100, 0, 0))])
        self.feed()
        self.transcript("s1", [user("go", at("2026-11-22", 9)),
                               asst("msg_2", at("2026-11-22", 9, 1), usage(0, 100, 0, 0))],
                        append=True, mtime=NOW - 4000)
        self.feed()
        self.transcript("s1", [user("go", at(TODAY, 12, 20)),
                               asst("msg_3", at(TODAY, 12, 21), usage(0, 300, 0, 0))],
                        append=True, mtime=NOW - 30)
        f = self.feed()
        self.assertEqual((f["periods"]["week"]["tokens"], f["running"]), (0, []))
        with open(LV.cache_file()) as fh:
            self.assertNotIn("/tmp/demo", fh.read())

    def flip(self, answers):
        """Stand in for the account gate: answers is a list the test changes between runs."""
        class Gate(object):
            dropped = False

            def __call__(self, day):
                if not answers[0]:
                    self.dropped = True
                return answers[0]

        real = LV.SC.Scope.gate
        LV.SC.Scope.gate = lambda self_, sid, transcript=None: Gate()
        self.addCleanup(setattr, LV.SC.Scope, "gate", real)

    def test_account_evidence_for_the_wanted_account_that_comes_later(self):
        # read while the gate said no; the transcript then shows it is the wanted account
        answers = [False]
        self.flip(answers)
        self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1), usage(0, 110, 0, 0))])
        self.transcript("s1", [user("x", at(TODAY, 9, 2), isSidechain=True),
                               asst("msg_s", at(TODAY, 9, 3), usage(0, 500, 0, 0), isSidechain=True)],
                        sub="agent-1")
        self.assertEqual(self.feed()["periods"]["today"]["tokens"], 0)
        answers[0] = True
        self.transcript("s1", [asst("msg_2", at(TODAY, 9, 5), usage(0, 7, 0, 0))], append=True, mtime=NOW - 10)
        t = self.feed()["periods"]["today"]
        self.assertEqual((t["tokens"], t["calls"]), (617, 3))       # the earlier records and the subagent too

    def test_another_account_seen_later_takes_the_subagents_out_too(self):
        answers = [True]
        self.flip(answers)
        self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1), usage(0, 110, 0, 0))])
        self.transcript("s1", [user("x", at(TODAY, 9, 2), isSidechain=True),
                               asst("msg_s", at(TODAY, 9, 3), usage(0, 500, 0, 0), isSidechain=True)],
                        sub="agent-1")
        self.assertEqual(self.feed()["periods"]["today"]["tokens"], 610)
        answers[0] = False
        self.transcript("s1", [asst("msg_2", at(TODAY, 9, 5), usage(0, 7, 0, 0))], append=True, mtime=NOW - 10)
        t = self.feed()["periods"]["today"]
        self.assertEqual((t["tokens"], t["calls"], t["sessions"]), (0, 0, 0))
        with open(LV.cache_file()) as fh:
            self.assertNotIn("500", json.dumps(json.load(fh)["files"]).replace(str(NOW), ""))

    def test_a_prompt_read_before_its_answer_keeps_its_turn(self):
        # the run falls between the prompt and the answer: the prompt's flags and correction stay
        self.transcript("s1", [user("no. rename it", at(TODAY, 9))])
        self.feed()
        self.transcript("s1", [asst("msg_1", at(TODAY, 9, 1), usage(0, 1000, 0, 0),
                                    tools=[("Edit", {"file_path": "a"})])], append=True, mtime=NOW - 20)
        t = self.feed()["periods"]["today"]
        self.assertEqual(t["corrections"]["count"], 1)
        self.assertEqual([a["name"] for a in t["activities"]], ["Refactoring"])

    def test_a_broken_config_stops_the_feed(self):
        self.config(exclude="oops")
        self.assertRaises(C.ConfigError, self.feed)
        self.assertFalse(os.path.exists(LV.live_file()))

    def test_a_broken_config_takes_the_old_feed_down(self):
        self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1))])
        LV.write(force=True, now=NOW, projects_dir=self.projects)
        self.config(exclude="oops")
        self.assertRaises(C.ConfigError, LV.write, True, NOW + 60, self.projects)
        with open(LV.live_file()) as fh:
            feed = json.load(fh)
        self.assertEqual((feed["version"], feed["periods"], feed["running"], feed["tips"]), (1, {}, [], []))
        self.assertEqual(feed["problem"], "settings")

    def test_a_resumed_copy_of_a_hidden_session_does_not_bring_its_work_back(self):
        self.config(exclude=[{"sessions": ["hidden-session-1"]}])
        old = [user("go", at(TODAY, 8)), asst("msg_h1", at(TODAY, 8, 1), usage(0, 5000, 0, 0))]
        self.transcript("hidden-session-1", old, mtime=NOW - 7000)
        self.feed()
        later = [asst("msg_h2", at(TODAY, 8, 30), usage(0, 2800, 0, 0))]
        self.transcript("hidden-session-1", later, append=True, mtime=NOW - 5000)
        self.feed()
        self.transcript("copy-session-22", old + later + [user("more", at(TODAY, 10)),
                        asst("msg_c1", at(TODAY, 10, 1), usage(0, 15, 0, 0))], mtime=NOW - 100)
        self.assertEqual(self.feed()["periods"]["today"]["tokens"], 15)

    def test_account_evidence_inside_the_transcript_that_comes_later(self):
        self.config(only_account="aaaaaa")
        self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1), usage(0, 110, 0, 0))])
        self.assertEqual(self.feed()["periods"]["today"]["tokens"], 110)    # no evidence yet: shown
        real = LV.SC.Scope.gate

        class Other(object):
            dropped = True

            def __call__(self, day):
                return False

        LV.SC.Scope.gate = lambda self_, sid, transcript=None: Other()     # the file now names another account
        self.addCleanup(setattr, LV.SC.Scope, "gate", real)
        try:
            self.transcript("s1", [asst("msg_2", at(TODAY, 9, 5), usage(0, 7, 0, 0))], append=True,
                            mtime=NOW - 10)
            f = self.feed()
        finally:
            LV.SC.Scope.gate = real
        self.assertEqual((f["periods"]["today"]["tokens"], f["periods"]["today"]["calls"]), (0, 0))
        with open(LV.cache_file()) as fh:
            self.assertNotIn('"cwd":"/tmp', fh.read().replace(" ", ""))


class Cache(Home):
    def test_unchanged_files_are_not_opened(self):
        self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1))])
        self.transcript("s2", [user("go", at(TODAY, 9)), asst("msg_2", at(TODAY, 9, 1))])
        st = {}
        first = self.feed(stats=st)
        self.assertEqual((st["files"], st["read"]), (2, 2))
        st = {}
        second = self.feed(stats=st)
        self.assertEqual((st["files"], st["read"], st["bytes"]), (2, 0, 0))
        self.assertEqual(first["periods"], second["periods"])

    def test_a_grown_file_is_read_from_where_it_stopped(self):
        recs = [user("tidy", at(TODAY, 9)),
                asst("msg_1", at(TODAY, 9, 1), usage(0, 10, 0, 0), tools=[("Edit", {"file_path": "a"})])]
        more = [asst("msg_2", at(TODAY, 9, 2), usage(0, 20, 0, 0), tools=[("Bash", {"command": "make"})]),
                asst("msg_3", at(TODAY, 9, 3), usage(0, 30, 0, 0), tools=[("Edit", {"file_path": "a"})]),
                user("no, wrong", at(TODAY, 9, 5)),
                asst("msg_4", at(TODAY, 9, 6), usage(0, 40, 0, 0))]
        path = self.transcript("s1", recs)
        size = os.path.getsize(path)
        self.feed()
        self.transcript("s1", more, append=True, mtime=NOW - 100)
        st = {}
        grown = self.feed(stats=st)
        self.assertEqual(st["read"], 1)
        self.assertEqual(st["bytes"], os.path.getsize(path) - size)     # only the new part
        os.remove(LV.cache_file())
        cold = self.feed()
        self.assertEqual(grown["periods"], cold["periods"])
        t = grown["periods"]["today"]
        self.assertEqual((t["calls"], t["tokens"], t["corrections"]["count"]), (4, 100, 1))
        self.assertEqual(t["first_try"]["rate"], 0.0)                   # the retry spans the two reads

    def test_a_half_written_line_waits(self):
        path = self.transcript("s1", [user("go", at(TODAY, 9)),
                                      asst("msg_1", at(TODAY, 9, 1), usage(0, 10, 0, 0))])
        line = json.dumps(asst("msg_2", at(TODAY, 9, 2), usage(0, 5, 0, 0)))
        with open(path, "a") as fh:
            fh.write(line[:40])
        os.utime(path, (NOW - 90, NOW - 90))
        self.assertEqual(self.feed()["periods"]["today"]["tokens"], 10)
        with open(path, "a") as fh:
            fh.write(line[40:] + "\n")
        os.utime(path, (NOW - 80, NOW - 80))
        self.assertEqual(self.feed()["periods"]["today"]["tokens"], 15)

    def test_a_rewritten_file_starts_again(self):
        self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1), usage(0, 10, 0, 0)),
                               asst("msg_2", at(TODAY, 9, 2), usage(0, 10, 0, 0))])
        self.feed()
        self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1), usage(0, 7, 0, 0))],
                        mtime=NOW - 50)
        self.assertEqual(self.feed()["periods"]["today"]["tokens"], 7)

    def test_another_time_zone_empties_the_cache(self):
        self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1))])
        self.feed()
        with open(LV.cache_file()) as fh:
            cache = json.load(fh)
        cache["zone"] = [28800, ["PST", "PDT"]]
        with open(LV.cache_file(), "w") as fh:
            json.dump(cache, fh)
        st = {}
        self.feed(stats=st)
        self.assertEqual(st["read"], 1)

    def test_a_settings_change_empties_the_cache(self):
        self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1))])
        self.feed()
        self.config(exclude_dates=["2026-01-01"])
        st = {}
        self.feed(stats=st)
        self.assertEqual(st["read"], 1)

    def test_a_run_cut_short_loses_nothing(self):
        self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1), usage(0, 10, 0, 0))])
        self.feed()
        self.transcript("s1", [asst("msg_2", at(TODAY, 9, 2), usage(0, 10000, 0, 0))], append=True,
                        mtime=NOW - 40)
        cache = LV.refresh(self.projects, NOW)          # the run dies here, before anything is saved
        del cache
        t = self.feed()["periods"]["today"]
        self.assertEqual((t["tokens"], t["calls"]), (10010, 2))

    def test_a_rename_reaches_a_transcript_that_is_gone(self):
        a = self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1))])
        self.transcript("s2", [user("go", at(TODAY, 9)), asst("msg_2", at(TODAY, 9, 1))])
        self.feed()
        os.remove(a)
        self.config(projects=[{"name": "Renamed", "match": ["/demo/alpha"]}])
        rows = self.feed()["periods"]["today"]["projects"]
        self.assertEqual([(r["name"], r["sessions"]) for r in rows], [("Renamed", 2)])

    def test_a_deleted_recent_transcript_keeps_its_numbers(self):
        path = self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1),
                                                                     usage(0, 10, 0, 0))])
        self.feed()
        os.remove(path)
        self.assertEqual(self.feed()["periods"]["today"]["tokens"], 10)


class LedgerDays(Home):
    def test_a_closed_day_the_ledger_holds_more_of(self):
        # the transcript of Nov 10 is gone; the ledger still has the day
        block = [0, 2000000, 0, 0, 0]
        led = {"version": 1, "days": {"2026-11-10": {"closed": True, "sessions": {
            "old-session-1": {"project": "beta", "scheduled": False,
                              "models": {MODEL: [block, [0, 0, 0, 0, 0]]}}}}}}
        L.save(L._clean(led))
        self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1), usage(0, 10, 0, 0))])
        m = self.feed()["periods"]["month"]
        self.assertEqual(m["tokens"], 2000010)
        self.assertEqual(m["sessions"], 2)
        self.assertEqual([r["name"] for r in m["projects"]], ["beta", "alpha"])
        self.assertEqual(m["calls"], 1)             # calls exist only where the transcript does
        day = [pt for pt in m["series"] if pt["t"][:10] == "2026-11-10"][0]
        self.assertEqual(day["tokens"], 2000000)

    def test_today_never_comes_from_the_ledger(self):
        led = {"version": 1, "days": {TODAY: {"closed": False, "sessions": {
            "x-session-1": {"project": "beta", "scheduled": False,
                            "models": {MODEL: [[0, 999, 0, 0, 0], [0, 0, 0, 0, 0]]}}}}}}
        L.save(L._clean(led))
        self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1), usage(0, 10, 0, 0))])
        self.assertEqual(self.feed()["periods"]["today"]["tokens"], 10)


# ---------- the All time period ----------

class AllTime(Home):
    BLOCK = [0, 2000000, 0, 0, 0]

    def ledger(self, *days, **more):
        """A ledger with one closed session of 2M output tokens on each given day."""
        led = {"version": 1, "days": {}}
        for i, day in enumerate(days):
            led["days"][day] = {"closed": True, "sessions": {
                "old-session-%d" % i: {"project": more.get("project", "beta"), "scheduled": False,
                                       "models": {MODEL: [list(self.BLOCK), [0, 0, 0, 0, 0]]}}}}
        L.save(L._clean(led))

    def all(self):
        return self.feed()["periods"]["all"]

    def test_label_names_the_first_day_the_ledger_has(self):
        self.ledger("2026-11-09", "2026-11-15")
        self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1), usage(0, 10, 0, 0))])
        a = self.all()
        self.assertEqual(a["label"], "All time, since Nov 9")
        self.assertEqual(a["since"][:19], "2026-11-09T00:00:00")
        self.assertRegex(a["since"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
        # cost, sessions, tokens and projects cover every day, the ledger's included
        self.assertEqual(a["tokens"], 4000010)
        self.assertEqual(a["sessions"], 3)
        self.assertEqual([r["name"] for r in a["projects"]], ["beta", "alpha"])
        self.assertGreater(a["cost_usd"], self.feed()["periods"]["today"]["cost_usd"])
        self.assertEqual(a["calls"], 1)                 # calls exist only where the transcript does

    def test_a_single_digit_day_has_no_leading_zero(self):
        self.ledger("2026-10-03")
        self.assertEqual(self.all()["label"], "All time, since Oct 3")

    def test_a_first_day_in_an_earlier_year_says_the_year(self):
        self.ledger("2025-12-03")
        a = self.all()
        self.assertEqual(a["label"], "All time, since Dec 3, 2025")
        self.assertEqual(a["series_unit"], "week")
        self.assertEqual(a["series"][0]["t"][:10], "2025-12-01")        # that week's Monday
        self.assertEqual(a["series"][-1]["t"][:10], TODAY)
        self.assertEqual(LV._since_text("2026-01-01", TODAY), "Jan 1")
        self.assertEqual(LV._since_text("2025-12-31", TODAY), "Dec 31, 2025")

    def test_a_day_from_a_broken_clock_does_not_stretch_all_time(self):
        # a record dated 2010 reached the ledger; the real history starts in October
        self.ledger("2010-01-05", "2026-10-20")
        self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1), usage(0, 10, 0, 0))])
        a = self.all()
        self.assertEqual(a["label"], "All time, since Oct 20")
        self.assertEqual(a["tokens"], 2000010)              # the newest days are all there
        self.assertEqual(a["sessions"], 2)
        self.assertEqual(a["series"][-1]["t"][:10], TODAY)
        # ten years is the furthest it goes
        self.assertEqual(LV.first_day(type("T", (), {"days": {L.shift(TODAY, -3659): {"s": 1}}})(), TODAY),
                         L.shift(TODAY, -3659))
        self.assertEqual(LV.first_day(type("T", (), {"days": {L.shift(TODAY, -3660): {"s": 1}}})(), TODAY), TODAY)

    def test_a_date_left_out_stays_out_of_all_time(self):
        self.ledger("2026-11-09", "2026-11-15")
        self.config(exclude_dates=["2026-11-15"])
        L.save(L._clean(dict(L.load()[0], exclude_sig=LV.SC.Scope(C.load_config()).sig())))
        self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1), usage(0, 10, 0, 0))])
        a = self.all()
        self.assertEqual(a["label"], "All time, since Nov 9")
        self.assertEqual(a["tokens"], 2000010)              # Nov 15's two million are not counted
        self.assertEqual([p["tokens"] for p in a["series"] if p["t"][:10] == "2026-11-15"], [0])

    def test_with_nothing_at_all_it_starts_today(self):
        a = self.all()
        self.assertEqual(a["label"], "All time, since Nov 23")
        self.assertEqual((a["cost_usd"], a["tokens"], a["sessions"], a["calls"]), (0, 0, 0, 0))
        self.assertEqual((a["series_unit"], len(a["series"])), ("day", 1))
        self.assertEqual(a["since"][:10], TODAY)
        self.assertIsNone(a["detail_since"])
        self.assertIsNone(a["cache_hit"]["rate"])

    def test_without_a_ledger_it_starts_on_the_first_day_read(self):
        self.transcript("s1", [user("go", at("2026-11-20", 9)), asst("msg_1", at("2026-11-20", 9, 1)),
                               user("more", at(TODAY, 9)), asst("msg_2", at(TODAY, 9, 1))])
        a = self.all()
        self.assertEqual(a["label"], "All time, since Nov 20")
        self.assertEqual(a["since"], a["detail_since"])     # the detail covers the whole period

    def test_one_point_a_day_up_to_sixty_days(self):
        self.ledger(L.shift(TODAY, -59))                    # 60 days, today included
        a = self.all()
        self.assertEqual((a["series_unit"], len(a["series"])), ("day", 60))
        self.assertEqual(a["series"][0]["t"][:10], L.shift(TODAY, -59))
        self.assertEqual(a["series"][-1]["t"][:10], TODAY)
        self.assertEqual(sum(p["tokens"] for p in a["series"]), a["tokens"])

    def test_one_point_a_week_beyond_sixty_days(self):
        first = L.shift(TODAY, -60)                         # 61 days: Thursday Sep 24
        self.ledger(first, "2026-10-14")
        self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1), usage(0, 10, 0, 0))])
        a = self.all()
        self.assertEqual(a["series_unit"], "week")
        days = [p["t"][:10] for p in a["series"]]
        self.assertEqual(days[0], "2026-09-21")             # the Monday of the first, partial week
        self.assertEqual(days[-1], TODAY)                   # Nov 23 is a Monday
        self.assertEqual(len(days), 10)
        for day in days:
            self.assertEqual(L._date(day).weekday(), 0)     # every point is dated by its Monday
        self.assertEqual([L.shift(d, 7) for d in days[:-1]], days[1:])
        self.assertEqual(sum(p["tokens"] for p in a["series"]), a["tokens"])
        self.assertEqual(a["series"][0]["tokens"], 2000000)
        self.assertEqual([p for p in a["series"] if p["t"][:10] == "2026-10-12"][0]["tokens"], 2000000)
        self.assertEqual(a["series"][-1]["tokens"], 10)
        self.assertEqual(a["peak"]["t"][:10], "2026-09-21")
        self.assertEqual(a["label"], "All time, since Sep 24")      # the label keeps the real first day
        for p in a["series"]:
            self.assertRegex(p["t"], r"^\d{4}-\d\d-\d\dT00:00:00[+-]\d\d:\d\d$")

    def test_there_is_no_period_before(self):
        self.ledger("2026-08-01", "2026-10-20")
        self.transcript("s1", [
            user("edit it", at("2026-11-22", 9)),
            asst("msg_0", at("2026-11-22", 9, 1), tools=[("Edit", {"file_path": "/tmp/demo/alpha/a.py"})]),
            user("no, that is wrong", at(TODAY, 9)),
            asst("msg_1", at(TODAY, 9, 1), tools=[("Edit", {"file_path": "/tmp/demo/alpha/a.py"})])])
        f = self.feed()["periods"]
        a = f["all"]
        self.assertIsNone(a["prev_cost_usd"])
        self.assertIsNone(a["first_try"]["prev_rate"])
        self.assertIsNone(a["cache_hit"]["prev_rate"])
        self.assertIsNone(a["corrections"]["prev_count"])
        self.assertIsNotNone(a["first_try"]["rate"])         # the figures themselves are there
        self.assertIsNotNone(f["today"]["prev_cost_usd"])     # and the other periods still compare
        self.assertIsNotNone(f["month"]["prev_cost_usd"])

    def test_detail_starts_where_the_transcripts_do(self):
        self.ledger("2026-09-01", "2026-11-10")
        self.transcript("s1", [user("go", at("2026-11-20", 9)),
                               asst("msg_0", at("2026-11-20", 9, 1), usage(100, 10, 300, 0)),
                               user("more", at(TODAY, 9)),
                               asst("msg_1", at(TODAY, 9, 1), usage(100, 10, 300, 0))])
        a = self.all()
        self.assertEqual(a["since"][:10], "2026-09-01")
        self.assertEqual(a["detail_since"][:10], "2026-11-20")
        self.assertRegex(a["detail_since"], r"^\d{4}-\d\d-\d\dT00:00:00[+-]\d\d:\d\d$")
        self.assertEqual(a["calls"], 2)
        self.assertEqual([r["turns"] for r in a["activities"]], [2])
        # cache hit is worked out from the days read: 600 read of 800 in. The ledger's
        # days (output only here, so no cache at all) do not pull it down or blank it.
        self.assertEqual(a["cache_hit"]["rate"], 0.75)
        self.assertEqual(a["tokens"], 4000000 + 2 * 410)

    def test_small_series_follow_the_graph_and_have_at_most_twelve_steps(self):
        self.ledger("2026-06-01")                           # 176 days: 26 weeks
        self.transcript("s1", [user("go", at("2026-11-20", 9)),
                               asst("msg_0", at("2026-11-20", 9, 1), usage(100, 10, 300, 0)),
                               user("more", at(TODAY, 9)),
                               asst("msg_1", at(TODAY, 9, 1), usage(100, 10, 100, 0))])
        a = self.all()
        self.assertEqual((a["series_unit"], len(a["series"])), ("week", 26))
        for key in ("first_try", "cache_hit", "corrections"):
            self.assertEqual(len(a[key]["series"]), 12)
        # weeks with no transcript read have nothing to measure, the ledger's first week included
        self.assertEqual(a["cache_hit"]["series"][:11], [None] * 11)
        self.assertEqual(a["corrections"]["series"][:11], [None] * 11)
        self.assertEqual(a["first_try"]["series"], [None] * 12)        # no edit turns anywhere
        # the last step is the last three weeks, so it holds both days read: 400 read of 600 in
        self.assertEqual(a["cache_hit"]["series"][11], 0.6667)
        self.assertEqual(a["corrections"]["series"][11], 0)

    def test_small_series_by_day_when_the_graph_is_by_day(self):
        self.ledger(L.shift(TODAY, -29))                    # 30 days
        self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1), usage(100, 10, 300, 0))])
        a = self.all()
        self.assertEqual(a["series_unit"], "day")
        self.assertEqual(len(a["cache_hit"]["series"]), 12)
        self.assertEqual(a["cache_hit"]["series"][:11], [None] * 11)     # the ledger's day adds nothing here
        self.assertEqual(a["cache_hit"]["series"][11], 0.75)

    def test_week_groups(self):
        days = L.day_range("2026-11-19", "2026-11-24")
        self.assertEqual(LV.week_groups(days), [("2026-11-16", ["2026-11-19", "2026-11-20", "2026-11-21", "2026-11-22"]),
                                                ("2026-11-23", ["2026-11-23", "2026-11-24"])])
        self.assertEqual(LV.week_groups([]), [])


# ---------- running sessions ----------

class Running(Home):
    def test_changed_in_the_last_ten_minutes(self):
        big = usage(2, 1000, 600000, 40000)
        self.transcript("s1", [user("go", at(TODAY, 10, 29)),
                               asst("msg_1", at(TODAY, 10, 30), usage(1, 1, 1, 1)),
                               asst("msg_2", at(TODAY, 12, 25), big)], mtime=NOW - 120)
        self.transcript("s2", [user("go", at(TODAY, 9)), asst("msg_3", at(TODAY, 9, 1))],
                        mtime=NOW - 601)
        rows = self.feed()["running"]
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(sorted(r), ["branch", "context_left_tokens", "context_pct", "level", "model",
                                     "project", "started"])
        self.assertEqual((r["project"], r["branch"], r["model"]), ("alpha", "main", "Fable 5.2"))
        self.assertEqual(r["started"][:19], "2026-11-23T10:29:00")
        # context = input + cache read + cache write + output of the last assistant message
        self.assertEqual(r["context_left_tokens"], 1000000 - 641002)
        self.assertEqual(r["context_pct"], 64)
        self.assertEqual(r["level"], "crit")            # over signals.T peak_context

    def raw(self, path, line):
        """Append one line exactly as Claude Code writes it (no spaces), keeping the file's time."""
        mtime = os.path.getmtime(path)
        with open(path, "a") as fh:
            fh.write(line + "\n")
        os.utime(path, (mtime, mtime))

    CLOSED = '{"type":"cost-state","sessionId":"s1","totalCostUSD":1.5}'

    def test_a_resumed_session_runs_from_its_current_run(self):
        # the owner's case: opened on Nov 20, closed, and picked up again today
        path = self.transcript("s1", [user("go", at("2026-11-20", 15, 18)),
                                      asst("msg_1", at("2026-11-20", 16, 24))])
        self.raw(path, self.CLOSED)
        self.transcript("s1", [user("back again", at(TODAY, 11, 36)), asst("msg_2", at(TODAY, 12, 25))],
                        append=True, mtime=NOW - 60)
        r = self.feed()["running"][0]
        self.assertEqual(r["started"][:19], "2026-11-23T11:36:00")

    def test_a_close_starts_a_new_run_even_minutes_later(self):
        # closed at 11:00 and opened again at 11:20: far inside the six hours, so only the
        # close record can be what starts the new run
        path = self.transcript("s1", [user("go", at(TODAY, 9, 0)), asst("msg_1", at(TODAY, 11, 0))])
        self.raw(path, self.CLOSED)
        self.transcript("s1", [user("back again", at(TODAY, 11, 20)), asst("msg_2", at(TODAY, 12, 25))],
                        append=True, mtime=NOW - 60)
        self.assertEqual(self.feed()["running"][0]["started"][:19], "2026-11-23T11:20:00")
        # the same file without the close record is one run
        self.transcript("s2", [user("go", at(TODAY, 9, 0)), asst("msg_3", at(TODAY, 11, 0)),
                               user("back again", at(TODAY, 11, 20)), asst("msg_4", at(TODAY, 12, 26))],
                        mtime=NOW - 30)
        starts = sorted(r["started"][:19] for r in self.feed()["running"])
        self.assertEqual(starts, ["2026-11-23T09:00:00", "2026-11-23T11:20:00"])

    def test_a_session_resumed_between_two_reads(self):
        path = self.transcript("s1", [user("go", at("2026-11-20", 15, 18)),
                                      asst("msg_1", at("2026-11-20", 16, 24))], mtime=NOW - 60)
        self.assertEqual(self.feed()["running"][0]["started"][:19], "2026-11-20T15:18:00")
        self.raw(path, self.CLOSED)
        self.feed()                                     # the close is read on its own
        self.transcript("s1", [user("back again", at(TODAY, 11, 36)), asst("msg_2", at(TODAY, 12, 25))],
                        append=True, mtime=NOW - 30)
        self.assertEqual(self.feed()["running"][0]["started"][:19], "2026-11-23T11:36:00")

    def test_a_long_silence_starts_a_new_run_without_a_close(self):
        self.transcript("s1", [user("go", at("2026-11-20", 15, 18)),
                               asst("msg_1", at("2026-11-20", 16, 24)),
                               user("back again", at(TODAY, 5, 0)), asst("msg_2", at(TODAY, 5, 1)),
                               user("and on", at(TODAY, 11, 1)),           # six hours and no more: same run
                               asst("msg_3", at(TODAY, 12, 25))], mtime=NOW - 60)
        self.assertEqual(self.feed()["running"][0]["started"][:19], "2026-11-23T05:00:00")
        self.assertEqual(LV.RUN_GAP_SEC, 6 * 3600)

    def test_a_short_break_is_the_same_run(self):
        self.transcript("s1", [user("go", at(TODAY, 7, 0)), asst("msg_1", at(TODAY, 7, 1)),
                               user("after lunch", at(TODAY, 12, 20)),
                               asst("msg_2", at(TODAY, 12, 25))], mtime=NOW - 60)
        self.assertEqual(self.feed()["running"][0]["started"][:19], "2026-11-23T07:00:00")

    def test_a_close_with_nothing_after_it_keeps_the_run(self):
        path = self.transcript("s1", [user("go", at(TODAY, 12, 0)), asst("msg_1", at(TODAY, 12, 1))],
                               mtime=NOW - 60)
        self.raw(path, self.CLOSED)
        self.assertEqual(self.feed()["running"][0]["started"][:19], "2026-11-23T12:00:00")

    def test_window_is_small_until_seen_above_200k(self):
        self.transcript("s1", [user("go", at(TODAY, 12)),
                               asst("msg_1", at(TODAY, 12, 1), usage(0, 0, 50000, 0))], mtime=NOW - 5)
        r = self.feed()["running"][0]
        self.assertEqual((r["context_pct"], r["context_left_tokens"], r["level"]), (25, 150000, "ok"))
        self.assertEqual(LV.window_for("m", {"m": 200000}), 200000)
        self.assertEqual(LV.window_for("m", {"m": 200001}), 1000000)
        self.assertEqual(LV.window_for("m", {}, 300000), 1000000)

    def test_levels_follow_the_coach_thresholds(self):
        import signals
        T = SL.thresholds()
        self.assertEqual(T["ctx_over_tokens"], signals.T["peak_context"])
        self.assertEqual(T["ctx_warn_tokens"], signals.T["idle_context"])
        self.assertEqual(LV.context_level(signals.T["idle_context"] - 1, 10, T), "ok")
        self.assertEqual(LV.context_level(signals.T["idle_context"], 20, T), "warn")
        self.assertEqual(LV.context_level(signals.T["peak_context"], 50, T), "crit")
        self.assertEqual(LV.context_level(150000, 75, T), "warn")
        self.assertEqual(LV.context_level(180000, 90, T), "crit")

    def test_only_the_tail_is_read(self):
        recs = [user("go", at(TODAY, 9))]
        recs += [asst("msg_%d" % i, at(TODAY, 9, 1), usage(0, 0, i, 0),
                      tools=[("Bash", {"command": "x" * 3000})]) for i in range(400)]
        path = self.transcript("s1", recs, mtime=NOW - 5)
        self.assertGreater(os.path.getsize(path), 4 * LV.TAIL_BYTES)
        opened = []
        real_open = open

        class Spy(object):
            def __init__(self, fh):
                self.fh, self.n = fh, 0

            def __enter__(self):
                return self

            def __exit__(self, *a):
                opened.append(self.n)
                self.fh.close()

            def seek(self, *a):
                return self.fh.seek(*a)

            def read(self, n):
                self.n += n
                return self.fh.read(n)

        LV.open = lambda p, mode="r", *a, **k: Spy(real_open(p, mode, *a, **k))
        try:
            got = LV.tail_usage(path)
        finally:
            del LV.open
        self.assertEqual(got["context"], 399)
        self.assertEqual(opened, [LV.TAIL_BYTES])

    def test_sidechain_and_bookkeeping_lines_are_skipped_in_the_tail(self):
        path = self.transcript("s1", [
            user("go", at(TODAY, 9)),
            asst("msg_1", at(TODAY, 9, 1), usage(1, 2, 3, 4)),
            asst("msg_2", at(TODAY, 9, 2), usage(9, 9, 9, 9), isSidechain=True),
            {"type": "last-prompt", "lastPrompt": "x"}])
        self.assertEqual(LV.tail_usage(path)["context"], 10)

    def test_no_usage_means_no_row(self):
        self.transcript("s1", [user("go", at(TODAY, 12, 29))], mtime=NOW - 5)
        self.assertEqual(self.feed()["running"], [])


# ---------- limits ----------

def limits_payload(five=None, seven=None):
    """A status line payload carrying the documented rate_limits object."""
    rl = {}
    if five:
        rl["five_hour"] = {"used_percentage": five[0], "resets_at": five[1]}
    if seven:
        rl["seven_day"] = {"used_percentage": seven[0], "resets_at": seven[1]}
    return {"session_id": "sess-1", "model": {"id": "m", "display_name": "M"},
            "cost": {"total_cost_usd": 1.0}, "rate_limits": rl}


class Limits(Home):
    H5, D7 = 5 * 3600, 7 * 86400

    def test_no_data_means_no_rows(self):
        self.assertEqual(LV.limits(NOW), {"as_of": None, "rows": []})
        SL.snapshot({"session_id": "s"}, coach_dir=self.home, now=NOW)
        self.assertFalse(os.path.exists(SL.limits_path(self.home)))

    def test_the_status_line_keeps_what_claude_code_sends(self):
        SL.snapshot(limits_payload((23.5, NOW + 3600), (41.2, NOW + 86400)), coach_dir=self.home,
                    now=NOW - 30)
        with open(SL.limits_path(self.home)) as fh:
            kept = json.load(fh)
        self.assertEqual(kept["windows"]["five_hour"],
                         {"used": 23.5, "resets_at": int(NOW + 3600), "t": int(NOW - 30)})
        out = LV.limits(NOW)
        self.assertEqual([r["key"] for r in out["rows"]], ["five_hour", "weekly"])
        self.assertEqual([r["label"] for r in out["rows"]], ["5-hour", "Weekly"])
        self.assertEqual([r["used_pct"] for r in out["rows"]], [24, 41])
        self.assertEqual(out["as_of"], LV.local_iso(NOW - 30))
        self.assertEqual(out["rows"][0]["resets_at"], LV.local_iso(NOW + 3600))
        self.assertEqual(sorted(out["rows"][0]), ["key", "label", "level", "pace_pct", "resets_at",
                                                  "runs_out_at", "used_pct"])

    def test_json_mode_and_bad_values_keep_nothing(self):
        SL.snapshot(limits_payload((23.5, NOW + 3600)), coach_dir=self.home, now=NOW, save=False)
        self.assertFalse(os.path.exists(SL.limits_path(self.home)))
        self.assertEqual(SL.read_limits({"rate_limits": {"five_hour": {"used_percentage": "7"}}}), {})
        self.assertEqual(SL.read_limits({"rate_limits": {"five_hour": {"used_percentage": True,
                                                                       "resets_at": 5}}}), {})
        self.assertEqual(SL.read_limits({"rate_limits": "x"}), {})

    def test_windows_are_kept_one_by_one(self):
        SL.capture_limits(limits_payload((10, NOW + 3600), (40, NOW + 86400)), self.home, NOW - 600)
        SL.capture_limits(limits_payload((12, NOW + 3600)), self.home, NOW - 10)
        rows = dict((r["key"], r) for r in LV.limits(NOW)["rows"])
        self.assertEqual((rows["five_hour"]["used_pct"], rows["weekly"]["used_pct"]), (12, 40))

    def test_rewritten_only_when_changed_or_a_minute_old(self):
        p = limits_payload((10, NOW + 3600))
        self.assertTrue(SL.capture_limits(p, self.home, NOW))
        self.assertFalse(SL.capture_limits(p, self.home, NOW + 20))
        self.assertTrue(SL.capture_limits(p, self.home, NOW + 61))
        self.assertTrue(SL.capture_limits(limits_payload((11, NOW + 3600)), self.home, NOW + 62))

    def test_a_window_past_its_reset_is_dropped(self):
        SL.capture_limits(limits_payload((50, NOW - 1), (40, NOW + 86400)), self.home, NOW - 600)
        self.assertEqual([r["key"] for r in LV.limits(NOW)["rows"]], ["weekly"])

    def test_pace_in_reserve(self):
        # 46% of the window gone, 7% used: 39 in reserve
        seen = NOW
        row = LV.limit_row("five_hour", "5-hour", self.H5, 7, seen + 0.54 * self.H5, seen)
        self.assertEqual((row["pace_pct"], row["runs_out_at"], row["level"]), (-39, None, "ok"))

    def test_over_pace_gives_the_time_it_runs_out(self):
        # half the week gone, 80% used: 30 over, and at that rate the rest lasts 0.875 days
        seen = NOW
        row = LV.limit_row("weekly", "Weekly", self.D7, 80, seen + 3.5 * 86400, seen)
        self.assertEqual(row["pace_pct"], 30)
        self.assertEqual(row["runs_out_at"], LV.local_iso(seen + 20 * 3.5 * 86400 / 80.0))
        self.assertEqual(row["level"], "warn")

    def test_no_run_out_time_unless_over_pace(self):
        seen = NOW
        row = LV.limit_row("five_hour", "5-hour", self.H5, 90, seen + 0.1 * self.H5, seen)
        self.assertEqual((row["pace_pct"], row["runs_out_at"], row["level"]), (0, None, "crit"))
        row = LV.limit_row("five_hour", "5-hour", self.H5, 92, seen + 0.1 * self.H5, seen)
        self.assertEqual(row["pace_pct"], 2)
        # over pace always runs out before the reset: here 8% is left at 92% per 4.5 hours
        self.assertEqual(row["runs_out_at"], LV.local_iso(seen + 8 * 0.9 * self.H5 / 92.0))
        self.assertLess(row["runs_out_at"], row["resets_at"])

    def test_silent_in_the_first_three_percent(self):
        seen = NOW
        row = LV.limit_row("five_hour", "5-hour", self.H5, 9, seen + 0.98 * self.H5, seen)
        self.assertEqual((row["pace_pct"], row["runs_out_at"], row["used_pct"]), (None, None, 9))
        row = LV.limit_row("five_hour", "5-hour", self.H5, 9, seen + 0.97 * self.H5, seen)
        self.assertEqual(row["pace_pct"], 6)

    def test_used_up_has_no_run_out_time(self):
        row = LV.limit_row("five_hour", "5-hour", self.H5, 100, NOW + 0.5 * self.H5, NOW)
        self.assertEqual((row["pace_pct"], row["runs_out_at"], row["level"]), (50, None, "crit"))

    def test_a_run_out_time_that_has_passed_is_not_given(self):
        seen = NOW - 9000
        at_ = seen + 20 * 3.5 * 86400 / 80.0
        row = LV.limit_row("weekly", "Weekly", self.D7, 80, seen + 3.5 * 86400, seen, now=at_ - 1)
        self.assertEqual(row["runs_out_at"], LV.local_iso(at_))
        row = LV.limit_row("weekly", "Weekly", self.D7, 80, seen + 3.5 * 86400, seen, now=at_ + 1)
        self.assertEqual((row["pace_pct"], row["runs_out_at"]), (30, None))

    def test_an_idle_session_cannot_put_older_numbers_back(self):
        ends = NOW - 50
        SL.capture_limits(limits_payload((40, ends)), self.home, NOW - 300)
        self.assertFalse(SL.capture_limits(limits_payload((30, ends)), self.home, NOW - 200))
        self.assertFalse(SL.capture_limits(limits_payload((90, ends - 18000)), self.home, NOW - 100))
        self.assertEqual(LV.limits(NOW - 60)["rows"][0]["used_pct"], 40)
        # the next window starts lower, and is taken
        self.assertTrue(SL.capture_limits(limits_payload((2, ends + 18000)), self.home, NOW - 5))
        self.assertEqual(LV.limits(NOW)["rows"][0]["used_pct"], 2)

    def test_levels(self):
        self.assertEqual([LV.level_for(p) for p in (0, 69, 70, 89, 90, 100)],
                         ["ok", "ok", "warn", "warn", "crit", "crit"])

    def test_pace_is_worked_out_for_when_the_numbers_were_seen(self):
        SL.capture_limits(limits_payload((50, NOW + 0.25 * self.H5)), self.home, NOW - 0.25 * self.H5)
        row = LV.limits(NOW)["rows"][0]
        self.assertEqual(row["pace_pct"], 0)        # 50% used when 50% of the window had passed

    def test_the_feed_carries_them(self):
        SL.capture_limits(limits_payload((7, NOW + 9780)), self.home, NOW - 50)
        f = self.feed()
        self.assertEqual(f["limits"]["rows"][0]["key"], "five_hour")
        self.assertEqual(f["limits"]["as_of"], LV.local_iso(NOW - 50))


# ---------- tips ----------

class Tips(Home):
    def findings(self, name, rows, mtime):
        folder = os.path.join(self.home, "reports", "digests")
        if not os.path.isdir(folder):
            os.makedirs(folder)
        with open(os.path.join(folder, name + ".json"), "w") as fh:
            json.dump({"sessions": [], "totals": {}}, fh)
        path = os.path.join(folder, name + ".findings.json")
        with open(path, "w") as fh:
            json.dump(rows, fh)
        os.utime(path, (mtime, mtime))

    def row(self, pattern, severity, n=1):
        return {"pattern": pattern, "severity": severity, "evidence": "Evidence %s %d" % (pattern, n),
                "session": "abcd1234", "suggestion": "Do %s" % pattern}

    def test_none(self):
        self.assertEqual(self.feed()["tips"], [])

    def test_latest_file_most_severe_first_up_to_three(self):
        self.findings("daily-2026-11-21", [self.row("old", "high")], NOW - 90000)
        self.findings("daily-2026-11-22", [self.row("a", "low"), self.row("b", "high"),
                                           self.row("c", "medium"), self.row("d", "high")], NOW - 3600)
        tips = self.feed()["tips"]
        self.assertEqual([t["pattern"] for t in tips], ["b", "d", "c"])
        self.assertEqual(tips[0], {"title": "Evidence b 1", "advice": "Do b", "pattern": "b"})

    def test_a_different_pattern_each_while_there_are_enough(self):
        rows = [self.row("a", "high", 1), self.row("a", "high", 2), self.row("a", "high", 3),
                self.row("b", "medium")]
        self.assertEqual([t["title"] for t in LV.pick_tips(rows)],
                         ["Evidence a 1", "Evidence a 2", "Evidence b 1"])
        self.assertEqual(len(LV.pick_tips([self.row("a", "high", i) for i in range(5)])), 3)

    def test_excluded_work_is_not_tipped(self):
        self.config(exclude=[{"sessions": ["abcd1234"]}])
        self.findings("daily-2026-11-22", [self.row("a", "high")], NOW - 3600)
        self.assertEqual(self.feed()["tips"], [])


# ---------- writing the file ----------

class Write(Home):
    def test_write_is_whole_and_leaves_no_temp_file(self):
        self.transcript("s1", [user("go", at(TODAY, 9)), asst("msg_1", at(TODAY, 9, 1))])
        path = LV.write(force=True, now=NOW, projects_dir=self.projects)
        self.assertEqual(path, os.path.join(self.home, "live.json"))
        with open(path) as fh:
            self.assertEqual(json.load(fh)["version"], 1)
        self.assertEqual([n for n in os.listdir(self.home) if n.endswith(".tmp")], [])

    def test_at_most_once_a_minute(self):
        path = LV.write(force=True, now=NOW, projects_dir=self.projects)
        os.utime(path, (NOW, NOW))
        self.assertIsNone(LV.write(now=NOW + 20, projects_dir=self.projects))
        self.assertEqual(LV.write(now=NOW + 60, projects_dir=self.projects), path)
        os.utime(path, (NOW + 60, NOW + 60))
        self.assertEqual(LV.write(force=True, now=NOW + 61, projects_dir=self.projects), path)

    def test_page_url_comes_from_config(self):
        self.config(page_url="https://claude.ai/artifact/abc-123")
        self.assertEqual(self.feed()["page_url"], "https://claude.ai/artifact/abc-123")

    def test_unknown_words_do_nothing(self):
        out = io.StringIO()
        old, sys.stdout = sys.stdout, out
        try:
            self.assertEqual(LV.main(["write", "--everything"]), 2)
            self.assertEqual(LV.main(["bogus"]), 2)
        finally:
            sys.stdout = old
        self.assertFalse(os.path.exists(LV.live_file()))


# ---------- the page command (rebuilds the coach page with a fresh feed) ----------

class Page(Home):
    def setUp(self):
        Home.setUp(self)
        # The page build scans transcripts for its trends: keep it on the temp folder.
        self._projects_dir = C.PROJECTS_DIR
        C.PROJECTS_DIR = self.projects
        self.site = os.path.join(self.home, "site", "index.html")

    def tearDown(self):
        C.PROJECTS_DIR = self._projects_dir
        Home.tearDown(self)

    def run_main(self, *args):
        out = io.StringIO()
        err = io.StringIO()
        old = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = C.run_main(lambda: LV.main(list(args)))
        finally:
            sys.stdout, sys.stderr = old
        return code, out.getvalue(), err.getvalue()

    def test_builds_the_page_in_the_coach_home(self):
        path = LV.page(projects_dir=self.projects, today=TODAY)
        self.assertEqual(path, self.site)
        self.assertEqual(path, C.site_file())
        with open(path, encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("<title>", html)
        self.assertIn('id="tab-overview"', html)
        self.assertIn('id="tab-today"', html)

    def stub_build(self, html="<title>stub</title>"):
        """Stand in for build_site.build_page; returns the list its calls are noted in."""
        import build_site as B
        calls = []
        real = B.build_page

        def fake(*args, **kwargs):
            calls.append((args, kwargs))
            return html, {}
        B.build_page = fake
        self.addCleanup(setattr, B, "build_page", real)
        return calls

    def test_is_the_page_the_site_builder_makes(self):
        import build_site as B
        want, _card = B.build_page(C.reports_dir(), self.projects, TODAY, True)
        with open(LV.page(projects_dir=self.projects, today=TODAY), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), want)

    def test_is_the_full_build_with_the_trend_scan(self):
        calls = self.stub_build("<title>from the site builder</title>")
        path = LV.page(projects_dir=self.projects, today=TODAY)
        self.assertEqual(calls, [((C.reports_dir(), self.projects, TODAY, True), {})])   # scan is on
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "<title>from the site builder</title>")

    def test_a_write_that_fails_leaves_the_old_page_and_no_temp_file(self):
        self.stub_build("new page")
        os.makedirs(os.path.dirname(self.site))
        with open(self.site, "w") as fh:
            fh.write("old page")
        seen = []
        real = os.replace

        def refuse(src, dst):
            # The new page is whole in a file beside the old one before the old one is touched.
            with open(src) as fh:
                seen.append((fh.read(), os.path.dirname(src) == os.path.dirname(dst), dst))
            with open(dst) as fh:
                seen.append(fh.read())
            raise OSError("disk full")
        os.replace = refuse
        try:
            self.assertRaises(OSError, LV.page, self.projects, TODAY)
        finally:
            os.replace = real
        self.assertEqual(seen, [("new page", True, self.site), "old page"])
        with open(self.site) as fh:
            self.assertEqual(fh.read(), "old page")
        self.assertEqual(os.listdir(os.path.dirname(self.site)), ["index.html"])

    def test_replaces_the_old_page_whole_and_leaves_no_temp_file(self):
        os.makedirs(os.path.dirname(self.site))
        with open(self.site, "w") as fh:
            fh.write("old page")
        LV.page(projects_dir=self.projects, today=TODAY)
        with open(self.site, encoding="utf-8") as fh:
            self.assertNotIn("old page", fh.read())
        self.assertEqual(os.listdir(os.path.dirname(self.site)), ["index.html"])

    def test_command_prints_the_path(self):
        code, out, _err = self.run_main("page")
        self.assertEqual(code, 0)
        self.assertEqual(out, self.site + "\n")
        self.assertTrue(os.path.isfile(self.site))

    def test_command_takes_no_other_words(self):
        code, _out, _err = self.run_main("page", "--no-trends")
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(self.site))

    def test_settings_that_cannot_be_read_build_nothing_and_keep_the_old_page(self):
        os.makedirs(os.path.dirname(self.site))
        with open(self.site, "w") as fh:
            fh.write("old page")
        with open(os.path.join(self.home, "config.json"), "w") as fh:
            fh.write("{not json")
        calls = self.stub_build()
        self.assertRaises(C.ConfigError, LV.page, self.projects, TODAY)
        self.assertEqual(calls, [])              # refused before the builder is reached
        code, out, err = self.run_main("page")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("coach: "))
        with open(self.site) as fh:
            self.assertEqual(fh.read(), "old page")


# ---------- the once-a-minute runner ----------

class LiveAgent(Home):
    def test_plist(self):
        raw = S.live_plist_bytes(python="/usr/bin/python3")
        p = plistlib.loads(raw)
        self.assertEqual(p["Label"], "com.agentic-coach.live")
        self.assertEqual(p["ProgramArguments"],
                         ["/usr/bin/python3", os.path.join(self.home, "coach_live.py"), "write"])
        self.assertEqual(p["StartInterval"], 60)
        self.assertTrue(p["RunAtLoad"])
        self.assertEqual(p["EnvironmentVariables"], {"COACH_HOME": self.home})
        self.assertNotIn("KeepAlive", p)
        self.assertTrue(os.path.isabs(S.live_plist()["ProgramArguments"][0]))

    def test_plist_lives_in_the_per_user_folder(self):
        os.environ.pop("COACH_LAUNCH_AGENTS_DIR")
        self.assertEqual(S.live_plist_path(), os.path.join(
            os.path.expanduser("~"), "Library", "LaunchAgents", "com.agentic-coach.live.plist"))

    @unittest.skipUnless(sys.platform == "darwin", "the runner is a macOS LaunchAgent")
    def test_install_status_uninstall_with_a_stand_in_launchctl(self):
        calls = []
        loaded = []

        def run(args):
            calls.append(list(args))
            if args[0] == "bootstrap":
                loaded.append(1)
            if args[0] == "bootout":
                del loaded[:]
            if args[0] == "print":
                return (0 if loaded else 113), ""
            return 0, ""

        before = S.live_agent_status(run=run)
        self.assertEqual((before["installed"], before["loaded"], before["current"]), (False, False, False))
        res = S.live_agent_install(run=run)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["plist"].startswith(self.tmp))
        domain = "gui/%d" % os.getuid()
        self.assertEqual(calls[-2:], [["bootout", domain + "/com.agentic-coach.live"],
                                      ["bootstrap", domain, res["plist"]]])
        with open(res["plist"], "rb") as fh:
            self.assertEqual(plistlib.load(fh), S.live_plist())
        with open(res["launcher"]) as fh:
            compile(fh.read(), "launcher", "exec")
        with open(os.path.join(self.home, "live.path")) as fh:
            self.assertEqual(fh.read().strip(), ROOT)
        after = S.live_agent_status(run=run)
        self.assertEqual((after["installed"], after["loaded"], after["current"]), (True, True, True))
        gone = S.live_agent_uninstall(run=run)
        self.assertTrue(gone["ok"])
        self.assertEqual(len(gone["removed"]), 3)
        self.assertFalse(os.path.exists(res["plist"]))
        self.assertFalse(os.path.exists(res["launcher"]))
        self.assertFalse(S.live_agent_status(run=run)["installed"])

    @unittest.skipUnless(sys.platform == "darwin", "the runner is a macOS LaunchAgent")
    def test_a_failed_load_is_reported(self):
        tries = []

        def run(args):
            tries.append(args[0])
            return (5, "Input/output error") if args[0] == "bootstrap" else (0, "")

        res = S.live_agent_install(run=run, wait=0)
        self.assertFalse(res["ok"])
        self.assertIn("launchctl", res["error"])
        self.assertEqual(tries.count("bootstrap"), 4)       # launchd may still be unloading: it retries

    @unittest.skipUnless(sys.platform == "darwin", "the runner is a macOS LaunchAgent")
    def test_status_from_another_shell_does_not_ask_for_a_reinstall(self):
        ok = lambda args: (0, "")       # noqa: E731
        S.live_agent_install(run=ok, python="/some/other/python3")
        os.environ["CLAUDE_CONFIG_DIR_X"] = "1"
        self.assertTrue(S.live_agent_status(run=ok)["current"])
        del os.environ["CLAUDE_CONFIG_DIR_X"]
        with open(S.live_plist_path(), "rb") as fh:
            p = plistlib.load(fh)
        p["StartInterval"] = 5
        with open(S.live_plist_path(), "wb") as fh:
            plistlib.dump(p, fh)
        self.assertFalse(S.live_agent_status(run=ok)["current"])

    def test_the_launcher_runs_live_write(self):
        import subprocess
        launcher = os.path.join(self.home, "coach_live.py")
        with open(launcher, "w") as fh:
            fh.write(S.LIVE_LAUNCHER)
        with open(os.path.join(self.home, "live.path"), "w") as fh:
            fh.write(ROOT + "\n")
        env = dict(os.environ, CLAUDE_CONFIG_DIR=os.path.join(self.tmp, "claude"))
        p = subprocess.run([sys.executable, launcher, "write"], env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(os.path.join(self.home, "live.json")) as fh:
            self.assertEqual(json.load(fh)["version"], 1)

    def test_unknown_words_never_install(self):
        out = io.StringIO()
        old, sys.stdout = sys.stdout, out
        try:
            self.assertEqual(S.main(["live-agent"]), 2)
            self.assertEqual(S.main(["live-agent", "instal"]), 2)
            self.assertEqual(S.main(["live-agent", "install", "now"]), 2)
            self.assertEqual(S.main(["--help"]), 2)
        finally:
            sys.stdout = old
        self.assertFalse(os.path.exists(S.live_plist_path()))

    def test_setup_check_reports_it(self):
        out = S.check(os.path.join(self.tmp, "claude"))
        self.assertEqual(out["live_agent"]["label"], "com.agentic-coach.live")
        self.assertFalse(out["live_agent"]["installed"])


class Wording(unittest.TestCase):
    def test_no_long_dashes(self):
        for rel in ("coach/live.py", "coach/activity.py", "coach/statusline.py", "coach/setup_check.py",
                    "skills/coach/references/setup.md", "skills/coach/references/statusline.md",
                    "tests/test_live.py"):
            with io.open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn(u"\u2014", text, rel)
            self.assertNotIn(u"\u2013", text, rel)


if __name__ == "__main__":
    unittest.main()
