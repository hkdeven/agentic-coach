"""Stdlib tests for what 1.2.0 changed in the shared code: whole-session titles,
the per-session project override, exclude rules, the account and date filters,
the name for one-off chats, and the recap plan. Synthetic data only.

Run: python3 -m unittest discover -s tests -v
"""
import io
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "coach"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import accounts  # noqa: E402
import build_site  # noqa: E402
import common  # noqa: E402
import config  # noqa: E402
import extract  # noqa: E402
import ledger  # noqa: E402
import projects  # noqa: E402
import recap  # noqa: E402
import scope  # noqa: E402
import setup_check  # noqa: E402
from test_cost import Base, M, Transcript, local_noon  # noqa: E402

OPUS = "claude-opus-5-5"
SID_A = "aaaaaaaa-1111-0000-0000-000000000001"
SID_B = "bbbbbbbb-2222-0000-0000-000000000002"
SID_C = "cccccccc-3333-0000-0000-000000000003"
# synthetic account and organisation ids
ACCT_1 = "11aa22bb-0000-4000-8000-000000000001"
ACCT_2 = "99ff88ee-0000-4000-8000-000000000002"
ORG_1 = "0a0b0c0d-0000-4000-8000-0000000000aa"
ACCT_FULL = ACCT_1


def write_config(**keys):
    folder = os.path.dirname(common.config_file())
    if not os.path.isdir(folder):
        os.makedirs(folder)
    with open(common.config_file(), "w") as fh:
        json.dump(keys, fh)


def digest(projects_dir, first, last_excl, **kw):
    d, _ = extract.run(common.parse_when(first), common.parse_when(last_excl),
                       projects_dir=projects_dir, inventory=False, **kw)
    return d


def names(d):
    return [r["name"] for r in d["projects"]]


def ledger_names(day=None):
    led, _ = ledger.load()
    t = ledger.totals(led, day or led["first_day"], day)
    return [r["name"] for r in t["by_project"]]


def long_session(projects_dir, sid=SID_A, title="Quarterly widget audit", cwd=""):
    """Three days of work; the title record sits on the first day only."""
    t = Transcript(sid, cwd, "2026-11-10", title=title)
    t.prompt().reply(OPUS, out=100000)
    t.at("2026-11-11").prompt("carry on").reply(OPUS, out=200000)
    t.at("2026-11-12").prompt("and again").reply(OPUS, out=300000)
    t.write(projects_dir)
    return t


class TitleTest(Base):
    RULES = [{"name": "Widgets", "match": ["widget audit"]}]

    def test_title_outside_the_scanned_range_still_names_the_project(self):
        long_session(self.projects)
        for day, nxt in (("2026-11-10", "2026-11-11"), ("2026-11-11", "2026-11-12"),
                         ("2026-11-12", "2026-11-13")):
            d = digest(self.projects, day, nxt, rules=self.RULES)
            self.assertEqual(names(d), ["Widgets"], day)
            self.assertEqual(d["sessions"][0]["title"], "Quarterly widget audit")

    def test_title_written_after_the_scanned_day_counts_too(self):
        t = Transcript(SID_A, "", "2026-11-10")
        t.prompt().reply(OPUS, out=1000)
        t.at("2026-11-12").title = "Quarterly widget audit"
        t.prompt("later").reply(OPUS, out=1000)
        t.write(self.projects)
        self.assertEqual(names(digest(self.projects, "2026-11-10", "2026-11-11", rules=self.RULES)),
                         ["Widgets"])

    def test_every_ledger_day_gets_the_same_project_and_rebuild_moves_it(self):
        long_session(self.projects)
        ledger.backfill(self.projects)
        for day in ("2026-11-10", "2026-11-11", "2026-11-12"):
            self.assertEqual(ledger_names(day), ["Quarterly widget audit"], day)
        r = ledger.backfill(self.projects, rules=self.RULES, rebuild=True)
        self.assertEqual(r["replaced"], ["2026-11-10", "2026-11-11", "2026-11-12"])
        for day in ("2026-11-10", "2026-11-11", "2026-11-12"):
            self.assertEqual(ledger_names(day), ["Widgets"], day)

    def test_a_retitled_session_matches_a_rule_on_any_of_its_titles(self):
        t = Transcript(SID_A, "", "2026-11-10")
        t.prompt().reply(OPUS, out=1000)
        for title in ("Fix the login page", "Widget audit follow-up"):
            t.lines.append(json.dumps({"type": "ai-title", "aiTitle": title},
                                      separators=(",", ":")))
        t.write(self.projects)
        self.assertEqual(names(digest(self.projects, "2026-11-10", "2026-11-11",
                                      rules=[{"name": "Widgets", "match": ["widget audit"]}])),
                         ["Widgets"])


class OverrideTest(Base):
    def test_prefix_and_full_id_win_over_every_rule(self):
        long_session(self.projects, cwd="/work/alpha")
        rules = [{"name": "By rule", "match": ["/work/alpha"]}]
        for key in (SID_A, SID_A[:8], SID_A[:12].upper()):
            d = digest(self.projects, "2026-11-11", "2026-11-12", rules=rules,
                       session_projects={key.lower(): "Pinned"})
            self.assertEqual(names(d), ["Pinned"], key)
        d = digest(self.projects, "2026-11-11", "2026-11-12", rules=rules,
                   session_projects={SID_A[:7]: "Too short"})
        self.assertEqual(names(d), ["By rule"])
        self.assertEqual(projects.session_override({SID_A[:8]: "Short", SID_A[:13]: "Long"}, SID_A),
                         "Long")

    def test_config_commands_ledger_digest_and_setup_status(self):
        long_session(self.projects, cwd="/work/alpha")
        self.assertIsNone(config.change(["set", "session_project", SID_A[:8], "Pinned", "project"]))
        self.assertEqual(common.load_config()["session_projects"], {SID_A[:8]: "Pinned project"})
        self.assertTrue(config.change(["set", "session_project", "short", "x"]))
        self.assertTrue(config.change(["set", "session_project", SID_A[:8]]))
        self.assertEqual(names(digest(self.projects, "2026-11-11", "2026-11-12")), ["Pinned project"])
        ledger.backfill(self.projects)
        self.assertEqual(ledger_names(), ["Pinned project"])
        self.assertEqual(setup_check.check(os.environ["CLAUDE_CONFIG_DIR"])["session_projects"], 1)
        self.assertIsNone(config.change(["unset", "session_project", SID_A[:8]]))
        self.assertTrue(config.change(["unset", "session_project", SID_A[:8]]))
        r = ledger.backfill(self.projects, rebuild=True)
        self.assertEqual(len(r["replaced"]), 3)
        self.assertEqual(ledger_names(), ["alpha"])

    def test_bad_config_values_are_ignored(self):
        write_config(session_projects={"short": "x", SID_A[:8]: 5, SID_B[:8]: " Kept "},
                     exclude=[{"match": "one text"}, {"sessions": [SID_C[:9]]}],
                     only_account=ACCT_FULL, exclude_dates=["2026-01-05", "..2026-01-31"],
                     other_project=" Misc | chats ")
        cfg = common.load_config()
        self.assertEqual(cfg["session_projects"], {SID_B[:8]: "Kept"})
        self.assertEqual(cfg["exclude"], [{"match": ["one text"]}, {"sessions": [SID_C[:9]]}])
        self.assertEqual(cfg["only_account"], "11aa22")        # a full id is cut to the short form
        self.assertEqual(cfg["exclude_dates"], [("2026-01-05", "2026-01-05"), (None, "2026-01-31")])
        self.assertEqual(cfg["other_project"], "Misc / chats")
        self.assertFalse(cfg["damaged"])

    def test_a_leave_out_setting_that_cannot_be_used_stops_every_run(self):
        import datetime
        long_session(self.projects, cwd="/work/alpha")
        for keys in ({"exclude": "a text"}, {"exclude": ["junk", {"match": ["ok text"]}]},
                     {"exclude": [{"match": ["ok text"], "sessions": ["short"]}]},
                     {"exclude": [{"match": ["ok text"], "session": [SID_A]}]},
                     {"exclude": [{"match": ["ok text", 5]}]}, {"exclude": [{}]},
                     {"exclude": [{"match": [5, " "]}]}, {"only_account": "not hex"},
                     {"unknown_account": "maybe"}, {"exclude_dates": ["2026-02-30"]},
                     {"exclude_dates": 7}):
            write_config(**keys)
            self.assertTrue(common.load_config()["damaged"], keys)
            with self.assertRaises(common.ConfigError):
                digest(self.projects, "2026-11-11", "2026-11-12")
            with self.assertRaises(common.ConfigError):
                ledger.backfill(self.projects)
            with self.assertRaises(common.ConfigError):
                recap.build([], today=datetime.date(2026, 11, 12), projects_dir=self.projects)
        with open(common.config_file(), "w") as fh:
            fh.write('{"exclude": [{"match": ["zebra"]}],}')         # one stray comma
        self.assertTrue(common.load_config()["damaged"])
        p = subprocess.run([sys.executable, os.path.join(ROOT, "coach", "recap.py"),
                            "--projects-dir", self.projects],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual((p.returncode, p.stdout), (2, b""))
        self.assertIn(b"Nothing was built", p.stderr)
        self.assertFalse(os.path.exists(ledger.ledger_file()))


class RobustTest(Base):
    def test_a_bad_byte_in_the_permission_log_does_not_stop_the_digest(self):
        os.makedirs(os.environ["COACH_HOME"])
        ts = local_noon("2026-11-10")
        with open(os.path.join(os.environ["COACH_HOME"], "permission-prompts.jsonl"), "wb") as fh:
            fh.write(json.dumps({"ts": ts, "tool": "Bash", "cmd": "gh pr view 1",
                                 "session": "s1"}).encode("utf-8") + b"\n")
            fh.write(b'{"ts": 1, "tool": "Bash", "cmd": "caf\xe9 \xff"}\n')
            fh.write(b"[1, 2]\n\xff\xfe\n")
        out = extract.read_permission_log(ts - 10, ts + 10)
        self.assertEqual([(g["cmd"], g["n"]) for g in out], [("gh pr view", 1)])

    def test_records_of_the_wrong_type_are_skipped(self):
        acc = extract.SessionAcc("s")
        for rec in ({"type": "assistant", "message": "not a map"},
                    {"type": "assistant", "message": ["x"]},
                    {"type": "attachment", "attachment": "text"},
                    {"type": "attachment", "attachment": ["x"]},
                    {"type": "attachment", "attachment": {"type": "queued_command", "origin": "x",
                                                          "prompt": "hello there"}},
                    {"type": "user", "message": "text"},
                    {"type": "system", "subtype": "compact_boundary", "compactMetadata": "x"},
                    {"type": "ai-title", "aiTitle": 5}, {"type": "custom-title", "customTitle": []}):
            acc.feed(rec)                       # none of these may raise
        self.assertEqual((acc.tool_calls, acc.titles, acc.custom_title), (0, [], None))


class ExcludeTest(Base):
    def setUp(self):
        Base.setUp(self)
        long_session(self.projects, SID_A, "Zebra client retainer")           # no folder
        b = Transcript(SID_B, "/work/beta", "2026-11-11")
        b.prompt().reply(OPUS, out=400000)
        b.write(self.projects)
        c = Transcript(SID_C, "/work/gamma", "2026-11-11")
        c.prompt().reply(OPUS, out=500000)
        c.write(self.projects)

    def test_title_match_outside_the_range_drops_the_session_everywhere(self):
        rules = [{"match": ["ZEBRA client"]}]
        d = digest(self.projects, "2026-11-11", "2026-11-12", exclude=rules)
        self.assertEqual(names(d), ["gamma", "beta"])
        self.assertEqual(d["totals"]["sessions"], 2)
        self.assertEqual(d["totals"]["excluded_sessions"], 1)
        blob = json.dumps(d).lower()
        self.assertNotIn("zebra", blob)
        self.assertNotIn(SID_A[:8], blob)
        self.assertAlmostEqual(sum(r["usd"] for r in d["projects"]), d["totals"]["cost"]["usd"],
                               places=2)

    def test_id_prefix_glob_folder_and_project_name(self):
        for rules, left in (([{"sessions": [SID_B[:8]]}], ["gamma", "Zebra client retainer"]),
                            ([{"match": [SID_B[:10]]}], ["gamma", "Zebra client retainer"]),
                            ([{"match": ["*/work/g*"]}], ["beta", "Zebra client retainer"]),
                            ([{"match": ["gamma"]}, {"sessions": [SID_A]}], ["beta"]),
                            ([{"sessions": [SID_B[:7]]}], ["gamma", "beta", "Zebra client retainer"])):
            d = digest(self.projects, "2026-11-11", "2026-11-12", exclude=rules)
            self.assertEqual(sorted(names(d)), sorted(left), rules)

    def test_exclusion_beats_the_override_and_every_rule(self):
        d = digest(self.projects, "2026-11-11", "2026-11-12", exclude=[{"match": ["zebra"]}],
                   session_projects={SID_A[:8]: "Pinned"},
                   rules=[{"name": "Named", "match": ["zebra"]}])
        self.assertEqual(sorted(names(d)), ["beta", "gamma"])

    def test_ledger_purge_on_the_next_run_and_on_rebuild(self):
        first = ledger.backfill(self.projects)
        self.assertEqual(first["excluded_removed"], 0)
        led, _ = ledger.load()
        before = ledger.totals(led, led["first_day"], None)
        self.assertIn("Zebra client retainer", [r["name"] for r in before["by_project"]])
        write_config(exclude=[{"match": ["zebra"]}])
        # the next recap run: the rules changed, so every transcript is read once
        led, result = ledger.update("2026-11-12", "2026-11-12", self.projects)
        self.assertEqual(result["excluded_removed"], 1)
        after = ledger.totals(led, led["first_day"], None)
        self.assertEqual(sorted(r["name"] for r in after["by_project"]), ["beta", "gamma"])
        self.assertEqual(led["first_day"], "2026-11-11")
        self.assertAlmostEqual(sum(r["usd"] for r in after["by_project"]), after["usd"], places=6)
        self.assertLess(after["usd"], before["usd"])
        with open(ledger.ledger_file()) as fh:
            raw = fh.read()
        self.assertNotIn("ebra", raw)
        self.assertNotIn(SID_A, raw)                         # only the first 8 characters are kept
        self.assertEqual(json.loads(raw)["excluded"], [SID_A[:8]])
        again = ledger.backfill(self.projects, rebuild=True)
        self.assertEqual(again["excluded_removed"], 0)
        p = subprocess.run([sys.executable, os.path.join(ROOT, "coach", "ledger.py"), "backfill",
                            "--rebuild", "--projects-dir", self.projects],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_rebuild_says_how_many_sessions_were_removed(self):
        ledger.backfill(self.projects)
        write_config(exclude=[{"sessions": [SID_B[:8]]}, {"match": ["zebra"]}])
        p = subprocess.run([sys.executable, os.path.join(ROOT, "coach", "ledger.py"), "backfill",
                            "--rebuild", "--projects-dir", self.projects],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertIn(b"2 excluded sessions removed", p.stdout)
        self.assertNotIn(b"ebra", p.stdout)

    def test_a_row_whose_transcript_is_gone_is_purged_by_id_or_project_name(self):
        ledger.backfill(self.projects)
        os.remove(os.path.join(self.projects, "-demo", SID_A + ".jsonl"))
        os.remove(os.path.join(self.projects, "-demo", SID_B + ".jsonl"))
        write_config(exclude=[{"match": ["zebra"]}, {"sessions": [SID_B[:8]]}])
        r = ledger.backfill(self.projects)
        self.assertEqual(r["excluded_removed"], 2)
        self.assertEqual(ledger_names(), ["gamma"])

    def test_config_commands_and_setup_status_say_numbers_only(self):
        self.assertIsNone(config.change(["add", "exclude", "Zebra", "client"]))
        self.assertIsNone(config.change(["add", "exclude", "zebra client"]))       # no duplicate
        self.assertIsNone(config.change(["add", "exclude", SID_B[:8]]))
        self.assertTrue(config.change(["add", "exclude", "ab"]))
        self.assertEqual(common.load_config()["exclude"], [{"match": ["Zebra client", SID_B[:8]]}])
        ledger.backfill(self.projects)
        out = setup_check.check(os.environ["CLAUDE_CONFIG_DIR"])
        self.assertEqual((out["exclusions"]["rules"], out["exclusions"]["sessions_excluded"]), (2, 2))
        blob = json.dumps(out).lower()
        self.assertNotIn("zebra", blob)
        self.assertNotIn(SID_B[:8], blob)
        self.assertIsNone(config.change(["remove", "exclude", "ZEBRA client"]))
        self.assertTrue(config.change(["remove", "exclude", "zebra client"]))
        self.assertIsNone(config.change(["remove", "exclude", SID_B[:8]]))
        self.assertEqual(common.load_config()["exclude"], [])
        with open(common.config_file()) as fh:
            self.assertNotIn("exclude", json.load(fh))

    def test_a_recap_after_a_new_rule_is_not_mistaken_for_missing_transcripts(self):
        import datetime
        today = datetime.date(2026, 11, 12)
        recap.build([], today=today, projects_dir=self.projects)
        write_config(exclude=[{"match": ["zebra"]}])
        out = recap.build([], today=today, projects_dir=self.projects)      # must not raise
        self.assertEqual(out["sessions"], 2)
        self.assertNotIn("ebra", json.dumps(out))
        with open(out["digest"]) as fh:
            self.assertNotIn("ebra", fh.read())


class AccountBase(Base):
    def setUp(self):
        Base.setUp(self)
        self.app = os.path.join(self.tmp, "app-sessions")
        os.environ["COACH_APP_SESSIONS_DIR"] = self.app
        self.home_json = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], ".claude.json")
        os.makedirs(os.environ["CLAUDE_CONFIG_DIR"])
        with open(self.home_json, "w") as fh:
            json.dump({"oauthAccount": {"accountUuid": ACCT_1, "organizationUuid": ORG_1,
                                        "emailAddress": "person@example.invalid"}}, fh)

    def app_index(self, sid, acct=ACCT_2, org=ORG_1):
        folder = os.path.join(self.app, acct, org)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        with open(os.path.join(folder, "local_%s.json" % sid[:8]), "w") as fh:
            json.dump({"cliSessionId": sid}, fh)

    @staticmethod
    def owner(acct, org=ORG_1):
        return json.dumps({"type": "session-owner", "accountUuid": acct, "organizationUuid": org})

    @staticmethod
    def bridge(acct, org=ORG_1):
        return json.dumps({"type": "bridge-session", "ownerAccountUuid": acct,
                           "ownerOrganizationUuid": org})

    @staticmethod
    def autoreact(acct):
        return json.dumps({"type": "artifact-autoreact-ledger", "accountUuid": acct})


class ReviewFixesTest(AccountBase):
    def test_a_ranged_run_does_not_purge_a_sessions_earlier_days(self):
        t = Transcript(SID_A, "/work/alpha", "2026-11-10")
        t.prompt().reply(OPUS, out=100000)
        t.at("2026-11-13").prompt("later").reply(OPUS, out=200000)
        t.write(self.projects)
        other = Transcript(SID_B, "/work/beta", "2026-11-13")
        other.prompt().reply(OPUS, out=1000)
        other.write(self.projects)
        write_config(exclude_dates=["2026-11-13"])
        ledger.backfill(self.projects)
        led, _ = ledger.load()
        self.assertEqual(sorted(led["days"]), ["2026-11-10"])
        self.assertEqual(led["excluded"], [SID_B[:8]])       # only the session with nothing left
        led, _result = ledger.update("2026-11-13", "2026-11-13", self.projects)   # a daily run
        self.assertEqual(sorted(led["days"]), ["2026-11-10"])
        self.assertIn(SID_A, led["days"]["2026-11-10"]["sessions"])
        self.assertEqual(led["excluded"], [SID_B[:8]])

    def test_excluded_ids_survive_the_cleanup_of_their_transcripts(self):
        long_session(self.projects, SID_A, "Zebra client retainer")
        write_config(exclude=[{"match": ["zebra"]}])
        ledger.backfill(self.projects)
        os.remove(os.path.join(self.projects, "-demo", SID_A + ".jsonl"))
        r = ledger.backfill(self.projects, rebuild=True)
        led, _ = ledger.load()
        self.assertEqual((led["excluded"], r["excluded_sessions"]), ([SID_A[:8]], 1))
        write_config()
        ledger.backfill(self.projects)
        self.assertEqual(ledger.load()[0]["excluded"], [])   # no rule in force: nothing is hidden

    def test_a_removed_rule_stops_hiding_a_session_that_can_be_read_again(self):
        long_session(self.projects, SID_A, "Zebra client retainer")
        write_config(exclude=[{"match": ["zebra"]}], exclude_dates=["2025-01-01"])
        ledger.backfill(self.projects)
        self.assertEqual(ledger.load()[0]["excluded"], [SID_A[:8]])
        write_config(exclude_dates=["2025-01-01"])               # the rule goes, a date stays
        led, _result = ledger.update("2026-11-12", "2026-11-12", self.projects)
        self.assertEqual(led["excluded"], [])
        self.assertEqual(led["filter"]["rule"], 0)
        self.assertEqual(sorted(led["days"]), ["2026-11-10", "2026-11-11", "2026-11-12"])

    def test_a_removed_rule_brings_rows_back_on_closed_days_shared_with_others(self):
        long_session(self.projects, SID_A, "Zebra client retainer")
        long_session(self.projects, SID_B, "Plain work", cwd="/work/beta")
        ledger.backfill(self.projects)
        led, _ = ledger.load()
        before = ledger.totals(led, led["first_day"], None)["tokens"]
        write_config(exclude=[{"match": ["zebra"]}])
        ledger.backfill(self.projects)
        self.assertEqual(ledger_names(), ["beta"])
        write_config()
        r = ledger.backfill(self.projects)
        led, _ = ledger.load()
        after = ledger.totals(led, led["first_day"], None)
        self.assertEqual((after["tokens"], led["excluded"], r["kept"]), (before, [], []))
        self.assertEqual(sorted(x["name"] for x in after["by_project"]),
                         ["Zebra client retainer", "beta"])

    def test_permission_prompts_of_another_account_stay_out_when_no_transcript_is_read(self):
        accounts.record(SID_A, when=local_noon("2026-11-10"),
                        who={"account": "99ff88", "org": "0a0b0c"})
        with open(os.path.join(os.environ["COACH_HOME"], "permission-prompts.jsonl"), "w") as fh:
            fh.write(json.dumps({"ts": local_noon("2026-11-10") + 60, "tool": "Bash",
                                 "cmd": "gh pr view 1", "session": SID_A}) + "\n")
        write_config(only_account="11aa22")
        d = digest(self.projects, "2026-11-10", "2026-11-11")
        self.assertEqual(d["permission_prompts"], [])

    def test_a_digest_counted_under_day_settings_is_still_read_once_they_are_lifted(self):
        d = {"sessions": [], "totals": {"sessions": 3}, "scope_sig": "abc"}
        self.assertEqual(scope.stored(d, [])[0], d)

    def test_older_reports_and_digests_naming_left_out_work_are_not_handed_to_the_skill(self):
        import datetime
        long_session(self.projects, cwd="/work/alpha")
        root = os.path.join(os.environ["COACH_HOME"], "reports")
        for sub in ("weekly", "digests", "daily"):
            os.makedirs(os.path.join(root, sub))
        for day, text in (("2026-11-03", "about the zebra retainer"), ("2026-11-04", "plain")):
            with open(os.path.join(root, "weekly", day + ".md"), "w") as fh:
                fh.write("# Coach weekly: %s\n\n%s\n" % (day, text))
            with open(os.path.join(root, "daily", day + ".md"), "w") as fh:
                fh.write("# Coach: %s\n\n%s\n" % (day, text))
            with open(os.path.join(root, "digests", "weekly-%s.json" % day), "w") as fh:
                json.dump({"sessions": [{"id": "0000000%s" % day[-1], "title": text}],
                           "totals": {}}, fh)
        write_config(exclude=[{"match": ["zebra"]}])
        out = recap.build(["week"], today=datetime.date(2026, 11, 5), projects_dir=self.projects)
        for key in ("earlier_weekly_reports", "earlier_weekly_digests", "daily_reports_this_week"):
            self.assertEqual(["2026-11-03" in p for p in out[key]], [False], key)
            self.assertIn("2026-11-04", out[key][0])

    def test_config_script_never_rewrites_an_exclude_value_it_cannot_read(self):
        write_config(exclude="zebra client")
        self.assertIn("by hand", config.change(["add", "exclude", "other text"]))
        self.assertIn("by hand", config.change(["remove", "exclude", "zebra client"]))
        with open(common.config_file()) as fh:
            self.assertEqual(json.load(fh), {"exclude": "zebra client"})

    def test_every_cli_stops_with_one_line_on_unusable_settings(self):
        write_config(exclude="zebra client")
        for script, words in (("setup_check.py", []), ("ledger.py", ["show"]),
                              ("ledger.py", ["backfill", "--projects-dir", self.projects]),
                              ("compare.py", ["--projects-dir", self.projects]),
                              ("config_audit.py", ["--projects-dir", self.projects])):
            p = subprocess.run([sys.executable, os.path.join(ROOT, "coach", script)] + words,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(p.returncode, 2, (script, p.stderr[-300:]))
            self.assertNotIn(b"Traceback", p.stderr, script)
            self.assertIn(b"Nothing was built", p.stderr, script)
            self.assertNotIn(b"zebra", p.stdout.lower() + p.stderr.lower())
        # follow-through shows nothing rather than everything
        p = subprocess.run([sys.executable, os.path.join(ROOT, "coach", "followthrough.py"),
                            "status"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotIn(b"Traceback", p.stderr)
        self.assertNotIn(b"zebra", p.stdout.lower())

    def test_settings_change_does_not_let_a_thinner_digest_replace_a_fuller_one(self):
        import datetime
        today = datetime.date(2026, 11, 12)
        long_session(self.projects, SID_A, cwd="/work/alpha")
        t = Transcript(SID_B, "/work/beta", "2026-11-11")
        t.prompt().reply(OPUS, out=1000)
        path = t.write(self.projects)
        recap.build([], today=today, projects_dir=self.projects)
        os.remove(path)
        with self.assertRaises(recap.UsageError):
            recap.build([], today=today, projects_dir=self.projects)
        write_config(session_projects={SID_A[:8]: "Pinned"}, exclude=[{"match": ["nothing-here"]}])
        with self.assertRaises(recap.UsageError):
            recap.build([], today=today, projects_dir=self.projects)

    def test_a_short_match_text_is_not_tested_against_session_ids(self):
        self.assertFalse(projects.excluded([{"match": ["aaa"]}], SID_A, ["/work/alpha"]))
        self.assertFalse(projects.excluded([{"match": [SID_A[4:12]]}], SID_A, []))   # not a prefix
        self.assertTrue(projects.excluded([{"match": [SID_A[:8]]}], SID_A, []))
        self.assertTrue(projects.excluded([{"match": ["alp"]}], SID_A, ["/work/alpha"]))

    def test_permission_prompts_of_left_out_days_and_sessions_stay_out(self):
        mixed = Transcript(SID_A, "/work/alpha", "2026-11-10")
        mixed.prompt().reply(OPUS, out=1000)
        mixed.lines.append(self.owner(ACCT_2))
        mixed.at("2026-11-11").prompt("switched").reply(OPUS, out=1000)
        mixed.lines.append(self.owner(ACCT_1))
        mixed.write(self.projects)
        gone = Transcript(SID_B, "", "2026-11-11", title="Zebra client retainer")
        gone.prompt().reply(OPUS, out=1000)
        gone.write(self.projects)
        os.makedirs(os.environ["COACH_HOME"])
        with open(os.path.join(os.environ["COACH_HOME"], "permission-prompts.jsonl"), "w") as fh:
            for day, sid, cmd in (("2026-11-10", SID_A, "gh pr view 1"),
                                  ("2026-11-11", SID_A, "git status"),
                                  ("2026-11-11", SID_B, "npm ls zebra")):
                fh.write(json.dumps({"ts": local_noon(day) + 60, "tool": "Bash", "cmd": cmd,
                                     "session": sid}) + "\n")
        write_config(only_account="11aa22", exclude=[{"match": ["zebra"]}])
        d = digest(self.projects, "2026-11-10", "2026-11-12")
        self.assertEqual([g["cmd"] for g in d["permission_prompts"]], ["git status"])
        self.assertNotIn("zebra", json.dumps(d).lower())
        # the excluded transcript is cleaned up later: the ledger's list still keeps its prompts out
        ledger.backfill(self.projects)
        os.remove(os.path.join(self.projects, "-demo", SID_B + ".jsonl"))
        d = digest(self.projects, "2026-11-10", "2026-11-12")
        self.assertEqual([g["cmd"] for g in d["permission_prompts"]], ["git status"])

    def test_a_project_rule_that_renames_into_an_excluded_name_changes_the_fingerprint(self):
        base = {"exclude": [{"match": ["secret"]}], "projects": []}
        named = dict(base, projects=[{"name": "Secret plans", "match": ["alpha"]}])
        self.assertNotEqual(scope.Scope(base).sig(), scope.Scope(named).sig())
        self.assertEqual(scope.Scope({"projects": [1]}).sig(), "")       # no rule, no fingerprint

    def test_stored_digests_from_other_date_or_account_settings_are_not_reused(self):
        d = {"sessions": [], "totals": {"sessions": 3}}
        self.assertEqual(scope.stored(d, [])[0], d)
        write_config(exclude_dates=["2026-11-10"])
        self.assertEqual(scope.stored(d, []), (None, None))
        self.assertEqual(scope.stored(dict(d, scope_sig=scope.Scope().day_sig()), [])[0]["totals"],
                         {"sessions": 3})

    def test_followthrough_parser_keeps_totals_and_checked_out_of_the_last_item(self):
        import followthrough
        text = ("# Coach: 2026-11-16\n\n"
                "1. What happened: 9 failures (session 1a2b3c4d, turn 41, 13:39) "
                "(repeat of 2026-11-10, given 3 times)\n"
                "   Do instead: read the error first.\n"
                "   Evidence: python3 <plugin>/coach/evidence.py show 1a2b3c4d --turn 41\n"
                "2. What happened: 3 compactions (session 5e6f7a8b, turn 7, 09:10)\n"
                "   Do instead: hand off at each break.\n\n"
                "Totals: 1.2M tokens (0.3M output), about $9 API-equivalent.\n"
                "Checked: 2 items against the transcripts.\n")
        items = followthrough.parse_report(text, "daily")["items"]
        self.assertEqual([i["do"] for i in items],
                         ["read the error first.", "hand off at each break."])
        self.assertEqual(items[0]["marker"], {"since": "2026-11-10", "times": 3})

    def test_followthrough_does_not_read_back_advice_about_excluded_work(self):
        import followthrough
        root = os.path.join(os.environ["COACH_HOME"], "reports")
        os.makedirs(os.path.join(root, "daily"))
        with open(os.path.join(root, "daily", "2026-11-10.md"), "w") as fh:
            fh.write("# Coach: 2026-11-10\n\n"
                     "1. What happened: 9 failures in the zebra retainer work.\n"
                     "   Do instead: read the zebra error first.\n"
                     "2. What happened: 3 compactions in beta.\n"
                     "   Do instead: hand off at each break.\n")
        followthrough.backfill()
        self.assertEqual(len(followthrough.shown_log()), 2)
        write_config(exclude=[{"match": ["zebra"]}])
        log = followthrough.shown_log()
        self.assertEqual(len(log), 1)
        self.assertNotIn("zebra", json.dumps(log).lower())
        self.assertNotIn("zebra", json.dumps(followthrough.page_data()).lower())
        rows = followthrough.annotate([], texts=["read the zebra error first."], date="2026-11-12")
        self.assertEqual([r["status"] for r in rows], ["NEW"])
        # a rule made of the log's own field names or fixed words hides nothing by accident
        write_config(exclude=[{"match": ["item"]}, {"match": ["daily"]}, {"match": ["report"]}])
        self.assertEqual(len(followthrough.shown_log()), 2)
        # writing the log while a rule is in force never deletes what the rule hides
        write_config(exclude=[{"match": ["zebra"]}])
        with open(os.path.join(root, "daily", "2026-11-11.md"), "w") as fh:
            fh.write("# Coach: 2026-11-11\n\n1. What happened: 2 long pastes in beta.\n"
                     "   Do instead: keep the text in a file.\n")
        followthrough.record_report(os.path.join(root, "daily", "2026-11-11.md"))
        self.assertEqual(len(followthrough.shown_log()), 2)
        write_config()
        self.assertEqual(len(followthrough.shown_log()), 3)        # the rule is gone: it is back
        write_config(exclude_dates=["2026-11-10"])
        self.assertEqual(len(followthrough.shown_log()), 1)

    def test_statusline_setup_refuses_unknown_words(self):
        import statusline
        settings = os.path.join(self.tmp, "settings.json")
        with open(settings, "w") as fh:
            fh.write("{}")
        for words in (["install", "--help"], ["remove", "-h"], ["install", "--settings", settings,
                                                               "please"]):
            p = subprocess.run([sys.executable, os.path.join(ROOT, "coach", "statusline.py")]
                               + words, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(p.returncode, 2, words)
        with open(settings) as fh:
            self.assertEqual(fh.read(), "{}")
        self.assertFalse(os.path.exists(os.path.join(os.environ["COACH_HOME"],
                                                     statusline.LAUNCHER_NAME)))

    def test_plan_is_saved_and_skips_reports_of_left_out_days(self):
        import datetime
        long_session(self.projects, cwd="/work/alpha")
        root = os.path.join(os.environ["COACH_HOME"], "reports")
        os.makedirs(os.path.join(root, "daily"))
        for day in ("2026-11-10", "2026-11-11"):
            with open(os.path.join(root, "daily", day + ".md"), "w") as fh:
                fh.write("# Coach: %s\n" % day)
        write_config(exclude_dates=["2026-11-10"])
        out = recap.build(["2026-11-12"], today=datetime.date(2026, 11, 13),
                          projects_dir=self.projects)
        self.assertEqual([os.path.basename(p) for p in out["last_three_daily_reports"]],
                         ["2026-11-11.md"])
        with open(out["plan_file"]) as fh:
            self.assertEqual(json.load(fh)["report"], out["report"])

    def test_config_change_prints_counts_only(self):
        p = subprocess.run([sys.executable, os.path.join(ROOT, "coach", "config.py"), "add",
                            "exclude", "zebra client"], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn(b"zebra", p.stdout.lower())
        self.assertEqual(json.loads(p.stdout.decode())["exclude"], "1 rules")
        p = subprocess.run([sys.executable, os.path.join(ROOT, "coach", "config.py"), "show"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertIn(b"zebra client", p.stdout)

    def test_weekly_review_of_days_since_left_out_is_not_on_the_page(self):
        root = os.path.join(os.environ["COACH_HOME"], "reports")
        os.makedirs(os.path.join(root, "weekly"))
        for day in ("2026-11-10", "2026-11-17"):
            with open(os.path.join(root, "weekly", day + ".md"), "w") as fh:
                fh.write("# Coach weekly: %s\n\n## Week in numbers\nweekmark-%s sessions.\n"
                         % (day, day))
        write_config(exclude_dates=["..2026-11-08"])
        page, _card = build_site.build_page(root, self.projects, today="2026-11-18", scan=False)
        self.assertIn("weekmark-2026-11-17", page)
        self.assertNotIn("weekmark-2026-11-10", page)


class OtherProjectTest(Base):
    def test_one_row_for_chats_with_no_folder_and_no_rule(self):
        long_session(self.projects, SID_A, "A quick question")
        t = Transcript(SID_B, "", "2026-11-11", title="Budget notes")
        t.prompt().reply(OPUS, out=1000)
        t.write(self.projects)
        c = Transcript(SID_C, "/work/gamma", "2026-11-11")
        c.prompt().reply(OPUS, out=1000)
        c.write(self.projects)
        self.assertEqual(sorted(names(digest(self.projects, "2026-11-11", "2026-11-12"))),
                         ["A quick question", "Budget notes", "gamma"])
        self.assertIsNone(config.change(["set", "other_project", "One-off", "chats"]))
        rules = [{"name": "Money", "match": ["budget"]}]
        d = digest(self.projects, "2026-11-11", "2026-11-12", rules=rules)
        self.assertEqual(sorted(names(d)), ["Money", "One-off chats", "gamma"])
        ledger.backfill(self.projects, rules=rules)
        self.assertEqual(sorted(ledger_names()), ["Money", "One-off chats", "gamma"])
        self.assertTrue(setup_check.check(os.environ["CLAUDE_CONFIG_DIR"])["other_project"])
        self.assertIsNone(config.change(["unset", "other_project"]))
        self.assertIsNone(common.load_config()["other_project"])


class AccountEvidenceTest(AccountBase):
    def session(self, extra_lines=(), sid=SID_A):
        t = Transcript(sid, "/work/alpha", "2026-11-10")
        t.prompt().reply(OPUS, out=1000)
        t.lines.extend(extra_lines)
        path = t.write(self.projects)
        return accounts.Resolver().session(sid, path)

    def test_each_kind_of_evidence(self):
        s1, s2 = accounts.short(ACCT_1), accounts.short(ACCT_2)
        self.assertEqual((s1, s2, accounts.short("nope"), accounts.short(None)),
                         ("11aa22", "99ff88", "", ""))
        self.assertEqual(self.session().on("2026-11-10"),
                         {"account": "unknown", "org": "", "kind": "none"})
        self.assertEqual(self.session([self.owner(ACCT_1)]).on("2026-11-10"),
                         {"account": s1, "org": "0a0b0c", "kind": "owner"})
        self.assertEqual(self.session([self.bridge(ACCT_2)]).on("2026-11-10")["kind"], "bridge")
        got = self.session([self.autoreact(ACCT_2)]).on("2026-11-10")
        self.assertEqual((got["account"], got["org"], got["kind"]), (s2, "", "ledger"))
        self.app_index(SID_B)
        got = self.session(sid=SID_B).on("2026-11-10")
        self.assertEqual((got["account"], got["kind"]), (s2, "app"))
        accounts.record(SID_C, when=local_noon("2026-11-10"))
        got = self.session(sid=SID_C).on("2026-11-10")
        self.assertEqual((got["account"], got["kind"]), (s1, "hook"))

    def test_precedence_hook_owner_app_bridge_ledger(self):
        lines = [self.autoreact(ACCT_2), self.bridge(ACCT_2), self.owner(ACCT_1)]
        self.app_index(SID_A, ACCT_2)
        self.assertEqual(self.session(lines).on("2026-11-10")["kind"], "owner")
        self.assertEqual(self.session(lines[:2]).on("2026-11-10")["kind"], "bridge")  # dated beats app
        self.assertEqual(self.session(lines[:1]).on("2026-11-10")["kind"], "ledger")
        self.assertEqual(self.session(lines[:2]).on("2026-11-09")["kind"], "app")     # before any date
        accounts.record(SID_A, when=local_noon("2026-11-10"))
        got = self.session(lines).on("2026-11-10")
        self.assertEqual((got["kind"], got["account"]), ("hook", accounts.short(ACCT_1)))

    def test_a_session_that_spans_a_switch_is_answered_per_day(self):
        t = Transcript(SID_A, "/work/alpha", "2026-11-10")
        t.prompt().reply(OPUS, out=1000)
        t.lines.append(self.owner(ACCT_2))
        t.at("2026-11-11").prompt("next day").reply(OPUS, out=1000)
        t.at("2026-11-12").prompt("switched").reply(OPUS, out=1000)
        t.lines.append(self.owner(ACCT_1))
        t.at("2026-11-13").prompt("after").reply(OPUS, out=1000)
        s = accounts.Resolver().session(SID_A, t.write(self.projects))
        got = [s.on(d)["account"] for d in ("2026-11-09", "2026-11-10", "2026-11-11",
                                             "2026-11-12", "2026-11-13")]
        self.assertEqual(got, ["99ff88", "99ff88", "99ff88", "11aa22", "11aa22"])
        self.assertEqual(s.accounts(), set(["99ff88", "11aa22"]))

    def test_the_hook_writes_short_forms_only_and_never_prints(self):
        payload = json.dumps({"session_id": SID_A, "source": "startup", "cwd": "/work/alpha"})
        p = subprocess.run([sys.executable, os.path.join(ROOT, "coach", "accounts.py"), "hook"],
                           input=payload.encode("utf-8"), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""))
        with open(accounts.log_file()) as fh:
            raw = fh.read()
        rec = json.loads(raw)
        self.assertEqual((rec["session"], rec["account"], rec["org"]), (SID_A, "11aa22", "0a0b0c"))
        for secret in (ACCT_1, ORG_1, "example.invalid"):
            self.assertNotIn(secret, raw)
        for stdin in ("not json", json.dumps({"session_id": SID_B, "source": "compact"}), "[]", ""):
            self.assertEqual(accounts.hook_main(io.StringIO(stdin)), 0)
        with open(accounts.log_file()) as fh:
            self.assertEqual(len(fh.readlines()), 1)
        os.remove(self.home_json)
        self.assertEqual(accounts.hook_main(io.StringIO(payload)), 0)     # no login: nothing written
        with open(accounts.log_file()) as fh:
            self.assertEqual(len(fh.readlines()), 1)

    def test_missing_or_odd_files_fail_soft(self):
        os.environ["COACH_APP_SESSIONS_DIR"] = os.path.join(self.tmp, "nowhere")
        os.makedirs(os.path.dirname(accounts.log_file()))
        with open(accounts.log_file(), "wb") as fh:
            fh.write(b"\xff\xfe\n[1]\n{\"session\": 5}\n{\"session\": \"s\", \"ts\": \"x\"}\n")
        with open(self.home_json, "w") as fh:
            fh.write("{broken")
        self.assertEqual(accounts.current(), {"account": "", "org": ""})
        s = self.session([json.dumps({"type": "session-owner", "accountUuid": 7}),
                          '{"type": "session-owner", broken', self.owner("zz-not-hex")])
        self.assertFalse(s.known)


class AccountFilterTest(AccountBase):
    def setUp(self):
        AccountBase.setUp(self)
        mine = Transcript(SID_A, "/work/alpha", "2026-11-10")
        mine.prompt().reply(OPUS, out=100000)
        mine.lines.append(self.owner(ACCT_1))
        mine.write(self.projects)
        other = Transcript(SID_B, "/work/beta", "2026-11-10")
        other.prompt().reply(OPUS, out=200000)
        other.lines.append(self.owner(ACCT_2))
        other.at("2026-11-12").prompt("switched account").reply(OPUS, out=400000)
        other.lines.append(self.owner(ACCT_1))
        other.write(self.projects)
        nobody = Transcript(SID_C, "/work/gamma", "2026-11-10")
        nobody.prompt().reply(OPUS, out=800000)
        nobody.write(self.projects)

    def out_tokens(self):
        led, _ = ledger.load()
        t = ledger.totals(led, led["first_day"], None)
        self.assertAlmostEqual(sum(r["usd"] for r in t["by_project"]), t["usd"], places=6)
        return dict((r["name"], r["out"]) for r in t["by_project"])

    def test_only_account_drops_days_under_another_known_account(self):
        ledger.backfill(self.projects)
        self.assertEqual(self.out_tokens(), {"alpha": 100000, "beta": 600000, "gamma": 800000})
        # "current" from a Terminal session: no host account in the environment, so
        # the .claude.json login. (Inside the desktop app the host's account wins.)
        keys = [k for k in os.environ if k.startswith("CLAUDE_CODE_")]
        saved = dict((k, os.environ.pop(k)) for k in keys)
        try:
            self.assertIsNone(config.change(["set", "only_account", "current"]))
        finally:
            os.environ.update(saved)
        self.assertEqual(common.load_config()["only_account"], "11aa22")
        r = ledger.backfill(self.projects, rebuild=True)
        self.assertEqual(self.out_tokens(), {"alpha": 100000, "beta": 400000, "gamma": 800000})
        self.assertEqual(r["excluded_removed"], 1)
        self.assertEqual((r["filter"]["account"], r["filter"]["unknown"]), (1, 1))
        d = digest(self.projects, "2026-11-10", "2026-11-11")
        self.assertEqual(sorted(names(d)), ["alpha", "gamma"])
        self.assertEqual(d["totals"]["excluded_sessions"], 1)
        d = digest(self.projects, "2026-11-12", "2026-11-13")
        self.assertEqual(names(d), ["beta"])

    def test_unknown_sessions_can_be_included_or_excluded(self):
        write_config(only_account="11aa22", unknown_account="exclude")
        ledger.backfill(self.projects)
        self.assertEqual(self.out_tokens(), {"alpha": 100000, "beta": 400000})
        self.assertIsNone(config.change(["set", "unknown_account", "include"]))
        ledger.update("2026-11-12", "2026-11-12", self.projects)       # settings changed: full read
        # a closed day is never added to, so the unknown session comes back on rebuild of its row only
        self.assertEqual(self.out_tokens()["alpha"], 100000)
        out = setup_check.check(os.environ["CLAUDE_CONFIG_DIR"])["exclusions"]
        self.assertEqual((out["account_filter"], out["sessions_excluded_by_account"],
                          out["sessions_unknown_account"], out["unknown_account"]),
                         (True, 1, 1, "include"))

    def test_date_exclusion_with_open_ended_ranges(self):
        self.assertEqual(common.parse_date_range("2026-11-12..2026-11-10"),
                         ("2026-11-10", "2026-11-12"))
        self.assertIsNone(common.parse_date_range("2026-13-01"))
        for text, left in (("..2026-11-10", {"beta": 400000}),
                           ("2026-11-12..", {"alpha": 100000, "beta": 200000, "gamma": 800000}),
                           ("2026-11-10", {"beta": 400000}),
                           ("2026-11-11..2026-11-11", {"alpha": 100000, "beta": 600000,
                                                       "gamma": 800000})):
            write_config(exclude_dates=[text])
            if os.path.exists(ledger.ledger_file()):
                os.remove(ledger.ledger_file())
            ledger.backfill(self.projects)
            self.assertEqual(self.out_tokens(), left, text)
        self.assertEqual(digest(self.projects, "2026-11-10", "2026-11-11")["totals"]["sessions"], 3)
        write_config(exclude_dates=["..2026-11-10"])
        d = digest(self.projects, "2026-11-10", "2026-11-13")
        self.assertEqual(names(d), ["beta"])
        self.assertEqual(d["totals"]["tokens"]["out"], 400000)

    def test_dates_are_purged_from_an_existing_ledger_and_since_moves(self):
        ledger.backfill(self.projects)
        led, _ = ledger.load()
        self.assertEqual(led["first_day"], "2026-11-10")
        self.assertIsNone(config.change(["add", "exclude_dates", "..2026-11-11"]))
        self.assertTrue(config.change(["add", "exclude_dates", "yesterday"]))
        r = ledger.backfill(self.projects, rebuild=True)
        self.assertEqual((r["first_day"], r["days"], r["excluded_removed"]), ("2026-11-12", 1, 3))
        self.assertEqual(self.out_tokens(), {"beta": 400000})
        out = setup_check.check(os.environ["CLAUDE_CONFIG_DIR"])
        self.assertEqual((out["ledger"]["since"], out["exclusions"]["date_ranges"],
                          out["exclusions"]["sessions_excluded_by_date"]), ("2026-11-12", 1, 3))
        self.assertIsNone(config.change(["remove", "exclude_dates", "..2026-11-11"]))
        self.assertTrue(config.change(["remove", "exclude_dates", "..2026-11-11"]))

    def test_no_full_id_is_ever_written(self):
        write_config(only_account="11aa22", unknown_account="exclude",
                     exclude_dates=["2026-11-11"])
        accounts.record(SID_A)
        import datetime
        out = recap.build(["2026-11-10"], today=datetime.date(2026, 11, 13),
                          projects_dir=self.projects)
        page, card = build_site.build_page(out["reports_root"], self.projects, today="2026-11-13")
        self.assertIn('"oneAccount":true', page.replace(" ", ""))
        self.assertIn("Showing one account only", page)
        home = os.environ["COACH_HOME"]
        blob = page + json.dumps(card) + json.dumps(out) + json.dumps(
            setup_check.check(os.environ["CLAUDE_CONFIG_DIR"]))
        for folder, _dirs, files in os.walk(home):
            for name in files:
                with open(os.path.join(folder, name), "rb") as fh:
                    blob += fh.read().decode("utf-8", "replace")
        for secret in (ACCT_1, ACCT_2, ORG_1, "example.invalid", ACCT_1[:13], ACCT_2[:13]):
            self.assertNotIn(secret, blob)

    def test_scope_fingerprint_changes_with_every_setting(self):
        sigs = set()
        for cfg in ({}, {"exclude": [{"match": ["a text"]}]}, {"only_account": "11aa22"},
                    {"only_account": "11aa22", "unknown_account": "exclude"},
                    {"exclude_dates": [("2026-01-01", None)]}):
            sigs.add(scope.Scope(cfg).sig())
        self.assertEqual(len(sigs), 5)
        self.assertIn("", sigs)


class PlanTest(Base):
    def test_scheduled_word_marks_the_run_unattended(self):
        import datetime
        long_session(self.projects, cwd="/work/alpha")
        today = datetime.date(2026, 11, 13)
        out = recap.build(["week"], today=today, projects_dir=self.projects)
        self.assertFalse(out["unattended"])
        self.assertTrue(out["fixes"]["apply_allowed"])
        for key in ("followthrough", "evidence", "references", "compare", "config_audit"):
            self.assertIn(key, out)
        for path in list(out["references"].values()) + out["followthrough"] + out["evidence"][:1]:
            self.assertTrue(os.path.isfile(path), path)
        self.assertTrue(out["compare"] is None or os.path.isfile(out["compare"]))
        self.assertTrue(out["config_audit"] is None or os.path.isfile(out["config_audit"]))
        out = recap.build([], today=today, projects_dir=self.projects, unattended=True)
        self.assertTrue(out["unattended"])
        self.assertFalse(out["fixes"]["apply_allowed"])
        self.assertTrue(os.path.isfile(os.path.join(os.environ["COACH_HOME"], "unattended")))
        for task in setup_check.TASKS:
            self.assertIn(" scheduled and follow it exactly", setup_check.task_prompt(task))
            self.assertIn("never apply or undo a fix", setup_check.task_prompt(task))

    def test_page_leaves_out_a_daily_report_of_an_excluded_date(self):
        root = os.path.join(os.environ["COACH_HOME"], "reports")
        os.makedirs(os.path.join(root, "daily"))
        for day in ("2026-11-10", "2026-11-11"):
            with open(os.path.join(root, "daily", day + ".md"), "w") as fh:
                fh.write("# Coach: %s\n\n1. What happened: marker-%s happened.\n"
                         "   Do instead: something else.\n" % (day, day))
        write_config(exclude_dates=["..2026-11-10"])
        page, _card = build_site.build_page(root, self.projects, today="2026-11-12", scan=False)
        self.assertIn("marker-2026-11-11", page)
        self.assertNotIn("marker-2026-11-10", page)


if __name__ == "__main__":
    unittest.main()
