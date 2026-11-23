"""Stdlib tests for the account rules: which evidence names the account a
session really ran under, desktop app or Terminal. Made-up ids only.

Run: python3 -m unittest discover -s tests -v
"""
import io
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "coach"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import accounts  # noqa: E402
from test_cost import Base, local_noon  # noqa: E402

SID = "5e55a000-0000-4000-8000-000000000001"
SID_2 = "5e55b000-0000-4000-8000-000000000002"
OTHER_SID = "07e40000-0000-4000-8000-000000000009"
# made-up accounts: WORK is the desktop app's, HOME the one Terminal signed into
WORK = "abc123de-0000-4000-8000-00000000000a"
WORK_ORG = "def456ab-0000-4000-8000-00000000000b"
HOME = "fed987cb-0000-4000-8000-00000000000c"
HOME_ORG = "cab654ed-0000-4000-8000-00000000000d"
W, WO, H, HO = "abc123", "def456", "fed987", "cab654"
APP_ID = "local_a1a1a1a1-0000-4000-8000-0000000000ee"
DAY_1, DAY_2, DAY_3 = "2026-11-21", "2026-11-22", "2026-11-23"


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + ".000Z"


class AccountsBase(Base):
    def setUp(self):
        Base.setUp(self)
        self.app = os.path.join(self.tmp, "app-sessions")
        os.environ["COACH_APP_SESSIONS_DIR"] = self.app
        os.makedirs(os.environ["CLAUDE_CONFIG_DIR"])
        self.home_json = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], ".claude.json")
        self.login(HOME, HOME_ORG)

    def login(self, acct, org):
        with open(self.home_json, "w") as fh:
            json.dump({"oauthAccount": {"accountUuid": acct, "organizationUuid": org,
                                        "emailAddress": "someone@example.invalid"}}, fh)

    def app_file(self, name=APP_ID, acct=WORK, org=WORK_ORG, pad_before=0, pad_after=0, **fields):
        """One app index file; padding stands in for the large MCP settings block."""
        folder = os.path.join(self.app, acct, org)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        body = {}
        if pad_before:
            body["remoteMcpServersConfig"] = ["x" * pad_before]
        body.update(fields)
        if pad_after:
            body["toolSurfaceSnapshot"] = ["y" * pad_after]
        path = os.path.join(folder, name + ".json")
        with open(path, "w") as fh:
            json.dump(body, fh)
        return path

    def log_line(self, day, acct, org, sid=SID, **extra):
        """A hook log line as written by hand: older lines carry no via or entry."""
        path = accounts.log_file()
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        rec = dict({"session": sid, "ts": local_noon(day), "account": acct[:6], "org": org[:6]},
                   **extra)
        with open(path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")

    def transcript(self, days, sid=SID, ledger=()):
        """days: [(day, entrypoint)]; ledger: [(day, account)] appended after that day's record."""
        folder = os.path.join(self.projects, "-work-alpha")
        if not os.path.isdir(folder):
            os.makedirs(folder)
        lines = []
        for day, entry in days:
            rec = {"type": "user", "timestamp": iso(local_noon(day)), "sessionId": sid,
                   "message": {"role": "user", "content": "hello"}}
            if entry:
                rec["entrypoint"] = entry
            lines.append(json.dumps(rec, separators=(",", ":")))
            for lday, acct in ledger:
                if lday == day:
                    lines.append(json.dumps({"type": "artifact-autoreact-ledger", "v": 1,
                                             "sessionId": sid, "accountUuid": acct,
                                             "artifacts": {}}, separators=(",", ":")))
        path = os.path.join(folder, sid + ".jsonl")
        with open(path, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        return path

    def logged(self):
        try:
            with open(accounts.log_file()) as fh:
                return [json.loads(x) for x in fh if x.strip()]
        except OSError:
            return []

    def hook(self, env, sid=SID, source="startup"):
        payload = json.dumps({"session_id": sid, "source": source, "cwd": "/work/alpha"})
        self.assertEqual(accounts.hook_main(io.StringIO(payload), env=env), 0)
        return self.logged()


def desktop_env(**extra):
    env = {"CLAUDE_CODE_ENTRYPOINT": "claude-desktop", "CLAUDE_CODE_SESSION_ID": SID}
    env.update(extra)
    return env


class HookTest(AccountsBase):
    def test_the_account_the_host_gives_wins_over_the_login(self):
        lines = self.hook(desktop_env(CLAUDE_CODE_ACCOUNT_UUID=WORK,
                                      CLAUDE_CODE_ORGANIZATION_UUID=WORK_ORG))
        self.assertEqual([(r["account"], r["org"], r["via"], r["entry"]) for r in lines],
                         [(W, WO, "env", "claude-desktop")])
        with open(accounts.log_file()) as fh:
            raw = fh.read()
        for secret in (WORK, WORK_ORG, HOME, "example.invalid"):
            self.assertNotIn(secret, raw)

    def test_the_environment_of_another_session_is_not_used(self):
        env = desktop_env(CLAUDE_CODE_SESSION_ID=OTHER_SID, CLAUDE_CODE_ACCOUNT_UUID=WORK,
                          CLAUDE_CODE_ORGANIZATION_UUID=WORK_ORG,
                          CLAUDE_CODE_HOST_SESSION_ID=APP_ID)
        self.app_file()
        lines = self.hook(env)
        self.assertEqual([(r["account"], r["via"], "entry" in r) for r in lines],
                         [(H, "login", False)])

    def test_a_desktop_session_is_found_in_the_app_index_by_the_apps_own_id(self):
        self.app_file()                      # the app has not written the session id yet
        lines = self.hook(desktop_env(CLAUDE_CODE_HOST_SESSION_ID=APP_ID))
        self.assertEqual([(r["account"], r["org"], r["via"]) for r in lines], [(W, WO, "app")])

    def test_a_desktop_session_is_found_in_the_app_index_by_its_session_id(self):
        self.app_file(name="local_b2b2b2b2", cliSessionId=SID, pad_after=300000)
        lines = self.hook(desktop_env())
        self.assertEqual([(r["account"], r["via"]) for r in lines], [(W, "app")])

    def test_a_desktop_session_with_no_desktop_evidence_writes_nothing(self):
        self.app_file(cliSessionId=SID_2)    # another session's file
        self.assertEqual(self.hook(desktop_env(CLAUDE_CODE_HOST_SESSION_ID="local_nothere")), [])

    def test_terminal_and_older_claude_code_use_the_login(self):
        lines = self.hook({"CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_SESSION_ID": SID})
        self.assertEqual([(r["account"], r["org"], r["via"], r["entry"]) for r in lines],
                         [(H, HO, "login", "cli")])
        lines = self.hook({}, sid=SID_2)
        self.assertEqual((lines[-1]["account"], lines[-1]["via"], "entry" in lines[-1]),
                         (H, "login", False))

    def test_compact_is_skipped_and_no_login_writes_nothing(self):
        self.assertEqual(self.hook({}, source="compact"), [])
        os.remove(self.home_json)
        self.assertEqual(self.hook({"CLAUDE_CODE_ENTRYPOINT": "cli"}), [])

    def test_the_hook_script_prints_nothing(self):
        env = dict((k, v) for k, v in os.environ.items() if not k.startswith("CLAUDE_CODE"))
        env.update(desktop_env(CLAUDE_CODE_ACCOUNT_UUID=WORK, CLAUDE_CODE_ORGANIZATION_UUID=WORK_ORG))
        payload = json.dumps({"session_id": SID, "source": "resume"})
        p = subprocess.run([sys.executable, os.path.join(ROOT, "coach", "accounts.py"), "hook"],
                           input=payload.encode("utf-8"), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=env)
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""))
        self.assertEqual([(r["account"], r["via"]) for r in self.logged()], [(W, "env")])


class AppIndexTest(AccountsBase):
    def test_a_large_index_file_is_read_and_dated_by_its_creation(self):
        created = local_noon(DAY_1) * 1000
        self.app_file(cliSessionId=SID, lastAssistantCliSessionId=SID_2, createdAt=created,
                      pad_after=400000)
        idx = accounts._read_app_index()
        self.assertEqual(idx[SID], (W, WO, created / 1000.0))
        self.assertEqual(idx[SID_2][:2], (W, WO))

    def test_ids_anywhere_in_the_file_are_found_and_nested_ones_ignored(self):
        self.app_file(pad_before=300000, cliSessionId=SID, pad_after=300000,
                      lastAssistantCliSessionId=SID_2, createdAt=local_noon(DAY_2) * 1000,
                      children=[{"cliSessionId": OTHER_SID, "createdAt": local_noon(DAY_1) * 1000}])
        idx = accounts._read_app_index()
        self.assertEqual(sorted(idx), sorted([SID, SID_2]))
        self.assertEqual(accounts._day(idx[SID][2]), DAY_2)

    def test_a_sessions_own_file_wins_over_another_files_last_id(self):
        # the other file is read first (WORK's folder sorts before HOME's); the own file still wins
        self.app_file(name="local_00000000", cliSessionId=SID_2, lastAssistantCliSessionId=SID)
        self.app_file(name="local_ffffffff", acct=HOME, org=HOME_ORG, cliSessionId=SID)
        self.app_file(name="local_99999999", acct=HOME, org=HOME_ORG,
                      lastAssistantCliSessionId=OTHER_SID)
        idx = accounts._read_app_index()
        self.assertEqual((idx[SID][0], idx[SID_2][0], idx[OTHER_SID][0]), (H, W, H))

    def test_an_impossible_creation_time_is_not_a_date(self):
        for value in (1e30, 4e10, 10 ** 400, True, "soon", -5, 12):
            self.app_file(cliSessionId=SID, createdAt=value)
            self.assertIsNone(accounts._read_app_index()[SID][2], value)
            self.assertEqual(accounts.Resolver().session(SID).on(DAY_3)["kind"], "app")

    def test_odd_files_are_skipped(self):
        old = accounts.APP_MAX_BYTES
        accounts.APP_MAX_BYTES = 100000
        try:
            self.app_file(cliSessionId=SID, pad_before=200000)
            self.app_file(name="deleted_c3c3c3c3", cliSessionId=SID_2)
            folder = os.path.join(self.app, WORK, WORK_ORG)
            with open(os.path.join(folder, "local_broken.json"), "w") as fh:
                fh.write("{not json")
            self.assertEqual(accounts._read_app_index(), {})
        finally:
            accounts.APP_MAX_BYTES = old
        self.assertIsNone(accounts.app_lookup(None, "../../" + APP_ID, folder=self.app))
        self.assertEqual(accounts.app_lookup(None, APP_ID, folder=self.app), (W, WO))


class RulesTest(AccountsBase):
    def session(self, sid=SID, transcript=None):
        return accounts.Resolver().session(sid, transcript)

    def test_terminal_login_does_not_tag_a_desktop_session(self):
        # the incident: Terminal signs into HOME; the hook and the ledger repeat HOME
        path = self.transcript([(DAY_1, "claude-desktop"), (DAY_3, "claude-desktop")],
                               ledger=[(DAY_1, WORK), (DAY_3, HOME)])
        self.app_file(cliSessionId=SID, createdAt=local_noon(DAY_1) * 1000)
        self.log_line(DAY_1, WORK, WORK_ORG)
        self.log_line(DAY_3, HOME, HOME_ORG)
        s = self.session(transcript=path)
        self.assertEqual([(s.on(d)["account"], s.on(d)["kind"]) for d in (DAY_1, DAY_2, DAY_3)],
                         [(W, "app")] * 3)
        self.assertEqual(s.accounts(), set([W]))

    def test_an_undated_app_entry_also_beats_the_login(self):
        path = self.transcript([(DAY_3, "claude-desktop")], ledger=[(DAY_3, HOME)])
        self.app_file(cliSessionId=SID)
        self.log_line(DAY_3, HOME, HOME_ORG)
        self.assertEqual(self.session(transcript=path).on(DAY_3),
                         {"account": W, "org": WO, "kind": "app"})

    def test_a_desktop_session_with_only_login_evidence_is_unknown(self):
        path = self.transcript([(DAY_3, "claude-desktop")], ledger=[(DAY_3, HOME)])
        self.log_line(DAY_3, HOME, HOME_ORG)
        s = self.session(transcript=path)
        self.assertEqual(s.on(DAY_3), {"account": "unknown", "org": "", "kind": "none"})
        self.assertFalse(s.known)

    def test_a_host_line_is_trusted_on_a_desktop_day(self):
        path = self.transcript([(DAY_3, "claude-desktop")], ledger=[(DAY_3, HOME)])
        self.log_line(DAY_3, WORK, WORK_ORG, via="env", entry="claude-desktop")
        got = self.session(transcript=path).on(DAY_3)
        self.assertEqual((got["account"], got["kind"]), (W, "hook"))

    def test_a_login_line_that_says_desktop_is_login_even_without_a_transcript(self):
        self.log_line(DAY_3, HOME, HOME_ORG, via="login", entry="claude-desktop")
        self.app_file(cliSessionId=SID)
        self.assertEqual(self.session().on(DAY_3)["kind"], "app")

    def test_a_terminal_session_keeps_the_login_and_the_ledger(self):
        path = self.transcript([(DAY_3, "cli")], ledger=[(DAY_3, HOME)])
        self.assertEqual(self.session(transcript=path).on(DAY_3)["kind"], "ledger")
        self.log_line(DAY_3, HOME, HOME_ORG)
        got = self.session(transcript=path).on(DAY_3)
        self.assertEqual((got["account"], got["kind"]), (H, "hook"))

    def test_without_a_transcript_an_older_line_is_a_hook_line(self):
        self.log_line(DAY_3, HOME, HOME_ORG)
        self.assertEqual(self.session().on(DAY_3)["kind"], "hook")

    def test_without_a_transcript_the_app_index_marks_a_desktop_session(self):
        self.log_line(DAY_3, HOME, HOME_ORG)
        self.app_file(cliSessionId=SID, createdAt=local_noon(DAY_1) * 1000)
        got = self.session().on(DAY_3)
        self.assertEqual((got["account"], got["kind"]), (W, "app"))
        # same answer as with a transcript that names the desktop that day
        path = self.transcript([(DAY_3, "claude-desktop")])
        self.assertEqual(self.session(transcript=path).on(DAY_3), got)

    def test_a_desktop_session_resumed_in_terminal_is_answered_per_day(self):
        path = self.transcript([(DAY_1, "claude-desktop"), (DAY_3, "cli")])
        self.app_file(cliSessionId=SID, createdAt=local_noon(DAY_1) * 1000)
        self.log_line(DAY_1, HOME, HOME_ORG)                     # login on a desktop day
        self.log_line(DAY_3, HOME, HOME_ORG, via="login", entry="cli")
        s = self.session(transcript=path)
        self.assertEqual([(s.on(d)["account"], s.on(d)["kind"]) for d in (DAY_1, DAY_2, DAY_3)],
                         [(W, "app"), (W, "app"), (H, "hook")])

    def test_a_day_with_no_records_follows_the_sessions_other_days(self):
        pts = [(local_noon(DAY_3), H, HO, accounts._MAYBE)]
        self.assertEqual(accounts.classify(pts, {DAY_1: set(["claude-desktop"])})[0][3], "login")
        self.assertEqual(accounts.classify(pts, {DAY_1: set(["cli"])})[0][3], "hook")
        self.assertEqual(accounts.classify(pts, {DAY_3: set(["cli", "claude-desktop"])})[0][3],
                         "login")
        self.assertEqual(accounts.classify(pts, {})[0][3], "hook")

    def test_transcript_evidence_reads_the_entrypoint_per_day(self):
        path = self.transcript([(DAY_1, "claude-desktop"), (DAY_3, "cli"), (DAY_2, "")],
                               ledger=[(DAY_3, HOME)])
        points, entries = accounts.transcript_evidence(path)
        self.assertEqual(entries, {DAY_1: set(["claude-desktop"]), DAY_3: set(["cli"])})
        self.assertEqual([(p[1], p[3]) for p in points], [(H, "ledger")])
        self.assertEqual(accounts.transcript_points(path), points)
        self.assertTrue(accounts.is_desktop("claude-desktop"))
        self.assertFalse(accounts.is_desktop("cli") or accounts.is_desktop(None))


if __name__ == "__main__":
    import unittest
    unittest.main()
