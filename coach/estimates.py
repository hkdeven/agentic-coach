#!/usr/bin/env python3
"""Claude's time estimates against the time the work took, by model.

Claude states an estimate in a reply before a piece of work ("Estimate: 15
minutes", "Estimate 25 min.") and reports it when the work is done ("estimate
15 min, actual 11", "Estimate 20, actual 6."). Both lines are in the session
transcripts, and every assistant message there carries the model that wrote it.

The rules:
- A piece of work is counted only when Claude stated its actual time. The
  report line carries both figures, so they are taken from it.
- A report is paired with the latest open estimate of the same number of
  minutes earlier in the same transcript. The piece then belongs to the model
  that made the estimate. A report with no such estimate stands alone and
  belongs to the model that wrote it.
- The same report written again within an hour in the same transcript ("merged:
  estimate 15, actual 11", then "live: estimate 15, actual 11") is counted once.
- An estimate never followed by a report has no actual and is not in the
  totals; it is counted apart as "no actual stated". The time from the estimate
  to some later message is not used as a stand-in: the transcript does not mark
  where a piece of work ends, and that gap takes in waiting for checks or for
  the person, other tasks in the same session, and nights the session sat open.
- Overrun is (actual - estimated) / estimated over the model's totals, so a
  positive figure is over the estimate and a negative one under it.
- Hours are turned into minutes; an actual with no unit is in minutes. A
  range ("20 to 30 minutes") and a mixed figure ("1h 20m") are not read whole.
- Only main-session transcripts are read; a subagent's own estimates are not
  the ones the person was given.

An event is a tuple (timestamp, model, estimated minutes, actual minutes or
None): None marks an estimate, a number a done report. Python 3 stdlib only.
"""
import collections
import os
import re
import sys

try:
    from . import common as C
    from . import pricing as P
except ImportError:  # run as a script
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import common as C
    import pricing as P

REPEAT_SEC = 3600           # the same report again within this long is one piece
MAX_MINUTES = 100000        # a figure above this (about 70 days) is not a time estimate
_DATED_RE = re.compile(r"^(.+?)[-@]\d{8}$")

_NUM = r"(?:about\s+|around\s+|roughly\s+|~\s*)?(\d+(?:\.\d+)?)"
_UNIT = r"(minutes?|mins?|m|hours?|hrs?|h)\b"
# "estimate 15 min, actual 11", "Estimate 20 minutes, actual about 95", "estimate 45, actual 47"
# and "Estimate 15 min (actual 11)", "Estimate 20 min. Actual 35 min."
DONE_RE = re.compile(r"\bestimate\s*[:=]?\s*" + _NUM + r"\s*(?:" + _UNIT + r")?\s*[,;(.]\s*(?:and\s+)?"
                     r"actual\b\s*[:=]?\s*" + _NUM + r"\s*(?:" + _UNIT + r")?", re.I)
# "Estimate: 15 minutes", "Estimate 25 min.", "estimate 40 minutes"
START_RE = re.compile(r"\bestimate\s*[:=]?\s*" + _NUM + r"\s*" + _UNIT + r"(?!\s*(?:to|-)\s*\d)", re.I)


def _minutes(value, unit):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if unit and unit.lower().startswith("h"):
        v *= 60
    return v if 0 < v <= MAX_MINUTES else None


def find(text):
    """[(estimated minutes, actual minutes or None)] in the order they appear in text."""
    if not isinstance(text, str) or "stimate" not in text and "STIMATE" not in text:
        return []
    text = text.replace("**", "").replace("__", "")
    hits = []
    taken = []
    for m in DONE_RE.finditer(text):
        est = _minutes(m.group(1), m.group(2))
        # an actual with no unit is in minutes, as Claude writes it ("estimate 2 hours, actual 95")
        act = _minutes(m.group(3), m.group(4))
        taken.append((m.start(), m.end()))
        if est is not None and act is not None:
            hits.append((m.start(), est, act))
    for m in START_RE.finditer(text):
        if any(a <= m.start() < b for a, b in taken):
            continue
        est = _minutes(m.group(1), m.group(2))
        if est is not None:
            hits.append((m.start(), est, None))
    hits.sort(key=lambda x: x[0])
    return [(est, act) for _pos, est, act in hits]


def model_id(model):
    """A transcript's model id without a date suffix; None for Claude Code's own notes."""
    if not isinstance(model, str) or not model or model == "<synthetic>":
        return None
    m = _DATED_RE.match(model)
    return m.group(1) if m else model


def events(text, ts, model):
    """The events of one assistant text block."""
    mid = model_id(model)
    if mid is None:
        return []
    return [(ts, mid, est, act) for est, act in find(text)]


def pair(evs):
    """(pieces, open estimates) of one transcript's events.

    A piece: {"t" (the report's time), "model", "est", "act", "paired"}.
    An open estimate: {"t", "model", "est"}."""
    evs = sorted((e for e in evs if isinstance(e, (list, tuple)) and len(e) == 4),
                 key=lambda e: str(e[0]))
    opened = []
    pieces = []
    last_report = {}            # (est, act) -> epoch of the last report counted
    for ts, model, est, act in evs:
        if act is None:
            opened.append({"t": ts, "model": model, "est": est})
            continue
        ep = C.ts_epoch(ts)
        seen = last_report.get((est, act))
        if seen is not None and ep is not None and 0 <= ep - seen <= REPEAT_SEC:
            last_report[(est, act)] = ep
            continue                        # the same report written again
        if ep is not None:
            last_report[(est, act)] = ep
        match = None
        for i in range(len(opened) - 1, -1, -1):
            if abs(opened[i]["est"] - est) < 0.01:
                match = opened.pop(i)
                break
        pieces.append({"t": ts, "model": match["model"] if match else model,
                       "est": est, "act": act, "paired": match is not None})
    return pieces, opened


def overrun_pct(est, act):
    """Whole percent over (positive) or under (negative) the estimate; None with no estimate."""
    if not est:
        return None
    return int(round(100.0 * (act - est) / est))


def _fig(v):
    v = round(float(v), 1)
    return int(v) if v == int(v) else v


def rows(pieces, opened=(), prices=None):
    """One row per model, sorted by model id:
    {"model", "name", "pieces", "est_min", "act_min", "overrun_pct", "no_actual"}."""
    by = collections.OrderedDict()

    def row(model):
        if model not in by:
            by[model] = {"model": model, "name": P.display_name(model, prices), "pieces": 0,
                         "est_min": 0.0, "act_min": 0.0, "no_actual": 0}
        return by[model]
    for p in pieces:
        r = row(p["model"])
        r["pieces"] += 1
        r["est_min"] += p["est"]
        r["act_min"] += p["act"]
    for o in opened:
        row(o["model"])["no_actual"] += 1
    out = []
    for model in sorted(by):
        r = by[model]
        r["overrun_pct"] = overrun_pct(r["est_min"], r["act_min"]) if r["pieces"] else None
        r["est_min"], r["act_min"] = _fig(r["est_min"]), _fig(r["act_min"])
        out.append(r)
    return out


def summary(per_transcript, prices=None):
    """The digest's `estimates` block from each transcript's events, or None when there are none."""
    pieces, opened = [], []
    for evs in per_transcript:
        p, o = pair(evs or [])
        pieces.extend(p)
        opened.extend(o)
    return block(pieces, opened, prices)


def block(pieces, opened, prices=None):
    """The totals of these pieces and open estimates, or None when there are none."""
    if not pieces and not opened:
        return None
    est = sum(p["est"] for p in pieces)
    act = sum(p["act"] for p in pieces)
    return {"rows": rows(pieces, opened, prices), "pieces": len(pieces),
            "est_min": _fig(est), "act_min": _fig(act),
            "overrun_pct": overrun_pct(est, act) if pieces else None,
            "no_actual": len(opened),
            "meaning": "Claude's stated estimates against the actual time it reported, by the "
                       "model that made the estimate; estimates with no stated actual are not in "
                       "the totals"}


def _overrun_text(pct):
    if pct is None:
        return ""
    if pct == 0:
        return "on estimate"
    return "%d%% %s" % (abs(pct), "over" if pct > 0 else "under")


def _count(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def line(block):
    """The report's `Estimates:` line, or None when there is nothing to say."""
    if not isinstance(block, dict) or not block.get("rows"):
        return None
    parts = []
    for r in block["rows"]:
        if not r["pieces"]:
            continue
        parts.append("%s: %s, %s min estimated, %s min actual, %s" % (
            r["name"], _count(r["pieces"], "piece"), r["est_min"], r["act_min"],
            _overrun_text(r["overrun_pct"])))
    text = "Estimates: " + ("; ".join(parts) + "." if parts else "no piece of work with a stated actual.")
    if block.get("no_actual"):
        text += " Not counted: %s with no stated actual." % _count(block["no_actual"], "estimate")
    return text
