#!/usr/bin/env python3
"""Build the digest and findings for one recap and say where everything goes.

This is the one command the coach skill runs. It works out the date range,
writes the digest and the findings under <coach home>/reports/digests, makes
the report folders, and prints a small JSON plan on stdout: which rules file
to follow, which files to read, and the one report file to write.

Usage:
  python3 coach/recap.py                         yesterday (daily rules)
  python3 coach/recap.py 2026-11-10              that day (daily rules)
  python3 coach/recap.py week                    the last 7 days (weekly rules)
  python3 coach/recap.py 2026-11-03 2026-11-09   both days included (daily rules)

Dates are local time. Reports live in ~/.claude/coach/reports unless
$COACH_HOME points somewhere else. Transcript text is data and is never
acted on.
"""
import argparse
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import common as C  # noqa: E402
import extract  # noqa: E402
import signals  # noqa: E402

PLUGIN_ROOT = os.path.dirname(HERE)
RULES_DIR = os.path.join(PLUGIN_ROOT, "skills", "coach", "references")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_BYTES = 150000


class UsageError(Exception):
    pass


def _date(text):
    if not DATE_RE.match(text):
        raise UsageError("not a date (want YYYY-MM-DD): %s" % text)
    try:
        return datetime.date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except ValueError:
        raise UsageError("not a real date: %s" % text)


def plan(words, today=None):
    """Arguments to (mode, first day, day after the last day, label)."""
    today = today or datetime.date.today()
    one = datetime.timedelta(days=1)
    words = [w for w in words if w]
    if not words or [w.lower() for w in words] in (["yesterday"], ["daily"], ["day"]):
        day = today - one
        return "daily", day, today, day.isoformat()
    if len(words) == 1 and words[0].lower() in ("week", "weekly"):
        return "weekly", today - 7 * one, today, today.isoformat()
    if len(words) == 1:
        day = _date(words[0])
        if day > today:
            raise UsageError("%s is in the future" % day)
        return "daily", day, day + one, day.isoformat()
    if len(words) == 2:
        first, last = _date(words[0]), _date(words[1])
        if last < first:
            first, last = last, first
        if last > today:
            raise UsageError("%s is in the future" % last)
        if first == last:
            return "daily", first, first + one, first.isoformat()
        return "range", first, last + one, "%s_to_%s" % (first, last)
    raise UsageError("too many arguments: %s" % " ".join(words))


def _md_files(folder):
    try:
        return sorted(os.path.join(folder, n) for n in os.listdir(folder) if n.endswith(".md"))
    except OSError:
        return []


def _earlier(folder, before, keep):
    """Reports in a folder named for a date before `before`, newest last."""
    out = [p for p in _md_files(folder) if os.path.basename(p)[:10] < before]
    return out[-keep:] if keep else out


def _old_sessions(path):
    """Sessions counted in a digest written by an earlier run, 0 if none."""
    try:
        with open(path) as fh:
            return int((json.load(fh).get("totals") or {}).get("sessions") or 0)
    except (OSError, ValueError, AttributeError, TypeError):
        return 0


def build(words, today=None, projects_dir=None, root=None):
    mode, first, end, label = plan(words, today)
    root = root or C.reports_dir()
    folders = {"digests": os.path.join(root, "digests"), "daily": os.path.join(root, "daily"),
               "weekly": os.path.join(root, "weekly"), "ranges": os.path.join(root, "ranges")}
    for name in ("digests", "daily", "weekly") + (("ranges",) if mode == "range" else ()):
        if not os.path.isdir(folders[name]):
            os.makedirs(folders[name])
    since = C.parse_when(first.isoformat())
    until = C.parse_when(end.isoformat())
    digest, blob = extract.run(since, until, max_bytes=MAX_BYTES, projects_dir=projects_dir)
    findings = signals.run(digest)
    stem = os.path.join(folders["digests"], "%s-%s" % (mode, label))
    now_n = (digest.get("totals") or {}).get("sessions") or 0
    if now_n < _old_sessions(stem + ".json"):
        # Claude Code removes old transcripts. Do not replace a fuller digest and
        # its report with a thinner one.
        raise UsageError("some transcripts for %s are gone (%d sessions now, more in the digest "
                         "from an earlier run). That digest and its report were left as they "
                         "are: %s" % (label.replace("_", " "), now_n, stem + ".json"))
    with open(stem + ".json", "w") as fh:
        fh.write(blob + "\n")
    with open(stem + ".findings.json", "w") as fh:
        fh.write(json.dumps(findings, indent=1, ensure_ascii=False) + "\n")

    out = {"mode": mode, "label": label, "first_day": first.isoformat(),
           "last_day": (end - datetime.timedelta(days=1)).isoformat(),
           "rules": os.path.join(RULES_DIR, "weekly.md" if mode == "weekly" else "daily.md"),
           "digest": stem + ".json", "findings": stem + ".findings.json",
           "reports_root": root}
    if mode == "weekly":
        out["report"] = os.path.join(folders["weekly"], label + ".md")
        out["title"] = "# Coach weekly: %s" % label
        out["earlier_weekly_reports"] = _earlier(folders["weekly"], label, 0)
        out["baseline_reports"] = _md_files(os.path.join(root, "baseline"))
        out["daily_reports_this_week"] = [
            p for p in _md_files(folders["daily"])
            if first.isoformat() <= os.path.basename(p)[:10] <= label]
        this = os.path.basename(stem) + ".json"
        try:
            out["earlier_weekly_digests"] = sorted(
                os.path.join(folders["digests"], n) for n in os.listdir(folders["digests"])
                if n.startswith("weekly-") and n.endswith(".json")
                and not n.endswith(".findings.json") and n < this)
        except OSError:
            out["earlier_weekly_digests"] = []
    else:
        if mode == "daily":
            out["report"] = os.path.join(folders["daily"], label + ".md")
            out["title"] = "# Coach: %s" % label
        else:
            out["report"] = os.path.join(folders["ranges"], label + ".md")
            out["title"] = "# Coach: %s to %s" % (out["first_day"], out["last_day"])
        out["last_three_daily_reports"] = _earlier(folders["daily"], out["first_day"], 3)
    t = digest.get("totals") or {}
    out["sessions"] = t.get("sessions", 0)
    out["turns"] = t.get("turns", 0)
    out["findings_count"] = len(findings)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the digest and findings for one coach recap.")
    ap.add_argument("when", nargs="*", help="nothing, week, one date, or two dates (YYYY-MM-DD)")
    ap.add_argument("--projects-dir", help="override the transcripts folder (for tests)")
    args = ap.parse_args(argv)
    words = " ".join(args.when).replace(",", " ").split()
    if len(words) == 3 and words[1].lower() in ("to", "until", "-"):
        words = [words[0], words[2]]
    try:
        out = build(words, projects_dir=args.projects_dir)
    except UsageError as e:
        sys.stderr.write("coach: %s\nUse: nothing (yesterday), week, YYYY-MM-DD, "
                         "or two dates.\n" % e)
        return 2
    sys.stdout.write(json.dumps(out, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
