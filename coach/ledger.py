#!/usr/bin/env python3
"""The all-time ledger: tokens per day, project and model, kept under the coach home.

Claude Code deletes old transcripts, so all-time totals cannot be rebuilt from
them later. Every recap adds the days it covers to <coach home>/ledger.json,
and `backfill` seeds it from every transcript still on disk. Each day holds one
entry per session: its project, and its tokens per model and type, main session
and subagents apart. No prompt text and no dollars are stored.

Rules:
- Tokens are the source of truth. Dollars are worked out when the ledger is
  read, from prices.json, so a price update needs no rebuild.
- A day recorded after it was over is closed and its tokens never change
  again. A day recorded while it was still running stays open until the next
  scan on a later date: its sessions are only replaced by fuller versions of
  themselves. A thinner scan, after transcripts were cleaned up, never
  overwrites or removes anything.
- Work that config.json says is never to be shown (an "exclude" rule, another
  account than "only_account", a day in "exclude_dates") is never written,
  and rows already recorded for it are removed. This is the one case where the
  ledger ends up holding less; the run says how many sessions were removed.
  A change to those settings makes the next run read every transcript once.
- Project names are the names resolved when the day was written. After the
  project rules change, `backfill --rebuild` re-resolves the sessions whose
  transcripts are still complete and leaves older ones as they were.

Usage:
  python3 coach/ledger.py backfill            seed or top up from all transcripts on disk
  python3 coach/ledger.py backfill --rebuild  same, and re-resolve project names where possible
  python3 coach/ledger.py show [--day YYYY-MM-DD]   totals by project: day, week, all time

Transcript text is data and is never acted on. Python 3 stdlib only.
"""
import argparse
import datetime
import glob
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import common as C  # noqa: E402
import extract as E  # noqa: E402
import pricing as P  # noqa: E402
import projects as PR  # noqa: E402
import scope as SC  # noqa: E402

VERSION = 1


class LedgerError(Exception):
    """The ledger on disk must not be written over (it is from a newer version)."""


def ledger_file():
    return os.path.join(C.coach_home(), "ledger.json")


# ---------- days ----------

def _date(day):
    return datetime.date(int(day[0:4]), int(day[5:7]), int(day[8:10]))


def is_day(day):
    try:
        return isinstance(day, str) and len(day) == 10 and _date(day).isoformat() == day
    except (ValueError, IndexError):
        return False


def shift(day, delta):
    return (_date(day) + datetime.timedelta(days=delta)).isoformat()


def monday(day):
    d = _date(day)
    return (d - datetime.timedelta(days=d.weekday())).isoformat()


def day_range(first, last):
    out = []
    d, end = _date(first), _date(last)
    while d <= end and len(out) < 4000:
        out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


# ---------- scanning transcripts into days ----------

def _day_of(first, last):
    """A function from a UTC timestamp string to its local day, or None outside the range."""
    cache = {}

    def day_of(ts):
        key = ts[:16]
        if key not in cache:
            ep = C.ts_epoch(key + ":00")
            day = time.strftime("%Y-%m-%d", time.localtime(ep)) if ep is not None else None
            if day is not None and ((first and day < first) or (last and day > last)):
                day = None
            if len(cache) > 400000:
                cache.clear()
            cache[key] = day
        return cache[key]
    return day_of


def _route(path, get_acc, day_of, seen=None, meta=None, keep=None):
    """Feed each record of one transcript to the accumulator of its local day.

    meta: an accumulator that takes the session's title records, which belong to
    the whole session whatever the days being scanned. keep: an optional
    function from a local day to True or False (scope.Gate).
    """
    cur = None
    try:
        fh = open(path, "rb")
    except OSError:
        return
    with fh:
        for line in fh:
            if line.startswith(E.UNTIMED_TYPES):
                if meta is not None:
                    try:
                        d = json.loads(line)
                        if isinstance(d, dict) and d.get("type") in ("ai-title", "custom-title"):
                            meta.feed({"type": d["type"], "aiTitle": d.get("aiTitle"),
                                       "customTitle": d.get("customTitle")})
                    except (ValueError, AttributeError, TypeError, KeyError):
                        pass
                continue
            i = line.rfind(E.TS_KEY)
            if i < 0:
                continue
            cur = day_of(line[i + 13:i + 32].decode("ascii", "replace"))
            if cur is None or (keep is not None and not keep(cur)):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            k = cur
            ts = d.get("timestamp")
            if isinstance(ts, str):
                k = day_of(ts[:19])
                if k is None or (keep is not None and not keep(k)):
                    continue
            if seen is not None:
                uid = d.get("uuid")
                if uid:
                    if uid in seen:
                        continue
                    seen.add(uid)
            try:
                get_acc(k).feed(d)
            except (AttributeError, TypeError, ValueError, KeyError):
                continue                  # one malformed record must not stop the scan


def scan(projects_dir=None, first=None, last=None, rules=None, session_projects=None,
         exclude=None, info=None, scope=None, other=None):
    """Transcripts on disk to {day: day entry}. first and last are local days, both included.

    scope: what may be shown (scope.Scope); built from config.json when not given.
    info: an optional dict; on return info["excluded"] is the set of session ids
    with nothing left to show, info["seen"] every session id read, and
    info["scope"] the scope used.
    """
    projects_dir = projects_dir or C.PROJECTS_DIR
    naming = E.Naming(rules, session_projects, other)
    scope = SC.from_args(scope, exclude)
    dropped = scope.gone
    seen_ids = set()
    if info is not None:
        info["excluded"] = dropped
        info["seen"] = seen_ids
        info["scope"] = scope
    lo = C.parse_when(first) - 86400 if first else 0
    day_of = _day_of(first, last)
    days = {}
    claimed = {}           # message ids already counted: resumed copies count once
    for path, sid, pdir, _size in E.oldest_first(E.find_sessions(projects_dir, lo)):
        accs, subs = {}, {}

        def get_acc(k, accs=accs, sid=sid, path=path):
            if k not in accs:
                accs[k] = E.SessionAcc(sid, claimed=claimed, owner=path)
            return accs[k]

        def get_sub(k, subs=subs, path=path):
            if k not in subs:
                subs[k] = E.SessionAcc("sub", allow_sidechain=True, claimed=claimed,
                                       owner=path + "/sub")
            return subs[k]

        probe = E.SessionAcc(sid)
        gate = scope.gate(sid, path)
        seen_ids.add(sid)
        _route(path, get_acc, day_of, meta=probe, keep=gate)
        sub_dir = os.path.join(path[:-6], "subagents")
        if os.path.isdir(sub_dir):
            seen = set()
            for p in sorted(glob.glob(os.path.join(sub_dir, "*.jsonl"))):
                try:
                    if os.stat(p).st_mtime < lo:
                        continue
                except OSError:
                    continue
                _route(p, get_sub, day_of, seen, keep=gate)
        if not accs and not subs:
            if first is None and last is None:
                scope.emptied(sid, gate)     # every transcript day was read: nothing is left
            continue
        for a in accs.values():
            a.finish()
        scheduled = any(a.kind == "scheduled" for a in accs.values())
        named = [a for a in accs.values() if a.cwd] or list(accs.values())
        if named:
            probe.cwd = named[0].cwd
        project = naming.name(probe, pdir)
        if scope.hidden(probe, pdir, project):
            continue
        for day in set(accs) | set(subs):
            main = accs[day].by_model if day in accs else {}
            sub = subs[day].by_model if day in subs else {}
            if not main and not sub:
                continue
            models = {}
            for model in set(main) | set(sub):
                models[model] = [P.counts(main.get(model)), P.counts(sub.get(model))]
            days.setdefault(day, {"sessions": {}})["sessions"][sid] = {
                "project": project, "scheduled": bool(scheduled), "models": models}
    today = time.strftime("%Y-%m-%d")
    for day, entry in days.items():
        entry["total"] = day_tokens(entry)
        entry["closed"] = day < today          # scanned after the day was over
    return days


def session_tokens(sess):
    return sum(sum(pair[0]) + sum(pair[1]) for pair in sess["models"].values())


def day_tokens(entry):
    return sum(session_tokens(x) for x in entry["sessions"].values())


# ---------- the file ----------

def _clean(raw):
    """Whatever was on disk to a well-formed ledger. Bad entries are dropped, never raised on."""
    led = {"version": VERSION, "first_day": None, "days": {}}
    if not isinstance(raw, dict) or not isinstance(raw.get("days"), dict):
        return led
    if isinstance(raw.get("exclude_sig"), str):
        led["exclude_sig"] = raw["exclude_sig"]
    if isinstance(raw.get("excluded"), list):
        led["excluded"] = sorted(set(x for x in raw["excluded"] if isinstance(x, str) and x))
    if isinstance(raw.get("filter"), dict):
        led["filter"] = dict((k, v) for k, v in raw["filter"].items()
                             if isinstance(k, str) and isinstance(v, int))
    for day, entry in raw["days"].items():
        if not is_day(day) or not isinstance(entry, dict) or not isinstance(entry.get("sessions"), dict):
            continue
        sessions = {}
        for sid, x in entry["sessions"].items():
            if not (isinstance(sid, str) and isinstance(x, dict) and isinstance(x.get("project"), str)
                    and isinstance(x.get("models"), dict)):
                continue
            models = {}
            for model, pair in x["models"].items():
                if isinstance(model, str) and isinstance(pair, list) and len(pair) == 2:
                    models[model] = [P.counts(pair[0]), P.counts(pair[1])]
            if models:
                sessions[sid] = {"project": x["project"], "scheduled": x.get("scheduled") is True,
                                 "models": models}
        if sessions:
            day_entry = {"sessions": sessions, "closed": entry.get("closed") is True}
            day_entry["total"] = day_tokens(day_entry)
            led["days"][day] = day_entry
    if led["days"]:
        led["first_day"] = min(led["days"])
    return led


def load(path=None):
    """The ledger, or an empty one. Returns (ledger, state).

    state: "" when there is no file, "ok", "damaged" when the file cannot be read
    as a ledger, "newer" when a later version of the plugin wrote it. Only "ok"
    and "" are ever written over.
    """
    path = path or ledger_file()
    try:
        with open(path, "rb") as fh:
            raw = json.loads(fh.read().decode("utf-8", "replace"))
    except FileNotFoundError:
        return _clean(None), ""
    except (OSError, ValueError):
        return _clean(None), "damaged"
    if not isinstance(raw, dict) or not isinstance(raw.get("days"), dict):
        return _clean(None), "damaged"
    version = raw.get("version")
    if isinstance(version, int) and version > VERSION:
        return _clean(raw), "newer"
    led = _clean(raw)
    if raw["days"] and not led["days"]:
        return led, "damaged"          # days on disk, none readable: do not write over them
    return led, "ok"


def _open_for_write(path=None):
    """Load the ledger for a run that will save it. Never loses what is on disk.

    A damaged file is moved aside (ledger.json.damaged-<time>) and the ledger is
    seeded again from the transcripts. A file from a newer version is left alone.
    Returns (ledger, seed) where seed is True when everything on disk must be scanned.
    """
    path = path or ledger_file()
    led, state = load(path)
    if state == "newer":
        raise LedgerError("%s was written by a newer version of the plugin; it was left as it is"
                          % path)
    if state == "damaged":
        os.replace(path, "%s.damaged-%s-%d" % (path, time.strftime("%Y%m%d-%H%M%S"), os.getpid()))
    return led, state != "ok"


def save(led, path=None):
    path = path or ledger_file()
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    led["first_day"] = min(led["days"]) if led["days"] else None
    led["updated"] = time.strftime("%Y-%m-%d %H:%M")
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w") as fh:
            fh.write(json.dumps(led, sort_keys=True, separators=(",", ":")) + "\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def upsert(led, scanned, rebuild=False, restore=()):
    """Merge scanned days in. Nothing recorded is ever replaced by less, or counted twice.

    A day that was recorded after it was over is closed: its tokens never change
    again. That is what keeps the totals right when transcripts are cleaned up
    later, because a resumed copy of a deleted session would otherwise be read as
    new work on that old day.

    A day recorded while it was still running is open: each of its sessions is
    replaced by a fuller version of itself, sessions are added, none is removed.
    It closes the first time it is scanned on a later date.

    restore: ids of sessions that were left out whole until now and may be
    shown again (a rule was removed). Their rows are put back on closed days
    too; they are known sessions, not copies of deleted ones.

    rebuild: for re-resolving project names after the rules changed. A session
    whose scan has exactly the tokens the ledger has takes its new name, on open
    and closed days alike; any other session keeps the name it was written with.

    Returns {"added": [...], "replaced": [...], "kept": [...]} as lists of days;
    kept lists days where the ledger holds more than the transcripts now do.
    """
    out = {"added": [], "replaced": [], "kept": []}
    for day in sorted(scanned):
        new = scanned[day]
        if not is_day(day) or not new.get("sessions"):
            continue
        old = led["days"].get(day)
        if old is None:
            led["days"][day] = new
            out["added"].append(day)
            continue
        changed = held = False
        frozen = old.get("closed") is True
        for sid, sess in new["sessions"].items():
            cur = old["sessions"].get(sid)
            if cur is None:
                if frozen and sid not in restore:
                    held = True
                else:
                    old["sessions"][sid] = sess
                    changed = True
                continue
            now, was = session_tokens(sess), session_tokens(cur)
            if now == was:
                if rebuild and sess != cur:
                    old["sessions"][sid] = sess
                    changed = True
            elif now > was and not frozen:
                old["sessions"][sid] = sess
                changed = True
            else:
                held = True
        if any(sid not in new["sessions"] for sid in old["sessions"]):
            held = True
        if new.get("closed") and not frozen:
            old["closed"] = True
            changed = True
        old["total"] = day_tokens(old)
        if changed:
            out["replaced"].append(day)
        if held:
            out["kept"].append(day)
    led["first_day"] = min(led["days"]) if led["days"] else None
    return out


def purge(led, scope, sids=(), seen=None):
    """Remove every row that may not be shown. Returns the set of session ids that lost a row.

    A row goes when its day is in exclude_dates; when its session id is in sids
    (sessions a scan just found to have nothing left); when the scan dropped
    that session on that day for its account; when an exclude rule names its id
    or fits the project name it was written with (all that is left to test once
    the transcript is gone); or, for a session the scan did not read (seen is
    the set it did read), when the account evidence that needs no transcript
    says another account. This is the one place the ledger gives up what it holds.
    """
    gone = set()
    sids = set(sids)
    if not scope.on and not sids:
        return gone
    gates = {}
    for day in list(led["days"]):
        entry = led["days"][day]
        whole_day = scope.date_hidden(day)
        for sid in list(entry["sessions"]):
            drop = (whole_day or sid in sids or (sid, day) in scope.dropped_days
                    or PR.excluded(scope.rules, sid, [entry["sessions"][sid]["project"]]))
            if not drop and scope.only and seen is not None and sid not in seen:
                if sid not in gates:
                    gates[sid] = scope.gate(sid, None)
                drop = not gates[sid](day)
            if drop:
                del entry["sessions"][sid]
                gone.add(sid)
        if not entry["sessions"]:
            del led["days"][day]
        else:
            entry["total"] = day_tokens(entry)
    led["first_day"] = min(led["days"]) if led["days"] else None
    return gone


def visible(led, scope=None):
    """The ledger as it may be shown: rows of left-out work taken out, in memory only."""
    scope = scope or SC.Scope()
    if scope.on:
        purge(led, scope)
    return led


def _apply_scope(led, scope, info, reset):
    """Purge what may not be shown. Returns how many sessions lost at least one row.

    Call _note_excluded after the scanned days were merged in, to bring the short
    list of left-out sessions up to date.
    """
    found = set((info or {}).get("excluded") or ())
    gone = purge(led, scope, found, (info or {}).get("seen") if reset else None)
    (info if info is not None else {})["purged"] = gone
    led["exclude_sig"] = scope.sig()
    if reset or "filter" not in led:
        led["filter"] = scope.counts() if scope.on else {}
    return len(gone)


def _restorable(led, info):
    """Sessions on the left-out list that this full scan read and did not leave out."""
    was = set(led.get("excluded") or [])
    out_now = set((info or {}).get("excluded") or ())
    return set(sid for sid in (info or {}).get("seen") or ()
               if sid[:PR.MIN_ID] in was and sid not in out_now)


def _note_excluded(led, scope, info, full):
    """Keep the short list (first 8 characters of each id) of sessions that are
    left out whole. It lets the page drop items of old reports that cite one.

    full: every transcript on disk was just read. Then a session that was read
    is on the list only if this scan left it out, so a rule that was removed
    stops hiding it. A session that was not read (its transcript is gone) can
    no longer be tested and stays on the list while any rule is in force:
    staying hidden is the safe side. A session with rows is never on the list.
    """
    if not scope.on:
        led["excluded"] = []
        return
    info = info or {}
    ids = set(led.get("excluded") or [])
    if full:
        ids -= set(s[:PR.MIN_ID] for s in info.get("seen") or ())
    still = set(sid for entry in led["days"].values() for sid in entry["sessions"])
    ids.update(s[:PR.MIN_ID] for s in set(info.get("excluded") or ())
               | (set(info.get("purged") or ()) - still))
    ids -= set(s[:PR.MIN_ID] for s in still)
    led["excluded"] = sorted(ids)


def backfill(projects_dir=None, rules=None, path=None, rebuild=False, exclude=None, scope=None):
    """Walk every transcript still on disk and merge it in. Safe to run again and again."""
    led, _seed = _open_for_write(path)
    scope = SC.from_args(scope, exclude)
    info = {}
    scanned = scan(projects_dir, rules=rules, info=info, scope=scope)
    removed = _apply_scope(led, scope, info, reset=True)
    result = upsert(led, scanned, rebuild, _restorable(led, info))
    _note_excluded(led, scope, info, full=True)
    save(led, path)
    result["first_day"] = led["first_day"]
    result["days"] = len(led["days"])
    result["excluded_removed"] = removed
    result["excluded_sessions"] = len(led["excluded"])
    result["filter"] = led.get("filter") or {}
    return result


def update(first, last, projects_dir=None, rules=None, path=None, exclude=None, scope=None):
    """What a recap calls: add the days it covers. Seeds the whole ledger once if it is missing.

    When the settings for what may be shown changed since the last run, every
    transcript is read once more so that rows already recorded for work that is
    now left out are removed.
    """
    led, seeded = _open_for_write(path)
    scope = SC.from_args(scope, exclude)
    changed = led.get("exclude_sig", "") != scope.sig()
    info = {}
    full = seeded or not led["days"] or changed
    if full:
        scanned = scan(projects_dir, rules=rules, info=info, scope=scope)
    else:
        # from the last day on record, so days a missed run skipped are filled in too
        scanned = scan(projects_dir, min(first, max(led["days"])), last, rules=rules,
                       info=info, scope=scope)
    removed = _apply_scope(led, scope, info, reset=full)
    result = upsert(led, scanned, restore=_restorable(led, info) if full else ())
    _note_excluded(led, scope, info, full=full)
    save(led, path)
    result["seeded"] = seeded
    result["excluded_removed"] = removed
    result["excluded_sessions"] = len(led["excluded"])
    return led, result


# ---------- reading: totals for a period, priced now ----------

def totals(led, first, last, prices=None):
    """Tokens and dollars from first to last (both included), overall and by project, model, type."""
    prices = prices or P.load_prices()
    projects, models = {}, {}
    main_all, sub_all = {}, {}
    days_with_data = 0
    for day, entry in led["days"].items():
        if (first and day < first) or (last and day > last):
            continue
        days_with_data += 1
        for sess in entry["sessions"].values():
            g = projects.setdefault(sess["project"],
                                    {"main": {}, "sub": {}, "sessions": 0, "scheduled": 0})
            g["sessions"] += 1
            if sess["scheduled"]:
                g["scheduled"] += 1
            for model, (main, sub) in sess["models"].items():
                P.add(g["main"].setdefault(model, [0] * P.N), main)
                P.add(g["sub"].setdefault(model, [0] * P.N), sub)
                P.add(main_all.setdefault(model, [0] * P.N), main)
                P.add(sub_all.setdefault(model, [0] * P.N), sub)
                P.add(models.setdefault(model, [0] * P.N), main)
                P.add(models[model], sub)
    cm, cs = P.cost(main_all, prices), P.cost(sub_all, prices)
    tokens = P.add(list(cm["tokens"]), cs["tokens"])
    by_project = []
    for name, g in projects.items():
        pm, ps = P.cost(g["main"], prices), P.cost(g["sub"], prices)
        ptok = P.add(list(pm["tokens"]), ps["tokens"])
        by_project.append({"name": name, "usd": pm["usd"] + ps["usd"], "main_usd": pm["usd"],
                           "sub_usd": ps["usd"], "tokens": sum(ptok), "out": ptok[1],
                           "sessions": g["sessions"], "scheduled": g["scheduled"],
                           "unpriced_tokens": pm["unpriced_tokens"] + ps["unpriced_tokens"]})
    by_project.sort(key=lambda r: (-r["usd"], -r["tokens"], r["name"]))
    by_model = []
    for key, block in models.items():
        c = P.cost({key: block}, prices)
        by_model.append({"model": key, "name": P.display_name(key, prices), "tokens": sum(block),
                         "usd": c["usd"], "priced": not c["unpriced_tokens"]})
    by_model.sort(key=lambda r: (-r["usd"], -r["tokens"], r["model"]))
    by_type_usd = dict((k, cm["by_type"][k] + cs["by_type"][k]) for k in cm["by_type"])
    return {"first": first, "last": last, "days": days_with_data,
            "usd": cm["usd"] + cs["usd"], "main_usd": cm["usd"], "sub_usd": cs["usd"],
            "tokens": sum(tokens), "out": tokens[1],
            "by_type": [{"type": "Input", "tokens": tokens[0], "usd": by_type_usd["input"]},
                        {"type": "Output", "tokens": tokens[1], "usd": by_type_usd["output"]},
                        {"type": "Cache read", "tokens": tokens[2], "usd": by_type_usd["cache_read"]},
                        {"type": "Cache write", "tokens": tokens[3] + tokens[4],
                         "usd": by_type_usd["cache_write"]}],
            "by_project": by_project, "by_model": by_model,
            "unpriced_tokens": cm["unpriced_tokens"] + cs["unpriced_tokens"]}


def periods(led, day, prices=None):
    """The four periods ending on a day: that day, the last 7 days, the calendar week, all time."""
    first = led.get("first_day")
    out = {"day": totals(led, day, day, prices),
           "week7": totals(led, shift(day, -6), day, prices),
           "calweek": totals(led, monday(day), day, prices),
           "all": totals(led, first, None, prices) if first else totals(led, "9999-99-99", None, prices)}
    out["since"] = first
    out["prices_date"] = P.prices_date(prices)
    return out


def daily_series(led, days, prices=None):
    """[{day, usd, main_usd, sub_usd, tokens}] for the given days; None values where no data."""
    out = []
    for day in days:
        if day in led["days"]:
            t = totals(led, day, day, prices)
            out.append({"day": day, "usd": t["usd"], "main_usd": t["main_usd"],
                        "sub_usd": t["sub_usd"], "tokens": t["tokens"]})
        else:
            out.append({"day": day, "usd": None, "main_usd": None, "sub_usd": None, "tokens": None})
    return out


# ---------- the lines reports carry ----------

def _top_projects(rows, n=3):
    return ", ".join("%s %s" % (r["name"], P.fmt_usd(r["usd"])) for r in rows[:n] if r["usd"] > 0)


def totals_line(per, label="day"):
    """The one line a daily report ends with. The page parser reads the `Totals:` label."""
    d = per["day"]
    parts = ["Totals: %s tokens (%s output), about %s API-equivalent."
             % (P.fmt_tok(d["tokens"]), P.fmt_tok(d["out"]), P.fmt_usd(d["usd"]))]
    top = _top_projects(d["by_project"])
    if top:
        parts.append("Top projects: %s." % top)
    if d["unpriced_tokens"]:
        parts.append("Unpriced: %s tokens." % P.fmt_tok(d["unpriced_tokens"]))
    if label == "day":
        parts.append("Week to date: %s." % P.fmt_usd(per["calweek"]["usd"]))
    if per.get("since"):
        parts.append("All time since %s: %s." % (per["since"], P.fmt_usd(per["all"]["usd"])))
    return " ".join(parts)


def totals_items(per, label="day"):
    """The totals of a daily or range report, one entry per total, without the label."""
    d = per["day"]
    out = ["Tokens: %s (%s output), about %s API-equivalent"
           % (P.fmt_tok(d["tokens"]), P.fmt_tok(d["out"]), P.fmt_usd(d["usd"]))]
    top = _top_projects(d["by_project"])
    if top:
        out.append("Top projects: %s" % top)
    if d["unpriced_tokens"]:
        out.append("Unpriced: %s tokens" % P.fmt_tok(d["unpriced_tokens"]))
    if label == "day":
        out.append("Week to date: %s" % P.fmt_usd(per["calweek"]["usd"]))
    if per.get("since"):
        out.append("All time since %s: %s" % (per["since"], P.fmt_usd(per["all"]["usd"])))
    return out


def totals_block(per, label="day"):
    """The `Totals:` block a daily report ends with: the label, then one bullet per total.
    The page parser reads the label and the bullets under it."""
    return "\n".join(["Totals:"] + ["- " + x for x in totals_items(per, label)])


def cost_block(week, alltime, since, limit=8):
    """The `## Cost by project` block of a weekly report: week and all-time columns.

    alltime None (the ledger could not be read): the all-time column shows a hyphen.
    """
    all_by = dict((r["name"], r) for r in alltime["by_project"]) if alltime else {}
    lines = ["## Cost by project",
             "| Project | Week tokens | Week cost | All-time cost |",
             "|---|---|---|---|"]
    rows = week["by_project"]
    for r in rows[:limit]:
        a = all_by.get(r["name"])
        lines.append("| %s | %s | %s | %s |" % (r["name"].replace("|", "/"), P.fmt_tok(r["tokens"]),
                                             P.fmt_usd(r["usd"]), P.fmt_usd(a["usd"]) if a else "-"))
    if len(rows) > limit:
        rest = rows[limit:]
        lines.append("| %d more | %s | %s | |" % (len(rest), P.fmt_tok(sum(r["tokens"] for r in rest)),
                                               P.fmt_usd(sum(r["usd"] for r in rest))))
    lines.append("| Total | %s | %s | %s |" % (P.fmt_tok(week["tokens"]), P.fmt_usd(week["usd"]),
                                             P.fmt_usd(alltime["usd"]) if alltime else "-"))
    note = "API-equivalent at list prices, not a bill."
    if since:
        note = "All time is since %s. %s" % (since, note)
    unpriced = week["unpriced_tokens"]
    if unpriced:
        note += " Unpriced this week: %s tokens." % P.fmt_tok(unpriced)
    lines.append("")
    lines.append(note)
    return "\n".join(lines)


def table_text(t, title):
    lines = ["%s: %s tokens, %s" % (title, P.fmt_tok(t["tokens"]), P.fmt_usd(t["usd"]))]
    for r in t["by_project"]:
        lines.append("  %-40s %10s  %10s  (main %s, subagents %s, %d sessions%s)" % (
            r["name"][:40], P.fmt_tok(r["tokens"]), P.fmt_usd(r["usd"]), P.fmt_usd(r["main_usd"]),
            P.fmt_usd(r["sub_usd"]), r["sessions"],
            ", %d scheduled" % r["scheduled"] if r["scheduled"] else ""))
    if t["unpriced_tokens"]:
        lines.append("  unpriced tokens: %s" % P.fmt_tok(t["unpriced_tokens"]))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="The coach's all-time ledger of tokens and cost.")
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("backfill", help="seed or top up the ledger from the transcripts on disk")
    b.add_argument("--rebuild", action="store_true",
                   help="also re-resolve project names for days whose transcripts are still complete")
    b.add_argument("--projects-dir", help="override the transcripts folder (for tests)")
    s = sub.add_parser("show", help="totals by project for a day, its week and all time")
    s.add_argument("--day", help="YYYY-MM-DD (default: yesterday)")
    args = ap.parse_args(argv)
    if args.cmd == "backfill":
        try:
            r = backfill(args.projects_dir, rebuild=args.rebuild)
        except LedgerError as e:
            sys.stderr.write("coach: %s\n" % e)
            return 2
        sys.stdout.write("ledger: %s\n" % ledger_file())
        sys.stdout.write("days in ledger: %d, since %s\n" % (r["days"], r["first_day"] or "no data"))
        sys.stdout.write("added %d days, refreshed %d; on %d days the ledger holds more than the "
                         "transcripts now do, and what it holds was kept\n"
                         % (len(r["added"]), len(r["replaced"]), len(r["kept"])))
        if r.get("excluded_removed"):
            sys.stdout.write("%d excluded sessions removed\n" % r["excluded_removed"])
        if args.rebuild:
            sys.stdout.write("Project names were re-resolved for every session whose transcript is "
                             "still complete on disk (%d days changed). Sessions older than the "
                             "transcripts, on %d days, keep the names they were written with.\n"
                             % (len(r["replaced"]), len(r["kept"])))
        return 0
    if args.cmd == "show":
        led, existed = load()
        visible(led)
        if not existed or not led["days"]:
            sys.stdout.write("The ledger is empty. Run: python3 coach/ledger.py backfill\n")
            return 0
        day = args.day or (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        if not is_day(day):
            sys.stderr.write("coach: not a date (want YYYY-MM-DD): %s\n" % day)
            return 2
        per = periods(led, day)
        sys.stdout.write("\n\n".join([
            table_text(per["day"], "Day %s" % day),
            table_text(per["week7"], "Last 7 days to %s" % day),
            table_text(per["calweek"], "Calendar week from %s" % monday(day)),
            table_text(per["all"], "All time since %s" % per["since"])]) + "\n")
        sys.stdout.write("\nAPI-equivalent at list prices (price table of %s), not a bill.\n"
                         % per["prices_date"])
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(C.run_main(main))
