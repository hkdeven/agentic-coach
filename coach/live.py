#!/usr/bin/env python3
"""The live feed: one small JSON file that the Overview tab of the coach page reads.

The schema and field rules are in docs/LIVE-FEED.md. Other tools may read the
file too; it is plain JSON.

  python3 coach/live.py write            write <coach home>/live.json (at most once a minute)
  python3 coach/live.py write --force    write it now, whatever its age
  python3 coach/live.py show             print the feed; only the two cache files are written
  python3 coach/live.py time             cold and warm run times on this machine
  python3 coach/live.py page             build the coach page at <coach home>/site/index.html
                                         with a fresh feed, and print its path

What is in the feed (version 1): for today, the last 7 days, the last 30
days and all time: cost, calls, sessions, tokens, a series for a line graph,
the peak, the first-try edit rate, the cache hit rate, corrections, spend by
activity, spend by project, MCP servers called, figures per model, Claude's
time estimates against the actual time by model (estimates.py), and the
same figures for the period just before (all time has none). Then the plan limits last seen by the
status line, the sessions running now, and up to three tips from the latest
findings.

Rules:
- Nothing leaves this machine. No network call, no login token, no Keychain.
- Work that config.json says is never shown is never in the feed: a session
  under an "exclude" rule, another account than "only_account", a day in
  "exclude_dates". The cache keeps no figures for it either. For a session
  under an exclude rule it keeps no folder and no title, only that the file
  was seen. A change to those settings empties the cache.
  The ledger is used only when it was written under the settings in force now.
  With "only_account" set, a session's account is checked again on every run
  against the coach's own account log, and each transcript that changed is
  read once in full for account evidence, as the ledger does. When that
  evidence changes the answer for a day already read, the session and its
  subagents are read again from the start.
- When config.json cannot be trusted, `write` replaces the feed with an empty
  one (no periods, "problem": "settings") and exits 2, so an older feed never
  stays up showing what a new rule was meant to hide.
- Tokens are counted once per API message. A resumed session repeats its
  parent's messages; they count for the transcript that had them first.
- Dollars are API-equivalent at list prices (pricing.py), not a bill.
- A day that is over and that the ledger holds more of than the transcripts
  now do (old transcripts get cleaned up) takes its tokens, cost, sessions and
  projects from the ledger. Calls, activities, first-try, corrections and MCP
  servers exist only where the transcript still does.
- Calls is the number of assistant API messages. Cache hit is cache read /
  (input + cache read + cache write). First-try is the share of edit turns with no retry
  (activity.py). An activity's share is its cost over the cost of all
  activities in the period.
- First-try, cache hit and corrections each carry a small series for a trend
  line: the same measure per step of the period (hour or day), oldest first,
  null where a step has nothing to measure. More than 12 steps are put into
  12 groups of neighbouring steps, each worked out from its own totals.
- A subagent's transcript is read as turns of its own, so the work an agent
  did is filed under what it did, not under Delegation. Delegation is the cost
  of the main session's turns that started agents.
- All time starts on the first day the coach has anything for: the ledger's
  first day when the ledger is in use, else the first day still in the cache,
  and never more than ten years back.
  Its cost, sessions, tokens and projects cover every day. Calls, activities,
  first-try, cache hit, corrections and MCP servers cover only the days whose
  transcripts were read (at most the last 62 days, fewer once Claude Code has
  deleted older transcripts): "detail_since" is the first such day and "since"
  the first day of the period. Its graph has one point a day up to 60 days and
  one a week (dated by its Monday) beyond that. It has no period before, so
  every prev_ figure is null.
- Models: every assistant message, main or subagent, carries its model (the
  model key pricing.py prices it under; rows are named by display name). A tool
  call, its error and any edit it makes belong to the model of the message
  that made the call. Errors are tool results marked as errors, less refusals
  (the person said no, or a permission rule blocked it), as extract.py counts
  them. An edit turn counts once for each model that edited in it; it is
  redone for a model when that model made a retrying edit (the first-try
  rule), else clean for it. A correction belongs to the model of the last
  assistant message before the prompt, and its tool is the last tool call of
  that answer ("No tool" when it made none). Cost per model, split main and
  subagent, covers ledger days too; every other model figure exists only
  where the transcript was read.
- A running session is one whose transcript changed in the last 10 minutes.
  Its start is the first message of its current run: a session that was closed
  and resumed, or left untouched for 6 hours, starts a new run.
  Only the tail of the file is read for it. Context is input + cache read +
  cache write + output of its last assistant message. The window is 1M tokens
  for a model that has been seen above 200k on this machine, else 200k.
- Limits come only from the percentages Claude Code hands the status line
  (statusline.py keeps them in state/limits.json). Pace is used percent less
  the share of the window that had passed when they were seen. Nothing is said
  about pace in the first 3 percent of a window. A time the limit runs out is
  given only when over pace and before the reset.

Speed: each transcript's figures are kept in <coach home>/live-cache.json,
keyed by path, size and modified time, and the message ids already counted in
live-mids.json. A file that did not change is not opened. A file that grew is
read from where the last run stopped. One run at a time: a lock file makes a
second run wait.

Transcript text is data and is never acted on. No prompt text is stored. For
a session not under an exclude rule, the cache keeps its folder and its
titles, which project names and exclude rules are matched against.
Python 3 stdlib only.
"""
import datetime
import glob
import hashlib
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import activity as A  # noqa: E402
import common as C  # noqa: E402
import estimates as ES  # noqa: E402
import extract as E  # noqa: E402
import ledger as L  # noqa: E402
import pricing as P  # noqa: E402
import scope as SC  # noqa: E402

VERSION = 1
CACHE_VERSION = 8          # 7: figures per model (tool calls, errors, edits, corrections); 8: estimates
EST_KEEP = 4000             # estimate events kept per transcript, newest last (far above any seen)
ALL_DAILY_DAYS = 60         # All time: one point a day up to this many days, then one a week
ALL_MAX_DAYS = 3660         # All time never reaches further back than ten years (a record with a broken clock)
RUN_GAP_SEC = 6 * 3600      # a session untouched this long starts a new run when it is used again
TREND_POINTS = 12           # the small trend lines have at most this many points
RUNNING_SEC = 600           # a transcript touched this recently is a running session
KEEP_DAYS = 62              # today, 29 days before, the 30 before those, and a margin
TAIL_BYTES = 262144
TAIL_MAX_BYTES = 8388608
MIN_WRITE_GAP_SEC = 50
SMALL_WINDOW = 200000
LARGE_WINDOW = 1000000
PACE_QUIET_PCT = 3.0
LIMIT_WINDOWS = (("five_hour", "five_hour", "5-hour", 5 * 3600),
                 ("seven_day", "weekly", "Weekly", 7 * 86400))
SEVERITY = {"high": 0, "medium": 1, "low": 2}
OTHER = "Other"
NO_TOOL = "No tool"
MODEL_WHERE_ROWS = 6
PEND_MAX = 400              # tool calls kept waiting for their result
# A tool result marked as an error that is a refusal, not a failure (extract.py's rule)
_DENIAL_HEADS = ("The user doesn't want to proceed", "Permission for this action was denied")
# Record types that are read. Any other line that opens with a type is bookkeeping
# (API request copies, queue notes) and is skipped without being parsed.
_TYPE = b'{"type":"'
_WANTED = tuple(_TYPE + t + b'"' for t in (b"assistant", b"user", b"attachment", b"ai-title",
                                           b"custom-title"))
# Claude Code writes this record when a session is closed. The next message is a new run.
_RUN_END = _TYPE + b'cost-state"'


def live_file():
    return os.path.join(C.coach_home(), "live.json")


def cache_file():
    return os.path.join(C.coach_home(), "live-cache.json")


def mids_file():
    return os.path.join(C.coach_home(), "live-mids.json")


def limits_file():
    return os.path.join(C.coach_home(), "state", "limits.json")


# ---------- small helpers ----------

def local_iso(epoch):
    """Epoch seconds to local ISO 8601 with offset, to the second."""
    return datetime.datetime.fromtimestamp(int(epoch)).astimezone().isoformat(timespec="seconds")


def _hour_iso(hour_key):
    """'2026-11-23T10' (local) to the ISO time that hour starts."""
    ep = time.mktime((int(hour_key[0:4]), int(hour_key[5:7]), int(hour_key[8:10]),
                      int(hour_key[11:13]), 0, 0, 0, 0, -1))
    return local_iso(ep)


def _day_iso(day):
    return _hour_iso(day + "T00")


def _short_date(day):
    """'2026-11-03' to 'Nov 3'."""
    d = L._date(day)
    return "%s %d" % (d.strftime("%b"), d.day)


def _hour_lookup():
    """A function from a UTC timestamp string to its local hour key, with a cache."""
    cache = {}

    def hour_of(ts):
        key = ts[:16]
        if key not in cache:
            ep = C.ts_epoch(key + ":00")
            if len(cache) > 200000:
                cache.clear()
            cache[key] = time.strftime("%Y-%m-%dT%H", time.localtime(ep)) if ep is not None else None
        return cache[key]
    return hour_of


def _addc(dst, key, counts, sign=1):
    block = dst.get(key)
    if block is None:
        block = dst[key] = [0] * P.N
    for i in range(P.N):
        block[i] += sign * counts[i]
    if not any(block):
        del dst[key]


def _merge(dst, src):
    for key, counts in (src or {}).items():
        _addc(dst, key, P.counts(counts))
    return dst


def _read_json(path):
    try:
        with open(path, "rb") as fh:
            return json.loads(fh.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None


def _write_text(path, text):
    """Atomic: write a temp file beside it, then rename."""
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _write_json(path, data, indent=None):
    _write_text(path, json.dumps(data, indent=indent, separators=None if indent else (",", ":"),
                                 ensure_ascii=False) + "\n")


def _mid_key(mid):
    return mid[-16:]


# ---------- one transcript, read from where the last run stopped ----------

def new_entry(sub=False):
    return {"off": 0, "size": 0, "mtime": 0, "sub": bool(sub),
            "meta": {"cwd": None, "titles": [], "ctitles": [], "first": None, "branch": None,
                     "run": None, "last": None, "ended": False},
            "agg": {"h": {}, "calls": {}, "act": {}, "corr": {}, "edits": {}, "mcp": {}, "mod": {}},
            "open": None, "recent": {}, "pend": {}, "rp": {}, "peak": {}, "turn": 0,
            "ans": None,       # the last answer: [model key, its last tool key or None, turn number]
            "est": []}         # estimates.py events of a main session: [time, model, estimate, actual]


def _new_turn(hour, flags):
    # "me": model key -> "" (it edited, no retry) or the tool key of its first retrying edit
    return {"day": hour[:10], "hour": hour, "flags": flags, "kinds": "",
            "retry": A.new_retry_state(), "tok": {}, "msgs": 0, "me": {}}


def _new_mod():
    """One model's figures for one day: tool calls, errors, edit turns, clean ones,
    redone ones, corrections, and by tool key [errors, redone, corrections]."""
    return {"c": 0, "e": 0, "t": 0, "k": 0, "r": 0, "x": 0, "w": {}}


def _mod_row(agg, day, mkey):
    return agg.setdefault("mod", {}).setdefault(day, {}).setdefault(mkey, _new_mod())


def _where(row, tkey):
    return row["w"].setdefault(tkey, [0, 0, 0])


def _tool_key(name):
    """An MCP tool is kept under its server ("mcp__<server>"), any other tool under its name."""
    if name.startswith("mcp__"):
        server = name[5:].partition("__")[0]
        if server:
            return "mcp__" + server
    return name


def _msg_model(model, usage):
    """The model key a message's tool calls belong to, or None for Claude Code's own notes."""
    if model == "<synthetic>":
        return None
    return P.model_key(model, usage if isinstance(usage, dict) else None)


def _is_denial(d, block):
    """A tool result marked as an error that is a refusal (extract.py's rule)."""
    if d.get("toolDenialKind"):
        return True
    return C.result_head(block.get("content")).startswith(_DENIAL_HEADS)


def _act_row(agg, day, cat):
    return agg["act"].setdefault(day, {}).setdefault(
        cat, {"m": {}, "turns": 0, "edit": 0, "clean": 0})


def close_turn(agg, turn):
    """File a finished turn under its activity. A turn Claude never answered is dropped."""
    if not turn or not turn.get("msgs"):
        return
    cat = A.classify(turn["kinds"], turn["flags"])
    for day, models in turn["tok"].items():
        _merge(_act_row(agg, day, cat)["m"], models)
    row = _act_row(agg, turn["day"], cat)
    row["turns"] += 1
    if turn["retry"]["has_edits"]:
        clean = 0 if turn["retry"]["retries"] else 1
        row["edit"] += 1
        row["clean"] += clean
        pair = agg.setdefault("edits", {}).setdefault(turn["hour"], [0, 0])
        pair[0] += 1                        # edit turns and clean ones, by the hour the turn began
        pair[1] += clean
    for mkey, redo in (turn.get("me") or {}).items():
        row = _mod_row(agg, turn["day"], mkey)  # an edit turn for each model that edited in it
        row["t"] += 1
        if redo:
            row["r"] += 1
            _where(row, redo)[1] += 1
        else:
            row["k"] += 1


def _texts(d):
    """(joined text, images, tool_result blocks) of a user record."""
    m = d.get("message")
    content = m.get("content") if isinstance(m, dict) else None
    texts, images, results = [], 0, []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "tool_result":
                results.append(b)
            elif bt == "text":
                texts.append(b.get("text") or "")
            elif bt == "image":
                images += 1
    return "\n".join(texts).strip(), images, results


def human_prompt(d):
    """The text of a prompt the person typed, or None. The same tests as extract.py."""
    raw, images, _ = _texts(d)
    if not raw and not images:
        return None
    if raw.startswith(C.INTERRUPT_PREFIX):
        return None
    if d.get("isCompactSummary") or d.get("isMeta") or d.get("isVisibleInTranscriptOnly"):
        return None
    if raw.startswith("<command-name>") or raw.startswith("<scheduled-task"):
        return None
    origin = d.get("origin")
    if isinstance(origin, dict):
        if origin.get("kind") != "human":
            return None
    elif raw.startswith(C.NON_HUMAN_PREFIXES):
        return None
    text = C.strip_noise(raw)
    if not text:
        return "[image]" if images else None
    return text


class Reader(object):
    """Folds new records of one transcript into its cache entry."""

    def __init__(self, entry, path, claimed, keep=None, hour_of=None):
        self.e = entry
        self.path = path
        self.claimed = claimed          # message key -> path of the transcript that owns it
        self.keep = keep
        self.hour_of = hour_of or _hour_lookup()
        self.new_mids = []
        self.days = set()               # every local day met, kept or not

    # ----- prompts and turns -----
    def _start_turn(self, hour, text, ep, correction_counts):
        e = self.e
        if correction_counts:
            norm = C.norm_prompt(text[:400])
            key = hashlib.md5(norm[:60].encode("utf-8")).hexdigest()[:12]
            seen = e["rp"].get(key)
            if seen is not None and ep is not None and abs(ep - seen) < 120:
                return                      # the same prompt again within two minutes
            if ep is not None:
                e["rp"][key] = ep
                while len(e["rp"]) > 12:
                    del e["rp"][next(iter(e["rp"]))]
            if C.is_correction(text):
                e["agg"]["corr"][hour] = e["agg"]["corr"].get(hour, 0) + 1
                ans = e.get("ans")
                if ans and ans[0]:              # the model of the answer it corrects
                    row = _mod_row(e["agg"], hour[:10], ans[0])
                    row["x"] += 1
                    _where(row, ans[1] or "")[2] += 1
        close_turn(e["agg"], e["open"])
        e["open"] = _new_turn(hour, A.prompt_flags(text))
        e["turn"] = e.get("turn", 0) + 1

    def _user(self, d, hour, ep):
        e = self.e
        raw, _images, results = _texts(d)
        for b in results:
            hit = e["pend"].pop(b.get("tool_use_id"), None)
            if hit and b.get("is_error"):
                if hit[0]:                      # an MCP server's call
                    row = e["agg"]["mcp"].setdefault(hit[1], {}).setdefault(
                        hit[0], {"c": 0, "e": 0, "t": []})
                    row["e"] += 1
                if len(hit) > 3 and hit[2] and not _is_denial(d, b):
                    row = _mod_row(e["agg"], hit[1], hit[2])
                    row["e"] += 1
                    _where(row, hit[3])[0] += 1
        if e["sub"]:
            if raw and not results and not d.get("isMeta"):
                self._start_turn(hour, C.strip_noise(raw), ep, False)
            return
        text = human_prompt(d)
        if text is not None:
            self._start_turn(hour, text, ep, True)

    def _attachment(self, d, hour, ep):
        a = d.get("attachment")
        if self.e["sub"] or not isinstance(a, dict) or a.get("type") != "queued_command":
            return
        prompt = a.get("prompt")
        if not isinstance(prompt, str):
            return
        origin = a.get("origin") if isinstance(a.get("origin"), dict) else {}
        human = (origin.get("kind") == "human" if origin
                 else not prompt.startswith(C.NON_HUMAN_PREFIXES))
        if human and a.get("commandMode", "prompt") == "prompt":
            text = C.strip_noise(prompt)
            if text:
                self._start_turn(hour, text, ep, True)

    # ----- assistant messages -----
    def _assistant(self, d, hour, day):
        e = self.e
        agg = e["agg"]
        m = d.get("message")
        m = m if isinstance(m, dict) else {}
        mid = m.get("id") if isinstance(m.get("id"), str) and m.get("id") else None
        model = m.get("model") if isinstance(m.get("model"), str) else None
        mk = _msg_model(model, m.get("usage"))
        content = m.get("content")
        uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"] \
            if isinstance(content, list) else []         # odd content must not cost the message its usage
        if mk is not None:
            # The last answer so far, for a correction that may follow: its model and its
            # last tool call. Kept also for a copy counted elsewhere, as the prompt after it counts here.
            ans = e.get("ans")
            if not ans or ans[2] != e.get("turn", 0):
                ans = e["ans"] = [mk, None, e.get("turn", 0)]
            ans[0] = mk
            for b in uses:
                if isinstance(b.get("name"), str):
                    ans[1] = _tool_key(b["name"])
        key = _mid_key(mid) if mid else None
        if key is not None:
            owner = self.claimed.get(key)
            if owner is not None and owner != self.path:
                return                      # a copy of a message another transcript has
        if not e["sub"] and isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    for ev in ES.events(b.get("text"), d.get("timestamp"), model):
                        e.setdefault("est", []).append(list(ev))
            if len(e.get("est") or []) > EST_KEEP:
                del e["est"][:-EST_KEEP]
        if e["open"] is None:
            e["open"] = _new_turn(hour, list(A.NO_FLAGS))
        turn = e["open"]
        u = m.get("usage")
        if isinstance(u, dict) and model != "<synthetic>":
            counts = P.usage_counts(u)
            mkey = P.model_key(model, u)
            fresh = key is None or self.claimed.get(key) is None
            old = e["recent"].get(key) if key is not None else None
            if old is not None and old[3] != e.get("turn", 0):
                # a later block of a message that began before the last prompt: its turn is
                # filed already, so only what the usage grew by is added, to the turn running now
                full = [max(counts[i], old[2][i]) for i in range(P.N)]
                counts = [full[i] - old[2][i] for i in range(P.N)]
                e["recent"][key] = [old[0], old[1], full, old[3]]      # a further block adds only its own growth
                mkey, old = old[1], [old[0], old[1], None, old[3]]
                if sum(counts):
                    turn["msgs"] = turn["msgs"] or 1    # the turn has spend, so it is filed
            elif old is not None:           # a later block of the same message: last usage wins
                _addc(agg["h"].setdefault(old[0], {}), old[1], old[2], -1)
                if not agg["h"].get(old[0]):
                    agg["h"].pop(old[0], None)
                tday = turn["tok"].setdefault(old[0][:10], {})
                _addc(tday, old[1], old[2], -1)
            if fresh or old is not None:
                if sum(counts):
                    _addc(agg["h"].setdefault(hour if old is None else old[0], {}), mkey, counts)
                    _addc(turn["tok"].setdefault((hour if old is None else old[0])[:10], {}),
                          mkey, counts)
                if key is not None and (old is None or old[2] is not None):
                    e["recent"][key] = [hour if old is None else old[0], mkey, counts,
                                        e.get("turn", 0)]
                    while len(e["recent"]) > 64:
                        del e["recent"][next(iter(e["recent"]))]
            if fresh:
                agg["calls"][hour] = agg["calls"].get(hour, 0) + 1
                turn["msgs"] += 1
                if key is not None:
                    self.claimed[key] = self.path
                    self.new_mids.append(key)
                ctx = counts[0] + counts[2] + counts[3] + counts[4]
                if model and ctx > e["peak"].get(model, 0):
                    e["peak"][model] = ctx
        elif turn["msgs"] == 0:
            turn["msgs"] = 1                # answered, even if no usage was written
        names = []
        for b in uses:
            name = b.get("name") if isinstance(b.get("name"), str) else "?"
            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
            names.append(name)
            tkey = _tool_key(name)
            before = turn["retry"]["retries"]
            A.retry_feed(turn["retry"], name, inp.get("file_path") or inp.get("notebook_path"),
                         inp.get("command"), mid or d.get("uuid"))
            if mk is not None:
                _mod_row(agg, day, mk)["c"] += 1
                if name in A.EDIT_TOOLS:
                    me = turn.setdefault("me", {})
                    if turn["retry"]["retries"] > before and not me.get(mk):
                        me[mk] = tkey               # this model's edit was the retry
                    else:
                        me.setdefault(mk, "")
            server = None
            if name.startswith("mcp__"):
                server, _, tool = name[5:].partition("__")
                if server and tool:
                    row = agg["mcp"].setdefault(day, {}).setdefault(
                        server, {"c": 0, "e": 0, "t": []})
                    row["c"] += 1
                    if tool not in row["t"]:
                        row["t"].append(tool)
                else:
                    server = None
            if isinstance(b.get("id"), str) and (server or mk is not None):
                e["pend"][b["id"]] = [server, day, mk, tkey]
                while len(e["pend"]) > PEND_MAX:
                    del e["pend"][next(iter(e["pend"]))]
        if names:
            turn["kinds"] = "".join(sorted(set(turn["kinds"]) | set(A.tool_kinds(names))))

    # ----- the file -----
    def feed(self, d):
        e = self.e
        t = d.get("type")
        meta = e["meta"]
        if t in ("ai-title", "custom-title"):
            v = d.get("aiTitle") if t == "ai-title" else d.get("customTitle")
            lst = meta["titles"] if t == "ai-title" else meta["ctitles"]
            if isinstance(v, str) and v:
                if v in lst:
                    lst.remove(v)
                lst.append(v[:300])         # the latest title last; rules match any of them
                del lst[:-200]
            return
        if t not in ("assistant", "user", "attachment"):
            return
        if d.get("isSidechain") and not e["sub"]:
            return
        ts = d.get("timestamp")
        if not isinstance(ts, str):
            return
        hour = self.hour_of(ts)
        if hour is None:
            return
        day = hour[:10]
        self.days.add(day)
        if self.keep is not None and not self.keep(day):
            return
        if meta["first"] is None:
            meta["first"] = ts[:19]
        # The current run starts at the first message, at the first one after the
        # session was closed, or at the first one after a long silence.
        last, this = C.ts_epoch(meta.get("last")), C.ts_epoch(ts)
        if (meta.get("run") is None or meta.get("ended")
                or (last is not None and this is not None and this - last > RUN_GAP_SEC)):
            meta["run"] = ts[:19]
        meta["ended"] = False
        if meta.get("last") is None or ts[:19] > meta["last"]:
            meta["last"] = ts[:19]
        if meta["cwd"] is None and isinstance(d.get("cwd"), str):
            meta["cwd"] = d["cwd"]
        if isinstance(d.get("gitBranch"), str) and d["gitBranch"]:
            meta["branch"] = d["gitBranch"]
        if t == "assistant":
            self._assistant(d, hour, day)
        elif t == "user":
            self._user(d, hour, C.ts_epoch(ts))
        else:
            self._attachment(d, hour, C.ts_epoch(ts))

    def read(self):
        """Read the file from the entry's offset to its last whole line."""
        e = self.e
        try:
            fh = open(self.path, "rb")
        except OSError:
            return False
        with fh:
            st = os.fstat(fh.fileno())
            fh.seek(e["off"])
            off = e["off"]
            for line in fh:
                if not line.endswith(b"\n"):
                    break                       # a line still being written
                off += len(line)
                if line.startswith(_TYPE) and not line.startswith(_WANTED):
                    if line.startswith(_RUN_END):
                        e["meta"]["ended"] = True   # the session was closed here
                    continue                    # bookkeeping records: never parsed
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue
                try:
                    self.feed(d)
                except (AttributeError, TypeError, ValueError, KeyError, IndexError):
                    continue                    # one malformed record must not stop the run
            e["off"] = off
            e["size"] = st.st_size
            e["mtime"] = st.st_mtime
        return True


def scan_text(lines, sub=False, path="mem"):
    """Fold JSONL lines (an iterable of dicts) into a fresh entry. For tests and tools."""
    entry = new_entry(sub)
    reader = Reader(entry, path, {})
    for d in lines:
        reader.feed(d)
    return entry


# ---------- the cache: one entry per transcript ----------

class _Probe(object):
    """What naming and exclusion look at, taken from a cache entry."""

    def __init__(self, sid, meta):
        self.id = sid
        self.cwd = meta.get("cwd")
        self.titles = list(meta.get("titles") or [])
        self.custom_titles = list(meta.get("ctitles") or [])
        self.custom_title = self.custom_titles[-1] if self.custom_titles else None


def _naming_sig(cfg):
    blob = json.dumps([cfg.get("projects"), cfg.get("session_projects"), cfg.get("other_project")],
                      sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def find_transcripts(projects_dir, lo):
    """[(path, sid, pdir, parent path or None)]: each session oldest first, its subagents after it."""
    mains = E.oldest_first(E.find_sessions(projects_dir, 0))
    out = []
    for path, sid, pdir, _size in mains:
        try:
            recent = os.stat(path).st_mtime >= lo
        except OSError:
            continue
        subs = []
        sub_dir = os.path.join(path[:-6], "subagents")
        if os.path.isdir(sub_dir):
            for p in sorted(glob.glob(os.path.join(sub_dir, "*.jsonl"))):
                try:
                    if os.stat(p).st_mtime >= lo:
                        subs.append(p)
                except OSError:
                    continue
        if recent or subs:
            out.append((path, sid, pdir, None))
            out.extend((p, sid, pdir, path) for p in subs)
    return out


def refresh(projects_dir=None, now=None, cfg=None, scope=None, stats=None):
    """Bring the cache up to date with the transcripts on disk. Returns the cache."""
    projects_dir = projects_dir or C.PROJECTS_DIR
    now = time.time() if now is None else now
    cfg = cfg if cfg is not None else C.load_config()
    scope = scope or SC.Scope(cfg)
    stats = stats if stats is not None else {}
    # Hours are filed in local time when they are read, so another time zone needs a
    # fresh cache. Summer time is not another zone: tzname and the winter offset stay.
    head = {"v": CACHE_VERSION, "sig": scope.sig(), "naming": _naming_sig(cfg),
            "zone": [time.timezone, list(time.tzname)],
            "projects_dir": os.path.abspath(projects_dir)}
    cache = _read_json(cache_file())
    rename = False
    if (not isinstance(cache, dict) or not isinstance(cache.get("files"), dict)
            or any(cache.get(k) != head[k] for k in ("v", "sig", "zone", "projects_dir"))):
        cache = dict(head, files={}, tips=None)
        mids = {}
        try:
            os.remove(mids_file())
        except OSError:
            pass
    else:
        mids = None                         # read only if some transcript has to be opened
        rename = cache.get("naming") != head["naming"]
        cache["naming"] = head["naming"]
    files = cache["files"]
    naming = E.Naming(cfg=cfg)
    hour_of = _hour_lookup()
    lo = now - KEEP_DAYS * 86400
    claimed = None
    dirty = mids_dirty = False
    seen = set()
    gates = {}

    def gate_for(sid, main_path):
        if main_path not in gates:              # one read of the transcript for account evidence
            gates[main_path] = scope.gate(sid, main_path)
        return gates[main_path]

    def judge(path, entry, sid, pdir):
        """Name a session's project and say whether an exclude rule hides it."""
        probe = _Probe(sid, entry["meta"])
        project = naming.name(probe, pdir)
        hidden = bool(entry.get("hidden")) or bool(scope.hidden(probe, pdir, project))
        changed = entry.get("project") != project or bool(entry.get("hidden")) != hidden
        entry["project"], entry["hidden"] = project, hidden
        if hidden:                              # nothing is kept for work that is never shown
            blank = new_entry(False)
            blank.update({k: entry[k] for k in ("off", "size", "mtime", "sid", "pdir", "parent")})
            if "gd" in entry:
                blank["gd"] = entry["gd"]
            blank.update({"project": "", "hidden": True})
            files[path] = blank
        return changed
    stats.update({"files": 0, "read": 0, "bytes": 0})
    for path, sid, pdir, parent in find_transcripts(projects_dir, lo):
        seen.add(path)
        stats["files"] += 1
        try:
            st = os.stat(path)
        except OSError:
            continue
        entry = files.get(path)
        if parent is not None:
            pe = files.get(parent)
            if pe is None or pe.get("hidden"):
                if entry is None or not entry.get("skip"):
                    files[path] = {"skip": True, "sub": True, "size": st.st_size,
                                   "mtime": st.st_mtime, "sid": sid, "parent": parent}
                    dirty = True
                continue
        changed = (entry is None or entry.get("skip") or entry.get("size") != st.st_size
                   or entry.get("mtime") != st.st_mtime)
        if changed:
            if mids is None:
                mids = _read_json(mids_file())
                mids = mids if isinstance(mids, dict) else {}
            if claimed is None:
                claimed = {}
                for p, keys in mids.items():
                    for k in keys:
                        claimed.setdefault(k, p)
            if entry is not None and entry.get("off", 0) > st.st_size:
                entry = None                    # the file was rewritten: start it again
                for k in mids.pop(path, []):
                    if claimed.get(k) == path:
                        del claimed[k]
                mids_dirty = True
            if entry is None or entry.get("skip") or "agg" not in entry:
                entry = new_entry(parent is not None)
                files[path] = entry
            entry["sid"], entry["pdir"], entry["parent"] = sid, pdir, parent
            before = entry["off"]
            gate = gate_for(sid, parent or path)
            # (a hidden session is read too, and its figures thrown away below: its message
            # ids must be known, or a resumed copy under another name would count them)
            reader = Reader(entry, path, claimed, gate, hour_of)
            if not reader.read():
                continue
            if gate is not None:
                # "gd" on a session's entry: what the gate said for each day met, in its
                # own transcript and in its subagents'. Account evidence can arrive after
                # the first records were read. When it changes the answer for any day,
                # everything read under the old answer is thrown away and read again:
                # the session now, its subagents as the loop reaches them.
                owner = entry if parent is None else files.get(parent)
                told = (owner or {}).get("gd") or {}
                now_says = dict((d, bool(gate(d))) for d in set(told) | reader.days)
                if parent is None and any(told[d] != now_says[d] for d in told):
                    for k in reader.new_mids:           # claimed a moment ago by the read thrown away
                        if claimed.get(k) == path:
                            del claimed[k]
                    for p in [path] + [q for q, x in files.items() if x.get("parent") == path]:
                        del files[p]
                        for k in mids.pop(p, []):
                            if claimed.get(k) == p:
                                del claimed[k]
                    mids_dirty = True
                    entry = files[path] = new_entry(False)
                    entry["sid"], entry["pdir"], entry["parent"] = sid, pdir, parent
                    before = 0
                    reader = Reader(entry, path, claimed, gate, hour_of)
                    if not reader.read():
                        continue
                    now_says = dict((d, bool(gate(d))) for d in reader.days)
                if owner is not None:
                    (entry if parent is None else owner)["gd"] = now_says
            stats["read"] += 1
            stats["bytes"] += max(0, entry["off"] - before)
            if reader.new_mids:
                mids.setdefault(path, []).extend(reader.new_mids)
                mids_dirty = True
            dirty = True
        if parent is None and (changed or rename or "project" not in entry):
            if judge(path, entry, sid, pdir):
                dirty = True
    for path in list(files):
        e = files[path]
        if (rename and path not in seen and not e.get("sub") and not e.get("skip")
                and "meta" in e and e.get("mtime", 0) >= lo):
            if judge(path, e, e.get("sid") or "", e.get("pdir") or ""):
                dirty = True                    # a transcript that is gone still takes the new name
        if path not in seen and files[path].get("mtime", 0) < lo:
            del files[path]                     # too old to matter; a deleted recent one is kept
            if mids is None:
                mids = _read_json(mids_file())
                mids = mids if isinstance(mids, dict) else {}
            if mids.pop(path, None) is not None:
                mids_dirty = True
            dirty = True
    cache["_dirty"] = dirty
    cache["_mids"] = mids if mids_dirty else None
    return cache


def save_cache(cache):
    """Write the cache, then the message ids it counted.

    In that order: a run cut short between the two leaves ids unrecorded, which
    at worst counts a resumed copy twice later. The other order would leave
    messages recorded as counted that the cache never got, and lose them.
    """
    mids = cache.pop("_mids", None)
    if cache.pop("_dirty", False) or mids is not None:
        _write_json(cache_file(), cache)
    if mids is not None:
        _write_json(mids_file(), mids)


# ---------- adding the cache up ----------

class Totals(object):
    """Everything the periods are cut from, by local day."""

    def __init__(self):
        self.days = {}          # day -> {sid: {"project": name, "m": {model key: five counts}}}
        self.hours = {}         # hour key -> {model key: five counts}
        self.calls = {}         # day -> assistant API messages
        self.hour_calls = {}
        self.corr = {}          # day -> corrections
        self.hour_corr = {}     # hour key -> corrections
        self.edits = {}         # hour key -> [edit turns, edit turns with no retry]
        self.act = {}           # day -> {activity: {"m", "turns", "edit", "clean"}}
        self.mcp = {}           # day -> {server: {"c", "e", "t": set}}
        self.peak = {}          # model -> largest context seen
        self.mcost = {}         # day -> {"main": {model key: five counts}, "sub": {...}}
        self.mod = {}           # day -> {model key: _new_mod() figures}
        self.est = []           # (day, estimates.py piece): the day is the report's
        self.est_open = []      # (day, open estimate): the day is the estimate's
        self.from_ledger = set()


def _merge_mod(dst, src):
    """Add one day's figures per model (src) into dst."""
    for mkey, row in (src or {}).items():
        into = dst.setdefault(mkey, _new_mod())
        for k in ("c", "e", "t", "k", "r", "x"):
            into[k] += row.get(k, 0)
        for tkey, trio in (row.get("w") or {}).items():
            got = _where(into, tkey)
            for i in range(3):
                got[i] += trio[i]


def add_up(cache, scope=None):
    scope = scope or SC.Scope()
    tot = Totals()
    files = cache.get("files") or {}
    other = {}
    day_of = C.day_lookup()

    def out(sid, day):
        """True on a hidden date, or when the coach's account log now says another account."""
        if scope.date_hidden(day):
            return True
        if not scope.only:
            return False
        if sid not in other:
            try:
                other[sid] = (scope.resolver().session(sid, None), {})
            except Exception:
                other[sid] = (None, {})
        who, days = other[sid]
        if who is None:
            return False
        if day not in days:
            acct = who.on(day)["account"]
            days[day] = acct != "unknown" and acct != scope.only
        return days[day]

    for path, e in files.items():
        if e.get("skip") or e.get("hidden") or "agg" not in e:
            continue
        owner = files.get(e.get("parent")) if e.get("parent") else e
        if owner is None or owner.get("hidden") or "project" not in owner:
            continue
        sid, project = e.get("sid") or "?", owner["project"]
        hidden_day = lambda day, sid=sid: out(sid, day)     # noqa: E731
        agg = e["agg"]
        acts = [agg["act"]]
        edits = [agg.get("edits") or {}]
        mods = [agg.get("mod") or {}]
        if e.get("open"):
            last = {"act": {}, "edits": {}, "mod": {}}  # the turn still running is filed for now
            close_turn(last, e["open"])
            acts.append(last["act"])
            edits.append(last["edits"])
            mods.append(last["mod"])
        for day, rows in [(d, r) for part in mods for d, r in part.items()]:
            if not hidden_day(day):
                _merge_mod(tot.mod.setdefault(day, {}), rows)
        for hour, pair in [(h, p) for part in edits for h, p in part.items()]:
            if not hidden_day(hour[:10]):
                into = tot.edits.setdefault(hour, [0, 0])
                into[0] += pair[0]
                into[1] += pair[1]
        for hour, models in agg["h"].items():
            day = hour[:10]
            if hidden_day(day):
                continue
            _merge(tot.hours.setdefault(hour, {}), models)
            row = tot.days.setdefault(day, {}).setdefault(sid, {"project": project, "m": {}})
            _merge(row["m"], models)
            _merge(tot.mcost.setdefault(day, {}).setdefault("sub" if e.get("sub") else "main", {}),
                   models)
        for hour, n in agg["calls"].items():
            if hidden_day(hour[:10]):
                continue
            tot.calls[hour[:10]] = tot.calls.get(hour[:10], 0) + n
            tot.hour_calls[hour] = tot.hour_calls.get(hour, 0) + n
        for hour, n in agg["corr"].items():
            if not hidden_day(hour[:10]):
                tot.corr[hour[:10]] = tot.corr.get(hour[:10], 0) + n
                tot.hour_corr[hour] = tot.hour_corr.get(hour, 0) + n
        for day, cats in [(d, c) for act in acts for d, c in act.items()]:
            if hidden_day(day):
                continue
            for cat, row in cats.items():
                into = tot.act.setdefault(day, {}).setdefault(
                    cat, {"m": {}, "turns": 0, "edit": 0, "clean": 0})
                _merge(into["m"], row["m"])
                for k in ("turns", "edit", "clean"):
                    into[k] += row[k]
        for day, servers in agg["mcp"].items():
            if hidden_day(day):
                continue
            for server, row in servers.items():
                into = tot.mcp.setdefault(day, {}).setdefault(server, {"c": 0, "e": 0, "t": set()})
                into["c"] += row["c"]
                into["e"] += row["e"]
                into["t"].update(row["t"])
        for model, ctx in (e.get("peak") or {}).items():
            if ctx > tot.peak.get(model, 0):
                tot.peak[model] = ctx
        if e.get("est") and not e.get("sub"):
            pieces, opened = ES.pair(e["est"])
            for into, rows in ((tot.est, pieces), (tot.est_open, opened)):
                for r in rows:
                    day = day_of(r["t"]) if isinstance(r["t"], str) else None
                    if day is not None and not hidden_day(day):
                        into.append((day, r))
    return tot


def _day_tokens(sessions):
    return sum(sum(sum(c) for c in s["m"].values()) for s in sessions.values())


def use_ledger(tot, led, today):
    """For a day that is over, take the ledger's rows when it holds more than the transcripts."""
    for day, entry in (led.get("days") or {}).items():
        if day >= today:
            continue
        if entry.get("total", 0) <= _day_tokens(tot.days.get(day) or {}):
            continue
        rows = {}
        split = {"main": {}, "sub": {}}
        for sid, sess in entry["sessions"].items():
            m = {}
            for key, (main, sub) in sess["models"].items():
                _addc(m, key, P.counts(main))
                _addc(m, key, P.counts(sub))
                _addc(split["main"], key, P.counts(main))
                _addc(split["sub"], key, P.counts(sub))
            rows[sid] = {"project": sess["project"], "m": m}
        tot.days[day] = rows
        tot.mcost[day] = split
        tot.from_ledger.add(day)
    return tot


# ---------- one period ----------

def _usd(models, prices):
    return P.cost(models, prices)["usd"]


def _sum_models(maps):
    out = {}
    for m in maps:
        _merge(out, m)
    return out


def cache_hit_rate(counts):
    """cache read / (input + cache read + cache write), or None when there is none.

    counts are [in, out, cache read, cache write 5m, cache write 1h, ...]. Cache
    writes count as misses: without them nearly every session reads 100 percent,
    because Claude Code sends almost no uncached input."""
    writes = sum(counts[3:5])
    base = counts[0] + counts[2] + writes
    return round(float(counts[2]) / base, 4) if base else None


def _share(part, whole):
    return round(float(part) / whole, 4) if whole else 0.0


def _cut(tot, days, prices):
    """Raw figures for a list of days."""
    models = _sum_models(s["m"] for d in days for s in (tot.days.get(d) or {}).values())
    cost = P.cost(models, prices)
    acts = {}
    for d in days:
        for cat, row in (tot.act.get(d) or {}).items():
            into = acts.setdefault(cat, {"m": {}, "turns": 0, "edit": 0, "clean": 0})
            _merge(into["m"], row["m"])
            for k in ("turns", "edit", "clean"):
                into[k] += row[k]
    return {"models": models, "usd": cost["usd"], "tokens": sum(cost["tokens"]),
            "counts": cost["tokens"],
            "has_data": any(tot.days.get(d) for d in days),
            "has_detail": any(tot.calls.get(d) for d in days),
            "sessions": len(set(sid for d in days for sid in (tot.days.get(d) or {}))),
            "calls": sum(tot.calls.get(d, 0) for d in days),
            "corr": sum(tot.corr.get(d, 0) for d in days),
            "acts": acts,
            "edit": sum(r["edit"] for r in acts.values()),
            "clean": sum(r["clean"] for r in acts.values())}


def activity_rows(acts, prices):
    """Every activity with spend, highest cost first."""
    rows = []
    for cat, row in acts.items():
        usd = _usd(row["m"], prices)
        if usd > 0:
            rows.append({"name": cat, "usd": usd, "turns": row["turns"],
                         "first_try": A.first_try_rate(row["edit"], row["clean"])})
    whole = sum(r["usd"] for r in rows)
    rows.sort(key=lambda r: (-r["usd"], r["name"]))
    return [{"name": r["name"], "usd": round(r["usd"], 2), "share": _share(r["usd"], whole),
             "first_try": r["first_try"], "turns": r["turns"]} for r in rows]


def project_rows(tot, days, prices, limit=3):
    """Up to three projects by cost; the rest in one row named Other."""
    by = {}
    for d in days:
        for sid, s in (tot.days.get(d) or {}).items():
            g = by.setdefault(s["project"], {"m": {}, "sids": set()})
            _merge(g["m"], s["m"])
            g["sids"].add(sid)
    rows = [{"name": name, "sessions": len(g["sids"]), "usd": _usd(g["m"], prices)}
            for name, g in by.items()]
    rows.sort(key=lambda r: (-r["usd"], -r["sessions"], r["name"]))
    whole = sum(r["usd"] for r in rows)
    out = rows[:limit]
    rest = rows[limit:]
    if rest:
        out.append({"name": OTHER, "sessions": sum(r["sessions"] for r in rest),
                    "usd": sum(r["usd"] for r in rest)})
    return [{"name": r["name"], "sessions": r["sessions"], "usd": round(r["usd"], 2),
             "share": _share(r["usd"], whole)} for r in out]


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# Connectors added in the Claude app show up under an id, not a name. Their tool
# names tell them apart: the first rule whose word appears in a tool name wins.
_CONNECTOR_BY_TOOL = (
    (("coql", "createrecords", "getrecord", "searchrecords", "upsertrecords"), "Zoho CRM"),
    (("outlook", "teams_", "sharepoint", "find_meeting_availability"), "Microsoft 365"),
    (("certify_query", "live_results", "certified_quer"), "Certified queries"),
    (("meeting_transcript", "list_meetings", "meeting_action_items"), "Claude Meetings"),
    (("guide", "batch", "export"), "Claude Docs"),
)
_APP_TOOLS = {"ccd_turn": "Claude app: task list", "ccd_session": "Claude app: sessions",
              "ccd_session_mgmt": "Claude app: session settings", "ccd_pr": "Claude app: pull requests",
              "ccd_view": "Claude app: panes", "ccd_settings": "Claude app: settings",
              "ccd_sidebar": "Claude app: sidebar", "ccd_window": "Claude app: windows",
              "ccd_host": "Claude app: this Mac", "ccd_directory": "Claude app: folders",
              "ccd_connectors": "Claude app: connectors", "Claude_Browser": "Claude browser",
              "Claude_Code_iOS_Simulator": "iOS Simulator", "claude-in-chrome": "Claude in Chrome",
              "scheduled-tasks": "Scheduled tasks"}


def mcp_label(server, tools=()):
    """A readable name for an MCP server: a known app tool, a connector told
    apart by its tool names, or the server name with its dashes and underscores
    as spaces. An unknown connector id becomes "Connector" and its first 6 characters."""
    if server in _APP_TOOLS:
        return _APP_TOOLS[server]
    if _UUID_RE.match(server):
        low = [t.lower() for t in tools]
        for words, label in _CONNECTOR_BY_TOOL:
            if any(w in t for w in words for t in low):
                return label
        return "Connector " + server[:6]
    return server.replace("_", " ").replace("-", " ")


def mcp_rows(tot, days, limit=8):
    """One row per MCP server called, most calls first."""
    by = {}
    for d in days:
        for server, row in (tot.mcp.get(d) or {}).items():
            into = by.setdefault(server, {"c": 0, "e": 0, "t": set()})
            into["c"] += row["c"]
            into["e"] += row["e"]
            into["t"].update(row["t"])
    rows = [{"name": mcp_label(name, r["t"]), "calls": r["c"], "tools": len(r["t"]), "errors": r["e"]}
            for name, r in by.items() if r["c"]]
    rows.sort(key=lambda r: (-r["calls"], r["name"]))
    return rows[:limit]


def _rate(part, whole):
    return round(float(part) / whole, 4) if whole else None


def model_rows(tot, days, acts, prices):
    """One row per model with spend in these days, highest cost first.

    Models are grouped by their display name, so a dated id and its plain id are
    one row. acts: the period's activities (_cut), for where_most."""
    by = {}

    def group(key):
        name = P.display_name(key, prices)
        if name not in by:
            by[name] = {"main": {}, "sub": {}, "act": {}, "mod": _new_mod()}
        return by[name]

    tools = {}                                  # MCP server -> its tool names, for readable labels
    for d in days:
        for side, models in (tot.mcost.get(d) or {}).items():
            for key, counts in models.items():
                _merge(group(key)[side], {key: counts})
        for key, row in (tot.mod.get(d) or {}).items():
            _merge_mod(group(key)["mod"], {"m": row})
        for server, row in (tot.mcp.get(d) or {}).items():
            tools.setdefault(server, set()).update(row["t"])
    for cat, row in acts.items():
        for key, counts in row["m"].items():
            _merge(group(key)["act"].setdefault(cat, {}), {key: counts})
    rows = []
    for name, g in by.items():
        main, sub = _usd(g["main"], prices), _usd(g["sub"], prices)
        if main + sub <= 0:
            continue
        m = g["mod"].get("m") or _new_mod()
        spent = [(c, _usd(models, prices)) for c, models in g["act"].items()]
        spent = sorted([x for x in spent if x[1] > 0], key=lambda x: (-x[1], x[0]))
        where = {}
        for tkey, trio in m["w"].items():
            if not tkey:
                label, kind = NO_TOOL, "none"
            elif tkey.startswith("mcp__"):
                label, kind = mcp_label(tkey[5:], sorted(tools.get(tkey[5:], ()))), "mcp"
            else:
                label, kind = tkey, "tool"
            into = where.setdefault(label, {"name": label, "kind": kind, "errors": 0,
                                            "redone_edits": 0, "corrections": 0})
            into["errors"] += trio[0]
            into["redone_edits"] += trio[1]
            into["corrections"] += trio[2]
        where = [w for w in where.values() if w["errors"] or w["redone_edits"] or w["corrections"]]
        where.sort(key=lambda w: (-w["errors"], -w["redone_edits"], -w["corrections"], w["name"]))
        rows.append({"name": name, "usd": main + sub, "main_usd": round(main, 2), "sub_usd": round(sub, 2),
                     "first_try": A.first_try_rate(m["t"], m["k"]), "edit_turns": m["t"],
                     "tool_calls": m["c"], "tool_errors": m["e"], "error_rate": _rate(m["e"], m["c"]),
                     "redone_edits": m["r"], "corrections": m["x"],
                     "where_most": ", ".join(c for c, _u in spent[:3]) or None,
                     "where": where[:MODEL_WHERE_ROWS]})
    whole = sum(r["usd"] for r in rows)
    rows.sort(key=lambda r: (-r["usd"], r["name"]))
    for r in rows:
        r["share"] = _share(r["usd"], whole)
        r["usd"] = round(r["usd"], 2)
    keys = ("name", "usd", "share", "main_usd", "sub_usd", "first_try", "edit_turns", "tool_calls",
            "tool_errors", "error_rate", "redone_edits", "corrections", "where_most", "where")
    return [dict((k, r[k]) for k in keys) for r in rows]


def thin(steps, limit=TREND_POINTS):
    """Steps as at most `limit` groups of neighbouring steps, in order, none empty."""
    n = len(steps)
    k = min(limit, n)
    return [steps[i * n // k:(i + 1) * n // k] for i in range(k)]


def trends(tot, unit, steps, only=None):
    """The three small series of a period: first-try rate, cache hit rate, corrections.

    One value per group of steps (thin), oldest first. None where the group has
    nothing to measure: no edit turns, no input or cache read, no calls. A step
    is an hour key, a day, or a list of days (a week). only: when given, the
    days that count; any other day adds nothing.
    """
    first, cache, corr = [], [], []
    for group in thin(steps):
        if unit != "hour":
            group = [d for step in group for d in (step if isinstance(step, list) else [step])
                     if only is None or d in only]
        if unit == "hour":
            edit = sum(tot.edits.get(h, [0, 0])[0] for h in group)
            clean = sum(tot.edits.get(h, [0, 0])[1] for h in group)
            models = _sum_models(tot.hours.get(h) or {} for h in group)
            calls = sum(tot.hour_calls.get(h, 0) for h in group)
            fixes = sum(tot.hour_corr.get(h, 0) for h in group)
        else:
            rows = [r for d in group for r in (tot.act.get(d) or {}).values()]
            edit, clean = sum(r["edit"] for r in rows), sum(r["clean"] for r in rows)
            models = _sum_models(s["m"] for d in group for s in (tot.days.get(d) or {}).values())
            calls = sum(tot.calls.get(d, 0) for d in group)
            fixes = sum(tot.corr.get(d, 0) for d in group)
        first.append(A.first_try_rate(edit, clean))
        cache.append(cache_hit_rate(P.cost(models)["tokens"]))
        corr.append(fixes if calls else None)
    return first, cache, corr


def _peak(series):
    best = None
    for p in series:
        if p["tokens"] and (best is None or p["tokens"] > best["tokens"]):
            best = p
    return dict(best) if best else None


def period(tot, label, days, prev_days, prices, series, steps=None, detail_days=None):
    """One period of the feed. detail_days: when given, the days whose transcripts
    were read; the cache hit rate and the three small series are worked out from
    those days only (All time uses this, where older days come from the ledger)."""
    cur = _cut(tot, days, prices)
    prev = _cut(tot, prev_days, prices)
    seen = cur if detail_days is None else _cut(tot, detail_days, prices)
    first, cache, corr = trends(tot, series[0], steps if steps is not None else days,
                                None if detail_days is None else set(detail_days))
    return {
        "label": label,
        "cost_usd": round(cur["usd"], 2),
        "prev_cost_usd": round(prev["usd"], 2) if prev["has_data"] else None,
        "calls": cur["calls"],
        "sessions": cur["sessions"],
        "tokens": cur["tokens"],
        "series_unit": series[0],
        "series": series[1],
        "peak": _peak(series[1]),
        "first_try": {"rate": A.first_try_rate(cur["edit"], cur["clean"]),
                      "prev_rate": A.first_try_rate(prev["edit"], prev["clean"]),
                      "edit_turns": cur["edit"], "series": first},
        "cache_hit": {"rate": cache_hit_rate(seen["counts"]),
                      "prev_rate": cache_hit_rate(prev["counts"]), "series": cache},
        "corrections": {"count": cur["corr"],
                        "prev_count": prev["corr"] if prev["has_detail"] else None,
                        "series": corr},
        "activities": activity_rows(cur["acts"], prices),
        "projects": project_rows(tot, days, prices),
        "mcp_servers": mcp_rows(tot, days),
        "models": model_rows(tot, days, cur["acts"], prices),
        "estimates": estimate_rows(tot, days, prices),
    }


def estimate_rows(tot, days, prices=None):
    """Claude's estimates against the actual time it reported, per model, for these days
    (estimates.py), or None when there were none. A piece is filed on the day of its report."""
    days = set(days)
    out = ES.block([p for d, p in tot.est if d in days], [o for d, o in tot.est_open if d in days],
                   prices)
    if out:
        out.pop("meaning", None)
    return out


def hour_keys(today, now):
    """The hour keys of today, from midnight to the hour that is running."""
    last = time.localtime(now).tm_hour if time.strftime("%Y-%m-%d", time.localtime(now)) == today \
        else 23
    return ["%sT%02d" % (today, h) for h in range(last + 1)]


def hour_series(tot, today, now, prices):
    """One point per hour of today, from midnight to the hour that is running."""
    out = []
    for key in hour_keys(today, now):
        models = tot.hours.get(key) or {}
        cost = P.cost(models, prices)
        t = _hour_iso(key)
        point = {"t": t, "tokens": sum(cost["tokens"]), "usd": round(cost["usd"], 4)}
        if out and out[-1]["t"] == t:           # the hour the clocks skip in spring: one point
            out[-1]["tokens"] += point["tokens"]
            out[-1]["usd"] = round(out[-1]["usd"] + point["usd"], 4)
            continue
        out.append(point)
    return out


def day_series(tot, days, prices):
    out = []
    for day in days:
        models = _sum_models(s["m"] for s in (tot.days.get(day) or {}).values())
        cost = P.cost(models, prices)
        out.append({"t": _day_iso(day), "tokens": sum(cost["tokens"]), "usd": round(cost["usd"], 4)})
    return out


def week_groups(days):
    """Days grouped by calendar week, oldest first: [(the week's Monday, [its days here]), ...]."""
    out = []
    for day in days:
        monday = L.monday(day)
        if out and out[-1][0] == monday:
            out[-1][1].append(day)
        else:
            out.append((monday, [day]))
    return out


def week_series(tot, days, prices):
    """One point a week, dated by the week's Monday (also for a first week that starts later)."""
    out = []
    for monday, group in week_groups(days):
        models = _sum_models(s["m"] for d in group for s in (tot.days.get(d) or {}).values())
        cost = P.cost(models, prices)
        out.append({"t": _day_iso(monday), "tokens": sum(cost["tokens"]), "usd": round(cost["usd"], 4)})
    return out


def first_day(tot, today):
    """The first day there is anything for (the ledger's first day once use_ledger has run), else today.

    Never more than ten years back: a record dated by a broken clock (1970, say) must
    not stretch All time over decades, nor push its newest days out of the range."""
    floor = L.shift(today, -(ALL_MAX_DAYS - 1))
    days = [d for d, rows in tot.days.items() if rows and floor <= d <= today]
    return min(days) if days else today


def _since_text(day, today):
    """'Nov 9', with the year when it is not this year's: 'Dec 3, 2025'."""
    return _short_date(day) if day[:4] == today[:4] else "%s, %s" % (_short_date(day), day[:4])


def detail_first_day(tot, days):
    """The first of these days whose transcript was read, or None when none was."""
    for d in days:
        if tot.calls.get(d) or tot.act.get(d):
            return d
    return None


def all_time(tot, today, prices):
    """The All time period: every day from the first one the coach has."""
    since = first_day(tot, today)
    days = L.day_range(since, today)
    detail = detail_first_day(tot, days)
    if len(days) <= ALL_DAILY_DAYS:
        series, steps = ("day", day_series(tot, days, prices)), days
    else:
        series, steps = ("week", week_series(tot, days, prices)), [g for _monday, g in week_groups(days)]
    out = period(tot, "All time, since %s" % _since_text(since, today), days, [], prices, series, steps,
                 detail_days=[d for d in days if detail is not None and d >= detail])
    out["since"] = _day_iso(since)
    out["detail_since"] = _day_iso(detail) if detail is not None else None
    return out


def periods(tot, now, prices=None):
    prices = prices or P.load_prices()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    week = L.day_range(L.shift(today, -6), today)
    month = L.day_range(L.shift(today, -29), today)
    return {
        "all": all_time(tot, today, prices),
        "today": period(tot, "Today, %s" % _short_date(today), [today], [L.shift(today, -1)],
                        prices, ("hour", hour_series(tot, today, now, prices)),
                        hour_keys(today, now)),
        "week": period(tot, "Last 7 days", week, L.day_range(L.shift(today, -13), L.shift(today, -7)),
                       prices, ("day", day_series(tot, week, prices))),
        "month": period(tot, "Last 30 days", month,
                        L.day_range(L.shift(today, -59), L.shift(today, -30)),
                        prices, ("day", day_series(tot, month, prices))),
    }


# ---------- limits ----------

def level_for(pct):
    """ok below 70, warn 70 to 89, crit 90 and up."""
    return "crit" if pct >= 90 else "warn" if pct >= 70 else "ok"


def limit_row(key, label, length, used, resets_at, seen, now=None):
    """One limit row, with pace worked out for the moment the numbers were seen.

    used: percent of the limit used. resets_at, seen: epoch seconds. length:
    seconds the window lasts. now: a run-out time at or before it is not given,
    since the numbers are then too old to say.
    """
    used = max(0.0, float(used))
    start = resets_at - length
    elapsed = min(float(length), max(0.0, seen - start))
    elapsed_pct = 100.0 * elapsed / length
    pace = runs_out = None
    if elapsed_pct >= PACE_QUIET_PCT:
        over = used - elapsed_pct
        pace = int(round(over))
        if over > 0 and 0 < used < 100:
            at = seen + (100.0 - used) * elapsed / used     # at the rate used so far
            if at < resets_at and (now is None or at > now):
                runs_out = local_iso(at)
    return {"key": key, "label": label, "used_pct": int(round(used)),
            "resets_at": local_iso(resets_at), "pace_pct": pace, "runs_out_at": runs_out,
            "level": level_for(int(round(used)))}


def limits(now, path=None):
    """The limits block. No rows until the status line has seen limit data."""
    out = {"as_of": None, "rows": []}
    raw = _read_json(path or limits_file())
    windows = raw.get("windows") if isinstance(raw, dict) else None
    if not isinstance(windows, dict):
        return out
    as_of = None
    for src, key, label, length in LIMIT_WINDOWS:
        w = windows.get(src)
        if not isinstance(w, dict):
            continue
        used, resets, seen = w.get("used"), w.get("resets_at"), w.get("t")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or v != v
               for v in (used, resets, seen)):
            continue
        if resets <= now or seen > now + 60 or seen < resets - length - 60:
            continue                        # the window is over, or the numbers make no sense
        out["rows"].append(limit_row(key, label, length, used, resets, seen, now))
        as_of = seen if as_of is None else max(as_of, seen)
    if out["rows"]:
        out["as_of"] = local_iso(as_of)
    return out


# ---------- running sessions ----------

def tail_usage(path, size=None):
    """The last main-thread assistant message with usage, read from the end of the file.

    Returns {"model", "context", "branch", "cwd"} or None.
    """
    try:
        size = os.stat(path).st_size if size is None else size
        want = TAIL_BYTES
        with open(path, "rb") as fh:
            while True:
                start = max(0, size - want)
                fh.seek(start)
                lines = fh.read(size - start).split(b"\n")
                if start > 0:
                    lines = lines[1:]       # the first piece is the end of a longer line
                for line in reversed(lines):
                    if not line or (line.startswith(_TYPE) and not line.startswith(_WANTED)):
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(d, dict) or d.get("type") != "assistant" or d.get("isSidechain"):
                        continue
                    m = d.get("message") if isinstance(d.get("message"), dict) else {}
                    u = m.get("usage")
                    if not isinstance(u, dict) or m.get("model") == "<synthetic>":
                        continue
                    c = P.usage_counts(u)
                    return {"model": m.get("model") if isinstance(m.get("model"), str) else None,
                            "context": sum(c),
                            "branch": d.get("gitBranch") if isinstance(d.get("gitBranch"), str) else None,
                            "cwd": d.get("cwd") if isinstance(d.get("cwd"), str) else None}
                if start == 0 or want >= TAIL_MAX_BYTES:
                    return None
                want *= 4
    except OSError:
        return None


def window_for(model, peaks=None, context=0):
    """The context window of a model: 1M once it has been seen above 200k here, else 200k."""
    if max(context, (peaks or {}).get(model, 0)) > SMALL_WINDOW:
        return LARGE_WINDOW
    return SMALL_WINDOW


def context_level(tokens, pct, T):
    """ok, warn or crit, on the same thresholds as the status line (signals.T)."""
    if tokens >= T["ctx_over_tokens"] or pct >= T["ctx_over_pct"]:
        return "crit"
    if tokens >= T["ctx_warn_tokens"] or pct >= T["ctx_warn_pct"]:
        return "warn"
    return "ok"


def _model_name(model, prices):
    name = P.display_name(model or "unknown", prices)
    return name[7:] if name.startswith("Claude ") else name


def running(cache, tot, now, scope=None, prices=None):
    scope = scope or SC.Scope()
    prices = prices or P.load_prices()
    import statusline
    T = statusline.thresholds(statusline.load_settings())
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    out = []
    for path, e in (cache.get("files") or {}).items():
        if e.get("sub") or e.get("skip") or e.get("hidden") or "project" not in e:
            continue
        if now - e.get("mtime", 0) > RUNNING_SEC or scope.date_hidden(today):
            continue
        gate = scope.gate(e.get("sid"), path)
        if gate is not None and not gate(today):
            continue
        last = tail_usage(path)
        if not last:
            continue
        window = window_for(last["model"], tot.peak, last["context"])
        pct = int(round(100.0 * last["context"] / window))
        began = e["meta"].get("run") or e["meta"].get("first")     # this run, not the session's first day
        first = C.ts_epoch(began) if began else None
        out.append({"project": e["project"], "branch": last["branch"] or e["meta"].get("branch"),
                    "model": _model_name(last["model"], prices),
                    "started": local_iso(first) if first is not None else None,
                    "context_pct": pct,
                    "context_left_tokens": max(0, window - last["context"]),
                    "level": context_level(last["context"], pct, T),
                    "_t": e.get("mtime", 0)})
    out.sort(key=lambda r: -r["_t"])
    for r in out:
        del r["_t"]
    return out


# ---------- tips ----------

def pick_tips(findings, limit=3):
    """Up to three findings, most severe first, a different pattern each while there are enough."""
    rows = [f for f in findings or [] if isinstance(f, dict) and f.get("evidence")]
    rows.sort(key=lambda f: SEVERITY.get(f.get("severity"), 9))     # stable: file order within one
    picked, used = [], set()
    for f in rows:
        if f.get("pattern") not in used and len(picked) < limit:
            picked.append(f)
            used.add(f.get("pattern"))
    for f in rows:
        if len(picked) >= limit:
            break
        if not any(f is p for p in picked):
            picked.append(f)
    picked.sort(key=lambda f: SEVERITY.get(f.get("severity"), 9))
    return [{"title": C.one_line(str(f.get("evidence")), 180),
             "advice": C.one_line(str(f.get("suggestion") or ""), 220),
             "pattern": str(f.get("pattern") or "")} for f in picked]


def tips(cache, scope):
    """Tips from the findings file written last, less anything that may not be shown."""
    folder = os.path.join(C.reports_dir(), "digests")
    newest = None
    for path in glob.glob(os.path.join(folder, "*.findings.json")):
        try:
            mt = os.stat(path).st_mtime
        except OSError:
            continue
        if newest is None or (mt, path) > newest:
            newest = (mt, path)
    if newest is None:
        return []
    name = os.path.basename(newest[1])[:-len(".findings.json")]
    try:
        digest_time = os.stat(os.path.join(folder, name + ".json")).st_mtime
    except OSError:
        digest_time = None
    stamp = [newest[1], newest[0], digest_time, scope.sig()]
    kept = cache.get("tips")
    if isinstance(kept, dict) and kept.get("stamp") == stamp:
        return kept["rows"]
    day = name[6:] if name.startswith("daily-") and L.is_day(name[6:]) else None
    findings = _read_json(newest[1])
    digest = _read_json(os.path.join(folder, name + ".json"))
    rows = []
    if isinstance(findings, list) and isinstance(digest, dict):
        _digest, shown = SC.stored(digest, findings, day)
        rows = pick_tips(shown)
    cache["tips"] = {"stamp": stamp, "rows": rows}
    cache["_dirty"] = True
    return rows


# ---------- the feed ----------

def snapshot(now=None, projects_dir=None, stats=None):
    """The whole feed as a dict (the contract's live.json). Updates the cache on the way."""
    now = time.time() if now is None else now
    cfg = C.load_config()
    scope = SC.Scope(cfg)                    # raises ConfigError when config.json cannot be trusted
    lock = _lock()
    try:
        cache = refresh(projects_dir, now, cfg, scope, stats)
        save_cache(cache)                    # before anything else can fail or be cut short
        tot = add_up(cache, scope)
        led, state = L.load()
        # The ledger can only be purged by session id, project name and date. Rows
        # written under other settings may hold work that is left out now, so the
        # ledger is used only when its settings are the ones in force.
        if state == "ok" and led.get("exclude_sig", "") == scope.sig():
            use_ledger(tot, L.visible(led, scope), time.strftime("%Y-%m-%d", time.localtime(now)))
        out = {"version": VERSION,
               "updated": local_iso(now),
               "page_url": cfg.get("page_url"),
               "periods": periods(tot, now),
               "limits": limits(now),
               "running": running(cache, tot, now, scope),
               "tips": tips(cache, scope)}
        save_cache(cache)
        return out
    finally:
        _unlock(lock)


def _lock():
    """Wait for any other run to finish. Returns the open lock file, or None where locks do not exist."""
    try:
        import fcntl
        folder = C.coach_home()
        if not os.path.isdir(folder):
            os.makedirs(folder)
        fh = open(os.path.join(folder, "live.lock"), "w")
        fcntl.flock(fh, fcntl.LOCK_EX)
        return fh
    except (ImportError, OSError):
        return None


def _unlock(fh):
    if fh is not None:
        try:
            fh.close()
        except OSError:
            pass


def write(force=False, now=None, projects_dir=None, stats=None):
    """Write live.json. Returns its path, or None when it was written under a minute ago."""
    path = live_file()
    now = time.time() if now is None else now
    if not force:
        try:
            if 0 <= now - os.stat(path).st_mtime < MIN_WRITE_GAP_SEC:
                return None
        except OSError:
            pass
    try:
        feed = snapshot(now, projects_dir, stats)
    except C.ConfigError:
        # config.json cannot be read, and it may name work to leave out. The feed
        # written before must not stay up: it is replaced by an empty one.
        _write_json(path, {"version": VERSION, "updated": local_iso(now), "page_url": None,
                           "periods": {}, "limits": {"as_of": None, "rows": []}, "running": [],
                           "tips": [], "problem": "settings"}, indent=1)
        raise
    _write_json(path, feed, indent=1)
    return path


def page(projects_dir=None, today=None):
    """Build the coach page at <coach home>/site/index.html and return its path.

    For a tool that shows the page and wants it current each time. It is the full
    build the recap runs (build_site.build_page with the trend scan). Measured on
    one Mac: about half a second once the caches exist, about 16 seconds the
    first time, when every transcript is read. The file is written whole: a temp file
    beside it, then a rename, so a window showing the page never loads half of
    one. When config.json cannot be trusted nothing is built and the page on
    disk is left as it is, as build_site.py does.
    """
    if C.load_config().get("damaged"):
        raise C.ConfigError(C.config_problem())
    import build_site as B
    html, _card = B.build_page(C.reports_dir(), projects_dir, today, True)
    path = C.site_file()
    _write_text(path, html)
    return path


def _time_runs():
    """Cold (no cache) and warm run times, in a throwaway coach home copy of the settings."""
    import shutil
    import tempfile
    real = C.coach_home()
    tmp = tempfile.mkdtemp(prefix="coach-live-time-")
    try:
        for name in ("config.json", "ledger.json"):
            if os.path.isfile(os.path.join(real, name)):
                shutil.copy(os.path.join(real, name), os.path.join(tmp, name))
        os.environ["COACH_HOME"] = tmp
        out = []
        for label in ("cold", "warm", "warm"):
            stats = {}
            t0 = time.time()
            snapshot(stats=stats)
            out.append("%s: %.2fs (%d transcripts, %d opened, %.1f MB read)"
                       % (label, time.time() - t0, stats.get("files", 0), stats.get("read", 0),
                          stats.get("bytes", 0) / 1e6))
        return out
    finally:
        os.environ["COACH_HOME"] = real
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else ""
    if cmd == "write" and set(args[1:]) <= set(["--force"]):
        path = write(force="--force" in args)
        if path and sys.stdout.isatty():
            sys.stdout.write("wrote %s\n" % path)
        return 0
    if cmd == "show" and len(args) == 1:
        sys.stdout.write(json.dumps(snapshot(), indent=1, ensure_ascii=False) + "\n")
        return 0
    if cmd == "page" and len(args) == 1:
        sys.stdout.write(page() + "\n")
        return 0
    if cmd == "time" and len(args) == 1:
        sys.stdout.write("\n".join(_time_runs()) + "\n")
        return 0
    sys.stdout.write(__doc__)
    return 0 if cmd in ("-h", "--help") else 2


if __name__ == "__main__":
    sys.exit(C.run_main(main))
