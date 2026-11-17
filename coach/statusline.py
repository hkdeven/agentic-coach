#!/usr/bin/env python3
"""Status line version of the coach's nudge: always visible, costs no tokens.

Claude Code runs the status line command after each assistant message, hands it
JSON on stdin and shows what it prints (https://code.claude.com/docs/en/statusline).
This script prints one compact line from the documented fields only:

  ctx 182k 18% | $4.21                                      nothing wrong
  ctx 512k 51% !! | $41.20 | last turn 4.2x first | compact or hand off
  ctx 310k 31% ! | $9.80 | idle 1h12m | cache cold: compact before continuing

Thresholds are the coach's own (signals.T), so this line and the Stop-hook
nudge (nudge.py) agree. Both honour the off switch <coach home>/off.

Render (stdin is the status line JSON):
  python3 coach/statusline.py                 the whole line
  python3 coach/statusline.py --segment       only the alert parts; nothing when quiet
  python3 coach/statusline.py --segment --full   the whole line, for your own script
  python3 coach/statusline.py --wrap          run the status line you had, add the segment
  python3 coach/statusline.py --json          the numbers behind the line, as JSON
  flags: --no-color, --width N

Setup helpers (used by /coach setup; each prints JSON):
  python3 coach/statusline.py status  [--settings FILE] [--project DIR]
  python3 coach/statusline.py install [--wrap | --segment] [--replace] [--refresh N] [--settings FILE]
  python3 coach/statusline.py remove  [--settings FILE]

Speed: no transcript is read. Everything comes from stdin plus one small state
file per session, <coach home>/state/<session>.statusline.json, which keeps the
cost at the start of the current turn and the cost of the first turn.
No network. Any error prints a minimal line and exits 0.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)

MINIMAL_LINE = "ctx ?"
SEP = " | "
STATE_KEEP_DAYS = 30
LAUNCHER_NAME = "coach_statusline.py"
PATH_FILE_NAME = "statusline.path"
CONFIG_KEY = "statusline"
WRAP_ENV = "COACH_STATUSLINE_WRAPPED"
TTL_SECONDS = {"5m": 300, "1h": 3600}

# Used when signals.py cannot be imported. Same numbers as signals.T.
_FALLBACK_T = {"peak_context": 500000, "idle_context": 200000, "idle_gap_min": 60}

ACTION_CONTEXT = "compact or hand off"
ACTION_COLD = "cache cold: compact before continuing"

LEGEND = [
    {"part": "ctx 182k 18%", "meaning": "Tokens in the context window now, and the share of the window used."},
    {"part": "!", "meaning": "Context passed the first threshold: a break now would be costly."},
    {"part": "!!", "meaning": "Context passed the coach's context threshold: every turn re-reads all of it."},
    {"part": "$4.21", "meaning": "Estimated cost of the session so far, at list price."},
    {"part": "last turn 4.2x first", "meaning": "The current turn has cost that many times the session's first turn."},
    {"part": "idle 1h12m", "meaning": "Time since the last turn. Shown once the prompt cache has expired on a large context."},
    {"part": "cache cold: compact before continuing", "meaning": "The next turn re-sends the whole context at full price. Compact first, or hand off to a new session."},
    {"part": "compact or hand off", "meaning": "Context or turn cost is over its threshold: run /compact, or write a handoff and start a new session."},
]


# ---------- small helpers ----------

def _coach_home():
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import common as C
        return C.coach_home()
    except Exception:
        claude = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
        return os.environ.get("COACH_HOME") or os.path.join(claude, "coach")


def _claude_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def _num(x):
    """A finite number, or None. true and false are not numbers here."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    if x != x or x in (float("inf"), float("-inf")):
        return None
    return x


def _dict(x):
    return x if isinstance(x, dict) else {}


def _read_json(path):
    """(value, state): state is "ok", "missing" or "bad"."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), "ok"
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError):
        return None, "bad"


def _write_text(path, text, mode=None):
    """Atomic write. A symlink is followed, so the file it points at is the one replaced."""
    path = os.path.realpath(path)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644 if mode is None else mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def thresholds(cfg=None):
    """The numbers the line is judged against. JSON-serialisable.

    Context and cache numbers come from signals.T so the nudge and the line
    agree. A "statusline" object in <coach home>/config.json may override any
    of these keys with a positive number.
    """
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import signals
        T = signals.T
    except Exception:
        T = _FALLBACK_T
    out = {
        "ctx_warn_tokens": T.get("idle_context", _FALLBACK_T["idle_context"]),
        "ctx_over_tokens": T.get("peak_context", _FALLBACK_T["peak_context"]),
        "ctx_warn_pct": 70,
        "ctx_over_pct": 85,
        "turn_multiple": 3.0,
        "turn_floor_usd": 0.5,
        "cold_tokens": T.get("idle_context", _FALLBACK_T["idle_context"]),
        "fallback_ttl_min": T.get("idle_gap_min", _FALLBACK_T["idle_gap_min"]),
        "wrap_timeout_ms": 1500,
    }
    for k in list(out):
        v = _num(_dict(cfg).get(k))
        if v is not None and v > 0:
            out[k] = v
    return out


def load_settings(coach_dir=None):
    """The "statusline" object from <coach home>/config.json. {} when absent or broken."""
    raw, _ = _read_json(os.path.join(coach_dir or _coach_home(), "config.json"))
    return _dict(_dict(raw).get(CONFIG_KEY))


def _previous(cfg):
    """The status line that was set before the coach's, kept as JSON text so key order survives."""
    try:
        return json.loads(cfg.get("previous") or "null")
    except (TypeError, ValueError):
        return None


def fmt_tokens(n):
    if n >= 999500:
        return ("%.1fM" % (n / 1000000.0)).replace(".0M", "M")
    if n >= 1000:
        return "%dk" % int(round(n / 1000.0))
    return "%d" % int(n)


def fmt_cost(usd):
    if usd >= 100:
        return "$%d" % int(round(usd))
    return "$%.2f" % usd


def fmt_idle(seconds):
    m = int(max(0, seconds) // 60)
    if m < 60:
        return "%dm" % m
    if m < 48 * 60:
        return "%dh%02dm" % (m // 60, m % 60)
    return "%dd" % (m // 1440)


# ---------- per-session state ----------

def _sid(payload):
    raw = str(payload.get("session_id") or "")
    return "".join(ch for ch in raw if ch.isalnum() or ch in "-_")[:64]


def state_path(coach_dir, sid):
    return os.path.join(coach_dir, "state", sid + ".statusline.json")


def _new_state(pid, total=None):
    # clean: the state began before the first prompt and before any cost, so
    # the first turn it sees whole really is the session's first turn.
    return {"v": 1, "pid": "", "turns": 0, "clean": not pid and not (total or 0) >= 0.01,
            "base": 0.0, "last": 0.0,
            "first": None, "t": None, "tok": None}


def _load_state(path):
    st, how = _read_json(path)
    if how == "ok" and isinstance(st, dict) and st.get("v") == 1:
        base = _new_state("")
        base.update(st)
        for k in ("base", "last"):
            if _num(base[k]) is None:
                base[k] = 0.0
        if _num(base["first"]) is None:
            base["first"] = None
        if _num(base["t"]) is None:
            base["t"] = None
        if not isinstance(base["turns"], int) or isinstance(base["turns"], bool):
            base["turns"] = 0
        base["pid"] = str(base["pid"] or "")
        return base
    return None


def _prune(state_dir):
    cutoff = time.time() - STATE_KEEP_DAYS * 86400
    for name in os.listdir(state_dir):
        if not name.endswith(".statusline.json"):
            continue
        p = os.path.join(state_dir, name)
        try:
            if os.stat(p).st_mtime < cutoff:
                os.remove(p)
        except OSError:
            pass


def advance(st, pid, total, tokens, now):
    """Fold one status line payload into the session state. Returns True if it changed.

    A turn is one prompt_id. The cost of a turn is the session cost when the
    next prompt_id shows up, less the cost when this one did.
    """
    before = json.dumps(st, sort_keys=True)
    active = False
    if total is not None:
        if total < st["last"] - 1e-9:        # the cost went back (/clear or a restart)
            st["base"] = 0.0
            st["last"] = 0.0
        if pid and pid != st["pid"]:
            if st["pid"]:
                prev = st["last"] - st["base"]
                if st["first"] is None and st["clean"] and prev >= 0.01:
                    st["first"] = round(prev, 6)
            st["turns"] += 1
            st["pid"] = pid
            st["base"] = st["last"]
            active = True
        if abs(total - st["last"]) > 1e-9:
            active = True
        st["last"] = total
    elif pid and pid != st["pid"]:
        st["turns"] += 1
        st["pid"] = pid
        active = True
    if tokens is not None and tokens != st["tok"]:
        st["tok"] = tokens
        active = True
    if active or st["t"] is None:
        st["t"] = int(now)
    return json.dumps(st, sort_keys=True) != before


# ---------- the numbers ----------

def context_usage(payload):
    """(tokens or None, percent or None, window size or None) from the documented fields."""
    cw = _dict(payload.get("context_window"))
    size = _num(cw.get("context_window_size"))
    pct = _num(cw.get("used_percentage"))
    tokens = None
    cu = cw.get("current_usage")
    if isinstance(cu, dict):
        parts = [_num(cu.get(k)) for k in
                 ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
        if any(p is not None for p in parts):
            tokens = sum(p or 0 for p in parts)
    if tokens is None and pct is not None and size:
        tokens = pct * size / 100.0
    if tokens is None:
        tin = _num(cw.get("total_input_tokens"))
        # older versions kept a running session total here: only trust a
        # figure that fits in the window
        if tin is not None and (not size or tin <= size):
            tokens = tin
    if pct is None and tokens is not None and size:
        pct = 100.0 * tokens / size
    if tokens is not None and tokens < 0:
        tokens = None
    if pct is not None:
        pct = max(0.0, min(100.0, pct))
    return tokens, pct, size


def cache_state(payload, st, tokens, T, now):
    """{"cold": bool, "idle_s": seconds or None, "resend": tokens or None, "source": ...}.

    Uses prompt_cache when Claude Code sends it. Older versions do not: then
    the time since this script last saw the session move stands in, against a
    cautious cache life (fallback_ttl_min).
    """
    out = {"cold": False, "idle_s": None, "resend": tokens, "source": "none"}
    pc = payload.get("prompt_cache")
    if isinstance(pc, dict):
        out["source"] = "prompt_cache"
        if pc.get("caching_observed") is False:
            return out
        exp = _num(pc.get("expires_at"))
        if exp is None:
            return out
        re_tok = _num(pc.get("recache_tokens_if_cold"))
        if re_tok is not None:
            out["resend"] = re_tok
        out["cold"] = now >= exp - 1       # the redraw at expiry can come a moment early
        ttl = TTL_SECONDS.get(pc.get("ttl"))
        last = exp - ttl if ttl else (st or {}).get("t")
        if last is not None:
            out["idle_s"] = max(0, now - last)
        elif out["cold"]:
            out["idle_s"] = max(0, now - exp)
        return out
    last = (st or {}).get("t")
    if last is not None:
        out["source"] = "state"
        out["idle_s"] = max(0, now - last)
        out["cold"] = out["idle_s"] >= T["fallback_ttl_min"] * 60
    return out


def snapshot(payload, coach_dir=None, now=None, save=True):
    """Everything the line shows, as a JSON-serialisable dict. Never reads a transcript."""
    payload = _dict(payload)
    now = time.time() if now is None else now
    coach_dir = coach_dir or _coach_home()
    cfg = load_settings(coach_dir)
    T = thresholds(cfg)
    off = os.path.exists(os.path.join(coach_dir, "off"))

    tokens, pct, size = context_usage(payload)
    over_200k = payload.get("exceeds_200k_tokens") is True
    total = _num(_dict(payload.get("cost")).get("total_cost_usd"))
    if total is not None and total < 0:
        total = None
    pid = payload.get("prompt_id")
    pid = pid[:80] if isinstance(pid, str) and pid else ""

    sid = _sid(payload)
    st = None
    if sid:
        path = state_path(coach_dir, sid)
        st = _load_state(path)
        fresh = st is None
        if fresh:
            st = _new_state(pid, total)
        cold_before = cache_state(payload, st, tokens, T, now)
        seen = st["t"]
        changed = advance(st, pid, total, tokens, now)
        if cold_before["source"] == "state" and st["t"] != seen:
            # no prompt_cache field, and the session just moved: that request
            # already paid to re-warm the cache, so there is nothing to warn about
            cold_before["cold"] = False
            cold_before["idle_s"] = 0
        if save and (changed or fresh):
            try:
                state_dir = os.path.dirname(path)
                if not os.path.isdir(state_dir):
                    os.makedirs(state_dir)
                if fresh:
                    _prune(state_dir)
                _write_text(path, json.dumps(st, separators=(",", ":")))
            except OSError:
                pass
    else:
        cold_before = cache_state(payload, None, tokens, T, now)

    # context level: 0 fine, 1 warn, 2 over
    level = 0
    if tokens is not None:
        if tokens >= T["ctx_over_tokens"]:
            level = 2
        elif tokens >= T["ctx_warn_tokens"]:
            level = 1
    if pct is not None:
        if pct >= T["ctx_over_pct"]:
            level = 2
        elif pct >= T["ctx_warn_pct"]:
            level = max(level, 1)
    if tokens is None and over_200k:
        level = max(level, 1)

    turn_cost = first = multiple = None
    if st is not None and total is not None and st["pid"]:
        turn_cost = max(0.0, total - st["base"])
        first = st["first"]
        if first and st["turns"] >= 2 and turn_cost >= T["turn_floor_usd"]:
            m = turn_cost / first
            if m >= T["turn_multiple"]:
                multiple = round(m, 1)

    cache = cold_before
    resend = cache["resend"]
    cold_alert = bool(cache["cold"] and resend is not None and resend >= T["cold_tokens"])

    action = None
    if cold_alert:
        action = ACTION_COLD
    elif level == 2 or (multiple is not None and level >= 1):
        action = ACTION_CONTEXT

    snap = {
        "tokens": int(tokens) if tokens is not None else None,
        "percent": round(pct, 1) if pct is not None else None,
        "window": int(size) if size else None,
        "exceeds_200k": over_200k,
        "context_level": level,
        "cost_usd": round(total, 4) if total is not None else None,
        "turn_cost_usd": round(turn_cost, 4) if turn_cost is not None else None,
        "first_turn_cost_usd": first,
        "turn_multiple": multiple,
        "turns_seen": st["turns"] if st is not None else None,
        "cache_cold": bool(cache["cold"]),
        "cache_source": cache["source"],
        "idle_seconds": int(cache["idle_s"]) if cache["idle_s"] is not None else None,
        "resend_tokens": int(resend) if resend is not None else None,
        "cold_alert": cold_alert,
        "action": action,
        "off": off,
        "thresholds": T,
    }
    if off:      # the off switch silences the coaching, the plain numbers stay
        snap.update({"context_level": 0, "turn_multiple": None, "cold_alert": False, "action": None})
    snap["quiet"] = not (snap["context_level"] or snap["turn_multiple"] or snap["cold_alert"])
    return snap


# ---------- rendering ----------

_COLORS = {"warn": "\033[33m", "over": "\033[31m", "act": "\033[1;33m"}
_RESET = "\033[0m"


def want_color(cfg=None, flag=None):
    """Colour is on unless --no-color, NO_COLOR, TERM=dumb or config says otherwise."""
    if flag is False:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    if _dict(cfg).get("color") is False:
        return False
    return True


def env_width(cfg=None):
    """Usable width: config max_width, else COLUMNS less a small margin. 0 means unknown."""
    w = _num(_dict(cfg).get("max_width"))
    if w and w > 0:
        return int(w)
    try:
        cols = int(os.environ.get("COLUMNS") or 0)
    except ValueError:
        cols = 0
    return max(0, cols - 4) if cols > 0 else 0


def parts(snap, full=True):
    """[(keep priority, text, colour key or None)] in display order. Lower priority number is kept longer."""
    out = []
    level = snap["context_level"]
    if full or level:
        bits = ["ctx"]
        if snap["tokens"] is not None:
            bits.append(fmt_tokens(snap["tokens"]))
        elif snap["exceeds_200k"]:
            bits.append(">200k")
        if snap["percent"] is not None:
            bits.append("%d%%" % int(snap["percent"]))
        if len(bits) == 1:
            bits.append("?")
        if level:
            bits.append("!!" if level == 2 else "!")
        out.append((1, " ".join(bits), {1: "warn", 2: "over"}.get(level)))
    if full and snap["cost_usd"] is not None:
        out.append((3, fmt_cost(snap["cost_usd"]), None))
    if snap["turn_multiple"] is not None:
        out.append((5, "last turn %sx first" % ("%.1f" % snap["turn_multiple"]).rstrip("0").rstrip("."),
                    "warn"))
    if snap["cold_alert"]:
        if snap["idle_seconds"] is not None:
            out.append((4, "idle %s" % fmt_idle(snap["idle_seconds"]), "warn"))
    if snap["action"]:
        out.append((2, snap["action"], "act"))
    return out


def render(snap, full=True, width=0, color=False):
    """The line. Drops the least important parts first when it does not fit, then cuts."""
    ps = parts(snap, full)
    if width and width > 0:
        while len(ps) > 1 and len(SEP.join(p[1] for p in ps)) > width:
            drop = max(range(len(ps)), key=lambda i: ps[i][0])
            ps.pop(drop)
        if ps and len(ps[0][1]) > width:
            text = ps[0][1]
            text = text[:width] if width <= 3 else text[:width - 3] + "..."
            ps = [(ps[0][0], text, ps[0][2])]
    if not color:
        return SEP.join(p[1] for p in ps)
    return SEP.join((_COLORS[p[2]] + p[1] + _RESET) if p[2] else p[1] for p in ps)


def line_for(payload, full=True, width=None, color=None, coach_dir=None, now=None):
    """Payload dict to the finished line. The one call a caller needs."""
    coach_dir = coach_dir or _coach_home()
    cfg = load_settings(coach_dir)
    snap = snapshot(payload, coach_dir=coach_dir, now=now)
    return render(snap, full=full,
                  width=env_width(cfg) if width is None else width,
                  color=want_color(cfg, color))


# ---------- wrap mode ----------

def _visible_len(text):
    import re
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)     # OSC, such as links
    text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text)             # CSI, such as colours
    return len(text)


def run_existing(command, raw, timeout_s):
    """Run the person's own status line command. (lines, how): how is ok, timeout, failed or skipped."""
    if not command or os.environ.get(WRAP_ENV):
        return [], "skipped"
    import signal
    import subprocess
    env = dict(os.environ)
    env[WRAP_ENV] = "1"
    try:
        proc = subprocess.Popen(command, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    except OSError:
        return [], "failed"

    def kill_child():
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:       # no process groups here, or already gone
            try:
                proc.kill()
            except Exception:
                pass

    def cancelled(signum, frame):
        # Claude Code cancels a status line run when a newer one starts: take
        # the person's command down with us instead of leaving it running.
        kill_child()
        os._exit(0)

    for name in ("SIGTERM", "SIGHUP", "SIGINT"):
        try:
            signal.signal(getattr(signal, name), cancelled)
        except Exception:       # not the main thread, or no such signal
            pass
    try:
        out, _ = proc.communicate(raw, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        kill_child()
        try:
            proc.communicate(timeout=0.5)
        except Exception:
            pass
        return [], "timeout"
    except Exception:
        kill_child()
        return [], "failed"
    text = out.decode("utf-8", "replace")[:4000]
    lines = [ln.rstrip("\r") for ln in text.split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()
    return lines[:6], "ok"


def wrap_lines(existing, segment, width=0):
    """The existing output with the coach segment added: on the first line if it fits, else below."""
    if not segment:
        return list(existing)
    if not existing:
        return [segment]
    if width and _visible_len(existing[0]) + len(SEP) + _visible_len(segment) > width:
        return list(existing) + [segment]
    return [existing[0] + SEP + segment] + list(existing[1:])


def wrapped(payload, raw, full=None, width=None, color=None, coach_dir=None, now=None):
    """Lines for wrap mode. Falls back to the whole coach line when the old command gives nothing."""
    coach_dir = coach_dir or _coach_home()
    cfg = load_settings(coach_dir)
    T = thresholds(cfg)
    command = cfg.get("wrap_command") if isinstance(cfg.get("wrap_command"), str) else ""
    existing, how = run_existing(command, raw, T["wrap_timeout_ms"] / 1000.0)
    if full is None:
        full = cfg.get("wrap_full") is True
    width = env_width(cfg) if width is None else width
    try:
        snap = snapshot(payload, coach_dir=coach_dir, now=now)
        use_color = want_color(cfg, color)
        if not existing:
            return [render(snap, full=True, width=width, color=use_color)], how
        seg = render(snap, full=full, width=width, color=use_color)
    except Exception:       # never lose the person's own line over a coach error
        return (existing or [MINIMAL_LINE]), how
    return wrap_lines(existing, seg, width), how


# ---------- setup helpers: status, install, remove ----------

LAUNCHER = '''#!/usr/bin/env python3
# agentic-coach status line launcher, written by "statusline.py install".
# The plugin folder moves with every plugin version, so settings.json points
# here and this file finds the current plugin. Remove it with "statusline.py remove".
import os
import sys


def _ok(root):
    return (os.path.isfile(os.path.join(root, "coach", "statusline.py"))
            and not os.path.exists(os.path.join(root, ".orphaned_at")))


def _root():
    here = os.path.dirname(os.path.abspath(__file__))
    note = os.path.join(here, "statusline.path")
    try:
        with open(note) as fh:
            root = fh.read().strip()
        if root and _ok(root):
            return root
    except OSError:
        pass
    import glob
    claude = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    found = [os.path.dirname(os.path.dirname(p)) for p in glob.glob(os.path.join(
        claude, "plugins", "cache", "*", "agentic-coach", "*", "coach", "statusline.py"))]
    found = [r for r in found if _ok(r)]
    if not found:
        return None
    def rank(root):
        nums = []
        for bit in os.path.basename(root).split("."):
            digits = "".join(ch for ch in bit if ch.isdigit())
            nums.append(int(digits) if digits else 0)
        return (nums, os.path.getmtime(root))

    found.sort(key=rank)
    try:
        with open(note, "w") as fh:
            fh.write(found[-1] + "\\n")
    except OSError:
        pass
    return found[-1]


try:
    root = _root()
    if root:
        sys.path.insert(0, os.path.join(root, "coach"))
        import statusline
        statusline.main()
except BaseException:
    pass
sys.exit(0)
'''


def launcher_path(coach_dir=None):
    return os.path.join(coach_dir or _coach_home(), LAUNCHER_NAME)


def coach_command(coach_dir=None, wrap=False):
    """The command string that goes into settings.json. The path is quoted for the shell."""
    import shlex
    path = launcher_path(coach_dir)
    quoted = shlex.quote(path)
    if quoted == path or not any(ch in path for ch in '"$`\\!'):
        quoted = '"%s"' % path          # the plain, familiar form when it is safe
    cmd = "python3 " + quoted
    return cmd + " --wrap" if wrap else cmd


def is_coach_command(command, coach_dir=None):
    """True only for the exact command install writes for this coach home: the launcher
    alone, or with --wrap.

    A person's own script that calls the launcher (the segment) is theirs, not ours, and
    so is an entry written for another coach home: what it replaced is not recorded here.
    """
    if not isinstance(command, str):
        return False
    c = command.strip()
    return c in (coach_command(coach_dir), coach_command(coach_dir, wrap=True))


def _settings_path(settings_path=None):
    return settings_path or os.path.join(_claude_dir(), "settings.json")


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def _status_line_of(path):
    """(statusLine value or None, file state, whole settings dict or None, the file's text or None)."""
    try:
        text = _read_text(path)
    except (OSError, ValueError):
        return None, "bad", None, None
    if text is None:
        return None, "missing", None, None
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return None, "bad", None, text
    return data.get("statusLine"), "ok", data, text


def _config_state(coach_dir):
    raw, how = _read_json(os.path.join(coach_dir, "config.json"))
    return "bad" if how == "bad" or (how == "ok" and not isinstance(raw, dict)) else how


def _write_settings(path, data, text):
    """Write settings.json, unless it changed since it was read. Returns an error line or None."""
    try:
        now_text = _read_text(path)
    except (OSError, ValueError):
        now_text = object()
    if now_text != text:
        return "%s changed while this was running; nothing was written to it. Run it again." % path
    _dump_settings(path, data, text or "")
    return None


def proposed(coach_dir=None, wrap_of=None, refresh=None):
    """The exact JSON that install would put under "statusLine"."""
    if isinstance(wrap_of, dict):
        obj = {k: v for k, v in wrap_of.items() if k not in ("type", "command")}
        obj = dict({"type": "command", "command": coach_command(coach_dir, wrap=True)}, **obj)
    else:
        obj = {"type": "command", "command": coach_command(coach_dir)}
    if refresh:
        obj["refreshInterval"] = int(refresh)
    return {"statusLine": obj}


def status(settings_path=None, project_dir=None, coach_dir=None):
    """Read-only: is a status line set, is it ours, and what each choice would add."""
    coach_dir = coach_dir or _coach_home()
    path = _settings_path(settings_path)
    cur, how, data, _ = _status_line_of(path)
    cfg = load_settings(coach_dir)
    command = cur.get("command") if isinstance(cur, dict) else None
    if how == "bad":
        state = "unreadable"
    elif cur is None:
        state = "unset"
    elif is_coach_command(command, coach_dir):
        state = "coach_wrap" if "--wrap" in command.split() else "coach"
    else:
        state = "other"
    out = {
        "settings_file": path,
        "settings_file_state": how,
        "state": state,
        "current": cur,
        "disable_all_hooks": bool(_dict(data).get("disableAllHooks") is True),
        "launcher": {"file": launcher_path(coach_dir),
                     "exists": os.path.isfile(launcher_path(coach_dir))},
        "wrapped_command": cfg.get("wrap_command") if isinstance(cfg.get("wrap_command"), str) else None,
        "config_file_state": _config_state(coach_dir),
        "color": want_color(cfg),
        "coach_off": os.path.exists(os.path.join(coach_dir, "off")),
        "proposed": proposed(coach_dir),
        "segment_command": coach_command(coach_dir) + " --segment",
        "thresholds": thresholds(cfg),
        "elsewhere": [],
    }
    if state == "other":
        out["can_wrap"] = bool(isinstance(cur, dict) and cur.get("type", "command") == "command"
                               and isinstance(command, str) and command.strip())
        if out["can_wrap"]:
            out["proposed_wrap"] = proposed(coach_dir, wrap_of=cur)
    # a project file wins over the user file: say so, never change it
    if project_dir:
        for name in ("settings.json", "settings.local.json"):
            p = os.path.join(project_dir, ".claude", name)
            val = _status_line_of(p)[0]
            if val is not None:
                out["elsewhere"].append({"file": p, "statusLine": val})
    return out


def _dump_settings(path, data, original_text):
    indent = 2
    for ln in (original_text or "").split("\n")[1:3]:
        lead = len(ln) - len(ln.lstrip(" "))
        if ln.startswith("\t"):
            indent = "\t"
            break
        if lead:
            indent = lead
            break
    mode = None
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        pass
    _write_text(path, json.dumps(data, indent=indent, ensure_ascii=False) + "\n", mode)


def _save_coach_cfg(coach_dir, change):
    """Merge keys into the "statusline" object of config.json. None removes a key. False if the file is damaged."""
    path = os.path.join(coach_dir, "config.json")
    raw, how = _read_json(path)
    if how == "bad" or (how == "ok" and not isinstance(raw, dict)):
        return False
    raw = raw or {}
    sl = dict(_dict(raw.get(CONFIG_KEY)))
    for k, v in change.items():
        if v is None:
            sl.pop(k, None)
        else:
            sl[k] = v
    if sl:
        raw[CONFIG_KEY] = sl
    else:
        raw.pop(CONFIG_KEY, None)
    if not raw:                 # nothing left to keep: no file rather than an empty one
        if how == "ok":
            try:
                os.remove(path)
            except OSError:
                pass
        return True
    if not os.path.isdir(coach_dir):
        os.makedirs(coach_dir)
    mode = None
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        pass
    _write_text(path, json.dumps(raw, indent=1, sort_keys=True) + "\n", mode)
    return True


def install(mode="direct", settings_path=None, coach_dir=None, replace=False, refresh=None,
            full=False):
    """Point settings.json at the coach status line. Call only after the person said yes.

    mode "direct": the coach line is the status line. Refused when another
    status line is set, unless replace is True (the old one is kept in
    config.json so remove can put it back).
    mode "wrap": the status line already set keeps running and the coach
    segment is added to it.
    mode "segment": settings.json is not touched; only the launcher is written,
    for a person who calls the segment from their own status line script.
    Returns {"ok", "changed", "error", "added", "backup", "previous"}.
    """
    coach_dir = coach_dir or _coach_home()
    path = _settings_path(settings_path)
    res = {"ok": False, "changed": False, "error": None, "settings_file": path}
    if mode == "segment":
        # only the launcher, for people who call the segment from their own script
        try:
            if not os.path.isdir(coach_dir):
                os.makedirs(coach_dir)
            _write_text(launcher_path(coach_dir), LAUNCHER, 0o755)
            _write_text(os.path.join(coach_dir, PATH_FILE_NAME), PLUGIN_ROOT + "\n")
        except OSError as exc:
            res["error"] = "could not write: %s" % exc
            return res
        res.update({"ok": True, "mode": mode, "launcher": launcher_path(coach_dir),
                    "segment_command": coach_command(coach_dir) + " --segment"})
        return res
    if mode not in ("direct", "wrap"):
        res["error"] = "mode must be direct, wrap or segment"
        return res
    cur, how, data, text = _status_line_of(path)
    if how == "bad":
        res["error"] = "%s is not a valid JSON object. Fix it first; nothing was changed." % path
        return res
    if how == "missing":
        if not os.path.isdir(os.path.dirname(path)):
            res["error"] = "%s: the folder does not exist; nothing was changed." % path
            return res
        data = {}
    cfg_file = os.path.join(coach_dir, "config.json")
    if _config_state(coach_dir) == "bad":
        res["error"] = "%s is not valid JSON. Fix or delete it; nothing was changed." % cfg_file
        return res
    cfg = load_settings(coach_dir)
    ours = isinstance(cur, dict) and is_coach_command(cur.get("command"), coach_dir)
    previous = None
    if cur is not None and not ours:
        previous = cur
    elif ours:
        previous = _previous(cfg)

    if mode == "wrap":
        if previous is None:
            res["error"] = "there is no other status line to wrap; use mode direct."
            return res
        cmd = previous.get("command") if isinstance(previous, dict) else None
        if (not isinstance(previous, dict) or previous.get("type", "command") != "command"
                or not isinstance(cmd, str) or not cmd.strip()
                or is_coach_command(cmd, coach_dir)):
            res["error"] = "the status line already set is not a plain command, so it cannot be wrapped."
            return res
        new = proposed(coach_dir, wrap_of=previous, refresh=refresh)
        change = {"wrap_command": cmd, "previous": json.dumps(previous),
                  "wrap_full": True if full else None}
    else:
        wrapping_now = ours and cur.get("command", "").strip() == coach_command(coach_dir, wrap=True)
        if previous is not None and not replace and (not ours or wrapping_now):
            # their own line, set now or being wrapped now, would stop running
            res["error"] = ("another status line is already set; it was left alone. "
                            "Use wrap, or replace=True to swap it out.")
            res["previous"] = previous
            return res
        new = proposed(coach_dir, refresh=refresh)
        change = {"wrap_command": None, "wrap_full": None,
                  "previous": json.dumps(previous) if previous is not None else None}

    # Order matters. What remove needs (the old status line, the launcher) is
    # written before settings.json points at it, and is undone if that last
    # write does not happen. So settings.json never names a launcher that is
    # not there, and never loses the old line.
    note = os.path.join(coach_dir, PATH_FILE_NAME)
    had = {p: os.path.exists(p) for p in (launcher_path(coach_dir), note)}
    cfg_before = cfg_mode = None
    try:
        cfg_before = _read_text(cfg_file)
        if cfg_before is not None:
            cfg_mode = os.stat(cfg_file).st_mode & 0o777
    except (OSError, ValueError):
        pass
    done = False
    err = None
    try:
        if not os.path.isdir(coach_dir):
            os.makedirs(coach_dir)
        _save_coach_cfg(coach_dir, change)
        _write_text(launcher_path(coach_dir), LAUNCHER, 0o755)
        _write_text(note, PLUGIN_ROOT + "\n")
        data["statusLine"] = new["statusLine"]
        err = _write_settings(path, data, text)
        done = err is None
    except Exception as exc:
        err = "could not write: %s" % exc
    if not done:
        try:
            if cfg_before is None:
                if os.path.exists(cfg_file):
                    os.remove(cfg_file)
            else:
                _write_text(cfg_file, cfg_before, cfg_mode)
        except Exception:
            pass
        for p, was in had.items():
            try:
                if not was and os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        res["error"] = err
        return res
    if how == "ok" and not ours:
        # a copy of the file as the person had it, readable only by them
        try:
            backup = os.path.realpath(path) + ".coach-backup"
            _write_text(backup, text, 0o600)
            res["backup"] = backup
        except Exception:
            pass
    res.update({"ok": True, "changed": cur != new["statusLine"], "added": new, "mode": mode,
                "previous": previous, "launcher": launcher_path(coach_dir)})
    return res


def remove(settings_path=None, coach_dir=None):
    """Take the coach status line out again. Puts back the one that was there before, if any.

    A status line that is not the coach's is left alone, and so is the
    launcher then: the person's own script may call it for the segment.
    """
    coach_dir = coach_dir or _coach_home()
    path = _settings_path(settings_path)
    res = {"ok": False, "changed": False, "error": None, "settings_file": path, "restored": None}
    cur, how, data, text = _status_line_of(path)
    if how == "bad":
        res["error"] = "%s is not a valid JSON object. Fix it first; nothing was changed." % path
        return res
    ours = how == "ok" and isinstance(cur, dict) and is_coach_command(cur.get("command"), coach_dir)
    cfg_file = os.path.join(coach_dir, "config.json")
    if _config_state(coach_dir) == "bad":
        # the status line to put back is recorded in that file: do not guess
        res["error"] = "%s is not valid JSON. Fix or delete it first; nothing was changed." % cfg_file
        return res
    cfg = load_settings(coach_dir)
    try:
        if ours:
            previous = _previous(cfg)
            if previous is not None and not (isinstance(previous, dict) and is_coach_command(
                    previous.get("command"), coach_dir)):
                data["statusLine"] = previous
                res["restored"] = previous
            else:
                del data["statusLine"]
            err = _write_settings(path, data, text)
            if err:
                res["error"] = err
                return res
            res["changed"] = True
            res["removed"] = cur
        elif cur is not None:
            res["note"] = ("the status line in this file is not the coach's; it and the launcher "
                           "were left alone.")
            res["ok"] = True
            return res
        _save_coach_cfg(coach_dir, {"wrap_command": None, "wrap_full": None, "previous": None})
        if LAUNCHER_NAME in json.dumps(res["restored"]):
            # the line put back calls the segment: it still needs the launcher
            res["note"] = "the launcher was kept: the status line put back calls it."
        else:
            for name in (LAUNCHER_NAME, PATH_FILE_NAME):
                try:
                    os.remove(os.path.join(coach_dir, name))
                except OSError:
                    pass
    except Exception as exc:
        res["error"] = "could not write: %s" % exc
        return res
    res["ok"] = True
    return res


def page_data(settings_path=None, coach_dir=None):
    """A small block for the coach page. Every string in it is HTML-escaped."""
    import html
    s = status(settings_path=settings_path, coach_dir=coach_dir)
    T = s["thresholds"]

    def esc(x):
        return html.escape(str(x), quote=True)

    return {
        "state": esc(s["state"]),
        "on": s["state"] in ("coach", "coach_wrap"),
        "wrapping": s["state"] == "coach_wrap",
        "coach_off": s["coach_off"],
        "legend": [{"part": esc(r["part"]), "meaning": esc(r["meaning"])} for r in LEGEND],
        "thresholds": [
            {"name": esc("Context marker !"), "value": esc("%s tokens or %d%% of the window" % (
                fmt_tokens(T["ctx_warn_tokens"]), T["ctx_warn_pct"]))},
            {"name": esc("Context marker !!"), "value": esc("%s tokens or %d%% of the window" % (
                fmt_tokens(T["ctx_over_tokens"]), T["ctx_over_pct"]))},
            {"name": esc("Costly turn"), "value": esc("%gx the first turn, and at least %s" % (
                T["turn_multiple"], fmt_cost(T["turn_floor_usd"])))},
            {"name": esc("Cold cache"), "value": esc("cache expired with %s tokens or more to re-send" % (
                fmt_tokens(T["cold_tokens"])))},
        ],
    }


# ---------- command line ----------

def _flag_value(args, name):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            val = args[i + 1]
            del args[i:i + 2]
            return val
        del args[i]
    return None


_SETUP_FLAGS = {"status": ("--settings", "--project"),
                "install": ("--settings", "--project", "--refresh", "--segment", "--wrap",
                            "--replace", "--full"),
                "remove": ("--settings", "--project")}
_VALUE_FLAGS = ("--settings", "--project", "--refresh")


def _setup_cli(args):
    cmd = args.pop(0)
    # an unknown word (--help included) must never fall through to an install or a remove
    expect_value = False
    for a in args:
        if expect_value:
            expect_value = False
            continue
        if a not in _SETUP_FLAGS[cmd]:
            sys.stdout.write(json.dumps({"ok": False, "error": "unknown argument %r for %s; "
                                         "known: %s" % (a, cmd, " ".join(_SETUP_FLAGS[cmd]))},
                                        indent=1) + "\n")
            return 2
        expect_value = a in _VALUE_FLAGS
    try:
        return _setup_run(cmd, args)
    except Exception as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)},
                                    indent=1) + "\n")
        return 2


def _setup_run(cmd, args):
    settings = _flag_value(args, "--settings")
    project = _flag_value(args, "--project")
    refresh = _flag_value(args, "--refresh")
    if cmd == "status":
        out = status(settings_path=settings, project_dir=project)
    elif cmd == "install":
        try:
            refresh = max(1, int(refresh)) if refresh else None
        except ValueError:
            refresh = None
        mode = "segment" if "--segment" in args else "wrap" if "--wrap" in args else "direct"
        out = install(mode=mode, settings_path=settings,
                      replace="--replace" in args, refresh=refresh, full="--full" in args)
    else:
        out = remove(settings_path=settings)
    sys.stdout.write(json.dumps(out, indent=1) + "\n")
    return 0 if out.get("ok", True) else 2


def _render_cli(args):
    """Print the line. Never raises, never prints a traceback, always exit 0."""
    segment = "--segment" in args
    wrap = "--wrap" in args
    lines = None
    try:
        raw = b"" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.buffer.read()
        try:
            payload = json.loads(raw.decode("utf-8", "replace")) if raw.strip() else {}
        except ValueError:
            payload = {}
        color = False if "--no-color" in args else None
        width = None
        w = _flag_value(args, "--width")
        if w is not None:
            try:
                width = max(0, int(w))
            except ValueError:
                width = None
        full = True if "--full" in args else None
        if "--json" in args:
            lines = [json.dumps(snapshot(payload, save=False), indent=1)]
        elif wrap:
            lines, _ = wrapped(payload, raw, full=full, width=width, color=color)
        else:
            lines = [line_for(payload, full=(not segment) or bool(full), width=width, color=color)]
    except BaseException:   # a status line must never show a traceback
        lines = None
    if lines is None:
        lines = [] if segment else [MINIMAL_LINE]
    try:
        text = "\n".join(ln for ln in lines if ln is not None)
        if text:
            buf = getattr(sys.stdout, "buffer", None)
            if buf is not None:
                buf.write((text + "\n").encode(sys.stdout.encoding or "utf-8", "replace"))
            else:
                sys.stdout.write(text + "\n")
        sys.stdout.flush()
    except BaseException:
        pass
    return 0


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("status", "install", "remove"):
        return _setup_cli(args)
    if args and args[0] in ("-h", "--help"):
        sys.stdout.write(__doc__)
        return 0
    return _render_cli(args)


if __name__ == "__main__":
    sys.exit(main())
