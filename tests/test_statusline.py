"""Tests for coach/statusline.py. Payloads are built from the documented status line schema
(https://code.claude.com/docs/en/statusline). Everything runs in temp folders."""
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

import signals  # noqa: E402
import statusline as SL  # noqa: E402

SCRIPT = os.path.join(ROOT, "coach", "statusline.py")
NOW = 1800000000


def payload(tokens=15500, size=200000, cost=0.5, sid="sess-0001", pid=None, cache=None, **extra):
    """A status line payload in the documented shape."""
    p = {
        "cwd": "/tmp/demo", "session_id": sid, "transcript_path": "/tmp/demo/t.jsonl",
        "model": {"id": "model-x", "display_name": "Model X"},
        "workspace": {"current_dir": "/tmp/demo", "project_dir": "/tmp/demo", "added_dirs": []},
        "version": "2.1.290",
        "cost": {"total_cost_usd": cost, "total_duration_ms": 45000, "total_api_duration_ms": 2300},
        "context_window": {
            "total_input_tokens": tokens, "total_output_tokens": 1200, "context_window_size": size,
            "used_percentage": int(100.0 * tokens / size), "remaining_percentage": 100 - int(100.0 * tokens / size),
            "current_usage": {"input_tokens": 500, "output_tokens": 1200,
                              "cache_creation_input_tokens": 1000,
                              "cache_read_input_tokens": tokens - 1500},
        },
        "exceeds_200k_tokens": tokens > 200000,
    }
    if pid:
        p["prompt_id"] = pid
    if cache is not None:
        p["prompt_cache"] = cache
    p.update(extra)
    return p


def warm_cache(ttl="1h", resend=None):
    return {"warm": True, "caching_observed": True, "ttl": ttl,
            "expires_at": NOW + 1000, "requests": 4, "misses": 0, "recache_tokens_if_cold": resend}


def cold_cache(idle_s, ttl="1h", resend=None):
    life = {"5m": 300, "1h": 3600}[ttl]
    return {"warm": False, "caching_observed": True, "ttl": ttl,
            "expires_at": NOW - idle_s + life, "requests": 4, "misses": 0,
            "recache_tokens_if_cold": resend}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="coach-sl-")
        self.home = os.path.join(self.tmp, "coach")
        os.makedirs(self.home)
        self.saved = {k: os.environ.get(k) for k in ("COACH_HOME", "NO_COLOR", "COLUMNS", "TERM",
                                                     "CLAUDE_CONFIG_DIR", SL.WRAP_ENV)}
        os.environ["COACH_HOME"] = self.home
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        for k in ("NO_COLOR", "COLUMNS", "TERM", SL.WRAP_ENV):
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def line(self, p, now=NOW, **kw):
        kw.setdefault("color", False)
        kw.setdefault("width", 0)
        return SL.line_for(p, now=now, **kw)

    def set_cfg(self, **keys):
        with open(os.path.join(self.home, "config.json"), "w") as fh:
            json.dump({"notify": True, "statusline": keys}, fh)


class RenderTest(Base):
    def test_quiet_shows_only_context_and_cost(self):
        self.assertEqual(self.line(payload(tokens=36000, size=200000, cost=4.214)), "ctx 36k 18% | $4.21")

    def test_thresholds_come_from_the_coach(self):
        T = SL.thresholds()
        self.assertEqual(T["ctx_over_tokens"], signals.T["peak_context"])
        self.assertEqual(T["ctx_warn_tokens"], signals.T["idle_context"])
        self.assertEqual(T["cold_tokens"], signals.T["idle_context"])

    def test_context_warn_marker_no_action(self):
        out = self.line(payload(tokens=310000, size=1000000, cost=9.8))
        self.assertEqual(out, "ctx 310k 31% ! | $9.80")

    def test_context_over_marker_and_action(self):
        out = self.line(payload(tokens=512000, size=1000000, cost=41.2))
        self.assertEqual(out, "ctx 512k 51% !! | $41.20 | compact or hand off")

    def test_percent_threshold_on_a_small_window(self):
        self.assertEqual(self.line(payload(tokens=144000, size=200000, cost=1)), "ctx 144k 72% ! | $1.00")
        self.assertIn("!! |", self.line(payload(tokens=176000, size=200000, cost=1, sid="b")))

    def test_big_numbers(self):
        out = self.line(payload(tokens=1200000, size=2000000, cost=104.4))
        self.assertTrue(out.startswith("ctx 1.2M 60% !! | $104 |"), out)

    def test_config_can_move_a_threshold(self):
        self.set_cfg(ctx_over_tokens=100000)
        self.assertIn("!!", self.line(payload(tokens=120000, size=1000000)))

    def test_off_switch_keeps_numbers_drops_coaching(self):
        open(os.path.join(self.home, "off"), "w").close()
        self.assertEqual(self.line(payload(tokens=512000, size=1000000, cost=41.2)), "ctx 512k 51% | $41.20")
        self.assertEqual(self.line(payload(tokens=512000, size=1000000, cost=41.2), full=False), "")

    def test_segment_is_empty_when_quiet_and_has_no_cost(self):
        self.assertEqual(self.line(payload(), full=False), "")
        seg = self.line(payload(tokens=512000, size=1000000, cost=41.2), full=False)
        self.assertEqual(seg, "ctx 512k 51% !! | compact or hand off")


class DegradeTest(Base):
    def test_empty_payload(self):
        self.assertEqual(self.line({}), "ctx ?")

    def test_not_a_dict(self):
        self.assertEqual(self.line(["x"]), "ctx ?")
        self.assertEqual(self.line(None), "ctx ?")

    def test_no_cost(self):
        p = payload(tokens=36000)
        del p["cost"]
        self.assertEqual(self.line(p), "ctx 36k 18%")

    def test_null_current_usage_and_percentage(self):
        p = payload(tokens=36000)
        p["context_window"]["current_usage"] = None
        self.assertEqual(self.line(p), "ctx 36k 18% | $0.50")
        p["context_window"]["used_percentage"] = None
        self.assertEqual(self.line(p), "ctx 36k 18% | $0.50")     # from total_input_tokens

    def test_only_percentage(self):
        p = {"session_id": "x", "context_window": {"used_percentage": 42.7}}
        self.assertEqual(self.line(p), "ctx 42%")

    def test_running_total_larger_than_window_is_not_trusted(self):
        p = {"context_window": {"total_input_tokens": 900000, "context_window_size": 200000,
                                "current_usage": None, "used_percentage": None}}
        self.assertEqual(self.line(p), "ctx ?")

    def test_exceeds_200k_flag_alone(self):
        self.assertEqual(self.line({"exceeds_200k_tokens": True}), "ctx >200k !")

    def test_wrong_types_do_not_raise(self):
        p = {"session_id": 12, "prompt_id": 7, "cost": "lots", "context_window": [1],
             "prompt_cache": "warm", "exceeds_200k_tokens": "yes"}
        self.assertEqual(self.line(p), "ctx ?")
        p = payload()
        p["cost"]["total_cost_usd"] = True
        p["context_window"]["current_usage"]["input_tokens"] = "many"
        p["prompt_cache"] = {"expires_at": "soon", "ttl": 5}
        self.assertTrue(self.line(p).startswith("ctx "))

    def test_no_session_id_writes_no_state(self):
        p = payload()
        del p["session_id"]
        self.line(p)
        self.assertFalse(os.path.isdir(os.path.join(self.home, "state")))

    def test_damaged_state_and_config_files(self):
        os.makedirs(os.path.join(self.home, "state"))
        with open(SL.state_path(self.home, "sess-0001"), "w") as fh:
            fh.write("{not json")
        with open(os.path.join(self.home, "config.json"), "w") as fh:
            fh.write("[1, 2")
        self.assertEqual(self.line(payload(tokens=36000)), "ctx 36k 18% | $0.50")


class TurnMultipleTest(Base):
    def feed(self, steps, sid="sess-0001"):
        out = None
        for pid, cost in steps:
            out = self.line(payload(cost=cost, pid=pid, sid=sid))
        return out

    def test_three_times_the_first_turn(self):
        # session start (no prompt yet), turn 1 costs 0.40, turn 2 small, turn 3 costs 1.60
        out = self.feed([(None, 0.0), ("p1", 0.1), ("p1", 0.4), ("p2", 0.5), ("p3", 0.9), ("p3", 2.1)])
        # a costly turn on a small context is named, with no advice to compact
        self.assertEqual(out, "ctx 16k 7% | $2.10 | last turn 4x first")
        snap = SL.snapshot(payload(cost=2.1, pid="p3"), now=NOW, save=False)
        self.assertEqual(snap["first_turn_cost_usd"], 0.4)
        self.assertEqual(snap["turn_cost_usd"], 1.6)
        self.assertEqual(snap["turn_multiple"], 4.0)
        self.assertEqual(snap["turns_seen"], 3)

    def test_costly_turn_on_a_marked_context_gets_the_action(self):
        for pid, cost in ((None, 0.0), ("p1", 0.4), ("p2", 2.4)):
            out = self.line(payload(tokens=310000, size=1000000, cost=cost, pid=pid))
        self.assertEqual(out, "ctx 310k 31% ! | $2.40 | last turn 5x first | compact or hand off")

    def test_resumed_session_with_cost_already_has_no_baseline(self):
        out = self.feed([(None, 6.0), ("p1", 6.4), ("p2", 9.0)])
        self.assertEqual(out, "ctx 16k 7% | $9.00")

    def test_under_three_times_is_quiet(self):
        out = self.feed([(None, 0.0), ("p1", 0.4), ("p2", 1.5)])       # 1.1 / 0.4 = 2.75
        self.assertEqual(out, "ctx 16k 7% | $1.50")

    def test_decimal_multiple(self):
        out = self.feed([(None, 0.0), ("p1", 0.4), ("p2", 2.08)])      # 1.68 / 0.4 = 4.2
        self.assertIn("last turn 4.2x first", out)

    def test_first_turn_itself_never_flags(self):
        self.assertEqual(self.feed([(None, 0.0), ("p1", 9.0)]), "ctx 16k 7% | $9.00")

    def test_cheap_turn_under_the_floor_is_quiet(self):
        out = self.feed([(None, 0.0), ("p1", 0.02), ("p2", 0.42)])     # 20x, but only 40 cents
        self.assertEqual(out, "ctx 16k 7% | $0.42")

    def test_joined_mid_session_has_no_baseline(self):
        out = self.feed([("p7", 3.0), ("p8", 4.0), ("p9", 30.0)])
        self.assertEqual(out, "ctx 16k 7% | $30.00")

    def test_no_prompt_id_field_is_quiet(self):
        out = self.feed([(None, 0.0), (None, 0.4), (None, 5.0)])
        self.assertEqual(out, "ctx 16k 7% | $5.00")

    def test_cost_reset_does_not_go_negative(self):
        self.feed([(None, 0.0), ("p1", 0.4), ("p2", 3.0)])
        snap = SL.snapshot(payload(cost=0.1, pid="p3"), now=NOW)
        self.assertEqual(snap["turn_cost_usd"], 0.1)
        self.assertIsNone(snap["turn_multiple"])

    def test_sessions_do_not_share_state(self):
        self.feed([(None, 0.0), ("p1", 0.4), ("p2", 3.0)], sid="one")
        self.assertEqual(self.feed([(None, 0.0), ("p1", 3.0)], sid="two"), "ctx 16k 7% | $3.00")

    def test_state_file_is_small_and_only_rewritten_on_change(self):
        self.feed([(None, 0.0), ("p1", 0.4)])
        path = SL.state_path(self.home, "sess-0001")
        self.assertLess(os.path.getsize(path), 400)
        os.utime(path, (1, 1))
        self.line(payload(cost=0.4, pid="p1"))
        self.assertEqual(int(os.stat(path).st_mtime), 1)

    def test_session_id_cannot_escape_the_state_folder(self):
        self.line(payload(sid="../../evil"))
        self.assertEqual(os.listdir(os.path.join(self.home, "state")), ["evil.statusline.json"])


class CacheColdTest(Base):
    def test_cold_on_a_big_context(self):
        p = payload(tokens=310000, size=1000000, cost=9.8, cache=cold_cache(72 * 60, resend=312000))
        self.assertEqual(self.line(p),
                         "ctx 310k 31% ! | $9.80 | idle 1h12m | cache cold: compact before continuing")

    def test_cold_on_a_small_context_is_quiet(self):
        p = payload(tokens=36000, cache=cold_cache(72 * 60, resend=36000))
        self.assertEqual(self.line(p), "ctx 36k 18% | $0.50")

    def test_warm_cache_is_quiet(self):
        p = payload(tokens=310000, size=1000000, cost=9.8, cache=warm_cache(resend=312000))
        self.assertEqual(self.line(p), "ctx 310k 31% ! | $9.80")

    def test_five_minute_cache(self):
        p = payload(tokens=310000, size=1000000, cache=cold_cache(7 * 60, ttl="5m"))
        self.assertIn("idle 7m | cache cold", self.line(p))

    def test_cold_wins_over_context_action(self):
        p = payload(tokens=600000, size=1000000, cache=cold_cache(4000))
        out = self.line(p)
        self.assertIn("cache cold: compact before continuing", out)
        self.assertNotIn("compact or hand off", out)

    def test_caching_off_or_no_expiry_is_not_cold(self):
        c = cold_cache(4000)
        c["caching_observed"] = False
        self.assertNotIn("cold", self.line(payload(tokens=310000, size=1000000, cache=c)))
        c = cold_cache(4000)
        c["expires_at"] = None
        self.assertNotIn("cold", self.line(payload(tokens=310000, size=1000000, cache=c, sid="b")))

    def test_older_version_without_prompt_cache_uses_last_seen_time(self):
        p = payload(tokens=310000, size=1000000, cost=9.8)
        self.assertEqual(self.line(p, now=NOW), "ctx 310k 31% ! | $9.80")
        self.assertEqual(self.line(p, now=NOW + 20 * 60), "ctx 310k 31% ! | $9.80")
        self.assertEqual(self.line(p, now=NOW + 90 * 60),
                         "ctx 310k 31% ! | $9.80 | idle 1h30m | cache cold: compact before continuing")
        p["cost"]["total_cost_usd"] = 10.5      # the session moved again: no warning after the fact
        self.assertEqual(self.line(p, now=NOW + 91 * 60), "ctx 310k 31% ! | $10.50")
        self.assertEqual(self.line(p, now=NOW + 92 * 60), "ctx 310k 31% ! | $10.50")

    def test_older_version_reply_after_a_long_break_does_not_warn_late(self):
        p = payload(tokens=310000, size=1000000, cost=9.8)
        self.line(p, now=NOW)
        p["cost"]["total_cost_usd"] = 12.0
        self.assertEqual(self.line(p, now=NOW + 2 * 3600), "ctx 310k 31% ! | $12.00")

    def test_idle_format(self):
        self.assertEqual(SL.fmt_idle(59), "0m")
        self.assertEqual(SL.fmt_idle(3600), "1h00m")
        self.assertEqual(SL.fmt_idle(3 * 86400), "3d")


class WidthAndColorTest(Base):
    def big(self):
        return payload(tokens=600000, size=1000000, cost=41.2, cache=cold_cache(72 * 60))

    def test_parts_drop_in_order(self):
        full = self.line(self.big())
        self.assertEqual(full, "ctx 600k 60% !! | $41.20 | idle 1h12m | cache cold: compact before continuing")
        self.assertEqual(self.line(self.big(), width=70),
                         "ctx 600k 60% !! | $41.20 | cache cold: compact before continuing")
        self.assertEqual(self.line(self.big(), width=60),
                         "ctx 600k 60% !! | cache cold: compact before continuing")
        self.assertEqual(self.line(self.big(), width=20), "ctx 600k 60% !!")
        self.assertEqual(self.line(self.big(), width=10), "ctx 600...")
        for w in range(1, 90):
            self.assertLessEqual(len(self.line(self.big(), width=w)), w)

    def test_width_from_columns_and_config(self):
        os.environ["COLUMNS"] = "64"
        self.assertEqual(SL.env_width(), 60)
        self.assertLessEqual(len(SL.line_for(self.big(), now=NOW, color=False)), 60)
        self.set_cfg(max_width=30)
        self.assertEqual(SL.env_width(SL.load_settings()), 30)
        os.environ["COLUMNS"] = "junk"
        self.assertEqual(SL.env_width(), 0)

    def test_colour_on_by_default_and_three_ways_off(self):
        self.assertIn("\033[31m", SL.line_for(self.big(), now=NOW, width=0))
        self.assertNotIn("\033[", SL.line_for(payload(), now=NOW, width=0))     # quiet line is plain
        self.assertNotIn("\033[", SL.line_for(self.big(), now=NOW, width=0, color=False))
        os.environ["NO_COLOR"] = "1"
        self.assertNotIn("\033[", SL.line_for(self.big(), now=NOW, width=0))
        del os.environ["NO_COLOR"]
        self.set_cfg(color=False)
        self.assertNotIn("\033[", SL.line_for(self.big(), now=NOW, width=0))

    def test_colour_does_not_count_towards_width(self):
        out = SL.line_for(self.big(), now=NOW, width=70, color=True)
        self.assertEqual(SL._visible_len(out), len(self.line(self.big(), width=70)))


def run_cli(args, stdin_bytes, env_extra=None, home=None):
    env = dict(os.environ)
    env.update(env_extra or {})
    if home:
        env["COACH_HOME"] = home
    t0 = time.time()
    proc = subprocess.run([sys.executable, SCRIPT] + args, input=stdin_bytes, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=env, timeout=30)
    return proc, time.time() - t0


class CliTest(Base):
    def test_renders_from_stdin(self):
        proc, _ = run_cli(["--no-color"], json.dumps(payload(tokens=36000)).encode())
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, b"ctx 36k 18% | $0.50\n", b""))

    def test_bad_input_prints_a_safe_line_and_exits_zero(self):
        for raw in (b"", b"not json", b"[1,2,3]", b"\xff\xfe\x00", b'{"cost": {"total_cost_usd": 1e999}}'):
            proc, _ = run_cli([], raw)
            self.assertEqual(proc.returncode, 0, raw)
            self.assertEqual(proc.stderr, b"", raw)
            self.assertTrue(proc.stdout.startswith(b"ctx "), raw)
            self.assertNotIn(b"Traceback", proc.stdout)

    def test_internal_error_prints_the_minimal_line(self):
        # the coach home is a file, so nothing under it can be read or written
        bad = os.path.join(self.tmp, "afile")
        open(bad, "w").close()
        proc, _ = run_cli([], json.dumps(payload()).encode(), home=bad)
        self.assertEqual((proc.returncode, proc.stderr), (0, b""))
        self.assertTrue(proc.stdout.startswith(b"ctx "))
        orig = SL.snapshot
        SL.snapshot = lambda *a, **k: 1 / 0
        try:
            import io
            saved = sys.stdout, sys.stdin
            sys.stdout = io.StringIO()
            sys.stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload()).encode()))
            try:
                rc = SL.main(["--no-color"])
                text = sys.stdout.getvalue()
            finally:
                sys.stdout, sys.stdin = saved
        finally:
            SL.snapshot = orig
        self.assertEqual((rc, text), (0, SL.MINIMAL_LINE + "\n"))

    def test_segment_mode_prints_nothing_when_quiet(self):
        proc, _ = run_cli(["--segment"], json.dumps(payload()).encode())
        self.assertEqual((proc.returncode, proc.stdout), (0, b""))
        proc, _ = run_cli(["--segment", "--full", "--no-color"], json.dumps(payload(tokens=36000)).encode())
        self.assertEqual(proc.stdout, b"ctx 36k 18% | $0.50\n")

    def test_json_mode_is_json_and_saves_nothing(self):
        proc, _ = run_cli(["--json"], json.dumps(payload(tokens=512000, size=1000000)).encode())
        snap = json.loads(proc.stdout.decode())
        self.assertEqual(snap["context_level"], 2)
        self.assertEqual(snap["action"], "compact or hand off")
        self.assertFalse(os.path.isdir(os.path.join(self.home, "state")))

    def test_width_flag(self):
        p = payload(tokens=600000, size=1000000, cost=41.2)
        proc, _ = run_cli(["--no-color", "--width", "20"], json.dumps(p).encode())
        self.assertEqual(proc.stdout, b"ctx 600k 60% !!\n")

    def test_never_reads_the_transcript(self):
        p = payload()
        p["transcript_path"] = os.path.join(self.tmp, "missing", "t.jsonl")
        proc, _ = run_cli(["--no-color"], json.dumps(p).encode())
        self.assertEqual(proc.stdout, b"ctx 16k 7% | $0.50\n")

    def test_fast(self):
        raw = json.dumps(payload(tokens=600000, size=1000000, cost=41.2, pid="p1",
                                 cache=cold_cache(4000))).encode()
        run_cli([], raw)
        times = sorted(run_cli([], raw)[1] for _ in range(7))
        self.assertLess(times[3], 0.5, "median run took %.3fs" % times[3])   # loose bound for slow CI


class WrapTest(Base):
    def fake(self, body):
        path = os.path.join(self.tmp, "mine.sh")
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)
        return 'sh "%s"' % path

    def test_appends_segment_to_existing_line(self):
        self.set_cfg(wrap_command=self.fake('cat >/dev/null; printf "[Model X] demo\\nline two\\n"'))
        p = payload(tokens=512000, size=1000000, cost=41.2)
        proc, _ = run_cli(["--wrap", "--no-color"], json.dumps(p).encode())
        self.assertEqual(proc.stdout.decode(),
                         "[Model X] demo | ctx 512k 51% !! | compact or hand off\nline two\n")

    def test_existing_command_gets_the_same_stdin(self):
        self.set_cfg(wrap_command=self.fake(
            "python3 -c 'import sys,json; print(json.load(sys.stdin)[\"model\"][\"display_name\"])'"))
        proc, _ = run_cli(["--wrap", "--no-color"], json.dumps(payload()).encode())
        self.assertEqual(proc.stdout, b"Model X\n")      # quiet: their line, untouched

    def test_full_segment_when_asked(self):
        self.set_cfg(wrap_command=self.fake('echo mine'), wrap_full=True)
        proc, _ = run_cli(["--wrap", "--no-color"], json.dumps(payload(tokens=36000)).encode())
        self.assertEqual(proc.stdout, b"mine | ctx 36k 18% | $0.50\n")

    def test_segment_goes_below_when_the_line_is_full(self):
        self.set_cfg(wrap_command=self.fake('printf "\\033[32m%s\\033[0m\\n" "0123456789012345678901234567890123456789"'))
        p = payload(tokens=512000, size=1000000)
        proc, _ = run_cli(["--wrap", "--no-color", "--width", "60"], json.dumps(p).encode())
        lines = proc.stdout.decode().split("\n")
        self.assertEqual(lines[1], "ctx 512k 51% !! | compact or hand off")
        self.assertTrue(lines[0].endswith("\033[0m"))

    def test_slow_existing_command_times_out(self):
        self.set_cfg(wrap_command=self.fake("sleep 20; echo late"), wrap_timeout_ms=300)
        p = payload(tokens=512000, size=1000000, cost=41.2)
        proc, took = run_cli(["--wrap", "--no-color"], json.dumps(p).encode())
        self.assertLess(took, 5)
        self.assertEqual(proc.stdout, b"ctx 512k 51% !! | $41.20 | compact or hand off\n")
        self.assertEqual(proc.returncode, 0)

    def test_failing_or_missing_command_falls_back_to_the_coach_line(self):
        self.set_cfg(wrap_command="exit 3")
        proc, _ = run_cli(["--wrap", "--no-color"], json.dumps(payload(tokens=36000)).encode())
        self.assertEqual(proc.stdout, b"ctx 36k 18% | $0.50\n")
        self.set_cfg()
        proc, _ = run_cli(["--wrap", "--no-color"], json.dumps(payload(tokens=36000)).encode())
        self.assertEqual(proc.stdout, b"ctx 36k 18% | $0.50\n")

    def test_command_that_prints_nothing_gives_the_coach_line(self):
        self.set_cfg(wrap_command="true")
        proc, _ = run_cli(["--wrap", "--no-color"], json.dumps(payload(tokens=36000)).encode())
        self.assertEqual(proc.stdout, b"ctx 36k 18% | $0.50\n")

    def test_cancelled_wrapper_takes_the_slow_command_with_it(self):
        mark = os.path.join(self.tmp, "pid")
        self.set_cfg(wrap_command=self.fake("echo $$ > '%s'; sleep 30" % mark), wrap_timeout_ms=20000)
        proc = subprocess.Popen([sys.executable, SCRIPT, "--wrap"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        proc.stdin.write(json.dumps(payload()).encode())
        proc.stdin.close()
        for _ in range(100):
            if os.path.exists(mark) and os.path.getsize(mark):
                break
            time.sleep(0.05)
        with open(mark) as fh:
            child = int(fh.read().strip())
        proc.terminate()
        self.assertEqual(proc.wait(timeout=10), 0)
        proc.stdout.close()
        proc.stderr.close()
        gone = False
        for _ in range(60):
            try:
                os.kill(child, 0)
            except OSError:
                gone = True
                break
            time.sleep(0.05)
        self.assertTrue(gone, "the wrapped command was left running")

    def test_output_survives_a_narrow_output_encoding(self):
        self.set_cfg(wrap_command=self.fake("printf 'caf\\303\\251\\n'"))
        proc, _ = run_cli(["--wrap", "--no-color"], json.dumps(payload()).encode(),
                          env_extra={"PYTHONIOENCODING": "ascii"})
        self.assertEqual(proc.stdout, b"caf?\n")

    def test_never_runs_itself_in_a_loop(self):
        self.set_cfg(wrap_command=self.fake("echo should-not-run"))
        proc, _ = run_cli(["--wrap", "--no-color"], json.dumps(payload(tokens=36000)).encode(),
                          env_extra={SL.WRAP_ENV: "1"})
        self.assertEqual(proc.stdout, b"ctx 36k 18% | $0.50\n")

    def test_coach_error_keeps_their_line(self):
        self.set_cfg(wrap_command=self.fake("echo mine"))
        orig = SL.snapshot
        SL.snapshot = lambda *a, **k: 1 / 0
        try:
            lines, how = SL.wrapped(payload(), b"{}", color=False, width=0)
        finally:
            SL.snapshot = orig
        self.assertEqual((lines, how), (["mine"], "ok"))


class SetupTest(Base):
    def setUp(self):
        Base.setUp(self)
        self.claude = os.environ["CLAUDE_CONFIG_DIR"]
        os.makedirs(self.claude)
        self.settings = os.path.join(self.claude, "settings.json")

    def write_settings(self, obj, indent=2):
        with open(self.settings, "w") as fh:
            fh.write(json.dumps(obj, indent=indent) + "\n")

    def read_settings(self):
        with open(self.settings) as fh:
            return json.load(fh)

    def test_status_unset(self):
        self.write_settings({"model": "x", "hooks": {}})
        s = SL.status()
        self.assertEqual(s["state"], "unset")
        self.assertEqual(s["settings_file"], self.settings)
        self.assertEqual(s["proposed"], {"statusLine": {
            "type": "command", "command": 'python3 "%s"' % os.path.join(self.home, "coach_statusline.py")}})
        self.assertNotIn("proposed_wrap", s)
        json.dumps(s)

    def test_status_missing_and_unreadable_file(self):
        self.assertEqual(SL.status()["state"], "unset")
        self.assertEqual(SL.status()["settings_file_state"], "missing")
        with open(self.settings, "w") as fh:
            fh.write("{ nope")
        self.assertEqual(SL.status()["state"], "unreadable")
        self.assertFalse(SL.install()["ok"])
        with open(self.settings) as fh:
            self.assertEqual(fh.read(), "{ nope")

    def test_status_other_offers_wrap(self):
        mine = {"type": "command", "command": "~/.claude/statusline.sh", "padding": 2}
        self.write_settings({"statusLine": mine})
        s = SL.status()
        self.assertEqual(s["state"], "other")
        self.assertTrue(s["can_wrap"])
        self.assertEqual(s["proposed_wrap"]["statusLine"]["padding"], 2)
        self.assertTrue(s["proposed_wrap"]["statusLine"]["command"].endswith('coach_statusline.py" --wrap'))
        self.assertTrue(s["segment_command"].endswith('coach_statusline.py" --segment'))

    def test_status_sees_project_files_and_disable_all_hooks(self):
        self.write_settings({"disableAllHooks": True})
        proj = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(proj, ".claude"))
        with open(os.path.join(proj, ".claude", "settings.local.json"), "w") as fh:
            json.dump({"statusLine": {"type": "command", "command": "echo hi"}}, fh)
        s = SL.status(project_dir=proj)
        self.assertTrue(s["disable_all_hooks"])
        self.assertEqual(len(s["elsewhere"]), 1)
        self.assertTrue(s["elsewhere"][0]["file"].endswith("settings.local.json"))

    def test_install_direct_then_remove(self):
        before = {"model": "x", "permissions": {"allow": ["Bash(ls:*)"]}, "hooks": {"Stop": []}}
        self.write_settings(before, indent=4)
        res = SL.install()
        self.assertTrue(res["ok"] and res["changed"], res)
        after = self.read_settings()
        self.assertEqual(after["statusLine"], SL.status()["proposed"]["statusLine"])
        self.assertEqual({k: v for k, v in after.items() if k != "statusLine"}, before)
        self.assertEqual(list(after)[:3], list(before))                      # key order kept
        with open(self.settings) as fh:
            self.assertIn('\n    "model"', fh.read())                        # indent kept
        self.assertTrue(os.path.isfile(res["backup"]))
        self.assertEqual(SL.status()["state"], "coach")
        self.assertTrue(SL.status()["launcher"]["exists"])
        self.assertFalse(SL.install()["changed"])                            # second run changes nothing
        rem = SL.remove()
        self.assertTrue(rem["ok"] and rem["changed"])
        self.assertEqual(self.read_settings(), before)
        self.assertFalse(os.path.exists(os.path.join(self.home, "coach_statusline.py")))
        self.assertFalse(os.path.exists(os.path.join(self.home, "config.json")))

    def test_install_creates_a_missing_settings_file(self):
        res = SL.install(refresh=60)
        self.assertTrue(res["ok"])
        self.assertEqual(self.read_settings()["statusLine"]["refreshInterval"], 60)

    def test_existing_status_line_is_left_alone_by_default(self):
        mine = {"type": "command", "command": "~/.claude/statusline.sh"}
        self.write_settings({"statusLine": mine})
        res = SL.install()
        self.assertFalse(res["ok"])
        self.assertIn("left alone", res["error"])
        self.assertEqual(self.read_settings(), {"statusLine": mine})
        self.assertFalse(os.path.exists(os.path.join(self.home, "coach_statusline.py")))

    def test_wrap_install_and_remove_restores(self):
        mine = {"type": "command", "command": "echo mine", "padding": 1}
        self.write_settings({"statusLine": mine, "model": "x"})
        with open(os.path.join(self.home, "config.json"), "w") as fh:
            json.dump({"notify": True}, fh)
        res = SL.install(mode="wrap")
        self.assertTrue(res["ok"], res)
        sl = self.read_settings()["statusLine"]
        self.assertEqual(sl["padding"], 1)
        self.assertTrue(sl["command"].endswith("--wrap"))
        self.assertEqual(SL.status()["state"], "coach_wrap")
        self.assertEqual(SL.status()["wrapped_command"], "echo mine")
        self.assertEqual(SL._previous(SL.load_settings()), mine)
        # wrapping again does not wrap the wrapper
        self.assertTrue(SL.install(mode="wrap")["ok"])
        self.assertEqual(SL.load_settings()["wrap_command"], "echo mine")
        rem = SL.remove()
        self.assertEqual(rem["restored"], mine)
        self.assertEqual(self.read_settings(), {"statusLine": mine, "model": "x"})
        self.assertEqual(list(self.read_settings()["statusLine"]), ["type", "command", "padding"])
        with open(os.path.join(self.home, "config.json")) as fh:
            self.assertEqual(json.load(fh), {"notify": True})                # other keys kept

    def test_replace_keeps_the_old_one_for_remove(self):
        mine = {"type": "command", "command": "echo mine"}
        self.write_settings({"statusLine": mine})
        self.assertTrue(SL.install(replace=True)["ok"])
        self.assertEqual(SL.status()["state"], "coach")
        self.assertEqual(SL.remove()["restored"], mine)
        self.assertFalse(os.path.exists(os.path.join(self.home, "config.json")))   # no empty file left

    def test_wrap_needs_something_to_wrap(self):
        self.write_settings({})
        self.assertFalse(SL.install(mode="wrap")["ok"])
        self.write_settings({"statusLine": {"type": "other"}})
        self.assertFalse(SL.install(mode="wrap")["ok"])
        self.assertEqual(self.read_settings(), {"statusLine": {"type": "other"}})

    def test_remove_leaves_someone_elses_status_line(self):
        mine = {"type": "command", "command": "echo mine"}
        self.write_settings({"statusLine": mine})
        rem = SL.remove()
        self.assertTrue(rem["ok"])
        self.assertFalse(rem["changed"])
        self.assertEqual(self.read_settings(), {"statusLine": mine})

    def test_own_script_that_calls_the_segment_is_theirs(self):
        seg = SL.status()["segment_command"]
        for command in ("~/mine.sh | " + seg, seg, "sh ~/coach/statusline.py",
                        SL.coach_command() + " ; echo more"):
            mine = {"type": "command", "command": command}
            self.write_settings({"statusLine": mine})
            self.assertEqual(SL.status()["state"], "other", command)
            self.assertFalse(SL.install()["ok"], command)
            launcher = os.path.join(self.home, "coach_statusline.py")
            seg_res = SL.install(mode="segment")
            self.assertTrue(seg_res["ok"] and os.path.isfile(launcher))
            self.assertEqual(seg_res["segment_command"], seg)
            self.assertEqual(self.read_settings(), {"statusLine": mine})
            rem = SL.remove()
            self.assertFalse(rem["changed"], command)
            self.assertEqual(self.read_settings(), {"statusLine": mine})
            self.assertTrue(os.path.exists(launcher))        # their script may call it
            os.remove(launcher)

    def test_entry_from_another_coach_home_is_left_alone(self):
        mine = {"type": "command", "command": "echo mine"}
        self.write_settings({"statusLine": mine})
        self.assertTrue(SL.install(mode="wrap")["ok"])
        wrapped = self.read_settings()
        other = os.path.join(self.tmp, "other-home")
        os.makedirs(other)
        os.environ["COACH_HOME"] = other
        self.assertEqual(SL.status()["state"], "other")
        self.assertFalse(SL.remove()["changed"])
        self.assertEqual(self.read_settings(), wrapped)
        os.environ["COACH_HOME"] = self.home
        self.assertEqual(SL.remove()["restored"], mine)

    def test_wrap_to_direct_needs_replace(self):
        mine = {"type": "command", "command": "echo mine", "padding": 1}
        self.write_settings({"statusLine": mine})
        self.assertTrue(SL.install(mode="wrap")["ok"])
        wrapped = self.read_settings()
        self.assertFalse(SL.install()["ok"])
        self.assertEqual(self.read_settings(), wrapped)
        self.assertTrue(SL.install(replace=True)["ok"])
        self.assertEqual(SL.status()["state"], "coach")
        self.assertTrue(SL.install(mode="wrap")["ok"])            # and back again
        self.assertEqual(self.read_settings(), wrapped)
        self.assertEqual(SL.remove()["restored"], mine)

    def test_failed_install_changes_nothing_and_loses_nothing(self):
        mine = {"type": "command", "command": "echo mine"}
        self.write_settings({"statusLine": mine})
        with open(os.path.join(self.home, "config.json"), "w") as fh:
            fh.write('{"notify": true}\n')
        os.chmod(os.path.join(self.home, "config.json"), 0o600)
        real = SL._dump_settings

        def boom(*a, **k):
            raise OSError("disk full")
        SL._dump_settings = boom
        try:
            res = SL.install(mode="wrap")
        finally:
            SL._dump_settings = real
        self.assertFalse(res["ok"])
        self.assertIn("disk full", res["error"])
        self.assertEqual(self.read_settings(), {"statusLine": mine})
        self.assertFalse(os.path.exists(os.path.join(self.home, "coach_statusline.py")))
        with open(os.path.join(self.home, "config.json")) as fh:
            self.assertEqual(fh.read(), '{"notify": true}\n')
        # and the next try works and keeps the config private
        self.assertTrue(SL.install(mode="wrap")["ok"])
        self.assertEqual(os.stat(os.path.join(self.home, "config.json")).st_mode & 0o777, 0o600)
        self.assertEqual(SL.remove()["restored"], mine)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores folder permissions")
    def test_read_only_coach_home_leaves_settings_alone(self):
        mine = {"type": "command", "command": "echo mine"}
        self.write_settings({"statusLine": mine})
        os.chmod(self.home, 0o500)
        try:
            res = SL.install(mode="wrap")
        finally:
            os.chmod(self.home, 0o700)
        self.assertFalse(res["ok"])
        self.assertEqual(self.read_settings(), {"statusLine": mine})

    def test_rerun_after_replace_is_allowed(self):
        mine = {"type": "command", "command": "echo mine"}
        self.write_settings({"statusLine": mine})
        self.assertTrue(SL.install(replace=True)["ok"])
        res = SL.install(refresh=60)
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.read_settings()["statusLine"]["refreshInterval"], 60)
        self.assertEqual(SL.remove()["restored"], mine)

    def test_restored_line_that_calls_the_segment_keeps_the_launcher(self):
        self.assertTrue(SL.install(mode="segment")["ok"])
        launcher = os.path.join(self.home, "coach_statusline.py")
        mine = {"type": "command", "command": "echo mine; " + SL.status()["segment_command"]}
        self.write_settings({"statusLine": mine})
        self.assertTrue(SL.install(mode="wrap")["ok"])
        rem = SL.remove()
        self.assertEqual(rem["restored"], mine)
        self.assertTrue(os.path.isfile(launcher))

    def test_failed_install_puts_back_a_config_it_had_emptied(self):
        self.write_settings({"model": "x"})
        with open(os.path.join(self.home, "config.json"), "w") as fh:
            fh.write('{"statusline": {"wrap_full": true}}\n')
        real = SL._dump_settings

        def boom(*a, **k):
            raise UnicodeEncodeError("utf-8", "x", 0, 1, "bad")
        SL._dump_settings = boom
        try:
            res = SL.install()
        finally:
            SL._dump_settings = real
        self.assertFalse(res["ok"])
        with open(os.path.join(self.home, "config.json")) as fh:
            self.assertEqual(fh.read(), '{"statusline": {"wrap_full": true}}\n')
        self.assertFalse(os.path.exists(os.path.join(self.home, "coach_statusline.py")))
        self.assertFalse(os.path.exists(self.settings + ".coach-backup"))
        self.assertEqual(sorted(os.listdir(self.claude)), ["settings.json"])

    def test_replace_keeps_a_status_line_of_any_shape(self):
        self.write_settings({"statusLine": "legacy text"})
        self.assertFalse(SL.install(mode="wrap")["ok"])
        self.assertTrue(SL.install(replace=True)["ok"])
        self.assertEqual(SL.remove()["restored"], "legacy text")
        self.assertEqual(self.read_settings(), {"statusLine": "legacy text"})

    def test_remove_refuses_when_config_is_damaged(self):
        mine = {"type": "command", "command": "echo mine"}
        self.write_settings({"statusLine": mine})
        self.assertTrue(SL.install(mode="wrap")["ok"])
        wrapped = self.read_settings()
        with open(os.path.join(self.home, "config.json"), "w") as fh:
            fh.write("{ broken")
        rem = SL.remove()
        self.assertFalse(rem["ok"])
        self.assertIn("config.json", rem["error"])
        self.assertEqual(self.read_settings(), wrapped)
        self.assertFalse(SL.install()["ok"])
        self.assertEqual(self.read_settings(), wrapped)

    def test_backup_is_private_and_settings_mode_is_kept(self):
        self.write_settings({"env": {"TOKEN": "x"}})
        os.chmod(self.settings, 0o600)
        res = SL.install()
        self.assertEqual(os.stat(res["backup"]).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.settings).st_mode & 0o777, 0o600)
        with open(res["backup"]) as fh:
            self.assertEqual(json.load(fh), {"env": {"TOKEN": "x"}})

    def test_backup_is_refreshed_on_a_later_install(self):
        self.write_settings({"a": 1})
        SL.install()
        SL.remove()
        self.write_settings({"a": 2})
        res = SL.install()
        with open(res["backup"]) as fh:
            self.assertEqual(json.load(fh), {"a": 2})

    def test_symlinked_settings_file_stays_a_link(self):
        real = os.path.join(self.tmp, "dotfiles-settings.json")
        with open(real, "w") as fh:
            fh.write('{"model": "x"}\n')
        os.symlink(real, self.settings)
        self.assertTrue(SL.install()["ok"])
        self.assertTrue(os.path.islink(self.settings))
        with open(real) as fh:
            self.assertIn("statusLine", json.load(fh))
        self.assertTrue(SL.remove()["ok"])
        self.assertTrue(os.path.islink(self.settings))
        with open(real) as fh:
            self.assertEqual(json.load(fh), {"model": "x"})

    def test_settings_changed_meanwhile_is_not_overwritten(self):
        self.write_settings({"model": "x"})
        orig = SL._save_coach_cfg
        real_read = SL._read_text
        calls = []

        def racing_read(path):
            calls.append(path)
            if len(calls) == 2:           # someone else writes between our read and our write
                with open(self.settings, "w") as fh:
                    fh.write('{"model": "y"}\n')
            return real_read(path)
        SL._read_text = racing_read
        try:
            res = SL.install()
        finally:
            SL._read_text = real_read
            SL._save_coach_cfg = orig
        self.assertFalse(res["ok"])
        self.assertIn("changed while", res["error"])
        self.assertEqual(self.read_settings(), {"model": "y"})
        self.assertFalse(os.path.exists(os.path.join(self.home, "coach_statusline.py")))

    def test_command_is_shell_safe_for_an_odd_coach_home(self):
        odd = os.path.join(self.tmp, 'co$ach "home`')
        os.makedirs(odd)
        os.environ["COACH_HOME"] = odd
        self.write_settings({})
        self.assertTrue(SL.install()["ok"])
        command = self.read_settings()["statusLine"]["command"]
        self.assertEqual(SL.status()["state"], "coach")
        proc = subprocess.run(command, shell=True, input=json.dumps(payload(tokens=36000)).encode(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=dict(os.environ, NO_COLOR="1"), timeout=30)
        self.assertEqual((proc.stdout, proc.stderr), (b"ctx 36k 18% | $0.50\n", b""))
        self.assertTrue(SL.remove()["changed"])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores folder permissions")
    def test_setup_cli_reports_errors_as_json(self):
        os.chmod(self.claude, 0o500)
        try:
            proc, _ = run_cli(["install"], b"")
        finally:
            os.chmod(self.claude, 0o700)
        out = json.loads(proc.stdout.decode())
        self.assertEqual((proc.returncode, out["ok"], proc.stderr), (2, False, b""))

    def test_installed_launcher_runs_the_line_end_to_end(self):
        self.write_settings({"statusLine": {"type": "command", "command": "echo mine"}})
        self.assertTrue(SL.install(mode="wrap")["ok"])
        command = self.read_settings()["statusLine"]["command"]
        p = payload(tokens=512000, size=1000000, cost=41.2)
        proc = subprocess.run(command, shell=True, input=json.dumps(p).encode(), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=dict(os.environ, NO_COLOR="1"), timeout=30)
        self.assertEqual((proc.returncode, proc.stderr), (0, b""))
        self.assertEqual(proc.stdout, b"mine | ctx 512k 51% !! | compact or hand off\n")

    def test_launcher_finds_a_newer_plugin_version_and_survives_none(self):
        self.assertTrue(SL.install()["ok"])
        launcher = os.path.join(self.home, "coach_statusline.py")
        note = os.path.join(self.home, "statusline.path")
        with open(note, "w") as fh:
            fh.write(os.path.join(self.tmp, "gone") + "\n")
        raw = json.dumps(payload(tokens=36000)).encode()
        env = dict(os.environ, NO_COLOR="1")
        proc = subprocess.run([sys.executable, launcher], input=raw, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env, timeout=30)
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, b"", b""))   # no plugin: blank, no error
        base = os.path.join(self.claude, "plugins", "cache", "market", "agentic-coach")
        new, old, older = (os.path.join(base, v) for v in ("9.10.0", "9.9.9", "9.9.8"))
        for root in (new, old, older):         # the highest number is made first, so it is not the newest folder
            shutil.copytree(os.path.join(ROOT, "coach"), os.path.join(root, "coach"),
                            ignore=shutil.ignore_patterns("__pycache__"))
        os.utime(new, (1000, 1000))
        open(os.path.join(old, ".orphaned_at"), "w").close()
        proc = subprocess.run([sys.executable, launcher], input=raw, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env, timeout=30)
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, b"ctx 36k 18% | $0.50\n", b""))
        with open(note) as fh:
            self.assertEqual(fh.read().strip(), new)

    def test_cli_status_install_remove_on_a_copy(self):
        self.write_settings({"model": "x"})
        copy = os.path.join(self.tmp, "settings-copy.json")
        shutil.copy(self.settings, copy)
        proc, _ = run_cli(["status", "--settings", copy], b"")
        self.assertEqual(json.loads(proc.stdout.decode())["state"], "unset")
        proc, _ = run_cli(["install", "--settings", copy], b"")
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(json.loads(proc.stdout.decode())["ok"])
        proc, _ = run_cli(["status", "--settings", copy], b"")
        self.assertEqual(json.loads(proc.stdout.decode())["state"], "coach")
        proc, _ = run_cli(["remove", "--settings", copy], b"")
        self.assertTrue(json.loads(proc.stdout.decode())["changed"])
        with open(copy) as fh:
            self.assertEqual(json.load(fh), {"model": "x"})
        self.assertEqual(self.read_settings(), {"model": "x"})               # the other file never touched

    def test_page_data_is_escaped_and_serialisable(self):
        self.write_settings({})
        d = SL.page_data()
        json.dumps(d)
        self.assertFalse(d["on"])
        self.assertEqual(d["state"], "unset")
        self.assertTrue(any("&gt;" in r["meaning"] or "<" not in r["meaning"] for r in d["legend"]))
        for r in d["legend"] + d["thresholds"]:
            for v in r.values():
                self.assertNotIn("<", v)


if __name__ == "__main__":
    unittest.main()
