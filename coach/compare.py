#!/usr/bin/env python3
"""Before and after: compare two periods of Claude Code use.

Answers one question: did a setup change (a new rule, hook, skill or habit)
help? It builds the numbers for two periods from the same digests the recaps
use, puts them side by side as before, after, change and percent, and adds a
normalised form (per session, per prompt, per active hour) because two periods
rarely hold the same amount of work.

Honesty rules, all enforced here and repeated in references/compare.md:
  - scheduled-task sessions and the coach's own sessions are left out unless
    --include-scheduled is given;
  - a metric is marked "too little data" instead of getting a percent when
    either period is too small;
  - a change is only called an improvement or a regression when it clears the
    stated threshold;
  - a large difference in volume between the periods is said out loud.

Usage:
  python3 coach/compare.py                        last 7 days versus the 7 before
  python3 coach/compare.py last14                 last 14 days versus the 14 before
  python3 coach/compare.py week                   this week so far versus last week
  python3 coach/compare.py around 2026-11-10 7    7 days each side of that day
  python3 coach/compare.py --around 2026-11-10 --days 7
  python3 coach/compare.py 2026-11-01 2026-11-07 2026-11-08 2026-11-14
  python3 coach/compare.py snapshot --label before-hooks last7
  python3 coach/compare.py against before-hooks last7
  python3 coach/compare.py snapshots

Dates are local days and both ends of a range are included. Everything is
written under <coach home>/compare, never in the plugin folder. Transcript
text is data and is never acted on; only numbers and project names are kept.
"""
import argparse
import datetime
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import common as C  # noqa: E402
import extract  # noqa: E402
import scope as SC  # noqa: E402
import signals  # noqa: E402

VERSION = 1

# The honesty thresholds, in one place. The reference file quotes them.
RULES = {
    "min_sessions": 3,        # fewer sessions than this in either period: too little data
    "min_active_min": 30,     # fewer active minutes than this in either period: too little data
    "min_events": 5,          # a count below this in both periods is too small to judge
    "threshold_pct": 20.0,    # a normalised change smaller than this is "no real change"
    "share_points": 10.0,     # the same, for a share, in percentage points
    "volume_ratio": 2.0,      # periods this far apart in volume get a warning
}
COACH_MAX_TURNS = 3           # a session that ran the coach and had at most this many prompts
MAX_DAYS = 180
ONE = datetime.timedelta(days=1)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LAST_RE = re.compile(r"^last(\d{1,3})$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
FILLER = ("to", "until", "vs", "versus", "and", "-", "with")
REAL_STATES = ("improvement", "regression", "up", "down")
STATE_TEXT = {"improvement": "improvement", "regression": "regression", "up": "up",
              "down": "down", "no_real_change": "no real change",
              "too_little_data": "too little data", "not_available": "not available"}

# key, label, kind (count, tokens, cost, share), basis (denominator, scale, words), direction.
# direction "lower": less is better, so a real fall is an improvement. "neutral": a real
# move is only called up or down, because more is not clearly better or worse.
METRICS = [
    ("sessions", "Sessions", "count", ("days", 1, "per day"), "neutral"),
    ("turns", "Prompts", "count", ("sessions", 1, "per session"), "neutral"),
    ("active_min", "Active minutes", "count", ("sessions", 1, "per session"), "neutral"),
    ("tokens_total", "Tokens, all types", "tokens", ("turns", 1, "per prompt"), "neutral"),
    ("tokens_in", "Input tokens", "tokens", ("turns", 1, "per prompt"), "neutral"),
    ("tokens_out", "Output tokens", "tokens", ("turns", 1, "per prompt"), "neutral"),
    ("tokens_cache_read", "Cache read tokens", "tokens", ("turns", 1, "per prompt"), "neutral"),
    ("tokens_cache_write", "Cache write tokens", "tokens", ("turns", 1, "per prompt"), "neutral"),
    ("cost", "Cost", "cost", ("active_hours", 1, "per active hour"), "neutral"),
    ("tool_errors", "Tool errors", "count", ("tool_calls", 100, "per 100 tool calls"), "lower"),
    ("repeated_failing_commands", "Repeated failing commands", "count",
     ("sessions", 1, "per session"), "lower"),
    ("interruptions", "Interruptions", "count", ("turns", 100, "per 100 prompts"), "lower"),
    ("corrections", "Corrections", "count", ("turns", 100, "per 100 prompts"), "lower"),
    ("compactions", "Compactions", "count", ("active_hours", 1, "per active hour"), "lower"),
    ("peak_context", "Peak context", "tokens", None, "neutral"),
    ("sessions_over_context", "Sessions over the context mark", "count",
     ("sessions", 100, "per 100 sessions"), "lower"),
    ("idle_resumes", "Resumes after a long break", "count", ("sessions", 1, "per session"), "lower"),
    ("permission_prompts", "Permission prompts", "count",
     ("active_hours", 1, "per active hour"), "lower"),
    ("permission_denials", "Permission denials", "count",
     ("tool_calls", 100, "per 100 tool calls"), "lower"),
    ("subagent_share", "Subagent share of tokens", "share", None, "neutral"),
    ("findings_total", "Coach findings", "count", ("sessions", 1, "per session"), "lower"),
]
COUNT_KEYS = ("sessions", "turns", "active_min", "tool_calls", "tokens_total", "tokens_in",
              "tokens_out", "tokens_cache_read", "tokens_cache_write", "subagent_tokens",
              "tool_errors", "repeated_failing_commands", "interruptions", "corrections",
              "compactions", "peak_context", "sessions_over_context", "idle_resumes",
              "permission_denials", "findings_total")
TOKEN_TYPES = (("in", "tokens_in"), ("out", "tokens_out"), ("cache_read", "tokens_cache_read"),
               ("cache_write", "tokens_cache_write"))
PROJECT_KEYS = ("sessions", "turns", "active_min", "tokens_total", "tool_errors", "corrections")


class UsageError(Exception):
    pass


# ---------- where things live ----------

def compare_dir():
    """<coach home>/compare: stored period numbers, snapshots, the latest comparison."""
    return os.path.join(C.coach_home(), "compare")


def _write_json(path, obj):
    """Write through a temp file so a crash never leaves half a file."""
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w") as fh:
            fh.write(json.dumps(obj, indent=1, sort_keys=True) + "\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _read_json(path):
    try:
        with open(path) as fh:
            obj = json.load(fh)
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


# ---------- periods ----------

def _date(text):
    if not DATE_RE.match(text):
        raise UsageError("not a date (want YYYY-MM-DD): %s" % text)
    try:
        return datetime.date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except ValueError:
        raise UsageError("not a real date: %s" % text)


def _days(text):
    try:
        n = int(text)
    except (TypeError, ValueError):
        raise UsageError("not a number of days: %s" % text)
    if not 1 <= n <= MAX_DAYS:
        raise UsageError("days must be between 1 and %d" % MAX_DAYS)
    return n


def _period(first, end):
    if end <= first:
        raise UsageError("a period needs at least one day")
    if (end - first).days > MAX_DAYS:
        raise UsageError("a period can be at most %d days" % MAX_DAYS)
    return {"first": first, "end": end}


def _words(words):
    out = " ".join(w for w in words if w).replace(",", " ").split()
    return [w for w in out if w.lower() not in FILLER]


def one_period(words, today=None):
    """Words for one period to {"first": date, "end": day after the last day}.

    Nothing or lastN: the N full days before today (default 7). week: this week
    so far, Monday to today. lastweek: last Monday to Sunday. One date: that
    day. Two dates: both included.
    """
    today = today or datetime.date.today()
    words = _words(words) or ["last7"]
    low = [w.lower() for w in words]
    if len(words) == 1:
        m = LAST_RE.match(low[0])
        if m:
            return _period(today - _days(m.group(1)) * ONE, today)
        monday = today - today.weekday() * ONE
        if low[0] == "week":
            return _period(monday, today + ONE)
        if low[0] == "lastweek":
            return _period(monday - 7 * ONE, monday)
        day = _date(words[0])
        if day > today:
            raise UsageError("%s is in the future" % day)
        return _period(day, day + ONE)
    if len(words) == 2:
        first, last = sorted((_date(words[0]), _date(words[1])))
        if last > today:
            raise UsageError("%s is in the future" % last)
        return _period(first, last + ONE)
    raise UsageError("cannot read a period from: %s" % " ".join(words))


def _running(after, today, notes):
    if after["end"] > today:
        notes.append("The after period includes today, which is still running.")
    return notes


def plan(words, today=None):
    """Arguments to (before, after, notes).

    Each side is {"first": date, "end": date} or, for `against NAME`,
    {"snapshot": NAME} on the before side.
    """
    today = today or datetime.date.today()
    notes = []
    raw = " ".join(w for w in words if w).replace(",", " ").split()
    if raw and raw[0].lower() == "against":      # the name is read before filler words go
        if len(raw) < 2:
            raise UsageError("use: against NAME [period]")
        after = one_period(raw[2:], today)
        return {"snapshot": snapshot_name(raw[1])}, after, _running(after, today, notes)
    words = _words(words)
    low = [w.lower() for w in words]
    if not words:
        low = words = ["last7"]
    if len(words) == 1 and LAST_RE.match(low[0]):
        n = _days(LAST_RE.match(low[0]).group(1))
        return _period(today - 2 * n * ONE, today - n * ONE), _period(today - n * ONE, today), notes
    if low == ["week"]:
        monday = today - today.weekday() * ONE
        if today.weekday() < 6:
            notes.append("This week is not over: it has %d of 7 days so far."
                         % (today.weekday() + 1))
        after = _period(monday, today + ONE)
        return _period(monday - 7 * ONE, monday), after, _running(after, today, notes)
    if low[0] == "around":
        if len(words) not in (2, 3):
            raise UsageError("use: around YYYY-MM-DD [days]")
        pivot = _date(words[1])
        n = _days(words[2]) if len(words) == 3 else 7
        if pivot > today:
            raise UsageError("%s is in the future" % pivot)
        end = pivot + n * ONE
        if end > today + ONE:
            end = today + ONE
            notes.append("The after period is cut short at today: %d days instead of %d."
                         % ((end - pivot).days, n))
        after = _period(pivot, end)
        return _period(pivot - n * ONE, pivot), after, _running(after, today, notes)
    if len(words) == 4:
        a = one_period(words[0:2], today)
        b = one_period(words[2:4], today)
        before, after = (a, b) if a["first"] <= b["first"] else (b, a)
        if before["end"] > after["first"]:
            raise UsageError("the two periods overlap")
        return before, after, _running(after, today, notes)
    raise UsageError("cannot read two periods from: %s" % " ".join(words))


# ---------- one period's numbers ----------

def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _cost(holder):
    """Cost of a session or a subagent block if the digest carries one, else None.

    Cost fields are optional (they arrive with 1.1.0), so several spellings are
    accepted: cost or cost_usd on the block itself or inside its tokens.
    """
    if not isinstance(holder, dict):
        return None
    tok = holder.get("tokens") if isinstance(holder.get("tokens"), dict) else {}
    for src in (holder, tok):
        for key in ("cost_usd", "cost"):
            v = src.get(key)
            if isinstance(v, dict):
                v = v.get("total", v.get("usd"))
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
    return None


def _used_coach(s):
    names = list((s.get("skills") or {}).keys()) + list((s.get("slash_commands") or {}).keys())
    for name in names:
        name = str(name).lstrip("/").lower()
        if name == "coach" or name.endswith(":coach"):
            return True
    return False


def exclusion(s):
    """Why a session is left out by default: "scheduled", "coach" or None.

    scheduled: any scheduled-task run (the coach's own daily and weekly runs
    are scheduled tasks too). coach: a session whose only work was running the
    coach by hand, that is, it called the coach and had at most COACH_MAX_TURNS
    prompts. A long working session that also ran /coach once is kept.
    """
    if s.get("kind") == "scheduled":
        return "scheduled"
    if _used_coach(s) and _num(s.get("turns")) <= COACH_MAX_TURNS:
        return "coach"
    return None


def period_metrics(digest, include_scheduled=False, context_threshold=None):
    """Numbers for one period from a digest written by extract.run.

    Returns a JSON-serialisable dict: counts (name to number, None when the
    digest cannot tell), findings (detector pattern to count), projects (name
    to a few counts), excluded (how many sessions were left out and why) and
    skip_ids (8-character ids of the sessions left out).
    """
    mark = int(context_threshold or signals.T["peak_context"])
    c = dict((k, 0) for k in COUNT_KEYS)
    cost = None
    costed = 0
    excluded = {"scheduled": 0, "coach": 0}
    skip_ids = []
    kept = []
    projects = {}
    sessions = [s for s in digest.get("sessions") or [] if isinstance(s, dict)]
    minor = [s for s in digest.get("minor_sessions") or [] if isinstance(s, dict)]
    for s in sessions:
        why = None if include_scheduled else exclusion(s)
        if why:
            excluded[why] += 1
            skip_ids.append(str(s.get("id") or "")[:8])
            continue
        kept.append(s)
        tok = s.get("tokens") if isinstance(s.get("tokens"), dict) else {}
        sub = s.get("subagents") if isinstance(s.get("subagents"), dict) else {}
        sub_tok = sub.get("tokens") if isinstance(sub.get("tokens"), dict) else {}
        total = 0
        for short, key in TOKEN_TYPES:
            n = _num(tok.get(short)) + _num(sub_tok.get(short))
            c[key] += n
            total += n
        c["tokens_total"] += total
        c["subagent_tokens"] += sum(_num(sub_tok.get(short)) for short, _ in TOKEN_TYPES)
        costed += 1 if _cost(s) is not None else 0
        for holder in (s, sub):
            v = _cost(holder)
            if v is not None:
                cost = (cost or 0.0) + v
        errors = s.get("errors") if isinstance(s.get("errors"), dict) else {}
        repeated = [r for r in list(errors.get("repeated") or []) + list(sub.get("repeated_fails") or [])
                    if isinstance(r, dict) and _num(r.get("fails")) >= signals.T["fail_repeats"]]
        denials = s.get("denials") if isinstance(s.get("denials"), dict) else {}
        peak = _num(tok.get("peak_context"))
        c["sessions"] += 1
        c["turns"] += _num(s.get("turns"))
        c["active_min"] += _num(s.get("active_min"))
        c["tool_calls"] += _num(s.get("tool_calls"))
        c["tool_errors"] += _num(errors.get("total"))
        c["repeated_failing_commands"] += len(repeated)
        c["interruptions"] += _num(s.get("interruptions"))
        c["corrections"] += _num(s.get("corrections"))
        c["compactions"] += _num(s.get("compactions_total")) or len(s.get("compactions") or [])
        c["peak_context"] = max(c["peak_context"], peak)
        c["sessions_over_context"] += 1 if peak >= mark else 0
        c["idle_resumes"] += _num((s.get("idle_gaps") or {}).get("count"))
        c["permission_denials"] += sum(_num(v) for k, v in denials.items() if k != "top")
        name = str(s.get("project_name") or s.get("project") or "?")
        p = projects.setdefault(name, dict((k, 0) for k in PROJECT_KEYS))
        p["sessions"] += 1
        p["turns"] += _num(s.get("turns"))
        p["active_min"] += _num(s.get("active_min"))
        p["tokens_total"] += total
        p["tool_errors"] += _num(errors.get("total"))
        p["corrections"] += _num(s.get("corrections"))
    partial = 0
    for s in minor:        # only in a stored digest that was shrunk to fit a size limit
        if not include_scheduled and s.get("kind") == "scheduled":
            excluded["scheduled"] += 1
            skip_ids.append(str(s.get("id") or "")[:8])
            continue
        partial += 1
        c["sessions"] += 1
        c["turns"] += _num(s.get("turns"))
        c["tool_calls"] += _num(s.get("tool_calls"))
        c["tokens_out"] += _num(s.get("out_tokens"))
        c["tokens_total"] += _num(s.get("out_tokens"))
    findings = {}
    for f in signals.run({"sessions": kept, "repeated_prompts": digest.get("repeated_prompts")}):
        findings[f["pattern"]] = findings.get(f["pattern"], 0) + 1
    c["findings_total"] = sum(findings.values())
    c["cost"] = round(cost, 4) if cost is not None else None
    c["permission_prompts"] = None      # filled from the permission log by collect()
    return {"version": VERSION, "include_scheduled": bool(include_scheduled),
            "context_threshold": mark, "sessions_seen": len(sessions) + len(minor),
            "excluded": excluded, "skip_ids": sorted(set(skip_ids)),
            "partial_sessions": partial,
            "cost_partial": cost is not None and costed < len(kept) + partial, "counts": c, "findings": findings,
            "projects": projects}


def permission_prompts(lo, hi, skip_ids=()):
    """(count, first) from the optional permission log, for prompts in [lo, hi).

    count is None when there is no log or its first entry is after the period,
    so a period from before the log existed is not read as zero prompts.
    first is the time of the oldest entry in the log, or None.
    """
    skip = set(skip_ids)
    n = 0
    first = None
    home = C.coach_home()
    found = False
    for name in ("permission-prompts.jsonl.1", "permission-prompts.jsonl"):
        try:
            fh = open(os.path.join(home, name), "r", errors="replace")
        except OSError:
            continue
        found = True
        with fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                ep = d.get("ts") if isinstance(d, dict) else None
                if not isinstance(ep, (int, float)) or isinstance(ep, bool):
                    continue
                if first is None or ep < first:
                    first = ep
                if lo <= ep < hi and str(d.get("session") or "")[:8] not in skip:
                    n += 1
    if not found or first is None or first >= hi:
        return None, first
    return n, first


def _stored_digests(first, end):
    """Digests an earlier recap wrote for exactly this period, if any."""
    folder = os.path.join(C.reports_dir(), "digests")
    last = end - ONE
    names = ["range-%s_to_%s.json" % (first, last)]
    if (end - first).days == 1:
        names.append("daily-%s.json" % first)
    if (end - first).days == 7:
        names.append("weekly-%s.json" % end)
    return [os.path.join(folder, n) for n in names]


def _period_file(first, end, include_scheduled, mark):
    name = "%s_to_%s" % (first, end - ONE)
    if include_scheduled:
        name += ".all"
    if mark != signals.T["peak_context"]:
        name += ".ctx%d" % mark
    return os.path.join(compare_dir(), "periods", name + ".json")


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _valid_metrics(m):
    """True for stored numbers in the shape period_metrics and collect write."""
    if not (isinstance(m, dict) and m.get("version") == VERSION
            and all(isinstance(m.get(k), dict)
                    for k in ("counts", "findings", "projects", "excluded"))):
        return False
    c = m["counts"]
    return (all(_is_num(c.get(k)) for k in COUNT_KEYS)
            and all(v is None or _is_num(v) for v in c.values())
            and all(_is_num(v) for v in m["findings"].values())
            and all(_is_num(v) for v in m["excluded"].values())
            and all(isinstance(p, dict) and all(_is_num(v) for v in p.values())
                    for p in m["projects"].values())
            and all(_is_num(m.get(k)) for k in ("sessions_seen", "context_threshold"))
            and all(m.get(k) is None or _is_num(m.get(k)) for k in ("days", "partial_sessions"))
            and all(m.get(k) is None or isinstance(m.get(k), str)
                    for k in ("first_day", "last_day", "permission_log_first_day")))


def collect(first, end, projects_dir=None, include_scheduled=False, project=None,
            context_threshold=None, store=True):
    """Numbers for the local days from `first` up to, not including, `end`.

    Builds a fresh digest through extract.run. If the transcripts have thinned
    out since an earlier run (Claude Code deletes old ones), the fuller stored
    numbers or the fuller stored recap digest are used instead, and nothing
    stored is ever replaced by a thinner version. The digest folder is never
    written to.
    """
    mark = int(context_threshold or signals.T["peak_context"])
    lo = C.parse_when(first.isoformat())
    hi = C.parse_when(end.isoformat())
    digest, _blob = extract.run(lo, hi, project=project, max_bytes=0,
                                projects_dir=projects_dir, inventory=False)
    best = period_metrics(digest, include_scheduled, mark)
    best["source"] = "transcripts"
    fresh = True
    path = _period_file(first, end, include_scheduled, mark)
    sig = SC.Scope().sig()
    best["scope_sig"] = sig
    if not project:
        stored = _read_json(path)
        if not _valid_metrics(stored) or (stored.get("scope_sig") or "") != sig:
            stored = None             # numbers counted under other show rules are not reused
        if stored and stored["sessions_seen"] > best["sessions_seen"]:
            best, fresh = stored, False
            best["source"] = "stored numbers"
        for p in _stored_digests(first, end):
            old, _f = SC.stored(_read_json(p))
            if not old or not isinstance(old.get("sessions"), list):
                continue
            # only sessions kept in full count: a shrunk digest holds less, not more
            full = sum(1 for x in old["sessions"] if isinstance(x, dict))
            if full > best["sessions_seen"]:
                try:
                    cand = period_metrics(old, include_scheduled, mark)
                except (TypeError, AttributeError, ValueError, KeyError):
                    continue        # a damaged stored digest is skipped, not trusted
                best, fresh = cand, False
                best["source"] = "stored digest"
    best.update({"first_day": first.isoformat(), "last_day": (end - ONE).isoformat(),
                 "days": (end - first).days})
    if best["source"] != "stored numbers":
        n, log_first = permission_prompts(lo, hi, best.get("skip_ids") or ())
        best["counts"]["permission_prompts"] = n
        best["permission_log_first_day"] = (C.epoch_local(log_first, "%Y-%m-%d")
                                            if log_first is not None else None)
        if project:
            best["counts"]["permission_prompts"] = None    # the log does not name a project
        elif stored and stored["sessions_seen"] == best["sessions_seen"]:
            # the log rotates away; keep the fuller count an earlier run saw for these sessions
            was = stored["counts"].get("permission_prompts")
            if was is not None and (n is None or was > n):
                best["counts"]["permission_prompts"] = was
                best["permission_log_first_day"] = stored.get("permission_log_first_day")
    if fresh and store and not project:
        try:
            _write_json(path, best)
        except OSError:
            pass
    return best


# ---------- snapshots ----------

def snapshot_name(label):
    """A snapshot label as a safe file stem: letters, digits, dot, underscore, hyphen."""
    if (not isinstance(label, str) or not NAME_RE.match(label) or ".." in label
            or label.lower() in FILLER):
        raise UsageError("a snapshot name is 1 to 40 letters, digits, dots, underscores or "
                         "hyphens, starting with a letter or digit")
    return label.lower()


def _snapshot_file(name):
    return os.path.join(compare_dir(), "snapshots", snapshot_name(name) + ".json")


def save_snapshot(label, metrics, force=False):
    """Store one period's numbers under a name. Returns the file path.

    An existing snapshot is replaced only by the same period with at least as
    many sessions, or with force.
    """
    path = _snapshot_file(label)
    old = _read_json(path)
    if old and not force:
        m = old.get("period") if isinstance(old.get("period"), dict) else {}
        same = (m.get("first_day"), m.get("last_day")) == (metrics.get("first_day"),
                                                           metrics.get("last_day"))
        if not same:
            raise UsageError("snapshot %s already holds %s to %s. Pick another name or add "
                             "--force." % (snapshot_name(label), m.get("first_day"),
                                           m.get("last_day")))
        if ((bool(m.get("include_scheduled")), m.get("context_threshold"))
                != (bool(metrics.get("include_scheduled")), metrics.get("context_threshold"))):
            raise UsageError("snapshot %s was saved with other options (scheduled sessions or "
                             "the context mark). Pick another name or add --force."
                             % snapshot_name(label))
        if _num(m.get("sessions_seen")) > _num(metrics.get("sessions_seen")):
            raise UsageError("snapshot %s holds more sessions than are on disk now, so it was "
                             "left as it is. Add --force to replace it." % snapshot_name(label))
    kept = dict((k, v) for k, v in metrics.items() if k != "skip_ids")   # numbers only
    _write_json(path, {"version": VERSION, "label": snapshot_name(label),
                       "saved": C.epoch_local(time.time()), "period": kept})
    return path


def load_snapshot(label):
    """The stored numbers of a named snapshot, marked with where they came from."""
    snap = _read_json(_snapshot_file(label))
    m = snap.get("period") if snap else None
    if not _valid_metrics(m):
        raise UsageError("no snapshot named %s. See: compare.py snapshots" % snapshot_name(label))
    m = dict(m)
    m["source"] = "snapshot %s" % snapshot_name(label)
    m["snapshot"] = snapshot_name(label)
    return m


def list_snapshots():
    """[{label, saved, first_day, last_day, sessions}] for every stored snapshot."""
    folder = os.path.join(compare_dir(), "snapshots")
    out = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return out
    for n in names:
        snap = _read_json(os.path.join(folder, n)) if n.endswith(".json") else None
        m = snap.get("period") if snap else None
        if _valid_metrics(m):
            out.append({"label": n[:-5], "saved": snap.get("saved"),
                        "first_day": m.get("first_day"), "last_day": m.get("last_day"),
                        "sessions": _num(m["counts"].get("sessions")),
                        "include_scheduled": bool(m.get("include_scheduled"))})
    return out


# ---------- the comparison ----------

def pct_change(before, after):
    """Percent change, one decimal. None when there is nothing to divide by."""
    if before is None or after is None or not before:
        return None
    return round((after - before) * 100.0 / before, 1)


def normalise(value, denominator, scale=1):
    """value per `scale` units of the denominator. None when it cannot be formed."""
    if value is None or not denominator:
        return None
    return round(value * float(scale) / denominator, 4)


def _value(metrics, key):
    c = metrics["counts"]
    if key == "days":
        return metrics.get("days")
    if key == "active_hours":
        return _num(c.get("active_min")) / 60.0
    if key == "subagent_share":
        total = _num(c.get("tokens_total"))
        return round(_num(c.get("subagent_tokens")) * 100.0 / total, 1) if total else None
    if key.startswith("finding:"):
        return _num(metrics["findings"].get(key[8:]))
    v = c.get(key)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def too_little(metrics, rules=RULES):
    """Why this period is too small to judge, or None."""
    c = metrics["counts"]
    if _num(c.get("sessions")) < rules["min_sessions"]:
        return "%d sessions, fewer than %d" % (_num(c.get("sessions")), rules["min_sessions"])
    if _num(c.get("active_min")) < rules["min_active_min"]:
        return "%d active minutes, fewer than %d" % (_num(c.get("active_min")),
                                                     rules["min_active_min"])
    return None


def compare_metric(key, label, kind, basis, direction, before, after, small=None, rules=RULES):
    """One row: before, after, change, percent, the normalised form and a state.

    States: improvement, regression (direction "lower" only), up, down
    (neutral metrics), no_real_change, too_little_data, not_available.
    Percents are None unless the state is a real one or no_real_change.
    """
    b, a = _value(before, key), _value(after, key)
    row = {"key": key, "label": label, "kind": kind, "direction": direction,
           "before": b, "after": a, "change": None, "pct": None, "basis": None,
           "norm_before": None, "norm_after": None, "norm_change": None, "norm_pct": None,
           "from_zero": False, "state": "not_available", "reason": None}
    if basis:
        den, scale, words = basis
        row["basis"] = words
        row["norm_before"] = normalise(b, _value(before, den), scale)
        row["norm_after"] = normalise(a, _value(after, den), scale)
        if row["norm_before"] is not None and row["norm_after"] is not None:
            row["norm_change"] = round(row["norm_after"] - row["norm_before"], 4)
    if b is None or a is None:
        row["reason"] = "one period has no figure for this"
        return row
    row["change"] = round(a - b, 4)
    if small:
        row["state"], row["reason"] = "too_little_data", small
        return row
    if basis and (row["norm_before"] is None or row["norm_after"] is None):
        row["state"], row["reason"] = "too_little_data", "nothing to divide by"
        return row
    if kind == "count" and max(b, a) < rules["min_events"]:
        row["state"] = "too_little_data"
        row["reason"] = "fewer than %d in both periods" % rules["min_events"]
        return row
    row["pct"] = pct_change(b, a)
    jb, ja = float(b), float(a)                 # judged unrounded
    if basis:
        row["norm_pct"] = pct_change(row["norm_before"], row["norm_after"])
        jb = b * float(basis[1]) / _value(before, basis[0])
        ja = a * float(basis[1]) / _value(after, basis[0])
    if kind == "share":
        row["pct"] = None                       # a share moves in points, not percent
        real = abs(ja - jb) >= rules["share_points"] - 1e-9
    elif not jb:
        real = bool(ja)
        row["from_zero"] = real
    else:
        real = abs(ja - jb) * 100.0 / jb >= rules["threshold_pct"] - 1e-9
    if not real:
        row["state"] = "no_real_change"
    elif direction == "lower":
        row["state"] = "improvement" if ja < jb else "regression"
    else:
        row["state"] = "up" if ja > jb else "down"
    return row


def _period_view(m):
    return {"first_day": m.get("first_day"), "last_day": m.get("last_day"),
            "days": m.get("days"), "source": m.get("source"), "snapshot": m.get("snapshot"),
            "sessions_seen": m.get("sessions_seen"), "excluded": m.get("excluded"),
            "partial_sessions": m.get("partial_sessions") or 0,
            "include_scheduled": bool(m.get("include_scheduled")),
            "too_little_data": too_little(m)}


def _strength(row):
    judged = 0 if row["state"] in ("improvement", "regression") else 1
    if row["from_zero"]:
        size = 1e9
    elif row["kind"] == "share":
        size = abs(row["after"] - row["before"])
    else:
        size = abs(row["norm_pct"] if row["basis"] else row["pct"])
    return (judged, -size)


def compare(before, after, rules=RULES):
    """Compare two periods' numbers (from collect or load_snapshot). JSON-serialisable."""
    notes = []
    small = None
    for side, m in (("before", before), ("after", after)):
        why = too_little(m, rules)
        if why:
            small = small or "the %s period has %s" % (side, why)
            notes.append("Too little data: the %s period has %s. No percent is given."
                         % (side, why))
        if m.get("partial_sessions"):
            notes.append("The %s period comes from a stored digest in which %d small sessions "
                         "carry only prompts, tool calls and output tokens, so its other "
                         "counts run low." % (side, m["partial_sessions"]))
        if m.get("cost_partial"):
            notes.append("Only some sessions in the %s period carry a cost, so its cost runs low."
                         % side)
        if m.get("source") not in (None, "transcripts"):
            notes.append("The %s period uses %s, not today's transcripts."
                         % (side, m.get("source")))
    if bool(before.get("include_scheduled")) != bool(after.get("include_scheduled")):
        notes.append("One period includes scheduled sessions and the other does not, so the "
                     "two are not like for like.")
    if before.get("context_threshold") != after.get("context_threshold"):
        notes.append("The two periods use different context marks (%s and %s)."
                     % (before.get("context_threshold"), after.get("context_threshold")))
    log_first = after.get("permission_log_first_day")
    if (log_first and before.get("first_day") and before["first_day"] < log_first
            and before["counts"].get("permission_prompts") is not None):
        notes.append("The permission log starts on %s, inside the before period, so its "
                     "permission prompt count runs low." % log_first)
    volume = {"differs": False, "text": None}
    bm, am = _num(before["counts"].get("active_min")), _num(after["counts"].get("active_min"))
    bt, at = _num(before["counts"].get("turns")), _num(after["counts"].get("turns"))
    for lo_v, hi_v in ((min(bm, am), max(bm, am)), (min(bt, at), max(bt, at))):
        if hi_v and (not lo_v or hi_v / float(lo_v) >= rules["volume_ratio"]):
            volume["differs"] = True
    if volume["differs"]:
        volume["text"] = ("The periods differ a lot in volume: %d active minutes and %d prompts "
                          "before, %d and %d after. Read the normalised columns, not the totals."
                          % (bm, bt, am, at))
        notes.append(volume["text"])
    if before.get("days") != after.get("days"):
        notes.append("The periods differ in length: %s days before, %s after."
                     % (before.get("days"), after.get("days")))
    mark = after.get("context_threshold") or signals.T["peak_context"]
    rows = []
    for key, label, kind, basis, direction in METRICS:
        if key == "sessions_over_context":
            label = "Sessions over %s context" % _fmt(mark, "tokens")
        rows.append(compare_metric(key, label, kind, basis, direction, before, after, small, rules))
    patterns = sorted(set(before["findings"]) | set(after["findings"]))
    findings = [compare_metric("finding:" + p, p, "count", ("sessions", 1, "per session"),
                               "lower", before, after, small, rules) for p in patterns]
    projects = []
    names = set(before["projects"]) | set(after["projects"])
    for name in names:
        pb = before["projects"].get(name) or {}
        pa = after["projects"].get(name) or {}
        enough = (not small and min(_num(pb.get("sessions")), _num(pa.get("sessions")))
                  >= rules["min_sessions"])
        item = {"project": name, "too_little_data": not enough}
        for k in PROJECT_KEYS:
            item[k] = {"before": _num(pb.get(k)), "after": _num(pa.get(k))}
        tb = normalise(_num(pb.get("tokens_total")), _num(pb.get("turns")))
        ta = normalise(_num(pa.get("tokens_total")), _num(pa.get("turns")))
        item["tokens_per_prompt"] = {"before": tb, "after": ta,
                                     "pct": pct_change(tb, ta) if enough else None}
        projects.append(item)
    projects.sort(key=lambda p: (-(p["tokens_total"]["before"] + p["tokens_total"]["after"]),
                                 p["project"]))
    real = sorted((r for r in rows + findings if r["state"] in REAL_STATES), key=_strength)
    return {"version": VERSION, "generated": C.epoch_local(time.time()),
            "before": _period_view(before), "after": _period_view(after),
            "rules": dict(rules, context_threshold=mark),
            "too_little_data": bool(small), "volume": volume, "notes": notes,
            "metrics": rows, "findings": findings, "projects": projects[:12],
            "highlights": [r["key"] for r in real[:6]]}


def run_words(words, today=None, projects_dir=None, include_scheduled=False, project=None,
              context_threshold=None, store=True):
    """The whole thing from argument words: plan, collect both periods, compare."""
    before_p, after_p, notes = plan(words, today)
    kw = dict(projects_dir=projects_dir, include_scheduled=include_scheduled, project=project,
              context_threshold=context_threshold, store=store)
    if "snapshot" in before_p:
        if project:
            raise UsageError("a snapshot covers all projects; drop --project")
        before = load_snapshot(before_p["snapshot"])
        if str(before.get("last_day") or "") >= after_p["first"].isoformat():
            raise UsageError("snapshot %s covers %s to %s, which overlaps or follows the period "
                             "you are comparing it with" % (before_p["snapshot"],
                                                           before.get("first_day"),
                                                           before.get("last_day")))
    else:
        before = collect(before_p["first"], before_p["end"], **kw)
    after = collect(after_p["first"], after_p["end"], **kw)
    result = compare(before, after)
    result["scope_sig"] = SC.Scope().sig()
    result["notes"] = notes + result["notes"]
    if project:
        result["notes"].append("Only projects whose folder name contains \"%s\"." % project)
    return result


# ---------- output ----------

def _fmt(v, kind="count"):
    if v is None:
        return "-"
    if kind == "cost":
        return "$%.2f" % v
    if kind == "share":
        return "%.1f%%" % v
    a = abs(v)
    if a >= 1e9:
        return "%.2fB" % (v / 1e9)
    if a >= 1e6:
        return "%.1fM" % (v / 1e6)
    if a >= 1e4:
        return "%.0fk" % (v / 1e3)
    if isinstance(v, float) and v != int(v):
        return "%.2f" % v if a < 100 else "%.0f" % v
    return "%d" % v


def _short(name, limit=48):
    """A long folder path cut to its tail, for the text table only."""
    return name if len(name) <= limit else "..." + name[-(limit - 3):]


def _fmt_pct(p, from_zero=False):
    if from_zero:
        return "from 0"
    return "-" if p is None else "%+.0f%%" % p


def _fmt_change(row):
    if row["change"] is None:
        return "-"
    if row["kind"] == "share":
        return "%+.1f pts" % row["change"]
    text = _fmt(row["change"], row["kind"])
    return text if text.startswith("-") or row["change"] == 0 else "+" + text


def _period_line(name, p):
    left = p.get("excluded") or {}
    out = "%s: %s to %s (%s days), %s" % (name, p["first_day"], p["last_day"], p["days"],
                                          p.get("source") or "transcripts")
    if p.get("include_scheduled"):
        return out + ", scheduled and coach sessions included"
    return out + ", left out %d scheduled and %d coach sessions" % (
        _num(left.get("scheduled")), _num(left.get("coach")))


def text_table(result):
    """A compact plain-text table of the comparison."""
    lines = [_period_line("Before", result["before"]), _period_line("After", result["after"])]
    for n in result["notes"]:
        lines.append("Note: " + n)
    r = result["rules"]
    lines.append("Rules: a change counts only at %d%% or more on the normalised figure (%d points "
                 "for a share); counts under %d are not judged."
                 % (r["threshold_pct"], r["share_points"], r["min_events"]))
    head = ("Metric", "Before", "After", "Change", "%", "Basis", "Before", "After", "%", "Verdict")
    table = [head]
    for row in result["metrics"] + result["findings"]:
        if row["state"] == "not_available":
            continue
        table.append((row["label"], _fmt(row["before"], row["kind"]),
                      _fmt(row["after"], row["kind"]), _fmt_change(row),
                      _fmt_pct(row["pct"]), row["basis"] or "-",
                      _fmt(row["norm_before"], row["kind"]) if row["basis"] else "-",
                      _fmt(row["norm_after"], row["kind"]) if row["basis"] else "-",
                      _fmt_pct(row["norm_pct"], row["from_zero"]) if row["basis"]
                      else _fmt_pct(None, row["from_zero"]),
                      STATE_TEXT[row["state"]]))
        if row["key"] == "findings_total" and result["findings"]:
            table.append(("  by pattern:",) + ("",) * 9)
    widths = [max(len(str(t[i])) for t in table) for i in range(len(head))]
    for t in table:
        cells = [str(c).ljust(w) if i in (0, 5, 9) else str(c).rjust(w)
                 for i, (c, w) in enumerate(zip(t, widths))]
        lines.append("  ".join(cells).rstrip())
    if result["projects"]:
        lines.append("")
        lines.append("By project (before > after): sessions, prompts, tokens, tokens per prompt")
        for p in result["projects"]:
            tp = p["tokens_per_prompt"]
            lines.append("  %s: %d > %d sessions, %d > %d prompts, %s > %s tokens, %s > %s per "
                         "prompt (%s)" % (
                             _short(p["project"]), p["sessions"]["before"], p["sessions"]["after"],
                             p["turns"]["before"], p["turns"]["after"],
                             _fmt(p["tokens_total"]["before"], "tokens"),
                             _fmt(p["tokens_total"]["after"], "tokens"),
                             _fmt(tp["before"], "tokens"), _fmt(tp["after"], "tokens"),
                             "too little data" if p["too_little_data"] else _fmt_pct(tp["pct"])))
    return "\n".join(lines)


def latest_file():
    return os.path.join(compare_dir(), "latest.json")


def latest():
    """The most recent comparison stored by the CLI, or None."""
    obj = _read_json(latest_file())
    return obj if obj and obj.get("version") == VERSION else None


def json_for_script(obj):
    """JSON that is safe inside a script element: ASCII only, and no raw <, >, &
    or quote characters inside the data."""
    s = json.dumps(obj, ensure_ascii=True, separators=(",", ":"))
    s = re.sub(r"\\(.)", lambda m: "\\u0022" if m.group(1) == '"' else m.group(0), s)
    return (s.replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026").replace("'", "\\u0027"))


def page_block(result=None):
    """For the page builder: {"id", "json", "data"} or None when nothing is stored.

    json is the comparison as script-safe JSON, ready to sit in
    <script type="application/json" id="coach-compare-data">. data is the same
    comparison as a dict. Project names in it are text: render with textContent.
    """
    stored = result is None
    result = result or latest()
    if not result:
        return None
    if stored and (result.get("scope_sig") or "") != SC.Scope().sig():
        return None            # counted under other show settings: not shown
    return {"id": "coach-compare-data", "json": json_for_script(result), "data": result}


def weekly_file(label, today=None, projects_dir=None):
    """For recap.py in weekly mode: compare the last 7 days with the 7 before and
    write the result to <coach home>/compare/weekly-<label>.json (label is the
    weekly report's date). Returns the path, or None on failure."""
    if not DATE_RE.match(str(label)):
        return None
    path = os.path.join(compare_dir(), "weekly-%s.json" % label)
    try:
        result = run_words(["last7"], today=today, projects_dir=projects_dir)
        _write_json(path, result)
        _write_json(latest_file(), result)
    except Exception:       # the weekly recap must never fail because of this
        return None
    return path


def main(argv=None, today=None):
    """Command line entry. `today` pins the date relative periods count back from
    (lastN, week, lastweek); None means the real date."""
    ap = argparse.ArgumentParser(
        description="Compare two periods of Claude Code use, or store one as a snapshot.",
        epilog="Forms: nothing or lastN | week | around DATE [DAYS] | four dates | "
               "against NAME [period] | snapshot --label NAME [period] | snapshots")
    ap.add_argument("words", nargs="*", help="see the forms below")
    ap.add_argument("--around", help="pivot date YYYY-MM-DD: N days before it versus N days from it")
    ap.add_argument("--days", help="days each side of --around (default 7)")
    ap.add_argument("--before", nargs=2, metavar="DATE", help="the before period, two dates")
    ap.add_argument("--after", nargs=2, metavar="DATE", help="the after period, two dates")
    ap.add_argument("--label", help="snapshot name, for the snapshot form")
    ap.add_argument("--force", action="store_true", help="replace an existing snapshot")
    ap.add_argument("--include-scheduled", action="store_true",
                    help="count scheduled-task sessions and the coach's own sessions too")
    ap.add_argument("--context-threshold", type=int,
                    help="tokens of context that mark a heavy session (default %d)"
                         % signals.T["peak_context"])
    ap.add_argument("--project", help="only projects whose folder name contains this")
    ap.add_argument("--format", choices=("json", "text"), default="json")
    ap.add_argument("--out", help="write here instead of stdout")
    ap.add_argument("--no-store", action="store_true",
                    help="write nothing under the coach home")
    ap.add_argument("--projects-dir", help="override the transcripts folder (for tests)")
    args = ap.parse_intermixed_args(argv)     # words may sit either side of a flag
    words = list(args.words)
    kw = dict(projects_dir=args.projects_dir, include_scheduled=args.include_scheduled,
              project=args.project, context_threshold=args.context_threshold,
              store=not args.no_store)
    try:
        if args.context_threshold is not None and args.context_threshold <= 0:
            raise UsageError("--context-threshold must be above zero")
        if words and words[0].lower() == "snapshots":
            out = {"snapshots": list_snapshots()}
            text = "\n".join("%s  %s to %s  %d sessions  saved %s" % (
                s["label"], s["first_day"], s["last_day"], s["sessions"], s["saved"])
                for s in out["snapshots"]) or "no snapshots"
        elif words and words[0].lower() == "snapshot":
            if not args.label:
                raise UsageError("snapshot needs --label NAME")
            if args.project:
                raise UsageError("a snapshot covers all projects; drop --project")
            if args.no_store:
                raise UsageError("a snapshot is a stored file; drop --no-store")
            p = one_period(words[1:], today)
            m = collect(p["first"], p["end"], **kw)
            path = save_snapshot(args.label, m, args.force)
            out = {"snapshot": snapshot_name(args.label), "file": path,
                   "first_day": m["first_day"], "last_day": m["last_day"],
                   "source": m["source"], "sessions": m["counts"]["sessions"],
                   "excluded": m["excluded"], "too_little_data": too_little(m)}
            text = "saved snapshot %s: %s to %s, %d sessions" % (
                out["snapshot"], m["first_day"], m["last_day"], out["sessions"])
        else:
            if args.around:
                if words or args.before or args.after:
                    raise UsageError("--around cannot be mixed with other periods")
                words = ["around", args.around] + ([args.days] if args.days else [])
            elif args.before or args.after:
                if words or not (args.before and args.after):
                    raise UsageError("give both --before and --after, two dates each")
                words = list(args.before) + list(args.after)
            elif args.days:
                raise UsageError("--days goes with --around")
            out = run_words(words, today=today, **kw)
            if not args.no_store and not args.project:
                try:
                    _write_json(latest_file(), out)
                except OSError:
                    pass                # the comparison is still printed
            text = text_table(out)
    except OSError as e:
        sys.stderr.write("coach compare: cannot write: %s\n" % e)
        return 2
    except UsageError as e:
        sys.stderr.write("coach compare: %s\nUse: nothing or lastN, week, around DATE [DAYS], "
                         "four dates, against NAME [period], snapshot --label NAME [period], "
                         "snapshots.\n" % e)
        return 2
    blob = text if args.format == "text" else json.dumps(out, indent=1)
    if args.out:
        try:
            with open(args.out, "w") as fh:
                fh.write(blob + "\n")
        except OSError as e:
            sys.stderr.write("coach compare: cannot write: %s\n" % e)
            return 2
    else:
        sys.stdout.write(blob + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(C.run_main(main))
