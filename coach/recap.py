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
  python3 coach/recap.py --unattended [...]      a scheduled run: fixes may not be applied

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
import ledger as L  # noqa: E402
import pricing as P  # noqa: E402
import projects as PR  # noqa: E402
import scope as SC  # noqa: E402
import signals  # noqa: E402

PLUGIN_ROOT = os.path.dirname(HERE)
RULES_DIR = os.path.join(PLUGIN_ROOT, "skills", "coach", "references")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_BYTES = 150000


def _module(name):
    """One of the optional feature modules, or None. A recap never fails because of one."""
    try:
        return __import__(name)
    except Exception:
        return None


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


def _session_ids(digest):
    """First 8 characters of every session a digest holds."""
    out = set()
    for key in ("sessions", "minor_sessions"):
        for s in (digest.get(key) or []) if isinstance(digest, dict) else []:
            if isinstance(s, dict) and s.get("id"):
                out.add(str(s["id"])[:8])
    return out


def _old_digest(path):
    """The digest an earlier run wrote for the same period, or {}."""
    try:
        with open(path) as fh:
            old = json.load(fh)
        return old if isinstance(old, dict) else {}
    except (OSError, ValueError):
        return {}


def cost_plan(mode, first, end, digest, projects_dir=None):
    """Add the covered days to the ledger and return the cost lines the report carries."""
    last = (end - datetime.timedelta(days=1)).isoformat()
    try:
        led, result = L.update(first.isoformat(), last, projects_dir)
    except (L.LedgerError, OSError) as e:
        # the recap still runs; the lines then carry the range only, without all-time figures
        led, result = L._clean(None), {"error": str(e)}
        L.upsert(led, L.scan(projects_dir, min(first.isoformat(), L.monday(last)), last))
        led["first_day"] = None
    since = led.get("first_day")
    alltime = L.totals(led, since, None) if since else None
    span = L.totals(led, first.isoformat(), last)
    week_to_date = L.totals(led, L.monday(last), last)
    dc = (digest.get("totals") or {}).get("cost") or {}
    cost = {"usd": dc.get("usd", 0), "text": dc.get("text") or P.fmt_usd(0),
            "meaning": "API-equivalent at list prices, not a bill",
            "prices_date": P.prices_date(), "unpriced_tokens": dc.get("unpriced_tokens", 0),
            "all_time_usd": round(alltime["usd"], 2) if alltime else None,
            "all_time_text": P.fmt_usd(alltime["usd"]) if alltime else None,
            "since": since}
    projects = digest.get("projects") or []
    if projects:
        cost["top_project"] = {"name": projects[0]["name"], "usd": projects[0]["usd"],
                               "text": projects[0]["text"]}
    out = {"cost": cost, "ledger": L.ledger_file(),
           "ledger_seeded_now": bool(result.get("seeded"))}
    if result.get("error"):
        out["ledger_error"] = result["error"]
    if mode == "weekly":
        out["cost_block"] = L.cost_block(span, alltime, since)
    else:
        cost["week_to_date_usd"] = round(week_to_date["usd"], 2)
        cost["week_to_date_text"] = P.fmt_usd(week_to_date["usd"])
        per = {"day": span, "calweek": week_to_date, "all": alltime, "since": since}
        out["totals_line"] = L.totals_line(per, "day" if mode == "daily" else "range")
    return out


def _may_read(path, sc):
    """False for a report of a left-out day, or one whose text names left-out work."""
    name = os.path.basename(path)[:10]
    if DATE_RE.match(name) and sc.date_hidden(name):
        return False
    try:
        with open(path, "rb") as fh:
            text = fh.read(400000).decode("utf-8", "replace")
    except OSError:
        return False
    return not PR.text_hidden(sc.rules, text, sc.gone_ids())


def _digest_as_stored(path):
    """True when an older digest holds nothing that is now left out."""
    try:
        with open(path) as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return False
    clean, _f = SC.stored(raw)
    return clean is not None and len(clean.get("sessions") or []) == len(raw.get("sessions") or []) \
        and len(clean.get("minor_sessions") or []) == len(raw.get("minor_sessions") or [])


def feature_plan(mode, label, stem, today, projects_dir, unattended):
    """What the 1.2.0 features add to the plan. Every part is optional and fails soft."""
    out = {"references": dict((n, os.path.join(RULES_DIR, n + ".md")) for n in (
        "followthrough", "evidence", "compare", "configaudit", "applyfix", "statusline")),
        "followthrough": [os.path.join(HERE, "followthrough.py")],
        "evidence": [os.path.join(HERE, "evidence.py"), "candidates", "--digest", stem + ".json"],
        "unattended": bool(unattended),
        "fixes": {"apply_allowed": False}}
    fix = _module("applyfix")
    if fix is not None:
        try:
            out.update(fix.plan_fields(unattended))
            if unattended:
                fix.mark_unattended()
        except Exception:
            out["fixes"] = {"apply_allowed": False}
    if mode == "weekly":
        cmp_mod = _module("compare")
        try:
            out["compare"] = cmp_mod.weekly_file(label, today, projects_dir) if cmp_mod else None
        except Exception:
            out["compare"] = None
        audit = _module("config_audit")
        out["config_audit"] = None
        if audit is not None:
            try:
                kw = {"projects_dir": projects_dir} if projects_dir else {}
                out["config_audit"] = audit.save(audit.audit(**kw))
            except Exception:
                out["config_audit"] = None
    return out


def build(words, today=None, projects_dir=None, root=None, unattended=False):
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
    # Sessions the earlier digest had and this one lacks. Those that config.json
    # now leaves out are not "gone transcripts"; any other one is.
    lost = _session_ids(_old_digest(stem + ".json")) - _session_ids(digest) - extract.last_hidden
    if lost:
        # Claude Code removes old transcripts. Do not replace a fuller digest and
        # its report with a thinner one.
        raise UsageError("some transcripts for %s are gone (%d sessions now, more in the digest "
                         "from an earlier run). That digest and its report were left as they "
                         "are: %s" % (label.replace("_", " "), now_n, stem + ".json"))
    # (a session a date or account rule emptied on other days only is in neither set)
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
    out.update(cost_plan(mode, first, end, digest, projects_dir))
    cfg = C.load_config()
    out["page_file"] = C.site_file()
    out["page_build"] = [os.path.join(HERE, "build_site.py")] + (["--card"] if mode == "daily" else [])
    out["page_url"] = cfg["page_url"]                       # None: do not publish
    # notify for the runs a schedule makes (yesterday, the week), not for a look back
    fresh = mode == "weekly" or (mode == "daily" and end == (today or datetime.date.today()))
    out["notify"] = bool(cfg["notify"]) and fresh
    t = digest.get("totals") or {}
    out["sessions"] = t.get("sessions", 0)
    out["turns"] = t.get("turns", 0)
    out["findings_count"] = len(findings)
    out.update(feature_plan(mode, label, stem, today, projects_dir, unattended))
    sc = SC.Scope(cfg)
    if sc.on:
        # Older reports and digests are handed to the skill to read. One that was
        # written with work in it that is now left out is not handed over.
        for key in ("last_three_daily_reports", "daily_reports_this_week",
                    "earlier_weekly_reports", "baseline_reports"):
            if key in out:
                out[key] = [p for p in out[key] if _may_read(p, sc)]
        if "earlier_weekly_digests" in out:
            out["earlier_weekly_digests"] = [p for p in out["earlier_weekly_digests"]
                                             if _digest_as_stored(p)]
    out["plan_file"] = stem + ".plan.json"
    with open(out["plan_file"], "w") as fh:
        fh.write(json.dumps(out, indent=1) + "\n")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the digest and findings for one coach recap.")
    ap.add_argument("when", nargs="*", help="nothing, week, one date, or two dates (YYYY-MM-DD)")
    ap.add_argument("--projects-dir", help="override the transcripts folder (for tests)")
    ap.add_argument("--unattended", action="store_true",
                    help="a scheduled run with nobody to answer: fixes may not be applied or undone")
    args = ap.parse_args(argv)
    words = " ".join(args.when).replace(",", " ").split()
    if words and words[-1].lower() == "scheduled":       # the word the schedules pass
        words, args.unattended = words[:-1], True
    if len(words) == 3 and words[1].lower() in ("to", "until", "-"):
        words = [words[0], words[2]]
    try:
        out = build(words, projects_dir=args.projects_dir, unattended=args.unattended)
    except UsageError as e:
        sys.stderr.write("coach: %s\nUse: nothing (yesterday), week, YYYY-MM-DD, "
                         "or two dates.\n" % e)
        return 2
    except C.ConfigError as e:
        sys.stderr.write("coach: %s\n" % e)
        return 2
    sys.stdout.write(json.dumps(out, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
