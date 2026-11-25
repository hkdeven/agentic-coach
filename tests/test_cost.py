"""Stdlib tests for prices, project names, the ledger and the cost lines. Synthetic data only.

Run: python3 -m unittest discover -s tests -v
"""
import calendar
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "coach"))

import build_site  # noqa: E402
import common  # noqa: E402
import extract  # noqa: E402
import ledger  # noqa: E402
import pricing  # noqa: E402
import projects  # noqa: E402
import recap  # noqa: E402

M = 1000000


def local_noon(day):
    """Epoch seconds of 12:00 local time on a YYYY-MM-DD day."""
    y, m, d = int(day[0:4]), int(day[5:7]), int(day[8:10])
    return time.mktime((y, m, d, 12, 0, 0, 0, 0, -1))


class Transcript(object):
    """Writes just enough of a session transcript for token counting."""

    def __init__(self, sid, cwd, day, title=None):
        self.sid, self.cwd, self.lines, self.n = sid, cwd, [], 0
        self.t = local_noon(day)
        self.title = title

    def _ts(self):
        self.t += 30
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(self.t)) + ".000Z"

    def at(self, day):
        self.t = local_noon(day)
        return self

    def prompt(self, text="do the thing"):
        rec = {"type": "user", "origin": {"kind": "human"}, "sessionId": self.sid,
               "isSidechain": False, "timestamp": self._ts(),
               "message": {"role": "user", "content": text}}
        if self.cwd:
            rec["cwd"] = self.cwd
        self.lines.append(json.dumps(rec, separators=(",", ":")))
        if self.title:            # title records carry no timestamp and follow a timed record
            self.lines.append(json.dumps({"type": "custom-title", "customTitle": self.title,
                                          "sessionId": self.sid}, separators=(",", ":")))
            self.title = None
        return self

    def reply(self, model, inp=0, out=0, read=0, w5=0, w1=0, speed=None, sidechain=False):
        self.n += 1
        usage = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
                 "cache_creation_input_tokens": w5 + w1,
                 "cache_creation": {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1}}
        if speed:
            usage["speed"] = speed
        rec = {"type": "assistant", "sessionId": self.sid, "isSidechain": sidechain,
               "uuid": "%s-%d" % (self.sid, self.n), "timestamp": self._ts(),
               "message": {"id": "msg_%s_%d" % (self.sid, self.n), "model": model,
                           "role": "assistant", "usage": usage,
                           "content": [{"type": "text", "text": "ok"}]}}
        if self.cwd:
            rec["cwd"] = self.cwd
        # Claude Code repeats id and usage on every content block of one message
        self.lines.append(json.dumps(rec, separators=(",", ":")))
        self.lines.append(json.dumps(rec, separators=(",", ":")))
        return self

    def write(self, projects_dir, project="-demo", subagent_of=None):
        folder = os.path.join(projects_dir, project)
        if subagent_of:
            folder = os.path.join(folder, subagent_of, "subagents")
        if not os.path.isdir(folder):
            os.makedirs(folder)
        path = os.path.join(folder, self.sid + ".jsonl")
        with open(path, "w") as fh:
            fh.write("\n".join(self.lines) + "\n")
        return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="coach-cost-")
        self.projects = os.path.join(self.tmp, "projects")
        os.makedirs(self.projects)
        self.old_env = dict(os.environ)
        os.environ["COACH_HOME"] = os.path.join(self.tmp, "coach-home")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        projects._cache.clear()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        shutil.rmtree(self.tmp, ignore_errors=True)


class PricingTest(unittest.TestCase):
    def test_table_has_a_source_and_a_date(self):
        p = pricing.load_prices()
        self.assertTrue(p["source"].startswith("https://"))
        self.assertRegex(p["retrieved"], r"^\d{4}-\d{2}-\d{2}$")
        for model, row in p["models"].items():
            for t in pricing.TYPES:
                self.assertIsInstance(row[t], (int, float), "%s %s" % (model, t))

    def test_mixed_models_and_cache_types(self):
        by_model = {"claude-opus-5-5": [M, M, M, M, M],          # 4 + 20 + 0.20 + 5 + 8
                    "claude-haiku-4-5": [2 * M, 0, 0, 0, 0]}      # 2 x 1
        c = pricing.cost(by_model)
        self.assertAlmostEqual(c["usd"], 37.20 + 2.0, places=4)
        self.assertAlmostEqual(c["by_type"]["input"], 4 + 2, places=4)
        self.assertAlmostEqual(c["by_type"]["output"], 20, places=4)
        self.assertAlmostEqual(c["by_type"]["cache_read"], 0.20, places=4)
        self.assertAlmostEqual(c["by_type"]["cache_write"], 13, places=4)
        self.assertEqual(c["tokens"], [3 * M, M, M, M, M])
        self.assertEqual(c["unpriced_tokens"], 0)

    def test_unpriced_model_is_counted_and_never_guessed(self):
        c = pricing.cost({"some-new-model-9": [100, 200, 300, 0, 0], "claude-opus-5": [M, 0, 0, 0, 0]})
        self.assertAlmostEqual(c["usd"], 5.0, places=4)
        self.assertEqual(c["unpriced_tokens"], 600)
        self.assertEqual(c["unpriced_models"], ["some-new-model-9"])
        self.assertIsNone(pricing.rates("claude-opus-9"))
        self.assertIsNone(pricing.rates("claude-opus-5-5-latest"))

    def test_family_prefix_only_for_dated_snapshots(self):
        self.assertEqual(pricing.rates("claude-haiku-4-5-20251001"), pricing.rates("claude-haiku-4-5"))
        self.assertEqual(pricing.rates("claude-opus-4-5@20251101"), pricing.rates("claude-opus-4-5"))
        self.assertEqual(pricing.rates("claude-opus-4-20250514")[0], 15.0)     # not opus 4.5
        self.assertIsNone(pricing.rates("claude-opus-4-99"))

    def test_long_prompt_and_fast_mode_keys(self):
        short = {"input_tokens": 10, "cache_read_input_tokens": 50000}
        long_ = {"input_tokens": 10, "cache_read_input_tokens": 150000}
        self.assertEqual(pricing.model_key("claude-haiku-5-5", short), "claude-haiku-5-5")
        self.assertEqual(pricing.model_key("claude-haiku-5-5", long_), "claude-haiku-5-5+long")
        self.assertEqual(pricing.model_key("claude-opus-5-5", long_), "claude-opus-5-5")
        self.assertEqual(pricing.rates("claude-haiku-5-5+long")[0], 0.5)
        self.assertEqual(pricing.model_key("claude-opus-5-5", {"speed": "fast"}), "claude-opus-5-5+fast")
        self.assertIsNone(pricing.rates("claude-opus-5-5+fast"))                # counted, not priced

    def test_usage_split_and_money_format(self):
        u = {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 3,
             "cache_creation_input_tokens": 10, "cache_creation": {"ephemeral_1h_input_tokens": 4}}
        self.assertEqual(pricing.usage_counts(u), [1, 2, 3, 6, 4])
        u.pop("cache_creation")
        self.assertEqual(pricing.usage_counts(u), [1, 2, 3, 10, 0])            # no split: 5 minute rate
        self.assertEqual([pricing.fmt_usd(x) for x in (0, 0.004, 3.2, 9.999, 10, 45.4, 1234.5)],
                         ["$0.00", "$0.00", "$3.20", "$10", "$10", "$45", "$1,234"])
        self.assertEqual(pricing.fmt_usd(-3), "$0.00")
        bad = {"input_tokens": "7", "output_tokens": -5, "cache_creation_input_tokens": 5,
               "cache_creation": {"ephemeral_1h_input_tokens": -9}}
        self.assertEqual(pricing.usage_counts(bad), [0, 0, 0, 5, 0])
        self.assertEqual([pricing.fmt_tok(x) for x in (999, 1500, 3 * M, 1250 * M)],
                         ["999", "2k", "3M", "1.2B"])


class ProjectTest(Base):
    def test_each_rule_in_order(self):
        repo = os.path.join(self.tmp, "code", "widget-shop")
        os.makedirs(os.path.join(repo, ".git"))
        os.makedirs(os.path.join(repo, "src", "deep"))
        plain = os.path.join(self.tmp, "notes", "garden-plan")
        os.makedirs(plain)
        rules = [{"name": "Shop", "match": ["widget-shop"]}, {"name": "Second", "match": ["widget*"]},
                 {"name": "By title", "match": ["quarterly review"]}]
        # a: a rule wins over everything, first match first, on the folder or the title
        self.assertEqual(projects.resolve(repo, "x", rules), "Shop")
        self.assertEqual(projects.resolve(plain, "The Quarterly Review", rules), "By title")
        self.assertEqual(projects.resolve("", "quarterly review notes", rules), "By title")
        # b: inside a checkout, the repository's folder name
        self.assertEqual(projects.resolve(os.path.join(repo, "src", "deep")), "widget-shop")
        # c: otherwise the folder's own name
        self.assertEqual(projects.resolve(plain), "garden-plan")
        # d: no folder: the title, else "No folder"
        self.assertEqual(projects.resolve("", "Sketching ideas"), "Sketching ideas")
        self.assertEqual(projects.resolve("", ""), "No folder")
        self.assertEqual(projects.resolve("/x/scratch-workspaces/abc", ""), "No folder")
        self.assertEqual(projects.resolve(os.path.expanduser("~")), "Home folder")

    def test_glob_rules(self):
        rules = [{"name": "Clients", "match": ["*/clients/*"]}, {"bad": 1}, {"name": "", "match": ["x"]}]
        self.assertEqual(projects.resolve("/work/clients/acme-site", "", rules), "Clients")
        self.assertEqual(projects.resolve("/work/other/acme-site", "", rules), "acme-site")

    def test_worktree_that_still_exists_resolves_through_git(self):
        main = os.path.join(self.tmp, "code", "widget-shop")
        wt = os.path.join(self.tmp, "trees", "feature-x")
        gitdir = os.path.join(main, ".git", "worktrees", "feature-x")
        os.makedirs(gitdir)
        os.makedirs(os.path.join(wt, "sub"))
        with open(os.path.join(wt, ".git"), "w") as fh:
            fh.write("gitdir: %s\n" % gitdir)
        with open(os.path.join(gitdir, "commondir"), "w") as fh:
            fh.write("../..\n")
        self.assertEqual(projects.resolve(os.path.join(wt, "sub")), "widget-shop")
        os.remove(os.path.join(gitdir, "commondir"))          # still found from the path shape
        projects._cache.clear()
        self.assertEqual(projects.resolve(wt), "widget-shop")

    def test_worktree_shapes_when_the_folder_is_gone(self):
        self.assertEqual(projects.resolve("/gone/widget-shop/.claude/worktrees/brave-otter"), "widget-shop")
        self.assertEqual(projects.resolve("/gone/widget-shop/.claude/worktrees/brave-otter/src"),
                         "widget-shop")
        self.assertEqual(projects.resolve("/gone/widget-shop/.worktrees/fix-login"), "widget-shop")
        self.assertEqual(projects.resolve("/gone/plain-folder"), "plain-folder")


def two_days(projects_dir):
    """Day 1: two projects, two models, a subagent, a scheduled run. Day 2: one session."""
    a = Transcript("aaaaaaaa-0000-0000-0000-000000000001", "/work/alpha", "2026-11-10")
    a.prompt().reply("claude-opus-5-5", inp=1000, out=M, read=10 * M, w5=M, w1=M)
    a.reply("claude-haiku-4-5", inp=M, out=100000)
    a.write(projects_dir)
    sub = Transcript("agent-1", "/work/alpha", "2026-11-10")
    sub.reply("claude-sonnet-5-5", inp=5000, out=2 * M, read=M, sidechain=True)
    sub.write(projects_dir, subagent_of=a.sid)
    b = Transcript("bbbbbbbb-0000-0000-0000-000000000002", "/work/beta", "2026-11-10")
    b.prompt("<scheduled-task name=\"nightly\">run</scheduled-task>")
    b.reply("claude-opus-5-5", out=200000).reply("mystery-model-1", inp=500, out=500)
    b.write(projects_dir)
    c = Transcript("cccccccc-0000-0000-0000-000000000003", "", "2026-11-11", title="Loose thoughts")
    c.prompt().reply("claude-opus-5-5", out=500000)
    c.write(projects_dir)


class DigestCostTest(Base):
    def digest(self, first="2026-11-10", last_excl="2026-11-11", **kw):
        d, _blob = extract.run(common.parse_when(first), common.parse_when(last_excl),
                               projects_dir=self.projects, inventory=False, **kw)
        return d

    def test_costs_sit_beside_every_token_block_and_add_up(self):
        two_days(self.projects)
        d = self.digest()
        alpha = [s for s in d["sessions"] if s["id"].startswith("aaaa")][0]
        # opus 5.5: 0.004 + 20 + 2 + 5 + 8 = 35.004; haiku 4.5: 1 + 0.5 = 1.5
        self.assertAlmostEqual(alpha["cost"]["usd"], 36.50, places=2)
        self.assertEqual(alpha["cost"]["text"], "$37")
        self.assertEqual(alpha["tokens"]["cache_write"], 2 * M)           # old fields unchanged
        self.assertEqual(alpha["models_tokens"]["claude-opus-5-5"], [1000, M, 10 * M, M, M])
        # subagent, sonnet 5.5: 0.01 + 20 + 0.10 = 20.11
        self.assertAlmostEqual(alpha["subagents"]["cost"]["usd"], 20.11, places=2)
        self.assertEqual(alpha["project_name"], "alpha")
        t = d["totals"]["cost"]
        beta_usd = 200000 * 20 / 1e6
        self.assertAlmostEqual(t["usd"], 36.50 + 20.11 + beta_usd, places=1)
        self.assertAlmostEqual(t["main_usd"] + t["subagent_usd"], t["usd"], places=1)
        self.assertAlmostEqual(sum(t["by_type_usd"].values()), t["usd"], places=1)
        self.assertEqual(t["unpriced_tokens"], 1000)
        self.assertIn(False, [m["priced"] for m in t["by_model"]])
        # projects: sorted by cost, sum to the total, main and subagents apart, scheduled marked
        rows = d["projects"]
        self.assertEqual([r["name"] for r in rows], ["alpha", "beta"])
        self.assertAlmostEqual(sum(r["usd"] for r in rows), t["usd"], places=1)
        self.assertAlmostEqual(rows[0]["subagent_usd"], 20.11, places=2)
        self.assertEqual(rows[1]["scheduled_sessions"], 1)
        self.assertEqual(rows[1]["unpriced_tokens"], 1000)
        per_session = sum(s["cost"]["usd"] + s.get("subagents", {}).get("cost", {}).get("usd", 0)
                          for s in d["sessions"])
        self.assertAlmostEqual(per_session, t["usd"], places=1)
        # model tokens add up to the token totals that were always there
        mt = [sum(v[i] for v in d["totals"]["models_tokens"].values()) for i in range(5)]
        tk, sk = d["totals"]["tokens"], d["totals"]["subagent_tokens"]
        self.assertEqual(mt[1], tk["out"] + sk["out"])
        self.assertEqual(mt[3] + mt[4], tk["cache_write"] + sk["cache_write"])

    def test_no_folder_session_goes_under_its_title_and_rules_come_from_config(self):
        two_days(self.projects)
        d = self.digest("2026-11-11", "2026-11-12")
        self.assertEqual([r["name"] for r in d["projects"]], ["Loose thoughts"])
        os.makedirs(os.environ["COACH_HOME"])
        with open(common.config_file(), "w") as fh:
            json.dump({"projects": [{"name": "Shop work", "match": ["/work/al"]},
                                    {"name": 5, "match": ["x"]}, "junk"]}, fh)
        self.assertEqual(common.load_config()["projects"],
                         [{"name": "Shop work", "match": ["/work/al"]}])
        d = self.digest()
        self.assertEqual([r["name"] for r in d["projects"]], ["Shop work", "beta"])


class LedgerTest(Base):
    def test_backfill_is_idempotent_and_matches_the_digest(self):
        two_days(self.projects)
        first = ledger.backfill(self.projects)
        self.assertEqual((first["days"], first["first_day"]), (2, "2026-11-10"))
        self.assertEqual(len(first["added"]), 2)
        with open(ledger.ledger_file()) as fh:
            before = json.load(fh)
        again = ledger.backfill(self.projects)
        self.assertEqual((again["added"], again["replaced"], again["kept"]), ([], [], []))
        with open(ledger.ledger_file()) as fh:
            after = json.load(fh)
        before.pop("updated"), after.pop("updated")
        self.assertEqual(before, after)
        led, _ = ledger.load()
        day = ledger.totals(led, "2026-11-10", "2026-11-10")
        d, _blob = extract.run(common.parse_when("2026-11-10"), common.parse_when("2026-11-11"),
                               projects_dir=self.projects, inventory=False)
        self.assertAlmostEqual(day["usd"], d["totals"]["cost"]["usd"], places=1)
        self.assertEqual([r["name"] for r in day["by_project"]], ["alpha", "beta"])
        self.assertEqual(day["by_project"][1]["scheduled"], 1)
        self.assertEqual(day["unpriced_tokens"], 1000)

    def test_a_thinner_scan_never_replaces_a_day(self):
        two_days(self.projects)
        ledger.backfill(self.projects)
        led, _ = ledger.load()
        full = ledger.totals(led, "2026-11-10", "2026-11-10")["usd"]
        shutil.rmtree(os.path.join(self.projects, "-demo"))       # transcripts cleaned up
        b = Transcript("bbbbbbbb-0000-0000-0000-000000000002", "/work/beta", "2026-11-10")
        b.prompt().reply("claude-opus-5-5", out=1000).write(self.projects)
        r = ledger.backfill(self.projects)
        self.assertEqual(r["kept"], ["2026-11-10"])
        led, _ = ledger.load()
        self.assertEqual(ledger.totals(led, "2026-11-10", "2026-11-10")["usd"], full)
        self.assertIn("2026-11-11", led["days"])                  # days with no transcripts stay
        # a day recorded after it was over is closed. A transcript that later shows up with
        # that old day in it is a resumed copy of deleted work, not new work: not counted again.
        g = Transcript("gggggggg-0000-0000-0000-000000000007", "/work/alpha", "2026-11-10")
        g.prompt().reply("claude-opus-5", out=40 * M).write(self.projects)
        r = ledger.backfill(self.projects)
        self.assertEqual((r["replaced"], r["kept"]), ([], ["2026-11-10"]))
        led, _ = ledger.load()
        self.assertEqual(ledger.totals(led, "2026-11-10", "2026-11-10")["usd"], full)

    def test_a_day_still_running_grows_and_then_closes(self):
        today = time.strftime("%Y-%m-%d")
        a = Transcript("aaaaaaaa-0000-0000-0000-000000000001", "/work/alpha", today)
        a.prompt().reply("claude-opus-5", out=M).write(self.projects)             # $25 so far
        scanned = ledger.scan(self.projects)
        self.assertFalse(scanned[today]["closed"])
        led = ledger._clean(None)
        ledger.upsert(led, scanned)
        a.reply("claude-opus-5", out=M).write(self.projects)                      # the session goes on
        Transcript("bbbbbbbb-0000-0000-0000-000000000002", "/work/beta", today) \
            .prompt().reply("claude-opus-5", out=M).write(self.projects)          # and another starts
        r = ledger.upsert(led, ledger.scan(self.projects))
        self.assertEqual(r["replaced"], [today])
        self.assertAlmostEqual(ledger.totals(led, today, today)["usd"], 75.0)
        os.remove(os.path.join(self.projects, "-demo", a.sid + ".jsonl"))         # gone from disk
        r = ledger.upsert(led, ledger.scan(self.projects))
        self.assertEqual(r["kept"], [today])
        self.assertAlmostEqual(ledger.totals(led, today, today)["usd"], 75.0)     # still counted
        later = ledger.scan(self.projects)
        later[today]["closed"] = True                                             # as a scan tomorrow would say
        ledger.upsert(led, later)
        self.assertTrue(led["days"][today]["closed"])
        later[today]["sessions"]["zzzz"] = {"project": "x", "scheduled": False,
                                            "models": {"claude-opus-5": [[0, M, 0, 0, 0], [0] * 5]}}
        ledger.upsert(led, later)
        self.assertAlmostEqual(ledger.totals(led, today, today)["usd"], 75.0)     # closed: frozen
        ledger.save(led)
        again, state = ledger.load()
        self.assertEqual((state, again["days"]), ("ok", led["days"]))             # round trip

    def test_rebuild_renames_only_days_whose_transcripts_are_complete(self):
        two_days(self.projects)
        ledger.backfill(self.projects)
        rules = [{"name": "Renamed", "match": ["/work/alpha"]}]
        r = ledger.backfill(self.projects, rules=rules)            # without --rebuild: nothing moves
        self.assertEqual(r["replaced"], [])
        r = ledger.backfill(self.projects, rules=rules, rebuild=True)
        self.assertIn("2026-11-10", r["replaced"])
        led, _ = ledger.load()
        names = [p["name"] for p in ledger.totals(led, "2026-11-10", "2026-11-10")["by_project"]]
        self.assertEqual(names, ["Renamed", "beta"])

    def test_week_boundaries(self):
        self.assertEqual(ledger.monday("2026-11-15"), "2026-11-09")     # a Sunday
        self.assertEqual(ledger.monday("2026-11-16"), "2026-11-16")     # a Monday
        self.assertEqual(ledger.shift("2026-03-01", -1), "2026-02-28")
        for i, day in enumerate(["2026-11-08", "2026-11-09", "2026-11-15", "2026-11-16"]):
            Transcript("dddddddd-0000-0000-0000-00000000000%d" % i, "/work/alpha", day) \
                .prompt().reply("claude-opus-5", out=M).write(self.projects)   # $25 a day
        ledger.backfill(self.projects)
        led, _ = ledger.load()
        per = ledger.periods(led, "2026-11-15")
        self.assertAlmostEqual(per["day"]["usd"], 25.0)
        self.assertAlmostEqual(per["week7"]["usd"], 50.0)       # 9th to 15th
        self.assertAlmostEqual(per["calweek"]["usd"], 50.0)     # Monday 9th to Sunday 15th
        self.assertAlmostEqual(per["all"]["usd"], 100.0)
        self.assertEqual(per["since"], "2026-11-08")
        per = ledger.periods(led, "2026-11-16")
        self.assertAlmostEqual(per["calweek"]["usd"], 25.0)     # a new week starts on Monday
        self.assertAlmostEqual(per["week7"]["usd"], 50.0)       # 10th to 16th

    def test_all_time_is_the_sum_of_days_and_of_projects(self):
        two_days(self.projects)
        ledger.backfill(self.projects)
        led, _ = ledger.load()
        per = ledger.periods(led, "2026-11-11")
        by_day = sum(ledger.totals(led, d, d)["usd"] for d in led["days"])
        self.assertAlmostEqual(per["all"]["usd"], by_day, places=6)
        self.assertAlmostEqual(sum(p["usd"] for p in per["all"]["by_project"]), per["all"]["usd"], places=6)
        self.assertAlmostEqual(sum(m["usd"] for m in per["all"]["by_model"]), per["all"]["usd"], places=6)
        self.assertAlmostEqual(sum(t["usd"] for t in per["all"]["by_type"]), per["all"]["usd"], places=6)
        self.assertAlmostEqual(per["all"]["main_usd"] + per["all"]["sub_usd"], per["all"]["usd"], places=6)
        self.assertEqual(sum(t["tokens"] for t in per["all"]["by_type"]), per["all"]["tokens"])
        series = ledger.daily_series(led, ["2026-11-09", "2026-11-10", "2026-11-11"])
        self.assertIsNone(series[0]["usd"])
        self.assertAlmostEqual(series[1]["usd"] + series[2]["usd"], per["all"]["usd"], places=6)

    def test_dollars_are_worked_out_on_read_from_the_price_table(self):
        two_days(self.projects)
        ledger.backfill(self.projects)
        with open(ledger.ledger_file()) as fh:
            self.assertNotIn("usd", fh.read())                    # tokens only on disk
        led, _ = ledger.load()
        now = ledger.totals(led, None, None)
        doubled = json.loads(json.dumps(pricing.load_prices()))
        for row in doubled["models"].values():
            for t in pricing.TYPES:
                row[t] *= 2
        self.assertAlmostEqual(ledger.totals(led, None, None, doubled)["usd"], 2 * now["usd"], places=6)
        doubled["models"]["mystery-model-1"] = dict(doubled["models"]["claude-opus-5"])
        self.assertEqual(ledger.totals(led, None, None, doubled)["unpriced_tokens"], 0)

    def test_each_message_is_counted_once(self):
        a = Transcript("aaaaaaaa-0000-0000-0000-000000000001", "/work/alpha", "2026-11-10")
        a.prompt().reply("claude-opus-5", out=M)                      # $25
        first = a.lines[-1]
        a.reply("claude-opus-5", out=M)                               # $25
        a.lines.append(first)                                         # the first id comes back: A, B, A
        a.reply("claude-opus-5", out=10)                              # a streamed message: early records
        grown = json.loads(a.lines[-1])                               # carry less than the last one
        grown["message"]["usage"]["output_tokens"] = M
        a.lines[-1] = json.dumps(grown, separators=(",", ":"))        # final usage for that id: $25
        a.lines.append('{"type":"assistant","timestamp":"%s","message":"not an object"}' % grown["timestamp"])
        a.lines.append('{"type":"assistant","timestamp":"%s","message":{"id":["x"],"model":{"a":1},'
                       '"usage":{"output_tokens":"many"}}}' % grown["timestamp"])
        a.write(self.projects)
        copy = Transcript("ffffffff-0000-0000-0000-000000000009", "/work/alpha", "2026-11-10")
        copy.lines = list(a.lines)                                    # a resumed session copies its parent
        copy.t = a.t
        copy.reply("claude-opus-5", out=M)                            # and adds $25 of its own
        path = copy.write(self.projects)
        later = time.time() + 5
        os.utime(path, (later, later))
        d, _blob = extract.run(common.parse_when("2026-11-10"), common.parse_when("2026-11-11"),
                               projects_dir=self.projects, inventory=False)
        self.assertAlmostEqual(d["totals"]["cost"]["usd"], 100.0, places=2)
        self.assertEqual(d["totals"]["tokens"]["out"], 4 * M)
        by_session = dict((s["id"][:4], s["cost"]["usd"]) for s in d["sessions"])
        self.assertEqual(by_session, {"aaaa": 75.0, "ffff": 25.0})    # the copy only has its own $25
        ledger.backfill(self.projects)
        led, _ = ledger.load()
        self.assertAlmostEqual(ledger.totals(led, None, None)["usd"], 100.0, places=2)

    def test_a_message_that_straddles_midnight_is_not_dropped(self):
        a = Transcript("aaaaaaaa-0000-0000-0000-000000000001", "/work/alpha", "2026-11-10")
        a.prompt()
        a.t = common.parse_when("2026-11-11") - 40                    # 20 seconds before local midnight
        a.reply("claude-opus-5", out=10)
        early, late = json.loads(a.lines[-2]), json.loads(a.lines[-1])
        late["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S",
                                          time.gmtime(common.parse_when("2026-11-11") + 30)) + ".000Z"
        late["message"]["usage"]["output_tokens"] = M                 # the final record, after midnight
        a.lines[-2:] = [json.dumps(early, separators=(",", ":")), json.dumps(late, separators=(",", ":"))]
        a.write(self.projects)
        ledger.backfill(self.projects)
        led, _ = ledger.load()
        self.assertEqual(sorted(led["days"]), ["2026-11-10", "2026-11-11"])
        self.assertGreaterEqual(ledger.totals(led, None, None)["out"], M)

    def test_damaged_or_newer_ledger_is_never_written_over(self):
        two_days(self.projects)
        os.makedirs(os.environ["COACH_HOME"])
        with open(ledger.ledger_file(), "w") as fh:
            fh.write('{"version": 1, "days": {"2026-01-0')              # cut off mid-write
        led, r = ledger.update("2026-11-10", "2026-11-10", self.projects)
        self.assertTrue(r["seeded"])                                  # seeded again in full
        self.assertEqual(sorted(led["days"]), ["2026-11-10", "2026-11-11"])
        kept = [n for n in os.listdir(os.environ["COACH_HOME"]) if ".damaged-" in n]
        self.assertEqual(len(kept), 1)                                # the damaged file was moved aside
        with open(ledger.ledger_file()) as fh:
            raw = json.load(fh)
        raw["version"] = 99
        raw["days"]["2026-01-01"] = {"future": True}
        with open(ledger.ledger_file(), "w") as fh:
            json.dump(raw, fh)
        with open(ledger.ledger_file()) as fh:
            before = fh.read()
        self.assertRaises(ledger.LedgerError, ledger.backfill, self.projects)
        plan = recap.build([], today=datetime.date(2026, 11, 11), projects_dir=self.projects)
        self.assertIn("newer version", plan["ledger_error"])
        self.assertTrue(plan["totals_line"].startswith("Totals: "))
        self.assertNotIn("All time", plan["totals_line"])
        self.assertIn("about $61 API-equivalent.", plan["totals_line"])
        self.assertIn("Week to date: $61.", plan["totals_line"])
        self.assertIsNone(plan["cost"]["all_time_text"])
        week = recap.build(["week"], today=datetime.date(2026, 11, 12), projects_dir=self.projects)
        self.assertTrue(week["cost_block"].split("\n")[-3].endswith("| $71 | - |"))
        self.assertNotIn("$0.00 |", week["cost_block"])
        with open(ledger.ledger_file()) as fh:
            self.assertEqual(fh.read(), before)

    def test_missed_days_are_filled_in_by_the_next_run(self):
        two_days(self.projects)
        ledger.update("2026-11-10", "2026-11-10", self.projects)       # seeds both days
        for i, day in enumerate(["2026-11-13", "2026-11-20"]):
            Transcript("hhhhhhhh-0000-0000-0000-00000000000%d" % i, "/work/alpha", day) \
                .prompt().reply("claude-opus-5", out=M).write(self.projects)
        led, r = ledger.update("2026-11-20", "2026-11-20", self.projects)   # a run a week later
        self.assertEqual(r["added"], ["2026-11-13", "2026-11-20"])
        # a ledger whose days cannot be read (an older layout) is moved aside, not written over
        with open(ledger.ledger_file(), "w") as fh:
            fh.write('{"version": 1, "days": {"2026-11-10": {"rows": [1]}}}')
        self.assertEqual(ledger.load()[1], "damaged")

    def test_damaged_ledger_does_not_raise(self):
        os.makedirs(os.environ["COACH_HOME"])
        for text in ("not json", "[]", '{"days": {"2026-11-10": {"sessions": {"a": {"project": 1}, "b": 7}}, "bad": 3}}',
                     '{"days": {"2026-11-10": {"sessions": {"a": {"project": "p", "models": {"m": [[1e999, "x"], 5]}}}}}}'):
            with open(ledger.ledger_file(), "w") as fh:
                fh.write(text)
            led, state = ledger.load()
            self.assertIn(state, ("ok", "damaged"))
            ledger.periods(led, "2026-11-10")


class RecapCostTest(Base):
    def test_plan_carries_cost_lines_and_seeds_the_ledger_once(self):
        two_days(self.projects)
        today = datetime.date(2026, 11, 11)
        out = recap.build([], today=today, projects_dir=self.projects)
        self.assertTrue(out["ledger_seeded_now"])
        self.assertTrue(os.path.isfile(out["ledger"]))
        self.assertEqual(out["cost"]["top_project"]["name"], "alpha")
        self.assertEqual(out["cost"]["since"], "2026-11-10")
        self.assertEqual(out["cost"]["text"], "$61")
        line = out["totals_line"]
        self.assertTrue(line.startswith("Totals: "))
        self.assertIn("about $61 API-equivalent.", line)
        self.assertIn("Top projects: alpha $57, beta $4.00.", line)
        self.assertIn("Unpriced: 1k tokens.", line)
        self.assertIn("Week to date: $61.", line)
        self.assertIn("All time since 2026-11-10: $71.", line)   # the seed took in day 2 as well
        self.assertNotIn("cost_block", out)
        again = recap.build([], today=today, projects_dir=self.projects)
        self.assertFalse(again["ledger_seeded_now"])
        week = recap.build(["week"], today=datetime.date(2026, 11, 12), projects_dir=self.projects)
        block = week["cost_block"].split("\n")
        self.assertEqual(block[0], "## Cost by project")
        self.assertEqual(block[1], "| Project | Week tokens | Week cost | All-time cost |")
        self.assertRegex(block[3], r"^\| alpha \| 17(\.\d)?M \| \$57 \| \$57 \|$")
        self.assertTrue(block[-3].startswith("| Total | "))
        self.assertIn("All time is since 2026-11-10.", block[-1])
        self.assertIn("not a bill", block[-1])
        self.assertNotIn("totals_line", week)

    def test_report_lines_are_parsed_for_the_page(self):
        two_days(self.projects)
        day = recap.build([], today=datetime.date(2026, 11, 11), projects_dir=self.projects)
        text = ("# Coach: 2026-11-10\n\n1. What happened: aaaaaaaa, alpha, 12:00: 2M output tokens, about $20.\n"
                "   Do instead: one thing.\n\n" + day["totals_line"] + "\n")
        parsed = build_site.parse_daily(text)
        self.assertEqual(len(parsed["items"]), 1)
        self.assertEqual(parsed["items"][0]["do"], "One thing.")       # the totals line is not glued on
        self.assertRegex(parsed["totals"], r"^17(\.\d)?M tokens \(3(\.\d)?M output\), about \$61")
        self.assertIn("All time since 2026-11-10", parsed["totals"])
        self.assertEqual(build_site.parse_daily("# Coach\n\nNothing notable on 2026-11-10.\n")["totals"], "")
        week = recap.build(["week"], today=datetime.date(2026, 11, 12), projects_dir=self.projects)
        text = ("# Coach weekly: 2026-11-12\n\n## Week in numbers\n3 sessions.\n\n" + week["cost_block"]
                + "\n\n## What cost the most\n1. What happened: x\n   Do instead: y\n   Trend: new.\n")
        parsed = build_site.parse_weekly(text)
        self.assertEqual(parsed["cost"]["head"], ["Project", "Week tokens", "Week cost", "All-time cost"])
        self.assertEqual(parsed["cost"]["rows"][0][0], "alpha")
        self.assertEqual(parsed["cost"]["rows"][-1][0], "Total")
        self.assertIn("not a bill", parsed["cost"]["note"])
        self.assertEqual(len(parsed["items"]), 1)
        self.assertEqual(parsed["numbers"], "3 sessions.")

    def test_page_and_card_show_cost_and_old_digests_still_render(self):
        two_days(self.projects)
        day = recap.build([], today=datetime.date(2026, 11, 11), projects_dir=self.projects)
        with open(day["report"], "w") as fh:
            fh.write("# Coach: 2026-11-10\n\n1. What happened: aaaaaaaa, alpha: cost.\n"
                     "   Do instead: less.\n\n" + day["totals_line"] + "\n")
        out = os.path.join(self.tmp, "out", "index.html")
        p = subprocess.run([sys.executable, os.path.join(ROOT, "coach", "build_site.py"), "--card",
                            "--out", out, "--projects-dir", self.projects, "--today", "2026-11-12",
                            "--no-trends"], env=dict(os.environ),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8"))
        card = json.loads(p.stdout.decode("utf-8"))
        self.assertEqual(card["cost"]["text"], "$61")
        self.assertEqual(card["cost"]["top_project"]["name"], "alpha")
        self.assertIn("not a bill", card["cost"]["meaning"])
        with open(out) as fh:
            page = fh.read()
        self.assertIn("API-equivalent, not a bill", page)
        self.assertIn('id="view-cost"', page)
        data = json.loads(page.split('id="coach-data">', 1)[1].split("</script>", 1)[0])
        cost = data["cost"]
        self.assertEqual(sorted(cost["periods"]), ["all", "month", "today", "week"])   # the Overview's
        self.assertEqual(cost["periods"]["today"]["t"]["days"], 0)     # nothing on record for the 12th
        self.assertEqual(cost["periods"]["week"]["t"]["days"], 2)
        allt = cost["periods"]["all"]["t"]
        self.assertAlmostEqual(sum(r["usd"] for r in allt["split"]["project"]), allt["usd"], places=1)
        self.assertEqual([t["type"] for t in allt["by_type"]],
                         ["Input", "Output", "Cache read", "Cache write"])
        self.assertEqual(cost["since"], "2026-11-10")
        self.assertRegex(data["dailies"][0]["totals"], r"^17(\.\d)?M tokens")
        # a digest written before costs existed: tokens, no dollars, no crash
        old = {"totals": {"sessions": 1, "tokens": {"out": 5}},
               "sessions": [{"id": "eeeeeeee-1", "project": "~/old", "tokens": {"in": 1, "out": 5}}]}
        self.assertEqual(build_site.digest_cost(old), {})
        rows = build_site.digest_sessions(old)
        self.assertEqual((rows[0]["usd"], rows[0]["tokens"]), (None, 6))
        self.assertEqual(build_site.load_cost(self.tmp, "2026-11-12",
                                              os.path.join(self.tmp, "none.json")), (None, ""))


class ShipTest(unittest.TestCase):
    def test_no_long_dashes_or_machine_paths_in_new_files(self):
        for name in ("coach/pricing.py", "coach/projects.py", "coach/ledger.py", "coach/prices.json",
                     "tests/test_cost.py"):
            with open(os.path.join(ROOT, name), "rb") as fh:
                text = fh.read().decode("utf-8")
            self.assertNotIn(chr(0x2013), text, name)
            self.assertNotIn(chr(0x2014), text, name)
            self.assertNotIn("/" + "Users/", text, name)


if __name__ == "__main__":
    unittest.main()
