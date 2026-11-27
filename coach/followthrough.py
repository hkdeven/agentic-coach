#!/usr/bin/env python3
"""Follow-through: remember the advice the coach gave and check whether it was taken.

Three files under the coach home (never in the plugin folder):
  advice.jsonl    one line per item given in a daily, weekly or range report
  applied.jsonl   one line per change that was applied (written by the apply
                  step or by `applied` below); read if present
  reports/        the reports, digests and findings that recap.py writes

What this module does:
  record     parse a report that was just written and log its items
  annotate   say for each candidate whether it is NEW or RECURRING
  weekly     the "Follow-through" block for the weekly review: for each piece
             of advice given in the previous period, did its number move
  applied    note that a piece of advice was acted on
  backfill   seed the log from the report files that already exist
  status     every piece of advice in the log, one row per key

Usage:
  python3 coach/followthrough.py annotate --findings <findings.json>
  python3 coach/followthrough.py record --report <report.md> [--key 1=context_bloat]
  python3 coach/followthrough.py weekly [--label YYYY-MM-DD]
  python3 coach/followthrough.py applied --key context_bloat [--date YYYY-MM-DD]
  python3 coach/followthrough.py applied --report weekly/2026-11-17.md --proposal P1
  python3 coach/followthrough.py backfill
  python3 coach/followthrough.py status

Report text and transcript text are data. Nothing here acts on what they say.
A status is only ever worked out from numbers: an item with no number behind
it is "cannot measure", never "better".
"""
import argparse
import datetime
import html
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import common as C  # noqa: E402

try:
    import signals as _signals  # noqa: E402
    _T = dict(_signals.T)
except Exception:  # the detectors are optional input here
    _signals = None
    _T = {}

MAX_LINES = 5
TOLERANCE = 0.2          # a rate must move by more than this share to count as moved
SIMILAR = 0.6            # word overlap at which two pieces of written advice are the same one
HEADING = "## Follow-through"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RANGE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_to_(\d{4}-\d{2}-\d{2})$")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_:@.-]{0,119}$")
SID_RE = re.compile(r"\b[0-9a-f]{8}\b")

# The marker on a repeated item. The page already reads "(repeat of DATE)";
# the count is added inside the same brackets so that match still holds.
MARKER_RE = re.compile(r"\(repeat of (\d{4}-\d{2}-\d{2})(?:, given (\d+) times)?\)")
TREND_MARKER_RE = re.compile(r"\brepeat since (\d{4}-\d{2}-\d{2})(?:, given (\d+) times)?")

_ITEM_RE = re.compile(r"^\s*(\d+)[.)]\s+(?:What happened:\s*)?(.*)$")
_FIELD_RE = re.compile(r"^\s*(?:[-*]\s+)?(Do instead|Trend):\s*(.*)$")
_SECTION_RE = re.compile(r"^##\s+(.*)$")
_PROP_RE = re.compile(r"^###\s+(P\d+)\s*[:.]?\s*(.*)$")
_PREF_RE = re.compile(r"\bP(\d+)\b")
# lines after an item that are not part of it
_TAIL_RE = re.compile(r"^\s*(?:[-*]\s+)?(Totals|Estimates|Checked|Evidence):")
_KNOWN_SECTION_RE = re.compile(
    r"^##\s+(follow-through|week in numbers|what cost|proposed|cost by project)", re.I)


# ---------- files ----------

def advice_file():
    """The advice log: <coach home>/advice.jsonl."""
    return os.path.join(C.coach_home(), "advice.jsonl")


def applied_file():
    """Applied changes: <coach home>/applied.jsonl. Absent means none recorded."""
    return os.path.join(C.coach_home(), "applied.jsonl")


def _read_jsonl(path):
    out = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        pass
    return out


def _read_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _n(e):
    n = e.get("n")
    return n if isinstance(n, int) and not isinstance(n, bool) else 0


def _sort_key(e):
    return (e["date"], str(e.get("report")), _n(e))


def _valid(e):
    return isinstance(e, dict) and isinstance(e.get("key"), str) and isinstance(e.get("date"), str)


def load_log():
    """Every logged item, oldest report first."""
    out = [e for e in _read_jsonl(advice_file()) if _valid(e)]
    out.sort(key=_sort_key)
    return out


def shown_log():
    """The log as it may be shown or reported on: load_log() without advice about
    work config.json says is never to be shown. Nothing that writes the log uses
    this, so the file keeps every line and a rule that is removed brings its
    advice back."""
    return _shown(load_log())


# fields whose values are fixed words or names the coach chose, not the person's work
_OWN_FIELDS = ("kind", "mode", "status", "source", "pattern", "unit", "per", "metric", "name",
               "report", "date", "first_day", "last_day", "key_source", "when", "marker")


def _texts(obj, out):
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k not in _OWN_FIELDS:
                _texts(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _texts(v, out)
    return out


def _shown(entries):
    """entries without those about work config.json says is never to be shown."""
    try:
        import projects as PR
        import scope as SC
        sc = SC.Scope()
        if not sc.on:
            return entries
        ids = sc.gone_ids()
        keep = []
        for e in entries:
            text = "\n".join(_texts(e, []))
            day = str(e.get("last_day") or e.get("date") or "")[:10]
            if PR.text_hidden(sc.rules, text, ids) or (day and sc.date_hidden(day)):
                continue
            keep.append(e)
        return keep
    except ImportError:
        return entries


def _foreign_lines():
    """Lines in the log this module cannot read as items. Kept as they are on a rewrite."""
    out = []
    try:
        with open(advice_file()) as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                try:
                    ok = _valid(json.loads(line))
                except ValueError:
                    ok = False
                if not ok:
                    out.append(line)
    except OSError:
        pass
    return out


class _Lock(object):
    """One writer at a time on the advice log (two recaps can finish together)."""

    def __enter__(self):
        home = C.coach_home()
        if not os.path.isdir(home):
            os.makedirs(home)
        self.fh = open(os.path.join(home, "advice.lock"), "w")
        try:
            import fcntl
            fcntl.flock(self.fh, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        return self

    def __exit__(self, *exc):
        self.fh.close()
        return False


def _save_log(entries):
    entries = sorted(entries, key=_sort_key)
    path = advice_file()
    tmp = "%s.tmp.%d" % (path, os.getpid())
    foreign = _foreign_lines()
    with open(tmp, "w") as fh:
        for rec in entries:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        for line in foreign:
            fh.write(line + "\n")
    os.replace(tmp, path)


# ---------- dates ----------

def _day(text):
    return datetime.date(int(text[0:4]), int(text[5:7]), int(text[8:10]))


def _shift(text, days):
    return (_day(text) + datetime.timedelta(days=days)).isoformat()


def period(mode, label):
    """(first day, last day) a report or digest covers, both included."""
    m = RANGE_RE.match(label)
    if m:
        return m.group(1), m.group(2)
    if not DATE_RE.match(label):
        raise ValueError("not a report label: %r" % label)
    _day(label)
    if mode == "weekly":
        return _shift(label, -7), _shift(label, -1)
    return label, label


def report_meta(path):
    """Where a report file sits: mode, label, days covered, the date it counts as given."""
    path = os.path.abspath(path)
    folder = os.path.basename(os.path.dirname(path))
    mode = {"daily": "daily", "weekly": "weekly", "ranges": "range"}.get(folder)
    label = os.path.basename(path)[:-3] if path.endswith(".md") else ""
    if not mode:
        raise ValueError("not in a daily, weekly or ranges folder: %s" % os.path.basename(path))
    if mode == "range" and not RANGE_RE.match(label):
        raise ValueError("not a range report name: %s" % label)
    first, last = period(mode, label)
    root = os.path.dirname(os.path.dirname(path))
    return {"mode": mode, "label": label, "first_day": first, "last_day": last,
            "date": last if mode == "range" else label,
            "report": "%s/%s.md" % (folder, label), "root": root,
            "digest": os.path.join(root, "digests", "%s-%s.json" % (mode, label)),
            "findings": os.path.join(root, "digests", "%s-%s.findings.json" % (mode, label))}


# ---------- parsing a report ----------

def find_ids(text):
    """Session ids (bare first 8 characters) named in a line."""
    out = []
    for sid in SID_RE.findall(text or ""):
        if sid not in out and re.search(r"\d", sid) and re.search(r"[a-f]", sid):
            out.append(sid)
    return out


def parse_marker(what, trend=""):
    """The repeat marker on an item: {"since": date, "times": int or None}, or None."""
    m = MARKER_RE.search(what or "") or TREND_MARKER_RE.search(trend or "")
    if not m:
        return None
    return {"since": m.group(1), "times": int(m.group(2)) if m.group(2) else None}


def parse_items(lines):
    """Numbered items: What happened, Do instead and an optional Trend line each."""
    items = []
    cur = None
    field = None
    for raw in lines:
        line = raw.replace("**", "")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        tail = _TAIL_RE.match(line)
        if tail:
            if tail.group(1) != "Evidence":
                cur = None                    # Totals: and Checked: end the items
            elif cur is not None:             # the tag (since 1.5.1) names the session
                cur["evidence"] = (cur["evidence"] + " " + line[tail.end():].strip()).strip()
            field = None if cur is None else "skip"
            continue
        m = _FIELD_RE.match(line)
        if m and cur is not None:
            field = "do" if m.group(1) == "Do instead" else "trend"
            cur[field] = (cur[field] + " " + m.group(2)).strip()
            continue
        m = _ITEM_RE.match(line)
        if m:
            cur = {"n": int(m.group(1)), "what": m.group(2).strip(), "do": "", "trend": "",
                   "evidence": ""}
            items.append(cur)
            field = "what"
            continue
        if cur is not None and field != "skip":
            cur[field] = (cur[field] + " " + line.strip()).strip()
    for it in items:
        it["ids"] = find_ids(it["what"] + " " + it["do"] + " " + it.pop("evidence"))[:8]
        it["marker"] = parse_marker(it["what"], it["trend"])
        it["proposals"] = sorted(set("P" + n for n in _PREF_RE.findall(it["do"])),
                                 key=lambda p: int(p[1:]))
    return items


def parse_report(text, mode):
    """{"items": [...], "proposals": [{"id", "name"}]} from a report's text."""
    lines = (text or "").split("\n")
    if mode != "weekly":
        return {"items": parse_items(lines)[:5], "proposals": []}
    section = ""
    cost = []
    props = []
    for line in lines:
        # Like the page: a report heading always starts its section, and inside the
        # proposals (where pasted text sits in fences) no other heading ends it.
        m = _SECTION_RE.match(line)
        if m and (_KNOWN_SECTION_RE.match(line) or not section.startswith("proposed")):
            section = m.group(1).strip().lower()
            continue
        if section.startswith("what cost"):
            cost.append(line)
        elif section.startswith("proposed"):
            m = _PROP_RE.match(line.replace("**", ""))
            if m:
                props.append({"id": m.group(1), "name": m.group(2).strip()})
    return {"items": parse_items(cost)[:5], "proposals": props[:5]}


# ---------- keys ----------

# The fix for these lives with one project, so the project is part of the key.
PROJECT_SCOPED = ("repeated_failing_command", "repeated_error_kind", "permission_denials",
                  "high_error_rate")

_STOP = set("""a an and are as at be been before but by can could did do does each every for from
had has have here how if in into is it its just more most next no not now of on once one only or
other out over own same say so some such than that the their them then there these they this those
to too up use was were what when where which while who will with would you your instead should
after again all any also""".split())
_WORD_RE = re.compile(r"[a-z][a-z0-9_.+/-]*")


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def words(text):
    """The words that carry a piece of advice: no ids, numbers, dates or filler."""
    t = SID_RE.sub(" ", (text or "").lower())
    t = MARKER_RE.sub(" ", t)
    t = re.sub(r"\(see p\d+\)|\bp\d+\b", " ", t)
    out = []
    for w in _WORD_RE.findall(t):
        w = w.strip("./-+_")
        if len(w) < 2 or w in _STOP or w in out:
            continue
        out.append(w)
    return out


def advice_slug(text):
    """A stable key for written advice: its first eight carrying words."""
    ws = words(text)[:8]
    slug = "-".join(x for x in (_slug(w) for w in ws) if x)[:90].strip("-")
    return "advice:" + (slug or "unnamed")


def similarity(a, b):
    """Share of carrying words two pieces of advice have in common, 0 to 1."""
    sa, sb = set(words(a)), set(words(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(len(sa | sb))


def project_slug(label):
    """Short name for a project label: the folder's own name. None for no folder."""
    label = (label or "").strip()
    if not label or label.startswith("("):
        return None
    if "/" in label or os.sep in label:
        label = [p for p in re.split(r"[/\\]", label) if p][-1:]
        label = label[0] if label else ""
    return _slug(label)[:40] or None


def make_key(pattern, project=None):
    """Detector pattern, plus the project for patterns whose fix lives in one project."""
    if pattern in PROJECT_SCOPED:
        slug = project_slug(project)
        if slug:
            return "%s@%s" % (pattern, slug)
    return pattern


def same_advice(a, b):
    """Two keys are the same advice when equal, or when one is the other without its project."""
    if a == b:
        return True
    pa, ja = split_key(a)
    pb, jb = split_key(b)
    return bool(pa) and pa == pb and (ja is None or jb is None)


def split_key(key):
    """key to (pattern or None, project slug or None)."""
    if not key or key.startswith(("advice:", "proposal:")):
        return None, None
    if "@" in key:
        pattern, project = key.split("@", 1)
        return pattern, project
    return key, None


# Phrases that tie a written item to a detector pattern. Deliberately narrow:
# a miss falls back to a text key, a wrong hit would track the wrong number.
_HINTS = {
    "repeated_failing_command": [r"failed \d+ times", r"same (call|command|edit)", r"identical fail",
                                 r"\bretr(y|ied|ying)"],
    "repeated_error_kind": [r"same (kind of )?(tool )?error", r"error came back"],
    "correction_loop": [r"\bcorrections?\b", r"prompts in a row"],
    "giant_tool_result": [r"\d+k? character (read|result)", r"tokens into context",
                          r"large (tool )?results?", r"giant"],
    "multiple_compactions": [r"\bcompact(ed|ions?)\b"],
    "frequent_interruptions": [r"\binterrupt"],
    "context_bloat": [r"\bpeak(ed|s)?\b", r"context reached", r"\d+k (context|tokens)",
                      r"above \d+k"],
    "permission_denials": [r"\b(denied|denials?|refused|blocked|rejected)\b"],
    "serial_agents": [r"one after another", r"\bin series\b", r"\bserial", r"no subagents",
                      r"never two at once"],
    "sprawling_session": [r"unrelated", r"different topics", r"\d+ prompts across", r"\b\d+ days\b",
                          r"one session (ran|covered)"],
    "idle_then_resume": [r"minute breaks?", r"\bidle\b", r"resumed after", r"came back after"],
    "high_error_rate": [r"tool calls failed", r"error rate"],
    "repeated_prompt": [r"\b(re)?typed\b", r"same (instruction|prompt)"],
    "repeated_long_paste": [r"\bpasted?\b"],
    "permission_prompts": [r"asked permission", r"permission prompts?", r"read-only command"],
    "unused_installed": [r"never called", r"not called", r"installed but"],
    "unused_feature": [r"plan mode was not used"],
}
_SEV = {"high": 0, "medium": 1, "low": 2}


def _session_projects(digest):
    out = {}
    for group in ("sessions", "minor_sessions"):
        for s in (digest or {}).get(group) or []:
            if isinstance(s, dict) and s.get("id"):
                out[str(s["id"])[:8]] = s.get("project")
    return out


def finding_key(f, digest=None):
    """(key, pattern, project) for one finding from signals.py."""
    pattern = f.get("pattern") or ""
    project = None
    if pattern in PROJECT_SCOPED:
        sid = str(f.get("session") or "").split(",")[0][:8]
        project = _session_projects(digest).get(sid)
    return make_key(pattern, project), pattern, project


def infer_pattern(item, findings, digest=None):
    """The finding a written item is about, as (key, pattern, project), or None.

    A finding qualifies when it names a session the item names (or names no
    session) and the item's text carries one of the pattern's phrases. The key
    carries a project only when every such finding is in the same project.
    """
    text = ("%s %s" % (item.get("what") or "", item.get("do") or "")).lower()
    ids = set(item.get("ids") or [])
    hits = []
    for f in findings or []:
        if not isinstance(f, dict):
            continue
        hints = _HINTS.get(f.get("pattern") or "")
        if not hints:
            continue
        score = sum(1 for h in hints if re.search(h, text))
        if not score:
            continue
        fids = set(x for x in str(f.get("session") or "").split(",") if x)
        if fids and ids and not (fids & ids):
            continue
        if fids and not ids and score < 2:
            continue
        rank = (-score, _SEV.get(f.get("severity"), 9))
        hits.append((rank, f))
    if not hits:
        return None
    hits.sort(key=lambda h: h[0])
    pattern = hits[0][1].get("pattern")
    found = set(finding_key(f, digest) for _r, f in hits if f.get("pattern") == pattern)
    if len(found) == 1:
        return found.pop()
    return pattern, pattern, None                 # several projects: the habit, not one project


# ---------- the feature an item names ----------

_FEATURES = [
    ("plan mode", r"plan mode"),
    ("parallel agents", r"parallel agents?|agents? in parallel|subagents?|explore agent"),
    ("Workflow", r"\bworkflows?\b"),
    ("skill", r"\bskills?\b|slash command"),
    ("hook", r"\bhooks?\b"),
    ("CLAUDE.md rule", r"claude\.md"),
    ("permission rule", r"allow rule|deny rule|permissions\.allow|permission rule"),
    ("/compact", r"/compact"),
    ("/rewind", r"/rewind|\brewind\b"),
    ("new session with a handoff", r"hand-?off|new session|start fresh"),
    ("scheduled task", r"scheduled task"),
    ("memory", r"\bmemory\b"),
    ("connector", r"\bconnectors?\b"),
    ("plugin", r"\bplugins?\b"),
]


def feature_named(text):
    """The Claude Code feature a Do instead line names first, or None."""
    low = (text or "").lower()
    best = None
    for name, rx in _FEATURES:
        m = re.search(rx, low)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), name)
    return best[1] if best else None


# ---------- the number behind a pattern ----------

def _t(name, default):
    v = _T.get(name)
    return v if isinstance(v, (int, float)) else default


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _repeated_fails(s):
    rep = list(((s.get("errors") or {}).get("repeated")) or [])
    rep += list(((s.get("subagents") or {}).get("repeated_fails")) or [])
    return sum(_num(r.get("fails")) for r in rep
               if isinstance(r, dict) and _num(r.get("fails")) >= _t("fail_repeats", 3))


def _error_kinds(s):
    kinds = (s.get("errors") or {}).get("kinds") or {}
    return sum(_num(n) for k, n in kinds.items()
               if _num(n) >= _t("error_kind_repeats", 5) and not str(k).startswith("Exit code"))


def _compactions(s):
    return _num(s.get("compactions_total")) or len(s.get("compactions") or [])


def _denials(s):
    return sum(_num(v) for k, v in (s.get("denials") or {}).items() if k != "top")


def _bloated(s):
    return 1 if _num((s.get("tokens") or {}).get("peak_context")) >= _t("peak_context", 500000) else 0


def _big_tokens(s):
    return int(_num((s.get("big_results") or {}).get("chars")) / 4)


# pattern: (plain name, what it is divided by, per-session value or None, totals key or None)
_SESSION_METRICS = {
    "repeated_failing_command": ("repeated failed calls", "hour", _repeated_fails, None),
    "repeated_error_kind": ("repeats of the same tool error", "hour", _error_kinds, None),
    "correction_loop": ("corrections", "prompts",
                        lambda s: _num(s.get("corrections")), "corrections"),
    "giant_tool_result": ("tokens from large tool results", "session", _big_tokens, None),
    "multiple_compactions": ("compactions", "session", _compactions, "compactions"),
    "frequent_interruptions": ("interruptions", "prompts",
                               lambda s: _num(s.get("interruptions")), "interruptions"),
    "context_bloat": ("sessions above %dk context" % (_t("peak_context", 500000) // 1000),
                      "session", _bloated, None),
    "permission_denials": ("blocked tool calls", "hour", _denials, None),
    "high_error_rate": ("failed tool calls", "calls",
                        lambda s: _num((s.get("errors") or {}).get("total")), "errors"),
}
# Counted from the findings: one per session (or per period) the detector flagged.
_FINDING_METRICS = {
    "serial_agents": ("sessions with no parallel agents", "session"),
    "sprawling_session": ("sprawling sessions", "session"),
    "idle_then_resume": ("returns to a large session after a long break", "session"),
    "unused_installed": ("unused installed tools flagged", "period"),
    "unused_feature": ("unused features flagged", "period"),
}
_PER_WORDS = {"hour": "active hour", "session": "session", "prompts": "100 prompts",
              "calls": "100 tool calls", "period": "period"}


# These detectors need several days or sessions to fire, so a week cannot be
# rebuilt from one-day digests for them.
_WEEK_ONLY = ("unused_installed", "unused_feature", "repeated_prompt", "repeated_long_paste",
              "sprawling_session")


def _digest_metric(pattern, digest):
    if pattern == "repeated_prompt":
        v = sum(_num(g.get("count")) for g in digest.get("repeated_prompts") or []
                if _num(g.get("session_count")) >= _t("repeat_prompt_sessions", 3)
                and _num(g.get("avg_len")) < _t("long_paste_chars", 1500))
        return "repeats of the same typed prompt", "session", v
    if pattern == "repeated_long_paste":
        v = sum(_num(g.get("count")) for g in digest.get("repeated_prompts") or []
                if _num(g.get("avg_len")) >= _t("long_paste_chars", 1500)
                and _num(g.get("count")) >= 2)
        return "repeats of the same long paste", "session", v
    if pattern == "permission_prompts":
        v = sum(_num(p.get("n")) for p in digest.get("permission_prompts") or []
                if _num(p.get("n")) >= _t("permission_prompts_same", 3))
        return "repeated permission prompts", "hour", v
    return None


def _rate(value, per, denom):
    if per == "period":
        return float(value)
    if not denom:
        return None
    if per in ("prompts", "calls"):
        return value * 100.0 / denom
    return value / float(denom)


def _metric(pattern, name, per, value, sessions, hours, turns, calls):
    denom = {"hour": hours, "session": sessions, "prompts": turns, "calls": calls,
             "period": 1}[per]
    denom = round(denom, 2) if isinstance(denom, float) else denom
    rate = _rate(value, per, denom)
    return {"pattern": pattern, "name": name, "per": _PER_WORDS[per], "value": value,
            "denom": denom, "rate": None if rate is None else round(rate, 4),
            "sessions": sessions}


def measure(key, digest, findings=None):
    """The number behind a key for one period, normalised, or None if there is none.

    {"pattern", "name", "per", "value", "denom", "rate", "sessions"}: value is
    the raw count, denom what it is divided by (active hours, sessions, prompts
    or tool calls in the period), rate the normalised figure that is compared.
    A key with a project counts only that project's sessions.
    """
    pattern, project = split_key(key)
    if not pattern or not isinstance(digest, dict):
        return None
    full = [s for s in digest.get("sessions") or [] if isinstance(s, dict)]
    minor = [s for s in digest.get("minor_sessions") or [] if isinstance(s, dict)]
    totals = digest.get("totals") or {}
    if project:
        full = [s for s in full if project_slug(s.get("project")) == project]
        minor = [s for s in minor if project_slug(s.get("project")) == project]
        totals = {}
    sessions = _num(totals.get("sessions")) or len(full) + len(minor)
    hours = (_num(totals.get("active_min")) or sum(_num(s.get("active_min")) for s in full)) / 60.0
    turns = _num(totals.get("turns")) or sum(_num(s.get("turns")) for s in full + minor)
    calls = _num(totals.get("tool_calls")) or sum(_num(s.get("tool_calls")) for s in full + minor)

    # A large digest keeps only a short line for its smallest sessions (least
    # output, fewest prompts). A count that needs session detail cannot see
    # them, so the result says how many were left out ("partial"). The divisor
    # stays the whole period: dividing by the detailed sessions alone would
    # make the rate move with the size of the digest.
    partial = 0
    if pattern in _SESSION_METRICS:
        name, per, fn, tkey = _SESSION_METRICS[pattern]
        if tkey and tkey in totals:
            value = _num(totals.get(tkey))
        else:
            value = sum(fn(s) for s in full)
            partial = len(minor)
    elif pattern in _FINDING_METRICS:
        partial = len(minor)
        name, per = _FINDING_METRICS[pattern]
        if findings is None and _signals is not None:
            try:
                findings = _signals.run(digest)
            except Exception:
                findings = None
        if findings is None:
            return None
        ids = set(str(s.get("id") or "")[:8] for s in full)
        value = 0
        for f in findings:
            if not isinstance(f, dict) or f.get("pattern") != pattern:
                continue
            if project and not (set(str(f.get("session") or "").split(",")) & ids):
                continue
            value += 1
    else:
        got = _digest_metric(pattern, digest)
        if not got:
            return None
        name, per, value = got
    out = _metric(pattern, name, per, value, sessions, hours, turns, calls)
    if partial:
        out["partial"] = partial
    return out


def combine(metrics):
    """Several periods' numbers for one key added into one (for a week of daily digests)."""
    ms = [m for m in metrics if isinstance(m, dict)]
    if not ms:
        return None
    out = dict(ms[0])
    out["value"] = sum(_num(m.get("value")) for m in ms)
    out.pop("partial", None)
    partial = sum(_num(m.get("partial")) for m in ms)
    if partial:
        out["partial"] = partial
    out["sessions"] = sum(_num(m.get("sessions")) for m in ms)
    per = dict((v, k) for k, v in _PER_WORDS.items()).get(out.get("per"), "period")
    denom = 1 if per == "period" else sum(_num(m.get("denom")) for m in ms)
    out["denom"] = round(denom, 2) if isinstance(denom, float) else denom
    rate = _rate(out["value"], per, denom)
    if per == "period":
        rate = out["value"] / float(len(ms))
    out["rate"] = None if rate is None else round(rate, 4)
    return out


def compare(prev, cur, tolerance=TOLERANCE):
    """better, same, worse, not seen again, cannot measure or no data.

    no data: the current period has no sessions to judge by. cannot measure:
    there is no number for one of the two periods. Rates are compared, raw
    counts only when a rate cannot be formed.
    """
    if not isinstance(cur, dict) or not isinstance(prev, dict):
        return "cannot measure"
    if not _num(cur.get("sessions")):
        return "no data"
    if not _num(cur.get("value")):
        return "not seen again" if _num(prev.get("value")) else "same"
    a, b = prev.get("rate"), cur.get("rate")
    if a is None or b is None:
        a, b = _num(prev.get("value")), _num(cur.get("value"))
    if not a:
        return "worse"
    ratio = b / float(a)
    if ratio <= 1 - tolerance:
        return "better"
    if ratio >= 1 + tolerance:
        return "worse"
    return "same"


# ---------- cost of the sessions an item names ----------

def _cost_of(d):
    for k in ("cost_usd", "cost"):
        v = (d or {}).get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
    return None


def session_numbers(ids, digest):
    """Tokens, minutes, failures (and cost when the digest has it) for the named sessions."""
    out = {"out_tokens": 0, "peak_context": 0, "minutes": 0, "failures": 0}
    cost = None
    hit = 0
    for s in (digest or {}).get("sessions") or []:
        if not isinstance(s, dict) or str(s.get("id") or "")[:8] not in ids:
            continue
        hit += 1
        tok = s.get("tokens") or {}
        sub = ((s.get("subagents") or {}).get("tokens")) or {}
        out["out_tokens"] += _num(tok.get("out")) + _num(sub.get("out"))
        out["peak_context"] = max(out["peak_context"], _num(tok.get("peak_context")))
        out["minutes"] += _num(s.get("active_min"))
        out["failures"] += _num((s.get("errors") or {}).get("total"))
        c = _cost_of(s)
        if c is None:
            c = _cost_of(tok)
        if c is not None:
            cost = (cost or 0) + c
    if not hit:
        return None
    if cost is not None:
        out["cost"] = round(cost, 2)
    return out


# ---------- classification ----------

def when(mode, label):
    """Where a report sits in time: (last day covered, 1 for a weekly). A weekly
    for the days up to D is written after the daily for D, and before the daily
    for the day it is dated."""
    return (period(mode, label)[1], 1 if mode == "weekly" else 0)


def _earlier(e, before):
    """Was this logged item given before the report at `before` (a date or a when())?"""
    if isinstance(before, tuple):
        return (str(e.get("last_day") or e["date"]), 1 if e.get("mode") == "weekly" else 0) < before
    return e["date"] < before


def history(key, log, before):
    """What the log holds for a key in reports given before `before`."""
    seen = [e for e in log if same_advice(e.get("key"), key) and _earlier(e, before)
            and e.get("kind") != "proposal"]
    reports = sorted(set((e["date"], str(e.get("report"))) for e in seen))
    return {"times": len(reports), "since": reports[0][0] if reports else None,
            "last": reports[-1][0] if reports else None}


def classify(key, log, date):
    """NEW, or RECURRING with the day first given and how many times it will have been given."""
    h = history(key, log, date)
    if not h["times"]:
        return {"status": "NEW", "since": None, "times": 1, "last": None, "marker": "",
                "trend": "new"}
    times = h["times"] + 1
    return {"status": "RECURRING", "since": h["since"], "times": times, "last": h["last"],
            "marker": "(repeat of %s, given %d times)" % (h["since"], times),
            "trend": "repeat since %s, given %d times" % (h["since"], times)}


def similar_key(text, log, before, floor=SIMILAR):
    """The key of the earlier advice closest in wording to this text, or None."""
    best = (floor, None)
    for e in log:
        if not _earlier(e, before) or e.get("kind") == "proposal":
            continue
        s = similarity(text, e.get("do") or "")
        if s >= best[0]:
            best = (s, e["key"])
    return best[1]


def text_key(text, log, date):
    """Key for written advice: an earlier item in the same words, else its own slug."""
    return similar_key(text, log, date) or advice_slug(text)


def applied_dates(log=None):
    """{key: sorted list of dates} from applied.jsonl. Tolerant of extra or missing fields."""
    out = {}
    for rec in _read_jsonl(applied_file()):
        if rec.get("action") and not rec.get("advice_key"):
            continue                      # applyfix.py's own line about a file change
        key = rec.get("advice_key") or rec.get("key")
        if not key and rec.get("report") and rec.get("proposal"):
            key = key_for_proposal(rec["report"], rec["proposal"], log)
        date = str(rec.get("date") or rec.get("applied") or rec.get("ts") or "")[:10]
        if not isinstance(key, str) or not DATE_RE.match(date):
            continue
        out.setdefault(key, [])
        if date not in out[key]:
            out[key].append(date)
    for v in out.values():
        v.sort()
    return out


def annotate(findings, digest=None, date=None, texts=(), log=None, mode="daily"):
    """Say for each candidate whether it is NEW or RECURRING, before the report is written.

    findings: the list signals.py wrote. texts: Do instead lines for items Claude
    adds that no detector found. One row per key, in the order first seen.
    """
    log = shown_log() if log is None else log
    date = date or datetime.date.today().isoformat()
    if DATE_RE.match(date) or RANGE_RE.match(date):
        date = when(mode, date)
    applied = applied_dates(log)
    rows = []
    by_key = {}
    for f in findings or []:
        if not isinstance(f, dict) or not f.get("pattern"):
            continue
        key, pattern, project = finding_key(f, digest)
        row = by_key.get(key)
        if row is None:
            row = dict(classify(key, log, date), key=key, pattern=pattern, project=project,
                       sessions=[], findings=0, applied=(applied.get(key) or [None])[-1])
            by_key[key] = row
            rows.append(row)
        row["findings"] += 1
        for sid in str(f.get("session") or "").split(","):
            if sid and sid not in row["sessions"]:
                row["sessions"].append(sid)
    for text in texts or ():
        key = text_key(text, log, date)
        if key in by_key:
            continue
        row = dict(classify(key, log, date), key=key, pattern=None, project=None,
                   sessions=[], findings=0, applied=(applied.get(key) or [None])[-1],
                   text=C.one_line(text, 160))
        by_key[key] = row
        rows.append(row)
    return rows


# ---------- recording ----------

def _entry_identity(e):
    return (str(e.get("report")), e.get("n"))


def record_report(path, keys=None, write=True):
    """Log the items of a report file. Running it again for the same file changes nothing.

    keys: {item number: key} for items whose key is already known (from
    annotate). Other items are tied to a finding by their text, then to earlier
    advice in the same words, then get a key made from their own words.
    Returns the entries written for this report.
    """
    meta = report_meta(path)
    with open(path) as fh:
        parsed = parse_report(fh.read(), meta["mode"])
    digest = _read_json(meta["digest"])
    if not isinstance(digest, dict):
        digest = None
    findings = _read_json(meta["findings"])
    if not isinstance(findings, list):
        findings = None
    if write:
        with _Lock():
            return _record(meta, parsed, digest, findings, keys, True)
    return _record(meta, parsed, digest, findings, keys, False)


def _record(meta, parsed, digest, findings, keys, write):
    everything = load_log()
    log = [e for e in everything if str(e.get("report")) != meta["report"]]
    # a key given on an earlier run for the same item still holds on a re-run
    kept = dict((_n(e), e) for e in everything if str(e.get("report")) == meta["report"]
                and e.get("key_source") == "given")
    keys = dict((int(k), v) for k, v in (keys or {}).items())
    here = when(meta["mode"], meta["label"])
    entries = []
    linked = set()
    for it in parsed["items"]:
        pattern = project = None
        given = keys.get(it["n"])
        old = kept.get(it["n"])
        if not given and old and old.get("do") == it["do"]:
            given = old["key"]
        if given and KEY_RE.match(given):
            key, source = given, "given"
            pattern, project = split_key(key)
        else:
            hit = infer_pattern(it, findings, digest)
            if hit:
                key, pattern, project = hit
                source = "finding"
            else:
                key = None
                if it["marker"]:
                    pool = [e for e in log if e["date"] == it["marker"]["since"]]
                    key = similar_key(it["do"], pool, here, floor=0.3)
                    source = "marker"
                if not key:
                    key = similar_key(it["do"], log, here)
                    source = "similar"
                if not key:
                    key, source = advice_slug(it["do"] or it["what"]), "slug"
                if not key.startswith("advice:"):
                    pattern, project = split_key(key)
        cls = classify(key, log, here)
        metric = measure(key, digest, findings)
        entry = {"key": key, "key_source": source, "kind": "item", "pattern": pattern,
                 "project": project, "date": meta["date"], "report": meta["report"],
                 "mode": meta["mode"], "first_day": meta["first_day"],
                 "last_day": meta["last_day"], "n": it["n"], "do": it["do"],
                 "feature": feature_named(it["do"]), "sessions": it["ids"],
                 "given": {"status": cls["status"], "since": cls["since"], "times": cls["times"]},
                 "marker": it["marker"], "metric": metric,
                 "numbers": session_numbers(it["ids"], digest)}
        if it["proposals"]:
            entry["proposals"] = it["proposals"]
            linked.update(it["proposals"])
        entries.append(entry)
    for p in parsed["proposals"]:
        if p["id"] in linked:
            continue
        entries.append({"key": "proposal:" + (_slug(p["name"])[:60] or p["id"].lower()),
                        "key_source": "slug", "kind": "proposal", "pattern": None,
                        "project": None, "date": meta["date"], "report": meta["report"],
                        "mode": meta["mode"], "first_day": meta["first_day"],
                        "last_day": meta["last_day"], "n": 100 + int(p["id"][1:]),
                        "do": p["name"], "feature": feature_named(p["name"]), "sessions": [],
                        "given": {"status": "NEW", "since": None, "times": 1}, "marker": None,
                        "metric": None, "numbers": None, "proposals": [p["id"]]})
    if write:
        _save_log(log + entries)
    return entries


def report_files(root=None):
    """Every daily, weekly and range report under the reports folder, in the order given."""
    root = root or C.reports_dir()
    out = []
    for folder in ("daily", "weekly", "ranges"):
        try:
            names = os.listdir(os.path.join(root, folder))
        except OSError:
            continue
        for name in names:
            path = os.path.join(root, folder, name)
            try:
                meta = report_meta(path)
            except ValueError:
                continue
            out.append((when(meta["mode"], meta["label"]), meta["report"], path))
    return [p for _w, _r, p in sorted(out)]


def backfill(root=None):
    """Seed the log from the report files that exist. Safe to run again."""
    done = []
    for path in report_files(root):
        entries = record_report(path)
        done.append({"report": report_meta(path)["report"], "items": len(entries)})
    return {"reports": len(done), "items": sum(d["items"] for d in done), "recorded": done,
            "log": advice_file()}


def key_for_proposal(report, proposal, log=None):
    """The advice key behind a weekly proposal, for example ("weekly/2026-11-17.md", "P1")."""
    log = load_log() if log is None else log
    report = str(report).replace(os.sep, "/")
    rel = "/".join(report.split("/")[-2:])
    proposal = str(proposal).upper()
    hits = [e for e in log if str(e.get("report")) == rel and proposal in (e.get("proposals") or [])]
    hits.sort(key=lambda e: (e.get("kind") == "proposal", _n(e)))
    return hits[0]["key"] if hits else None


def record_applied(key, date=None, note="", source="manual"):
    """Append one line to applied.jsonl: this advice was acted on that day."""
    if not isinstance(key, str) or not KEY_RE.match(key):
        raise ValueError("not an advice key: %r" % (key,))
    date = date or datetime.date.today().isoformat()
    if not DATE_RE.match(date):
        raise ValueError("not a date (want YYYY-MM-DD): %r" % (date,))
    _day(date)
    rec = {"key": key, "date": date, "source": source}
    if note:
        rec["note"] = C.one_line(note, 200)
    path = applied_file()
    if not os.path.isdir(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path))
    with open(path, "a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    return rec


# ---------- did it move ----------

def _fmt(v):
    if v is None:
        return "?"
    v = float(v)
    if abs(v) >= 10000:
        return "%dk" % int(round(v / 1000.0))
    if v == int(v):
        return "%d" % int(v)
    if abs(v) >= 10:
        return "%.1f" % v
    return ("%.2f" % v).rstrip("0").rstrip(".")


def _raw(m):
    if m["per"] == "period":
        return _fmt(m["value"])
    unit = {"active hour": "active hours", "session": "sessions", "100 prompts": "prompts",
            "100 tool calls": "tool calls"}[m["per"]]
    return "%s in %s %s" % (_fmt(m["value"]), _fmt(m["denom"]), unit)


def _stored(stem, day=None):
    """A digest and its findings from disk, without work config.json says to leave out."""
    d = _read_json(stem + ".json")
    f = _read_json(stem + ".findings.json")
    try:
        import scope as SC
        return SC.stored(d, f, day)
    except ImportError:
        return d, f


def period_metric(key, root, first, last, entries=()):
    """The number for a key over first..last: the weekly digest for exactly those
    days, else that period's daily digests added up, else what the log kept."""
    digests = os.path.join(root, "digests")
    stem = os.path.join(digests, "weekly-%s" % _shift(last, 1))
    if (_day(last) - _day(first)).days == 6:
        d, f = _stored(stem)
        if isinstance(d, dict):
            m = measure(key, d, f if isinstance(f, list) else None)
            if m:
                return dict(m, source="weekly digest")
    parts = []
    day = first
    if split_key(key)[0] in _WEEK_ONLY and first != last:
        day = _shift(last, 1)                     # skip: not rebuildable from single days
    while day <= last:
        stem = os.path.join(digests, "daily-%s" % day)
        d, f = _stored(stem, day)
        if isinstance(d, dict):
            parts.append(measure(key, d, f if isinstance(f, list) else None))
        day = _shift(day, 1)
    m = combine(parts)
    if m:
        return dict(m, source="daily digests")
    kept = {}
    for e in entries:
        if e.get("key") == key and isinstance(e.get("metric"), dict):
            kept[str(e.get("report"))] = e                    # exact key: same scope
    weekly = [e for e in kept.values() if e.get("mode") == "weekly"]
    m = combine([e["metric"] for e in (weekly[-1:] or kept.values())])
    return dict(m, source="advice log") if m else None


_ORDER = {"worse": 0, "recurred": 1, "same": 2, "better": 3, "not seen again": 4,
          "cannot measure": 5, "no data": 6}


def _line(it):
    do = C.one_line(it["do"], 90)
    given = "given %s" % it["given"] if it["times"] < 2 else "given %d times since %s" % (
        it["times"], it["since"])
    if it["again"]:
        given += ", again on %s" % it["again"]
    head = it["status"]
    if it["applied"]:
        head += ", applied on %s" % it["applied"]
    prev, cur = it["previous"], it["current"]
    if it["status"] in ("better", "same", "worse", "not seen again") and prev and cur:
        what = "%s per %s" % (prev["name"], prev["per"])
        if prev["per"] == "period":
            what = prev["name"]
        if it["status"] == "same":
            verb = "stayed at %s then %s" % (_fmt(prev["rate"]), _fmt(cur["rate"]))
        else:
            verb = "%s from %s to %s" % ("moved" if it["applied"] else "went",
                                         _fmt(prev["rate"]), _fmt(cur["rate"]))
        tail = "%s %s (%s, then %s)." % (what, verb, _raw(prev), _raw(cur))
        left = _num(prev.get("partial")) + _num(cur.get("partial"))
        if left:
            tail += " %d small sessions are not in the digest detail and are not counted." % left
        if it["applied"]:
            tail = "then " + tail
    elif it["status"] == "no data":
        tail = "no sessions this period to judge it by."
    elif it["status"] == "recurred" and it["pattern"]:
        tail = "no digest for one of the two periods, and it was given again."
    elif it["status"] == "recurred":
        tail = "no number tracks this advice, and it was given again."
    elif it["pattern"]:
        tail = "no digest for one of the two periods."
    else:
        tail = "no number tracks this advice, and it was not given again."
    return "- %s: \"%s\" (%s). %s" % (head, do, given, tail[:1].upper() + tail[1:])


def followthrough(label=None, mode="weekly", root=None, today=None, extra_keys=(),
                  limit=MAX_LINES, log=None):
    """Did the advice from the previous period move? The weekly "Follow-through" block.

    The period is the one the report `label` covers; the previous period is the
    same number of days just before it. Returns a dict with every item, the
    lines to write (at most `limit`, worst first) and the block as text.
    extra_keys: keys of the items about to be given again in this report.
    """
    root = root or C.reports_dir()
    today = today or datetime.date.today()
    label = label or today.isoformat()
    first, last = period(mode, label)
    span = (_day(last) - _day(first)).days + 1
    p_first, p_last = _shift(first, -span), _shift(first, -1)
    log = shown_log() if log is None else log
    stem = os.path.join(root, "digests", "%s-%s" % (mode, label))
    digest, findings = _stored(stem)
    if not isinstance(findings, list):
        findings = None
    applied = applied_dates(log)

    prev_entries = [e for e in log if p_first <= str(e.get("last_day")) <= p_last]
    keys = []
    for e in prev_entries:
        if e.get("kind") != "proposal" and e["key"] not in keys:
            keys.append(e["key"])
    # one line per habit: drop a per-project key when the bare pattern is also there
    keys = [k for k in keys if not ("@" in k and split_key(k)[0] in keys)]
    for key, dates in sorted(applied.items()):
        given = [e["date"] for e in log if e["key"] == key and e["date"] <= label
                 and e.get("kind") != "proposal"]
        if given and not any(same_advice(k, key) for k in keys) and any(
                max(p_first, _shift(min(given), -1)) <= d <= last for d in dates):
            keys.append(key)

    items = []
    for key in keys:
        mine = [e for e in log if same_advice(e["key"], key) and e["date"] <= label]
        if not mine:
            continue
        shown = [e for e in mine if str(e.get("last_day")) <= p_last] or mine
        latest = sorted(shown, key=lambda e: (e["date"], e.get("mode") == "weekly"))[-1]
        pattern, project = split_key(key)
        reports = sorted(set((e["date"], str(e.get("report"))) for e in shown
                             if e.get("kind") != "proposal")) or [(latest["date"], "")]
        again = sorted(e["date"] for e in mine
                       if first <= str(e.get("last_day")) <= last and e.get("kind") != "proposal")
        if any(same_advice(k, key) for k in extra_keys) and not again:
            again = [label]
        done = sorted(d for k, ds in applied.items() if same_advice(k, key)
                      for d in ds if _shift(reports[0][0], -1) <= d <= last)
        prev = period_metric(key, root, p_first, p_last, prev_entries) if pattern else None
        cur = measure(key, digest, findings) if pattern else None
        status = compare(prev, cur)
        if status == "cannot measure" and again:
            status = "recurred"
        change = None
        if prev and cur and prev.get("rate") and cur.get("rate") is not None:
            change = round(cur["rate"] / float(prev["rate"]) - 1, 3)
        it = {"key": key, "pattern": pattern, "project": latest.get("project") or project,
              "do": latest.get("do") or "", "feature": latest.get("feature"),
              "given": reports[-1][0], "since": reports[0][0], "times": len(reports),
              "again": again[-1] if again else None, "applied": done[-1] if done else None,
              "status": status, "previous": prev, "current": cur, "change": change}
        it["line"] = _line(it)
        items.append(it)
    items.sort(key=lambda it: (_ORDER.get(it["status"], 9), -(it["change"] or 0), it["key"]))
    lines = [it["line"] for it in items[:max(0, limit)]]
    return {"mode": mode, "label": label,
            "period": {"first_day": first, "last_day": last, "days": span,
                       "digest": bool(digest)},
            "previous": {"first_day": p_first, "last_day": p_last},
            "items": items, "omitted": max(0, len(items) - len(lines)), "lines": lines,
            "block": (HEADING + "\n" + "\n".join(lines)) if lines else ""}


# ---------- for the page ----------

def page_data(root=None, today=None, log=None):
    """Plain data for the coach page: one row per piece of advice, plus the
    latest weekly follow-through. JSON-serialisable, text as written."""
    root = root or C.reports_dir()
    log = shown_log() if log is None else log
    applied = applied_dates(log)
    rows = {}
    for e in log:
        if e.get("kind") == "proposal":
            continue
        r = rows.setdefault(e["key"], {"key": e["key"], "pattern": e.get("pattern"),
                                       "project": e.get("project"), "first": e["date"],
                                       "reports": set()})
        r["reports"].add((e["date"], str(e.get("report"))))
        r["last"] = e["date"]
        r["do"] = C.one_line(e.get("do") or "", 300)
        r["feature"] = e.get("feature")
    advice = []
    for r in rows.values():
        r["times"] = len(r.pop("reports"))
        r["status"] = "RECURRING" if r["times"] > 1 else "NEW"
        r["applied"] = (applied.get(r["key"]) or [None])[-1]
        advice.append(r)
    advice.sort(key=lambda r: (-r["times"], r["first"], r["key"]))
    weekly = None
    try:
        names = sorted(n[7:-5] for n in os.listdir(os.path.join(root, "digests"))
                       if n.startswith("weekly-") and n.endswith(".json")
                       and not n.endswith(".findings.json") and DATE_RE.match(n[7:-5]))
    except OSError:
        names = []
    if names:
        ft = followthrough(names[-1], "weekly", root, today, log=log)
        weekly = {"label": ft["label"], "period": ft["period"], "previous": ft["previous"],
                  "omitted": ft["omitted"], "lines": ft["lines"],
                  "items": [dict((k, it[k]) for k in (
                      "key", "do", "feature", "given", "since", "times", "again", "applied",
                      "status", "previous", "current", "change")) for it in ft["items"]]}
    return {"advice": advice, "followthrough": weekly}


def _escape(obj):
    if isinstance(obj, str):
        return html.escape(obj, quote=True)
    if isinstance(obj, dict):
        return dict((k, _escape(v)) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return [_escape(v) for v in obj]
    return obj


def page_block(root=None, today=None):
    """page_data() with every string HTML-escaped, safe to place in markup as is."""
    return _escape(page_data(root, today))


# ---------- command line ----------

def _keys_arg(pairs):
    out = {}
    for pair in pairs or []:
        n, _, key = pair.partition("=")
        if not n.isdigit() or not KEY_RE.match(key):
            raise ValueError("want N=KEY, for example 1=context_bloat: %r" % pair)
        out[int(n)] = key
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Track whether the coach's advice was taken.")
    sub = ap.add_subparsers(dest="cmd")

    a = sub.add_parser("annotate", help="NEW or RECURRING for each candidate, before writing")
    a.add_argument("--findings", required=True, help="the findings file named in the plan")
    a.add_argument("--digest", help="the digest (default: beside the findings file)")
    a.add_argument("--date", help="the report's date (default: read from the file name)")
    a.add_argument("--mode", choices=("daily", "weekly", "range"),
                   help="default: read from the file name, else daily")
    a.add_argument("--text", action="append", default=[],
                   help="a Do instead line for an item no detector found (repeatable)")

    r = sub.add_parser("record", help="log the items of a report that was just written")
    r.add_argument("--report", required=True, help="the report file")
    r.add_argument("--key", action="append", default=[], metavar="N=KEY",
                   help="the key annotate gave for item N (repeatable)")

    w = sub.add_parser("weekly", help="the Follow-through block for the weekly review")
    w.add_argument("--label", help="the weekly report's date (default: today)")
    w.add_argument("--mode", choices=("weekly", "daily", "range"), default="weekly")
    w.add_argument("--also", action="append", default=[], metavar="KEY",
                   help="a key this report is about to give again (repeatable)")
    w.add_argument("--format", choices=("json", "text"), default="json")

    p = sub.add_parser("applied", help="note that a piece of advice was acted on")
    p.add_argument("--key")
    p.add_argument("--report", help="with --proposal: the weekly report it came from")
    p.add_argument("--proposal", help="for example P1")
    p.add_argument("--date", help="YYYY-MM-DD (default: today)")
    p.add_argument("--note", default="")

    sub.add_parser("backfill", help="seed the log from the reports that exist")
    sub.add_parser("status", help="every piece of advice in the log")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "annotate":
            findings = _read_json(args.findings)
            if not isinstance(findings, list):
                raise ValueError("cannot read findings: %s" % args.findings)
            digest_path = args.digest or re.sub(r"\.findings\.json$", ".json", args.findings)
            m = re.search(r"(daily|weekly|range)-([0-9_to-]+)\.findings\.json$", args.findings)
            date = args.date or (m.group(2) if m else None)
            digest = _read_json(digest_path)
            out = annotate(findings, digest if isinstance(digest, dict) else None, date,
                           args.text, mode=args.mode or (m.group(1) if m else "daily"))
        elif args.cmd == "record":
            entries = record_report(args.report, _keys_arg(args.key))
            out = {"report": report_meta(args.report)["report"], "log": advice_file(),
                   "items": [dict((k, e.get(k)) for k in (
                       "n", "key", "key_source", "given", "feature", "metric"))
                       for e in entries]}
        elif args.cmd == "weekly":
            out = followthrough(args.label, args.mode, extra_keys=tuple(args.also))
            if args.format == "text":
                sys.stdout.write(out["block"] + ("\n" if out["block"] else ""))
                return 0
        elif args.cmd == "applied":
            key = args.key
            if not key and args.report and args.proposal:
                key = key_for_proposal(args.report, args.proposal)
                if not key:
                    raise ValueError("no logged advice for %s in %s (record the report first)"
                                     % (args.proposal, args.report))
            if not key:
                raise ValueError("give --key, or --report with --proposal")
            out = record_applied(key, args.date, args.note)
        elif args.cmd == "backfill":
            out = backfill()
        elif args.cmd == "status":
            out = page_data()
        else:
            ap.print_help()
            return 2
    except (ValueError, OSError, TypeError, AttributeError, KeyError) as e:
        sys.stderr.write("coach followthrough: %s\n" % (e or type(e).__name__))
        return 2
    sys.stdout.write(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(C.run_main(main))
