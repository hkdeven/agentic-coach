"""Tests for coach/config_audit.py on synthetic config trees and transcripts.

Run: python3 -m unittest discover -s tests -v
"""
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

import config_audit as A  # noqa: E402

# "Now" sits well ahead of the real clock, so files written by the test count
# as installed long ago and only what the test dates on purpose is new.
NOW = time.time() + 60 * 86400
DAY = 86400
MODEL = "claude-test-1"


def write(path, text):
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    with open(path, "w") as fh:
        fh.write(text)
    return path


def skill_md(name, desc, extra=""):
    return "---\nname: %s\ndescription: %s\n%s---\n\nBody of %s.\n" % (name, desc, extra, name)


def iso(ep):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ep)) + ".000Z"


class Transcript(object):
    """Writes records in the shapes Claude Code uses."""

    def __init__(self, sid, cwd, start):
        self.sid = sid
        self.cwd = cwd
        self.t = start
        self.n = 0
        self.lines = []

    def _add(self, rec):
        self.n += 1
        self.t += 20
        rec.update({"uuid": "%s-%d" % (self.sid[:6], self.n), "sessionId": self.sid,
                    "cwd": self.cwd, "isSidechain": False, "timestamp": iso(self.t)})
        self.lines.append(json.dumps(rec, separators=(",", ":")))
        return self

    def prompt(self, text):
        return self._add({"type": "user", "origin": {"kind": "human"},
                          "message": {"role": "user", "content": text}})

    def slash(self, name):
        return self._add({"type": "user", "message": {
            "role": "user", "content": "<command-name>/%s</command-name>" % name}})

    def call(self, tool, inp=None, model=MODEL):
        self.n += 1
        return self._add({"type": "assistant", "message": {
            "id": "msg_%s_%d" % (self.sid[:6], self.n), "model": model, "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu_%d" % self.n, "name": tool,
                         "input": inp or {}}],
            "usage": {"input_tokens": 5, "output_tokens": 20,
                      "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 10}}})

    def attach(self, body):
        return self._add({"type": "attachment", "attachment": body})

    def stop_summary(self, commands):
        return self._add({"type": "system", "subtype": "stop_hook_summary",
                          "hookCount": len(commands),
                          "hookInfos": [{"command": c} for c in commands]})

    def save(self, projects_dir, pdir):
        path = write(os.path.join(projects_dir, pdir, self.sid + ".jsonl"),
                     "\n".join(self.lines) + "\n")
        os.utime(path, (NOW, NOW))
        return path


class Tree(unittest.TestCase):
    """A full synthetic setup: user level, one project, one plugin, transcripts."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.claude = os.path.join(self.tmp, "claude")
        self.proj = os.path.join(self.tmp, "work", "app")
        self.other = os.path.join(self.tmp, "work", "site")
        self.managed = os.path.join(self.tmp, "managed")
        self.home_json = os.path.join(self.claude, ".claude.json")
        self.prices = write(os.path.join(self.tmp, "prices.json"), json.dumps({
            "retrieved": "2026-01-01",
            "models": {MODEL: {"cache_read": 0.5, "cache_write_5m": 5.0}}}))
        c = self.claude
        os.makedirs(self.other)

        # memory
        write(os.path.join(c, "CLAUDE.md"),
              "# Rules\nBe brief.\n@notes.md\nNot an import: `@secret.md`\n```\n@fenced.md\n```\n")
        write(os.path.join(c, "notes.md"), "Imported notes.\n")
        write(os.path.join(c, "secret.md"), "never loaded\n")
        write(os.path.join(c, "fenced.md"), "never loaded\n")
        write(os.path.join(c, "rules", "always.md"), "Always applies.\n")
        write(os.path.join(c, "rules", "scoped.md"), "---\npaths:\n  - src/**\n---\nOnly in src.\n")
        write(os.path.join(self.proj, "CLAUDE.md"), "line of project rules\n" * 300)
        write(os.path.join(c, "projects", "-work-app", "memory", "MEMORY.md"), "- a note\n" * 10)
        write(os.path.join(self.managed, "CLAUDE.md"), "Company rules.\n")

        # skills, commands, agents, styles
        write(os.path.join(c, "skills", "alpha", "SKILL.md"), skill_md("alpha", "Does alpha things"))
        write(os.path.join(c, "skills", "beta", "SKILL.md"), skill_md("beta", "B" * 400))
        write(os.path.join(c, "skills", "manual", "SKILL.md"),
              skill_md("manual", "Typed only", "disable-model-invocation: true\n"))
        write(os.path.join(c, "commands", "old.md"), "---\ndescription: An old command\n---\nDo it.\n")
        write(os.path.join(c, "agents", "reviewer.md"),
              "---\nname: reviewer\ndescription: Reviews code\n---\nYou review.\n")
        write(os.path.join(c, "agents", "idle.md"),
              "---\nname: idle\ndescription: Never launched\n---\nYou wait.\n")
        write(os.path.join(c, "output-styles", "terse.md"),
              "---\nname: terse\ndescription: Short\n---\nBe terse.\n")
        write(os.path.join(c, "output-styles", "long.md"),
              "---\nname: long\ndescription: Long\n---\nBe long.\n")
        write(os.path.join(self.proj, ".claude", "skills", "alpha", "SKILL.md"),
              skill_md("alpha", "A project copy of alpha"))
        write(os.path.join(self.proj, ".claude", "agents", "local-agent.md"),
              "---\nname: local-agent\ndescription: Project agent\n---\nWork.\n")

        # plugins
        self.plug = os.path.join(c, "plugins", "cache", "mk", "tools", "1.0.0")
        write(os.path.join(self.plug, ".claude-plugin", "plugin.json"), json.dumps({"name": "tools"}))
        write(os.path.join(self.plug, "skills", "deploy", "SKILL.md"), skill_md("deploy", "Ships it"))
        write(os.path.join(self.plug, "agents", "helper.md"),
              "---\nname: helper\ndescription: Helps\n---\nHelp.\n")
        write(os.path.join(self.plug, "hooks", "hooks.json"), json.dumps({"hooks": {"Stop": [
            {"hooks": [{"type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}/stop.sh\""}]}]}}))
        write(os.path.join(self.plug, ".mcp.json"), json.dumps({"mcpServers": {"api": {"command": "x"}}}))
        fresh = os.path.join(c, "plugins", "cache", "mk", "fresh", "0.1.0")
        write(os.path.join(fresh, "skills", "shiny", "SKILL.md"), skill_md("shiny", "Brand new"))
        stale = os.path.join(c, "plugins", "cache", "mk", "stale", "2.0.0")
        write(os.path.join(stale, "skills", "dusty", "SKILL.md"), skill_md("dusty", "D" * 300))
        guard = os.path.join(c, "plugins", "cache", "mk", "guard", "1.0.0")
        write(os.path.join(guard, "hooks", "hooks.json"), json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.sh"}]}]}}))
        off = os.path.join(c, "plugins", "cache", "mk", "off", "1.0.0")
        write(os.path.join(off, "skills", "hidden", "SKILL.md"), skill_md("hidden", "Off"))
        write(os.path.join(c, "plugins", "installed_plugins.json"), json.dumps({
            "version": 2, "plugins": {
                "tools@mk": [{"scope": "user", "installPath": self.plug,
                              "installedAt": iso(NOW - 90 * DAY)}],
                "fresh@mk": [{"scope": "user", "installPath": fresh,
                              "installedAt": iso(NOW - 5 * DAY)}],
                "stale@mk": [{"scope": "user", "installPath": stale,
                              "installedAt": iso(NOW - 90 * DAY)}],
                "guard@mk": [{"scope": "user", "installPath": guard,
                              "installedAt": iso(NOW - 90 * DAY)}],
                "off@mk": [{"scope": "user", "installPath": off,
                            "installedAt": iso(NOW - 90 * DAY)}]}}))

        # settings, hooks, MCP
        write(os.path.join(c, "settings.json"), json.dumps({
            "outputStyle": "terse",
            "enabledPlugins": {"tools@mk": True, "fresh@mk": True, "stale@mk": True,
                               "guard@mk": True, "off@mk": False},
            "hooks": {
                "PreToolUse": [{"matcher": "Bash", "hooks": [
                    {"type": "command", "command": "block-push.sh",
                     "statusMessage": "Checking for push"}]}],
                "Stop": [{"hooks": [{"type": "command", "command": "after-stop.sh"}]}],
                "PostToolUse": [{"matcher": "Write", "hooks": [
                    {"type": "command", "command": "never-runs.sh"}]}]}}))
        write(self.home_json, json.dumps({
            "mcpServers": {"big": {"type": "http", "url": "https://example.com/mcp"},
                           "used": {"command": "used-server"},
                           "ghost": {"command": "ghost-server"}},
            "projects": {self.proj: {"mcpServers": {"localsrv": {"command": "l"}},
                                     "enabledMcpjsonServers": ["shared", "used"]},
                         os.path.join(self.tmp, "deleted"): {}},
            "skillUsage": {"beta": {"usageCount": 3, "lastUsedAt": (NOW - 50 * DAY) * 1000}}}))
        write(os.path.join(self.proj, ".mcp.json"), json.dumps({"mcpServers": {
            "shared": {"command": "s"}, "pending": {"command": "p"}, "used": {"command": "dup"}}}))
        write(os.path.join(self.proj, ".claude", "settings.json"), json.dumps({
            "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "hello.sh"}]}]}}))
        write(os.path.join(self.managed, "managed-mcp.json"),
              json.dumps({"mcpServers": {"corp": {"type": "http", "url": "https://example.com"}}}))

        self.projects_dir = os.path.join(c, "projects")
        self._transcripts()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _transcripts(self):
        big_tools = ["mcp__big__tool_%02d" % i for i in range(25)]
        listing = ("- alpha: Does alpha things\n- beta: " + "B" * 400 + "\n"
                   "- tools:deploy: Ships it\n- stale:dusty: " + "D" * 300 + "\n"
                   "- builtin-thing: Comes with the app\n- cloud:sync: From the account\n")
        a = Transcript("aaaaaaaa-0000-0000-0000-000000000001", self.proj, NOW - 10 * DAY)
        a.attach({"type": "skill_listing", "content": listing, "isInitial": True})
        a.attach({"type": "deferred_tools_delta",
                  "addedNames": big_tools + ["mcp__used__do", "mcp__plugin_tools_api__ping", "Other"],
                  "addedLines": []})
        a.attach({"type": "mcp_instructions_delta", "addedNames": ["big"], "addedBlocks": ["i" * 400]})
        a.attach({"type": "agent_listing_delta", "addedTypes": ["general-purpose", "reviewer"],
                  "addedLines": ["- general-purpose: does anything", "- reviewer: Reviews code"],
                  "builtInTypes": ["general-purpose"]})
        a.attach({"type": "deferred_tools_record", "entries": [
            {"name": "mcp__used__do", "description": "d" * 800, "input_schema": {"type": "object"}}]})
        a.prompt("please do the alpha thing for this project")
        a.call("Skill", {"skill": "alpha"})
        a.call("Skill", {"skill": "alpha"})
        a.call("mcp__used__do", {"q": 1})
        a.call("Agent", {"subagent_type": "reviewer", "prompt": "x"})
        a.call("Bash", {"command": "ls"})
        a.attach({"type": "hook_success", "hookEvent": "PreToolUse", "hookName": "PreToolUse:Bash",
                  "command": "Checking for push", "exitCode": 0})
        a.slash("old")
        a.call("Skill", {"skill": "tools:deploy"})
        a.stop_summary(["after-stop.sh", "\"${CLAUDE_PLUGIN_ROOT}/stop.sh\""])
        a.save(self.projects_dir, "-work-app")
        b = Transcript("bbbbbbbb-0000-0000-0000-000000000002", self.other, NOW - 3 * DAY)
        b.attach({"type": "skill_listing", "content": listing, "isInitial": True})
        b.prompt("a different project entirely, nothing special here")
        b.call("Bash", {"command": "ls"})
        b.call("Bash", {"command": "pwd"})
        b.call("mcp__plugin_tools_api__ping", {})
        b.stop_summary(["after-stop.sh"])
        b.save(self.projects_dir, "-work-site")
        self.turns_a, self.turns_b = 6, 3

    def run_audit(self, **kw):
        args = dict(days=30, claude=self.claude, home_json=self.home_json,
                    managed=self.managed, prices_path=self.prices, now=NOW)
        args.update(kw)
        return A.audit(**args)

    def item(self, report, kind, name, scope=None):
        hits = [i for i in report["items"] if i["kind"] == kind and i["name"] == name
                and (scope is None or i["scope"] == scope)]
        self.assertEqual(len(hits), 1, "%s %s: %d found" % (kind, name, len(hits)))
        return hits[0]


class InventoryTest(Tree):

    def kinds(self, inv, kind, scope=None):
        return sorted(i["name"] for i in inv.items if i["kind"] == kind
                      and (scope is None or i["scope"] == scope))

    def test_every_kind_is_listed(self):
        inv = A.inventory(self.claude, self.home_json, [self.proj], self.managed)
        self.assertEqual(self.kinds(inv, "skill", "user"), ["alpha", "beta", "manual"])
        self.assertEqual(self.kinds(inv, "skill", "project"), ["alpha"])
        self.assertEqual(self.kinds(inv, "skill", "plugin"),
                         ["fresh:shiny", "stale:dusty", "tools:deploy"])
        self.assertEqual(self.kinds(inv, "command"), ["old"])
        self.assertEqual(self.kinds(inv, "agent"),
                         ["idle", "local-agent", "reviewer", "tools:helper"])
        self.assertEqual(self.kinds(inv, "output_style"), ["long", "terse"])
        self.assertEqual(self.kinds(inv, "plugin"),
                         ["fresh@mk", "guard@mk", "off@mk", "stale@mk", "tools@mk"])
        self.assertEqual(self.kinds(inv, "mcp", "user"), ["big", "ghost", "used"])
        self.assertEqual(self.kinds(inv, "mcp", "project"), ["pending", "shared", "used"])
        self.assertEqual(self.kinds(inv, "mcp", "local"), ["localsrv"])
        self.assertEqual(self.kinds(inv, "mcp", "plugin"), ["api"])
        self.assertEqual(self.kinds(inv, "mcp", "managed"), ["corp"])
        hooks = [(i["scope"], i["event"]) for i in inv.items if i["kind"] == "hook"]
        self.assertEqual(sorted(hooks), [("plugin", "PreToolUse"), ("plugin", "Stop"),
                                         ("project", "SessionStart"), ("user", "PostToolUse"),
                                         ("user", "PreToolUse"), ("user", "Stop")])
        self.assertEqual(inv.problems, [])

    def test_disabled_plugin_brings_nothing(self):
        inv = A.inventory(self.claude, self.home_json, [], self.managed)
        self.assertNotIn("off:hidden", [i["name"] for i in inv.items])
        off = [i for i in inv.items if i["name"] == "off@mk"][0]
        self.assertFalse(off["enabled"])
        self.assertEqual(off["tokens_per_turn"], 0)

    def test_memory_files_sizes_and_imports(self):
        inv = A.inventory(self.claude, self.home_json, [self.proj], self.managed)
        mem = dict((os.path.basename(i["path"]) + ":" + i["role"], i)
                   for i in inv.items if i["kind"] == "memory")
        self.assertIn("notes.md:import", mem)
        self.assertEqual(mem["notes.md:import"]["imported_by"].split("/")[-1], "CLAUDE.md")
        self.assertFalse(any(k.startswith(("secret.md", "fenced.md")) for k in mem))
        self.assertTrue(mem["always.md:rule"]["always_on"])
        self.assertFalse(mem["scoped.md:rule"]["always_on"])
        self.assertEqual(mem["scoped.md:rule"]["tokens_per_turn"], 0)
        proj = mem["CLAUDE.md:project"]
        self.assertEqual(proj["lines"], 300)
        self.assertEqual(proj["chars"], 300 * len("line of project rules\n"))
        self.assertEqual(mem["CLAUDE.md:managed"]["scope"], "managed")
        self.assertEqual(mem["MEMORY.md:auto"]["pdir"], "-work-app")

    def test_typed_only_skill_is_not_in_the_listing(self):
        inv = A.inventory(self.claude, self.home_json, [], self.managed)
        manual = [i for i in inv.items if i["name"] == "manual"][0]
        self.assertFalse(manual["always_on"])
        self.assertEqual(manual["tokens_per_turn"], 0)

    def test_project_mcp_approval_states(self):
        inv = A.inventory(self.claude, self.home_json, [self.proj], self.managed)
        state = dict((i["name"], i["enabled"]) for i in inv.items
                     if i["kind"] == "mcp" and i["scope"] == "project")
        self.assertIs(state["shared"], True)
        self.assertIsNone(state["pending"])

    def test_selected_output_style_only(self):
        inv = A.inventory(self.claude, self.home_json, [], self.managed)
        styles = dict((i["name"], i) for i in inv.items if i["kind"] == "output_style")
        self.assertTrue(styles["terse"]["always_on"])
        self.assertGreater(styles["terse"]["tokens_per_turn"], 0)
        self.assertFalse(styles["long"]["always_on"])

    def test_deleted_project_folder_is_skipped(self):
        inv = A.inventory(self.claude, self.home_json, [os.path.join(self.tmp, "deleted")],
                          self.managed)
        self.assertEqual(len(inv.projects), 1)

    def test_plugin_manifest_paths_must_stay_inside(self):
        outside = write(os.path.join(self.tmp, "outside", "evil", "SKILL.md"),
                        skill_md("evil", "outside the plugin"))
        write(os.path.join(self.plug, ".claude-plugin", "plugin.json"), json.dumps({
            "name": "tools", "skills": ["../../../../../../outside", "./extra"],
            "mcpServers": {"inline": {"command": "y"}}}))
        write(os.path.join(self.plug, "extra", "more", "SKILL.md"), skill_md("more", "extra"))
        inv = A.inventory(self.claude, self.home_json, [], self.managed)
        names = [i["name"] for i in inv.items]
        self.assertIn("tools:more", names)
        self.assertIn("tools:deploy", names)
        self.assertNotIn("tools:evil", names)
        self.assertIn("inline", names)
        self.assertTrue(os.path.isfile(outside))
        self.assertEqual(os.path.normpath(os.path.join(self.plug, "../../../../../../outside")),
                         os.path.dirname(os.path.dirname(outside)))
        self.assertIsNone(A._inside(self.plug, "../../../../../../outside"))
        self.assertIsNotNone(A._inside(self.plug, "./extra"))


class UsageTest(Tree):

    def test_counts_from_transcripts(self):
        u = A.scan_usage(NOW - 30 * DAY, NOW, self.projects_dir)
        self.assertEqual(u["sessions"], 2)
        self.assertEqual(u["requests"], self.turns_a + self.turns_b)
        self.assertEqual(u["prompts"], 2)
        self.assertEqual(u["skills"]["alpha"]["n"], 2)
        self.assertEqual(u["skills"]["tools:deploy"]["n"], 1)
        self.assertEqual(u["slash"]["old"]["n"], 1)
        self.assertEqual(u["agents"]["reviewer"]["n"], 1)
        self.assertEqual(u["mcp"]["used"]["n"], 1)
        self.assertEqual(u["mcp"]["used"]["tools"], {"do": 1})
        self.assertEqual(u["mcp"]["plugin_tools_api"]["n"], 1)
        self.assertEqual(u["by_project"][self.proj]["requests"], self.turns_a)
        self.assertEqual(u["stop_hooks"]["after-stop.sh"], 2)
        self.assertEqual(u["hooks"][0]["event"], "PreToolUse")
        self.assertEqual(len(u["seen"]["mcp"]["big"]["tools"]), 25)
        self.assertTrue(u["seen"]["deferral"])
        self.assertEqual(sum(u["by_day"].values()), u["requests"])
        json.dumps(u)

    def test_report_joins_uses_and_last_used(self):
        r = self.run_audit()
        alpha = self.item(r, "skill", "alpha", "user")
        self.assertEqual(alpha["uses"], 2)
        self.assertEqual(alpha["last_used"], A._day(NOW - 10 * DAY))
        self.assertEqual(self.item(r, "command", "old")["uses"], 1)
        self.assertEqual(self.item(r, "agent", "reviewer")["uses"], 1)
        self.assertEqual(self.item(r, "mcp", "used", "user")["uses"], 1)
        tools = self.item(r, "plugin", "tools@mk")
        self.assertEqual(tools["uses"], 2)          # one skill call, one MCP call
        self.assertEqual(tools["hook_runs"], 1)
        self.assertEqual(tools["parts"], {"skill": 1, "agent": 1, "hook": 1, "mcp": 1})

    def test_hook_runs_matched_by_command_or_status_message(self):
        r = self.run_audit()
        hooks = dict((i["command"], i["runs_recorded"]) for i in r["items"] if i["kind"] == "hook")
        self.assertEqual(hooks["block-push.sh"], 1)
        self.assertEqual(hooks["after-stop.sh"], 2)
        self.assertEqual(hooks["never-runs.sh"], 0)

    def test_window_is_capped_by_the_transcripts(self):
        r = self.run_audit()
        w = r["window"]
        self.assertEqual(w["asked_days"], 30)
        self.assertEqual(w["real_days"], 10)
        self.assertTrue(w["capped_by_transcripts"])
        self.assertEqual(w["turns"], 9)
        gone = self.run_audit(days=2)
        self.assertEqual(gone["window"]["sessions"], 0)
        self.assertEqual(gone["findings"]["never_used"], [])


class FindingsTest(Tree):

    def names(self, report, key):
        return sorted(f["name"] for f in report["findings"][key])

    def test_never_used_and_its_exceptions(self):
        r = self.run_audit()
        never = self.names(r, "never_used")
        self.assertIn("idle", never)                 # an agent nobody launched
        self.assertIn("stale@mk", never)             # an old plugin, skills unused
        self.assertIn("ghost", never)                # an MCP server never seen
        self.assertIn("cloud:sync", never)           # listed in sessions, not in local config
        for used in ("alpha", "old", "reviewer", "used", "tools@mk"):
            self.assertNotIn(used, never)
        self.assertNotIn("builtin-thing", never)     # part of the app
        self.assertNotIn("corp", never)              # managed by the organisation
        self.assertNotIn("off@mk", never)            # disabled already
        self.assertNotIn("guard@mk", never)          # hooks only: a quiet guard is working
        self.assertNotIn("tools:helper", never)      # plugin parts are judged with the plugin
        ghost = [f for f in r["findings"]["never_used"] if f["name"] == "ghost"][0]
        self.assertIsNone(ghost["tokens_per_turn"])
        self.assertEqual(ghost["last"], "never in 10 days")
        self.assertTrue(all(f["estimate"] for f in r["findings"]["never_used"]))

    def test_new_install_is_not_flagged(self):
        r = self.run_audit()
        self.assertNotIn("fresh@mk", self.names(r, "never_used"))
        self.assertEqual([f["name"] for f in r["findings"]["new_no_history"]], ["fresh@mk"])
        self.assertTrue(self.item(r, "plugin", "fresh@mk")["new"])
        older = self.run_audit(new_days=1)
        self.assertIn("fresh@mk", self.names(older, "never_used"))
        self.assertEqual(older["findings"]["new_no_history"], [])

    def test_last_used_before_the_window_comes_from_the_usage_record(self):
        r = self.run_audit()
        beta = [f for f in r["findings"]["never_used"] if f["name"] == "beta"][0]
        self.assertEqual(beta["last_used"], A._day(NOW - 50 * DAY))
        self.assertTrue(beta["last"].startswith("last used "))
        self.assertIn("remove", beta["action"])

    def test_use_recorded_outside_transcripts_is_not_flagged(self):
        with open(self.home_json) as fh:
            data = json.load(fh)
        data["skillUsage"]["beta"]["lastUsedAt"] = (NOW - 2 * DAY) * 1000
        write(self.home_json, json.dumps(data))
        self.assertNotIn("beta", self.names(self.run_audit(), "never_used"))

    def test_hooks_are_never_flagged_for_removal(self):
        r = self.run_audit()
        quiet = [h["name"] for h in r["findings"]["hooks_no_run_recorded"]]
        self.assertTrue(any("never-runs.sh" in n for n in quiet))
        for key in ("never_used", "rarely_used", "mcp_large_unused"):
            self.assertFalse([f for f in r["findings"][key] if f["kind"] == "hook"])
        self.assertIn("no action", r["findings"]["hooks_no_run_recorded"][0]["note"])

    def test_duplicates_across_levels(self):
        r = self.run_audit()
        dups = dict(((d["kind"], d["name"]), d) for d in r["findings"]["duplicates"])
        self.assertEqual(dups[("skill", "alpha")]["levels"], ["user", "project"])
        self.assertEqual(sorted(dups[("mcp", "used")]["levels"]), ["project", "user"])
        self.assertEqual(dups[("mcp", "used")]["wins"], "project")
        self.assertEqual(dups[("skill", "alpha")]["wins"], "user")
        self.assertIn("the user copy wins", dups[("skill", "alpha")]["why"])
        self.assertNotIn(("skill", "beta"), dups)
        write(os.path.join(self.claude, "skills", "deploy", "SKILL.md"), skill_md("deploy", "Mine"))
        r = self.run_audit()
        dups = dict(((d["kind"], d["name"]), d) for d in r["findings"]["duplicates"])
        self.assertEqual(dups[("skill", "deploy")]["levels"], ["user", "plugin tools@mk"])
        self.assertIn("loads as well", dups[("skill", "deploy")]["why"])
        self.assertEqual(dups[("skill", "deploy")]["action"], "check that both are needed")

    def test_large_memory_file(self):
        r = self.run_audit()
        big = r["findings"]["large_memory"]
        self.assertEqual(len(big), 1)
        self.assertEqual(big[0]["lines"], 300)
        self.assertTrue(big[0]["name"].endswith("app/CLAUDE.md"))
        self.assertNotIn("uses", big[0])

    def test_large_unused_mcp_server_says_what_is_deferred(self):
        r = self.run_audit()
        found = r["findings"]["mcp_large_unused"]
        self.assertEqual([f["name"] for f in found], ["big"])
        f = found[0]
        self.assertTrue(f["deferred"])
        self.assertEqual(f["tool_count"], 25)
        names_chars = sum(len("mcp__big__tool_%02d" % i) + 1 for i in range(25))
        self.assertEqual(f["tokens_per_turn"], A.est_tokens(names_chars + 400))
        self.assertEqual(f["definition_tokens"], 25 * A.ASSUMED_TOOL_DEF_TOKENS)
        self.assertIn("assumed", f["why"])
        self.assertIn("Deferred", f["why"])

    def test_mcp_definitions_count_when_tool_search_is_off(self):
        path = os.path.join(self.claude, "settings.json")
        with open(path) as fh:
            st = json.load(fh)
        st["env"] = {"ENABLE_TOOL_SEARCH": "false"}
        write(path, json.dumps(st))
        r = self.run_audit()
        big = self.item(r, "mcp", "big")
        self.assertFalse(big["deferred"])
        self.assertEqual(big["tokens_per_turn"], 25 * A.ASSUMED_TOOL_DEF_TOKENS + 100)
        used = self.item(r, "mcp", "used", "user")
        self.assertIn("1 of its definitions", used["definition_basis"])

    def test_short_window_lowers_confidence(self):
        r = self.run_audit(days=5)
        self.assertEqual(r["window"]["real_days"], 3)
        self.assertTrue(all(f["confidence"].startswith("low") for f in r["findings"]["never_used"]))

    def test_single_project_use_suggests_project_level(self):
        for n, name in enumerate(("docs", "tools2")):
            t = Transcript("cccccccc-0000-0000-0000-00000000000%d" % n,
                           os.path.join(self.tmp, "work", name), NOW - 4 * DAY)
            t.prompt("some other work in another folder here").call("Bash", {"command": "ls"})
            os.makedirs(t.cwd)
            t.save(self.projects_dir, "-work-" + name)
        r = self.run_audit()
        single = dict((f["name"], f) for f in r["findings"]["single_project"])
        self.assertIn("alpha", single)
        self.assertTrue(single["alpha"]["action"].startswith("move to project level: "))
        self.assertTrue(single["alpha"]["action"].endswith("app"))
        self.assertEqual(single["alpha"]["wasted_tokens"],
                         single["alpha"]["tokens_per_turn"] * (self.turns_b + 2))


class EstimateTest(Tree):

    def test_characters_divided_by_four(self):
        self.assertEqual(A.est_tokens(0), 0)
        self.assertEqual(A.est_tokens(1), 1)
        self.assertEqual(A.est_tokens(4), 1)
        self.assertEqual(A.est_tokens(5), 2)
        self.assertEqual(A.est_tokens(4000), 1000)

    def test_listing_estimate_from_the_file(self):
        inv = A.inventory(self.claude, self.home_json, [], self.managed)
        alpha = [i for i in inv.items if i["name"] == "alpha"][0]
        self.assertEqual(alpha["chars"], len("alpha") + len("Does alpha things") + 5)
        long_desc = skill_md("wordy", "x" * 5000)
        write(os.path.join(self.claude, "skills", "wordy", "SKILL.md"), long_desc)
        inv = A.inventory(self.claude, self.home_json, [], self.managed)
        wordy = [i for i in inv.items if i["name"] == "wordy"][0]
        self.assertEqual(wordy["chars"], len("wordy") + A.SKILL_DESC_CAP + 5)

    def test_tokens_times_turns_and_dollars(self):
        r = self.run_audit()
        total = self.turns_a + self.turns_b
        beta = self.item(r, "skill", "beta")
        chars = len("- beta: " + "B" * 400) + 1          # measured from the session listing
        self.assertEqual(beta["tokens_per_turn"], A.est_tokens(chars))
        self.assertIn("measured", beta["cost_basis"])
        self.assertEqual(beta["turns"], total)
        self.assertEqual(beta["window_tokens"], beta["tokens_per_turn"] * total)
        self.assertEqual(beta["usd"], round(beta["window_tokens"] * 0.5 / 1e6, 4))
        self.assertTrue(beta["estimate"])
        proj = [i for i in r["items"] if i["kind"] == "memory" and i.get("role") == "project"][0]
        self.assertEqual(proj["turns"], self.turns_a)   # only sessions inside that project
        auto = [i for i in r["items"] if i.get("role") == "auto"][0]
        self.assertEqual(auto["turns"], self.turns_a)
        self.assertEqual(self.item(r, "skill", "cloud:sync")["turns"], total)

    def test_totals_and_first_turn_write(self):
        r = self.run_audit()
        a = r["always_on"]
        leaves = [i for i in r["items"] if i["kind"] != "plugin" and i["always_on"]
                  and i.get("tokens_per_turn")]
        self.assertEqual(a["window_tokens"], sum(i["window_tokens"] for i in leaves))
        self.assertEqual(a["usd_cache_read"], round(a["window_tokens"] * 0.5 / 1e6, 2))
        self.assertEqual(a["usd_first_turn_write"],
                         round(a["first_turn_write_tokens"] * 5.0 / 1e6, 2))
        every = [i for i in leaves if not i.get("applies") and not i.get("pdir")]
        self.assertEqual(a["every_session_tokens_per_turn"],
                         sum(i["tokens_per_turn"] for i in every))
        self.assertEqual(sum(a["by_kind"].values()), a["every_session_tokens_per_turn"])
        self.assertEqual(a["unknown_load"], ["corp", "ghost", "localsrv", "shared"])
        self.assertEqual(r["prices"]["cache_read_per_mtok"], 0.5)
        self.assertEqual(r["prices"]["priced_share_of_turns"], 1.0)

    def test_no_price_table_means_no_dollars(self):
        r = self.run_audit(prices_path=os.path.join(self.tmp, "absent.json"))
        self.assertIsNone(r["prices"])
        self.assertIsNone(r["always_on"]["usd_cache_read"])
        self.assertIsNone(self.item(r, "skill", "beta")["usd"])
        self.assertGreater(r["always_on"]["window_tokens"], 0)
        self.assertNotIn("$", A.render_table(r).split("Set up but unused")[0].split("\n")[1])

    def test_dated_model_id_and_unpriced_model(self):
        prices = A.load_prices(self.prices)
        rates = A.blended_rates({MODEL + "-20260101": 3, "mystery-model": 1}, prices)
        self.assertEqual(rates["cache_read"], 0.5)
        self.assertEqual(rates["priced_share"], 0.75)
        self.assertIsNone(A.blended_rates({"mystery-model": 4}, prices))
        self.assertIsNone(A.blended_rates({MODEL: 1}, None))
        write(self.prices, "{not json")
        self.assertIsNone(A.load_prices(self.prices))

    def test_recent_install_is_charged_only_since_install(self):
        r = self.run_audit()
        shiny = self.item(r, "skill", "fresh:shiny")
        self.assertEqual(shiny["turns"], self.turns_b)  # only the session after the install
        self.assertIn("since it was installed", shiny["turns_basis"])


class OutputTest(Tree):

    def test_json_round_trip(self):
        r = self.run_audit()
        again = json.loads(json.dumps(r))
        self.assertEqual(again["window"]["turns"], 9)
        for it in again["items"]:
            self.assertFalse([k for k in it if k.startswith("_")])
            self.assertTrue(it["estimate"])

    def test_table_is_sorted_by_estimated_cost(self):
        r = self.run_audit()
        costs = [i["window_tokens"] for i in r["items"] if i["window_tokens"] is not None]
        self.assertEqual(costs, sorted(costs, reverse=True))
        text = A.render_table(r)
        self.assertIn("estimate", text)
        self.assertIn("10 days of transcripts, asked for 30", text)
        self.assertIn("UNUSED", text)
        self.assertIn("Set up but unused:", text)
        self.assertNotIn(chr(0x2014), text)
        self.assertNotIn(chr(0x2013), text)

    def test_weekly_block_is_at_most_five_lines(self):
        r = self.run_audit()
        block = r["weekly_block"]
        self.assertTrue(0 < len(block) <= 5)
        flagged = len(r["findings"]["never_used"]) + len(r["findings"]["mcp_large_unused"])
        self.assertGreater(flagged, 5)
        for line in block:
            self.assertTrue(line.startswith("- "))
            self.assertIn("(estimate)", line)
            self.assertIn("Action: ", line)
            self.assertRegex(line, r"\(estimate\), (never in 10 days|last used \d{4}-\d\d-\d\d"
                                   r"|used \d in 10 days)\. Action: ")
        self.assertEqual(A.weekly_block({}, 10), [])

    def test_page_block_is_html_safe(self):
        write(os.path.join(self.claude, "skills", "x", "SKILL.md"),
              skill_md("<script>alert(1)</script>", "A & B </script>"))
        r = self.run_audit()
        data = A.page_data(r, top=200)
        names = [row["name"] for row in data["rows"]]
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", names)
        blob = A.page_json(r, top=200)
        self.assertNotIn("<", blob)
        self.assertNotIn(">", blob)
        self.assertEqual(json.loads(blob)["title"], "Set up but unused")
        self.assertTrue(len(data["lines"]) <= 5)

    def test_cli(self):
        env = dict(os.environ, COACH_HOME=os.path.join(self.tmp, "coach-home"))
        base = [sys.executable, "-W", "error", os.path.join(ROOT, "coach", "config_audit.py"),
                "--claude-dir", self.claude, "--home-json", self.home_json,
                "--managed-dir", self.managed, "--prices", self.prices,
                "--since", "2020-01-01", "--until", A._day(NOW + 2 * DAY)]
        out = subprocess.run(base + ["--format", "json"], env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(got["window"]["sessions"], 2)
        self.assertIn("stale@mk", [f["name"] for f in got["findings"]["never_used"]])
        dest = os.path.join(self.tmp, "out", "table.txt")
        out = subprocess.run(base + ["--out", dest], env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        with open(dest) as fh:
            self.assertIn("Configured versus used", fh.read())
        bad = subprocess.run(base[:4] + ["--days", "0"], env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(bad.returncode, 2)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "coach-home")))


class ReviewFixesTest(Tree):

    def never(self, r):
        return sorted(f["name"] for f in r["findings"]["never_used"])

    def test_idle_project_is_not_flagged(self):
        idle = os.path.join(self.tmp, "work", "idle")
        write(os.path.join(idle, ".claude", "skills", "sleepy", "SKILL.md"), skill_md("sleepy", "x"))
        write(os.path.join(idle, ".claude", "agents", "napper.md"),
              "---\nname: napper\ndescription: y\n---\n")
        with open(self.home_json) as fh:
            data = json.load(fh)
        data["projects"][idle] = {"mcpServers": {"dozing": {"command": "z"}}}
        write(self.home_json, json.dumps(data))
        r = self.run_audit()
        self.assertEqual(self.item(r, "skill", "sleepy")["turns"], 0)
        for name in ("sleepy", "napper", "dozing"):
            self.assertNotIn(name, self.never(r))

    def test_unapproved_project_server_is_not_flagged_or_charged(self):
        r = self.run_audit()
        self.assertNotIn("pending", self.never(r))
        self.assertFalse(self.item(r, "mcp", "pending")["always_on"])

    def test_plugin_with_only_an_output_style_is_not_flagged(self):
        root = os.path.join(self.claude, "plugins", "cache", "mk", "look", "1.0.0")
        write(os.path.join(root, "output-styles", "terse.md"), "---\nname: terse\n---\nShort.\n")
        path = os.path.join(self.claude, "plugins", "installed_plugins.json")
        with open(path) as fh:
            data = json.load(fh)
        data["plugins"]["look@mk"] = [{"scope": "user", "installPath": root,
                                       "installedAt": iso(NOW - 90 * DAY)}]
        write(path, json.dumps(data))
        self.assertNotIn("look@mk", self.never(self.run_audit()))

    def test_plugin_installed_at_two_scopes_keeps_all_its_uses(self):
        path = os.path.join(self.claude, "plugins", "installed_plugins.json")
        with open(path) as fh:
            data = json.load(fh)
        data["plugins"]["tools@mk"].append({"scope": "project", "projectPath": self.other,
                                            "installPath": self.plug,
                                            "installedAt": iso(NOW - 20 * DAY)})
        write(path, json.dumps(data))
        r = self.run_audit()
        tools = self.item(r, "plugin", "tools@mk")
        self.assertEqual(tools["uses"], 2)
        self.assertIsNone(tools["applies"])
        self.assertEqual(tools["installed"], A._day(NOW - 90 * DAY))

    def test_plugin_enabled_in_one_project_is_charged_there_only(self):
        path = os.path.join(self.claude, "settings.json")
        with open(path) as fh:
            st = json.load(fh)
        del st["enabledPlugins"]["stale@mk"]
        write(path, json.dumps(st))
        write(os.path.join(self.proj, ".claude", "settings.json"),
              json.dumps({"enabledPlugins": {"stale@mk": True, "tools@mk": False}}))
        r = self.run_audit()
        self.assertEqual(self.item(r, "skill", "stale:dusty")["turns"], self.turns_a)
        self.assertEqual(self.item(r, "plugin", "stale@mk")["turns"], self.turns_a)
        self.assertEqual(self.item(r, "plugin", "tools@mk")["turns"], self.turns_a + self.turns_b)

    def test_same_name_in_two_projects_is_not_a_duplicate(self):
        write(os.path.join(self.other, ".mcp.json"), json.dumps({"mcpServers": {"localsrv": {}}}))
        r = self.run_audit()
        names = [(d["kind"], d["name"]) for d in r["findings"]["duplicates"]]
        self.assertNotIn(("mcp", "localsrv"), names)
        self.assertIn(("skill", "alpha"), names)

    def test_subagent_file_that_repeats_the_parent_adds_nothing(self):
        src = os.path.join(self.projects_dir, "-work-app",
                           "aaaaaaaa-0000-0000-0000-000000000001.jsonl")
        with open(src) as fh:
            lines = [json.loads(x) for x in fh.read().splitlines()]
        for rec in lines:
            rec["isSidechain"] = True
        extra = dict(lines[-2], uuid="fresh-1")
        dest = write(os.path.join(src[:-6], "subagents", "agent-1.jsonl"),
                     "\n".join(json.dumps(x, separators=(",", ":")) for x in lines + [extra]) + "\n")
        os.utime(dest, (NOW, NOW))
        u = A.scan_usage(NOW - 30 * DAY, NOW, self.projects_dir)
        self.assertEqual(u["skills"]["alpha"]["n"], 2)
        self.assertEqual(u["skills"]["tools:deploy"]["n"], 2)   # the one new record counts
        self.assertEqual(u["mcp"]["used"]["n"], 1)

    def test_odd_values_do_not_stop_the_audit(self):
        path = os.path.join(self.claude, "settings.json")
        write(path, json.dumps({"hooks": {"Stop": [{"hooks": 5}, {"hooks": {"a": 1}}]}}))
        write(os.path.join(self.proj, ".claude", "settings.json"),
              json.dumps({"enabledMcpjsonServers": [{"x": 1}, "shared"],
                          "disabledMcpjsonServers": [["y"]], "enabledPlugins": {"a@b": {}}}))
        write(self.home_json, '{"skillUsage": {"a": {"usageCount": Infinity, "lastUsedAt": NaN},'
                              ' "b": {"usageCount": 1e999, "lastUsedAt": 5}}}')
        write(os.path.join(self.tmp, "deep.json"), "[" * 100000)
        self.assertIsNone(A.load_prices(os.path.join(self.tmp, "deep.json")))
        r = self.run_audit()
        self.assertEqual(A.lifetime_usage(self.home_json)["skills"], {})
        json.dumps(r)

    def test_other_window_is_saved_beside_the_standard_one(self):
        root = os.path.join(self.tmp, "reports")
        first = A.save(self.run_audit(), root)
        short = A.save(self.run_audit(days=5), root)
        self.assertNotEqual(first, short)
        self.assertIn("-window-", os.path.basename(short))
        moved = A.save(self.run_audit(since=NOW - 40 * DAY, until=NOW - 10 * DAY), root)
        self.assertIn("-window-", os.path.basename(moved))   # 30 days, but not ending now
        self.assertEqual(len(os.listdir(os.path.join(root, "config-audit"))), 3)
        back = A.latest(root)
        self.assertEqual(back["window"]["asked_days"], 30)
        self.assertTrue(back["window"]["standard"])
        self.assertEqual(back["window"]["sessions"], 2)

    def test_project_copy_that_always_loses_costs_nothing(self):
        r = self.run_audit()
        loser = self.item(r, "skill", "alpha", "project")
        self.assertEqual(loser["shadowed_by"], "user")
        self.assertEqual((loser["tokens_per_turn"], loser["uses"], loser["window_tokens"]), (0, 0, 0))
        self.assertEqual(self.item(r, "skill", "alpha", "user")["uses"], 2)
        self.assertNotIn("alpha", self.never(r))

    def test_two_plugins_with_the_same_skill_name_are_not_duplicates(self):
        root = os.path.join(self.claude, "plugins", "cache", "mk", "ops", "1.0.0")
        write(os.path.join(root, "skills", "deploy", "SKILL.md"), skill_md("deploy", "Other"))
        path = os.path.join(self.claude, "plugins", "installed_plugins.json")
        with open(path) as fh:
            data = json.load(fh)
        data["plugins"]["ops@mk"] = [{"scope": "user", "installPath": root,
                                      "installedAt": iso(NOW - 90 * DAY)}]
        write(path, json.dumps(data))
        r = self.run_audit()
        self.assertEqual(self.item(r, "skill", "ops:deploy")["scope"], "plugin")
        self.assertNotIn("deploy", [d["name"] for d in r["findings"]["duplicates"]])

    def test_agent_duplicate_names_the_project_copy_as_winner(self):
        write(os.path.join(self.proj, ".claude", "agents", "reviewer.md"),
              "---\nname: reviewer\ndescription: Project reviewer\n---\n")
        r = self.run_audit()
        dup = [d for d in r["findings"]["duplicates"] if d["kind"] == "agent"][0]
        self.assertEqual(dup["wins"], "project")
        self.assertIn("the project copy wins", dup["why"])

    def test_local_settings_merge_plugins_by_key(self):
        path = os.path.join(self.claude, "settings.json")
        with open(path) as fh:
            st = json.load(fh)
        st["enabledPlugins"]["stale@mk"] = False
        write(path, json.dumps(st))
        write(os.path.join(self.proj, ".claude", "settings.json"),
              json.dumps({"enabledPlugins": {"stale@mk": True}}))
        write(os.path.join(self.proj, ".claude", "settings.local.json"),
              json.dumps({"enabledPlugins": {"guard@mk": True}}))
        r = self.run_audit()
        stale = self.item(r, "plugin", "stale@mk")
        self.assertTrue(stale["enabled"])
        self.assertEqual(stale["turns"], self.turns_a)

    def test_project_switching_a_plugin_off_does_not_switch_it_off_everywhere(self):
        path = os.path.join(self.claude, "settings.json")
        with open(path) as fh:
            st = json.load(fh)
        del st["enabledPlugins"]["stale@mk"]
        write(path, json.dumps(st))
        write(os.path.join(self.proj, ".claude", "settings.json"),
              json.dumps({"enabledPlugins": {"stale@mk": False}}))
        self.assertTrue(self.item(self.run_audit(), "plugin", "stale@mk")["enabled"])

    def test_no_creation_time_falls_back_to_last_change_for_new(self):
        real = A._file_time
        A._file_time = lambda path: None            # what Linux gives
        try:
            fresh = os.path.join(self.claude, "agents", "newborn.md")
            write(fresh, "---\nname: newborn\ndescription: Just made\n---\n")
            os.utime(fresh, (NOW - 2 * DAY, NOW - 2 * DAY))
            r = self.run_audit()
        finally:
            A._file_time = real
        self.assertTrue(self.item(r, "agent", "newborn")["new"])
        self.assertNotIn("newborn", self.never(r))
        self.assertIn("newborn", [f["name"] for f in r["findings"]["new_no_history"]])
        self.assertIn("idle", self.never(r))            # an old file is still flagged
        self.assertEqual(self.item(r, "agent", "newborn")["turns"], self.turns_a + self.turns_b)

    def test_odd_transcript_records_do_not_stop_the_audit(self):
        t = Transcript("dddddddd-0000-0000-0000-000000000004", self.other, NOW - 2 * DAY)
        t.prompt("a session with some strange records in it")
        t.call("Skill", {"skill": "alpha"})
        t._add({"type": "system", "subtype": "stop_hook_summary", "hookInfos": 7})
        t.attach({"type": "deferred_tools_delta", "addedNames": 5})
        t.attach({"type": "deferred_tools_record", "entries": {"a": 1}})
        t.attach({"type": "skill_listing", "content": 12})
        t.attach({"type": "agent_listing_delta", "addedTypes": "x", "addedLines": None})
        t.attach({"type": "mcp_instructions_delta", "addedNames": [1], "addedBlocks": [2]})
        t._add({"type": "attachment", "attachment": "text"})
        t._add({"type": "assistant", "message": "not a map"})
        t.call("Skill", {"skill": "alpha"})
        t.save(self.projects_dir, "-work-site")
        r = self.run_audit()
        self.assertEqual(r["window"]["sessions"], 3)
        # extract.py skips a record of the wrong type, so no transcript is cut short
        self.assertEqual(r["window"]["transcripts_cut_short"], 0)
        self.assertEqual(self.item(r, "skill", "alpha", "user")["uses"], 4)

    def test_server_switched_off_in_a_project_is_not_charged_there(self):
        with open(self.home_json) as fh:
            data = json.load(fh)
        data["projects"][self.proj]["disabledMcpServers"] = ["ghost"]
        write(self.home_json, json.dumps(data))
        r = self.run_audit()
        self.assertEqual(self.item(r, "mcp", "ghost")["turns"], self.turns_b)
        self.assertIn("ghost", self.never(r))
        data["projects"][self.other] = {"disabledMcpServers": ["ghost"]}
        write(self.home_json, json.dumps(data))
        r = self.run_audit()
        self.assertEqual(self.item(r, "mcp", "ghost")["turns"], 0)
        self.assertNotIn("ghost", self.never(r))

    def test_project_skill_used_from_another_checkout_is_not_flagged(self):
        for folder in (self.proj, self.other):
            write(os.path.join(folder, ".claude", "skills", "shared-skill", "SKILL.md"),
                  skill_md("shared-skill", "In the repo"))
        t = Transcript("eeeeeeee-0000-0000-0000-000000000005", self.proj, NOW - 2 * DAY)
        t.prompt("use the skill that lives in the repository").call("Skill", {"skill": "shared-skill"})
        t.save(self.projects_dir, "-work-app")
        r = self.run_audit()
        copies = [i for i in r["items"] if i["name"] == "shared-skill"]
        self.assertEqual(sorted(i["uses"] for i in copies), [0, 1])
        self.assertNotIn("shared-skill", self.never(r))
        self.assertTrue([i for i in copies if i.get("used_in_another_checkout")])

    def test_built_in_command_is_not_credited_to_a_plugin_skill(self):
        root = os.path.join(self.claude, "plugins", "cache", "mk", "stale", "2.0.0")
        write(os.path.join(root, "skills", "clear", "SKILL.md"), skill_md("clear", "Clears things"))
        t = Transcript("ffffffff-0000-0000-0000-000000000006", self.other, NOW - 2 * DAY)
        t.prompt("typing a built in command a couple of times").slash("clear").slash("clear")
        t.call("Bash", {"command": "ls"})
        t.save(self.projects_dir, "-work-site")
        r = self.run_audit()
        self.assertEqual(self.item(r, "skill", "stale:clear")["uses"], 0)
        self.assertIn("stale@mk", self.never(r))

    def test_skill_listed_in_few_sessions_is_not_called_built_in(self):
        listing = "- builtin-thing: Comes with the app\n- somewhere: A project skill, folder gone\n"
        for n in range(3):
            t = Transcript("9999999%d-0000-0000-0000-000000000007" % n, self.other, NOW - 2 * DAY)
            t.attach({"type": "skill_listing", "content": listing if n == 0 else listing[:38]})
            t.prompt("another session in the second project").call("Bash", {"command": "ls"})
            t.save(self.projects_dir, "-work-site")
        r = self.run_audit()
        self.assertEqual(self.item(r, "skill", "builtin-thing")["scope"], "builtin")
        self.assertEqual(self.item(r, "skill", "somewhere")["scope"], "session")

    def test_item_missing_from_newest_sessions_keeps_its_history(self):
        with open(self.home_json) as fh:
            data = json.load(fh)
        del data["mcpServers"]["big"]                  # now only known from sessions
        write(self.home_json, json.dumps(data))
        t = Transcript("77777777-0000-0000-0000-000000000008", self.other, NOW - 1 * DAY)
        t.attach({"type": "deferred_tools_delta", "addedNames": ["mcp__newer__x"]})
        t.prompt("a later session that lists another server").call("Bash", {"command": "ls"})
        t.save(self.projects_dir, "-work-site")
        r = self.run_audit()
        big = self.item(r, "mcp", "big")
        self.assertTrue(big["gone"])
        self.assertEqual(big["last_listed"], A._day(NOW - 10 * DAY))
        self.assertGreater(big["tokens_per_turn"], 0)
        self.assertEqual(big["window_tokens"], big["tokens_per_turn"] * self.turns_a)
        found = [f for f in r["findings"]["mcp_large_unused"] if f["name"] == "big"][0]
        self.assertTrue(found["may_be_off_already"])
        self.assertFalse([ln for ln in r["weekly_block"] if "mcp big " in ln])
        self.assertLess(r["always_on"]["by_kind"]["mcp"], big["tokens_per_turn"])

    def test_server_first_listed_this_week_is_new_and_charged_for_its_sessions(self):
        with open(self.home_json) as fh:
            data = json.load(fh)
        data["mcpServers"]["latecomer"] = {"command": "x"}
        write(self.home_json, json.dumps(data))
        t = Transcript("66666666-0000-0000-0000-000000000009", self.other, NOW - 1 * DAY)
        t.attach({"type": "deferred_tools_delta", "addedNames": ["mcp__latecomer__x"]})
        t.prompt("first session after adding a server").call("Bash", {"command": "ls"})
        t.save(self.projects_dir, "-work-site")
        r = self.run_audit()
        late = self.item(r, "mcp", "latecomer")
        self.assertTrue(late["new"])
        self.assertEqual(late["turns"], 1)
        self.assertNotIn("latecomer", self.never(r))
        self.assertEqual(self.item(r, "mcp", "big")["turns"], self.turns_a)

    def test_worktrees_of_one_repo_count_as_one_project(self):
        main = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(main, ".git", "worktrees", "wt"))
        tree = os.path.join(self.tmp, "trees", "wt")
        write(os.path.join(tree, ".git"), "gitdir: %s/.git/worktrees/wt\n" % main)
        os.makedirs(os.path.join(tree, "src"))
        self.assertEqual(A.repo_root(os.path.join(tree, "src")), main)
        self.assertEqual(A.repo_root(main), main)
        self.assertEqual(A.repo_root(self.other), self.other)
        for n, cwd in enumerate((main, tree, os.path.join(tree, "src"))):
            t = Transcript("5555555%d-0000-0000-0000-00000000000a" % n, cwd, NOW - 2 * DAY)
            t.prompt("work in the repo and its worktree here").call("Skill", {"skill": "beta"})
            t.save(self.projects_dir, "-repo-%d" % n)
        r = self.run_audit()
        self.assertEqual(self.item(r, "skill", "beta")["uses"], 3)
        single = [f for f in r["findings"]["single_project"] if f["name"] == "beta"]
        self.assertEqual(len(single), 1)                # one repo, three folders
        self.assertTrue(single[0]["action"].endswith("repo"))
        self.assertEqual(single[0]["wasted_tokens"],
                         single[0]["tokens_per_turn"] * (self.turns_a + self.turns_b))

    def test_page_data_can_skip_escaping(self):
        write(os.path.join(self.claude, "skills", "amp", "SKILL.md"), skill_md("a&b", "desc"))
        r = self.run_audit()
        raw = [row["name"] for row in A.page_data(r, top=200, escape=False)["rows"]]
        self.assertIn("a&b", raw)
        self.assertNotIn("<", A.page_json(r, top=200, escape=False))


class SaveTest(Tree):

    def test_save_and_latest(self):
        root = os.path.join(self.tmp, "reports")
        self.assertIsNone(A.latest(root))
        r = self.run_audit()
        path = A.save(r, root)
        self.assertEqual(os.path.dirname(path), os.path.join(root, "config-audit"))
        self.assertEqual(os.path.basename(path), r["generated"][:10] + ".json")
        write(os.path.join(root, "config-audit", "9999-01-01.json"), "{broken")
        write(os.path.join(root, "config-audit", "notes.txt"), "ignored")
        back = A.latest(root)
        self.assertEqual(back["window"], json.loads(json.dumps(r["window"])))
        self.assertEqual(A.page_data(back)["title"], "Set up but unused")
        self.assertEqual(os.listdir(os.path.join(root, "config-audit")).count(
            os.path.basename(path)), 1)

    def test_cli_save_goes_under_the_coach_home(self):
        home = os.path.join(self.tmp, "coach-home")
        env = dict(os.environ, COACH_HOME=home)
        out = subprocess.run(
            [sys.executable, "-W", "error", os.path.join(ROOT, "coach", "config_audit.py"),
             "--claude-dir", self.claude, "--home-json", self.home_json,
             "--managed-dir", self.managed, "--prices", self.prices,
             "--since", "2020-01-01", "--until", A._day(NOW + 2 * DAY),
             "--save", "--format", "weekly"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(out.stdout.startswith("Set up but unused\n"))
        self.assertIn("- plugin stale@mk (user)", out.stdout)
        saved = os.listdir(os.path.join(home, "reports", "config-audit"))
        self.assertEqual(len(saved), 1)
        self.assertIn("-window-", saved[0])           # not the standard window
        with open(os.path.join(home, "reports", "config-audit", saved[0])) as fh:
            self.assertEqual(json.load(fh)["window"]["sessions"], 2)


class BrokenInputTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.claude = os.path.join(self.tmp, "claude")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def audit(self):
        return A.audit(claude=self.claude, managed=os.path.join(self.tmp, "none"),
                       prices_path=os.path.join(self.tmp, "none.json"), now=NOW)

    def test_nothing_exists(self):
        r = self.audit()
        self.assertEqual(r["items"], [])
        self.assertEqual(r["problems"], [])
        self.assertEqual(r["window"]["real_days"], 0)
        self.assertEqual(r["weekly_block"], [])
        self.assertIn("nothing flagged", A.render_table(r))
        self.assertEqual(A.page_data(r)["rows"], [])

    def test_unreadable_json_is_reported_not_fatal(self):
        c = self.claude
        write(os.path.join(c, "settings.json"), "{ this is not json")
        write(os.path.join(c, ".claude.json"), "[1, 2, 3]")
        write(os.path.join(c, "plugins", "installed_plugins.json"), "\x00\x01 nope")
        write(os.path.join(c, "skills", "ok", "SKILL.md"), skill_md("ok", "Still found"))
        write(os.path.join(c, "skills", "empty", "SKILL.md"), "")
        write(os.path.join(c, "skills", "nofront", "SKILL.md"), "Just a first line.\nMore.\n")
        os.makedirs(os.path.join(c, "skills", "no-skill-file"))
        write(os.path.join(c, "agents", "plain.md"), "no frontmatter here\n")
        write(os.path.join(c, "projects", "-x", "s.jsonl"), "not json\n{\"type\":\n\n")
        r = self.audit()
        bad = sorted(os.path.basename(p["path"]) for p in r["problems"])
        self.assertEqual(bad, [".claude.json", "installed_plugins.json", "settings.json"])
        names = [i["name"] for i in r["items"] if i["kind"] == "skill"]
        self.assertEqual(sorted(names), ["empty", "nofront", "ok"])
        self.assertFalse([i for i in r["items"] if i["kind"] == "agent"])
        self.assertIn("Could not read", A.render_table(r))

    def test_odd_shapes_inside_valid_json(self):
        c = self.claude
        write(os.path.join(c, "settings.json"), json.dumps({
            "hooks": {"Stop": "oops", "PreToolUse": [None, {"hooks": [7, {"type": "command"}]}]},
            "enabledPlugins": ["not", "a", "map"], "env": "nope", "outputStyle": 5}))
        write(os.path.join(c, ".claude.json"), json.dumps({
            "mcpServers": {"odd": "not a map"}, "projects": {"relative/path": {}, "/": 3},
            "skillUsage": {"a": 1, "b": {"usageCount": "x"}}, "pluginUsage": []}))
        write(os.path.join(c, "plugins", "installed_plugins.json"), json.dumps({
            "plugins": {"p@m": [], "q@m": [{"installPath": 9}], "r@m": "x",
                        "s@m": [{"installPath": os.path.join(self.tmp, "missing")}]}}))
        r = self.audit()
        kinds = sorted(set(i["kind"] for i in r["items"]))
        self.assertEqual(kinds, ["hook", "mcp", "plugin"])
        self.assertTrue(any("missing" in p["problem"] for p in r["problems"]))
        json.dumps(r)

    def test_lifetime_usage_tolerates_anything(self):
        self.assertEqual(A.lifetime_usage(os.path.join(self.tmp, "nope")),
                         {"skills": {}, "plugins": {}})
        path = write(os.path.join(self.tmp, "h.json"), json.dumps({
            "skillUsage": {"a": {"usageCount": 2, "lastUsedAt": 1700000000000},
                           "zero": {"usageCount": 0, "lastUsedAt": 1700000000000}}}))
        self.assertEqual(A.lifetime_usage(path)["skills"], {"a": {"count": 2, "last": 1700000000.0}})


class ParsingTest(unittest.TestCase):

    def test_frontmatter(self):
        fields, body = A.parse_frontmatter(
            "---\nname: x\ndescription: \"Quoted: yes\"\nwhen_to_use: >\n  folded\n  text\n---\nBody\n")
        self.assertEqual(fields["name"], "x")
        self.assertEqual(fields["description"], "Quoted: yes")
        self.assertEqual(fields["when_to_use"], "folded text")
        self.assertEqual(body, "Body\n")
        self.assertEqual(A.parse_frontmatter("no front matter"), ({}, "no front matter"))
        self.assertEqual(A.parse_frontmatter("---\nname: x\nnever closed")[0], {})
        self.assertEqual(A.parse_frontmatter("")[0], {})

    def test_imports(self):
        tmp = tempfile.mkdtemp()
        try:
            write(os.path.join(tmp, "a.md"), "a")
            write(os.path.join(tmp, "sub dir", "b.md"), "b")
            text = ("See @a.md and @sub\\ dir/b.md.\nmail me@example.com\n"
                    "`@a.md` in a span\n@missing.md\n")
            found = A.find_imports(text, tmp)
            self.assertEqual([os.path.basename(p) for p in found], ["a.md", "b.md"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_import_depth_stops_at_four_hops(self):
        tmp = tempfile.mkdtemp()
        try:
            for i in range(7):
                write(os.path.join(tmp, "claude", "m%d.md" % i), "@m%d.md\n" % (i + 1))
            os.rename(os.path.join(tmp, "claude", "m0.md"), os.path.join(tmp, "claude", "CLAUDE.md"))
            inv = A.inventory(os.path.join(tmp, "claude"), os.path.join(tmp, "none.json"), [],
                              os.path.join(tmp, "none"))
            mem = [os.path.basename(i["path"]) for i in inv.items if i["kind"] == "memory"]
            self.assertEqual(sorted(mem), ["CLAUDE.md", "m1.md", "m2.md", "m3.md", "m4.md"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_circular_import_ends(self):
        tmp = tempfile.mkdtemp()
        try:
            write(os.path.join(tmp, "claude", "CLAUDE.md"), "@loop.md\n")
            write(os.path.join(tmp, "claude", "loop.md"), "@CLAUDE.md\n")
            inv = A.inventory(os.path.join(tmp, "claude"), os.path.join(tmp, "none.json"), [],
                              os.path.join(tmp, "none"))
            self.assertEqual(len([i for i in inv.items if i["kind"] == "memory"]), 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_module_has_no_long_dashes(self):
        for rel in ("coach/config_audit.py", "tests/test_configaudit.py",
                    "skills/coach/references/configaudit.md"):
            with open(os.path.join(ROOT, rel), "rb") as fh:
                text = fh.read().decode("utf-8")
            self.assertNotIn(chr(0x2014), text, rel)
            self.assertNotIn(chr(0x2013), text, rel)


if __name__ == "__main__":
    unittest.main()
