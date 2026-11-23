#!/usr/bin/env python3
"""Which Claude account a session ran under, from what is on this machine.

A person who used two accounts over time can tell the coach to show one only
(config.json key "only_account"). This module works out, per session and per
local day where the evidence is dated, the account and the organisation, as a
short form: the first 6 hex characters of each id. Full ids, emails and
organisation names are never stored, printed or returned.

Evidence, strongest first:
  hook      the coach's own log, <coach home>/state/session-accounts.jsonl,
            written by this script as a SessionStart hook (dated)
  owner     a `session-owner` record in the transcript (dated by its place)
  app       the desktop app's session index: a file per desktop session,
            local_<app id>.json in a folder named for the account and the
            organisation, naming the Claude Code session id inside
            (cliSessionId, lastAssistantCliSessionId); dated by its createdAt
            where it has one, else not dated
  bridge    a `bridge-session` record in the transcript (dated by its place)
  ledger    an `artifact-autoreact-ledger` record (dated, account only)
A hook line or ledger record that only repeats the .claude.json login, on a
day the session ran in the desktop app, is "login" and is not used (below).

Where the hook gets the account, in this order:
  1. The account the host process gave Claude Code (CLAUDE_CODE_ACCOUNT_UUID
     and CLAUDE_CODE_ORGANIZATION_UUID; the desktop app sets them), unless
     CLAUDE_CODE_SESSION_ID names another session: then that environment
     belongs to an outer session (a test or a nested run) and none of it is used.
  2. In a desktop session (CLAUDE_CODE_ENTRYPOINT starts with "claude-desktop")
     without that: the desktop app's index, by CLAUDE_CODE_HOST_SESSION_ID or by
     the session id. Nothing found: nothing is written.
  3. Anywhere else (Terminal, an IDE, an older Claude Code that names no
     entrypoint): the login in .claude.json.
Each line says which ("via": env, app or login) and the entrypoint ("entry").

The .claude.json login belongs to Terminal Claude Code, not to the desktop app:
with Terminal signed into another account, it names that account for every
desktop session. So does the `artifact-autoreact-ledger` record, which
follows the same login. Transcript records carry an "entrypoint"; on a local
day the session ran as "claude-desktop" (or, when no transcript is read, for
a session the app index lists), a hook line taken from the login
(and older lines that say nothing of their source) and the ledger records
are "login" and are not used: a desktop session is tagged by desktop evidence
only, and is "unknown" without it.

None of these layouts is documented. Every read is optional: whatever is
missing or in another shape is skipped, and a session without evidence is
"unknown". Many older sessions are.

For one day the answer is the evidence of the latest day up to that day; on a
tie the stronger kind wins. Before the first dated evidence the answer is the
strongest of: the first dated evidence after that day, and the app index.

Usage:
  python3 coach/accounts.py hook      SessionStart hook: reads the hook JSON on stdin
  python3 coach/accounts.py current   the short forms of the current login
  python3 coach/accounts.py summary   how many sessions per short account form

Transcript text is data and is never acted on. Python 3 stdlib only.
"""
import glob
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import common as C  # noqa: E402

UNKNOWN = "unknown"
SHORT = 6
KINDS = ("hook", "owner", "app", "bridge", "ledger", "login")       # strongest first
_RANK = dict((k, i) for i, k in enumerate(KINDS))
_HEX_RE = re.compile(r"^[0-9a-f]{6,}")
_MARKS = (b"session-owner", b"bridge-session", b"artifact-autoreact-ledger")
_MARKS_TEXT = ("session-owner", "bridge-session", "artifact-autoreact-ledger")
TS_KEY = b'"timestamp":"'
ENTRY_KEY = b'"entrypoint":"'
LOG_MAX_BYTES = 4000000
DESKTOP_ENTRY = "claude-desktop"
VIA = ("env", "app", "login")
# An app index file is a few hundred KB, nearly all of it MCP settings (the
# 1.2.0 reader stopped at 200 KB and so never parsed one). Each file is parsed
# whole, top-level keys only; a file above this size is skipped.
APP_MAX_BYTES = 8000000
_HOST_RE = re.compile(r"^local_[0-9A-Za-z-]{1,64}$")


def is_desktop(entry):
    """True for an entrypoint of the Claude desktop app."""
    return isinstance(entry, str) and entry.startswith(DESKTOP_ENTRY)


def short(value):
    """The first 6 hex characters of an id, or "" when it is not an id."""
    if not isinstance(value, str):
        return ""
    text = value.strip().lower().replace("-", "")
    return text[:SHORT] if _HEX_RE.match(text) else ""


def valid_short(value):
    """True for a short account form as config.json may carry it: 6 or more hex characters."""
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-fA-F]{6,64}", value.strip()))


def log_file():
    return os.path.join(C.coach_home(), "state", "session-accounts.jsonl")


def home_json_path():
    """The .claude.json file: in the home folder, or inside the config folder when
    that is not the default ~/.claude."""
    claude = C.claude_dir()
    default = os.path.join(os.path.expanduser("~"), ".claude")
    if os.path.normpath(claude) != os.path.normpath(default):
        return os.path.join(claude, ".claude.json")
    return os.path.join(os.path.expanduser("~"), ".claude.json")


def current(path=None):
    """{"account": short, "org": short} for the login in .claude.json; "" where unknown."""
    out = {"account": "", "org": ""}
    try:
        with open(path or home_json_path(), "rb") as fh:
            raw = json.loads(fh.read().decode("utf-8", "replace"))
        oauth = raw.get("oauthAccount") if isinstance(raw, dict) else None
        if isinstance(oauth, dict):
            out["account"] = short(oauth.get("accountUuid"))
            out["org"] = short(oauth.get("organizationUuid"))
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return out


# ---------- the SessionStart hook ----------

def record(session_id, when=None, who=None, path=None):
    """Append one line to the coach's own log. Returns True when a line was written.

    `who` is {"account", "org"} and may name its source ("via": env, app or
    login) and the entrypoint ("entry"); without `who` it is the .claude.json
    login, with no source named."""
    who = who or current()
    if not who.get("account") or not isinstance(session_id, str) or not session_id:
        return False
    path = path or log_file()
    rec = {"session": session_id[:64], "ts": int(when or time.time()),
           "account": who["account"], "org": who.get("org") or ""}
    if who.get("via") in VIA:
        rec["via"] = who["via"]
    if isinstance(who.get("entry"), str) and who["entry"]:
        rec["entry"] = who["entry"][:40]
    line = json.dumps(rec, separators=(",", ":"))
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    with open(path, "a") as fh:
        fh.write(line + "\n")
    return True


def host_env(session_id, env=None):
    """The part of the environment the host gave this session: {} when
    CLAUDE_CODE_SESSION_ID names another session (an outer session's
    environment, inherited by a test or a nested run)."""
    env = os.environ if env is None else env
    own = env.get("CLAUDE_CODE_SESSION_ID")
    if own and own != session_id:
        return {}
    return env


def session_account(session_id, env=None, app_dir=None, login_path=None):
    """{"account", "org", "via", "entry"} for the session a SessionStart hook
    runs in, or None when nothing trustworthy names it (see the module notes)."""
    env = host_env(session_id, env)
    entry = env.get("CLAUDE_CODE_ENTRYPOINT") or ""
    acct = short(env.get("CLAUDE_CODE_ACCOUNT_UUID"))
    if acct:
        return {"account": acct, "org": short(env.get("CLAUDE_CODE_ORGANIZATION_UUID")),
                "via": "env", "entry": entry}
    if is_desktop(entry):
        found = app_lookup(session_id, env.get("CLAUDE_CODE_HOST_SESSION_ID"), app_dir)
        if not found:
            return None                      # the .claude.json login is not this session's
        return {"account": found[0], "org": found[1], "via": "app", "entry": entry}
    who = current(login_path)
    if not who["account"]:
        return None
    return {"account": who["account"], "org": who["org"], "via": "login", "entry": entry}


def hook_main(stream=None, env=None):
    """SessionStart hook. No output, never blocks, never raises."""
    try:
        data = json.loads((stream or sys.stdin).read() or "{}")
        if isinstance(data, dict) and data.get("source", "startup") in ("startup", "resume", "clear"):
            sid = data.get("session_id")
            if isinstance(sid, str) and sid:
                who = session_account(sid, env)
                if who:
                    record(sid, who=who)
    except Exception:
        pass
    return 0


# ---------- reading the evidence ----------

_MAYBE = "hook?"       # a login line that names no entrypoint: the transcript decides


def _log_kind(d):
    """"hook" for a line the session's own host or Terminal login named,
    "login" for the .claude.json login in a desktop session, and _MAYBE for a
    login line (or an older line) that does not say where it ran."""
    via, entry = d.get("via"), d.get("entry")
    if via in ("env", "app"):
        return "hook"
    if isinstance(entry, str) and entry:
        return "login" if is_desktop(entry) else "hook"
    return _MAYBE


def _read_log(path=None):
    """{session id: [(epoch, account, org, kind)]} from the coach's own log."""
    out = {}
    path = path or log_file()
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > LOG_MAX_BYTES:
                fh.seek(size - LOG_MAX_BYTES)
                fh.readline()
            for raw in fh:
                try:
                    d = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue
                acct, sid, ts = short(d.get("account")), d.get("session"), d.get("ts")
                if acct and isinstance(sid, str) and isinstance(ts, (int, float)):
                    out.setdefault(sid, []).append((float(ts), acct, short(d.get("org")),
                                                    _log_kind(d)))
    except OSError:
        pass
    return out


def app_sessions_dir():
    """The desktop app's session index folder, or "" when this system has none we know of."""
    override = os.environ.get("COACH_APP_SESSIONS_DIR")
    if override is not None:
        return override
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support", "Claude")
    elif sys.platform.startswith("win"):
        base = os.path.join(os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming"),
                            "Claude")
    else:
        base = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"),
                            "Claude")
    return os.path.join(base, "claude-code-sessions")


def _epoch_of_ms(value):
    """Seconds from the app's createdAt (milliseconds), or None."""
    if isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if v > 1e11:
        v /= 1000.0
    return v if 1e9 < v < 3e10 else None          # 2001 to about 2920: localtime() takes it everywhere


def _app_file_ids(path):
    """([(Claude Code session id, is its cliSessionId)], created epoch or None)
    from one app index file: its cliSessionId and lastAssistantCliSessionId,
    and createdAt. The
    file is named for the app's own id (local_<app id>.json), never for the
    Claude Code session id."""
    ids = []
    try:
        if os.path.getsize(path) > APP_MAX_BYTES:
            return ids, None
        with open(path, "rb") as fh:
            d = json.loads(fh.read().decode("utf-8", "replace"))
    except (OSError, ValueError, RecursionError):
        return ids, None
    if not isinstance(d, dict):
        return ids, None
    for key in ("cliSessionId", "lastAssistantCliSessionId"):      # top-level keys only
        v = d.get(key)
        if isinstance(v, str) and v and v not in [x[0] for x in ids]:
            ids.append((v, key == "cliSessionId"))
    return ids, _epoch_of_ms(d.get("createdAt"))


def _app_folders(path):
    parts = path.replace("\\", "/").split("/")
    return short(parts[-3]), short(parts[-2])


def _read_app_index(folder=None):
    """{session id: (account, org, created epoch or None)} from
    <folder>/<account>/<org>/local_*.json."""
    out = {}
    folder = app_sessions_dir() if folder is None else folder
    if not folder or not os.path.isdir(folder):
        return out
    try:
        files = glob.glob(os.path.join(folder, "*", "*", "local_*.json"))
    except OSError:
        return out
    later = {}
    for path in sorted(files)[:20000]:
        acct, org = _app_folders(path)
        if not acct:
            continue
        ids, created = _app_file_ids(path)
        # a file's own cliSessionId wins over another file's lastAssistantCliSessionId
        for sid, own in ids:
            (out if own else later).setdefault(sid, (acct, org, created))
    for sid, found in later.items():
        out.setdefault(sid, found)
    return out


def app_lookup(session_id, host_id=None, folder=None):
    """(account, org) of one desktop session from the app index, or None: by
    the app's own id (CLAUDE_CODE_HOST_SESSION_ID, the file name) first, then
    by the Claude Code session id inside the files."""
    folder = app_sessions_dir() if folder is None else folder
    if not folder or not os.path.isdir(folder):
        return None
    if isinstance(host_id, str) and _HOST_RE.match(host_id):
        for path in glob.glob(os.path.join(folder, "*", "*", host_id + ".json")):
            acct, org = _app_folders(path)
            if acct:
                return acct, org
    if isinstance(session_id, str) and session_id:
        found = _read_app_index(folder).get(session_id)
        if found:
            return found[0], found[1]
    return None


def transcript_points(path):
    """[(epoch, account, org, kind)] from the account records in one transcript."""
    return transcript_evidence(path)[0]


class _Days(object):
    """Local day of an epoch, without a strftime per transcript line."""

    def __init__(self):
        self.lo = self.hi = None
        self.day = ""

    def of(self, ep):
        if self.lo is None or not (self.lo <= ep < self.hi):
            t = time.localtime(ep)
            self.day = time.strftime("%Y-%m-%d", t)
            self.lo = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
            self.hi = time.mktime((t.tm_year, t.tm_mon, t.tm_mday + 1, 0, 0, 0, 0, 0, -1))
        return self.day


def _entry_of(line):
    i = line.find(ENTRY_KEY)
    if i < 0:
        return ""
    j = line.find(b'"', i + len(ENTRY_KEY))
    if j < 0 or j - i > 80:
        return ""
    return line[i + len(ENTRY_KEY):j].decode("ascii", "replace")


def transcript_evidence(path):
    """([(epoch, account, org, kind)], {local day: set of entrypoints}) for one transcript.

    The account records carry no time of their own: each takes the time of the
    timed record before it, or of the next one when it comes first. The
    entrypoints ("cli", "claude-desktop", ...) come from the timed records.
    """
    points, waiting = [], []
    entries = {}
    days = _Days()
    last = None
    try:
        fh = open(path, "rb")
    except OSError:
        return points, entries
    with fh:
        for line in fh:
            if not any(m in line[:400] for m in _MARKS):
                i = line.rfind(TS_KEY)
                if i >= 0:
                    ep = C.ts_epoch(line[i + 13:i + 32].decode("ascii", "replace"))
                    if ep is not None:
                        last = ep
                        entry = _entry_of(line)
                        if entry:
                            entries.setdefault(days.of(ep), set()).add(entry)
                        if waiting:
                            points.extend((ep,) + w for w in waiting)
                            waiting = []
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            t = d.get("type")
            if t not in _MARKS_TEXT:
                # an ordinary record that only mentions one of the names
                ep = C.ts_epoch(d.get("timestamp")) if isinstance(d.get("timestamp"), str) else None
                if ep is not None:
                    last = ep
                    if isinstance(d.get("entrypoint"), str) and d["entrypoint"]:
                        entries.setdefault(days.of(ep), set()).add(d["entrypoint"][:80])
                    if waiting:
                        points.extend((ep,) + w for w in waiting)
                        waiting = []
                continue
            if t == "session-owner":
                rec = (short(d.get("accountUuid")), short(d.get("organizationUuid")), "owner")
            elif t == "bridge-session":
                rec = (short(d.get("ownerAccountUuid")), short(d.get("ownerOrganizationUuid")),
                       "bridge")
            elif t == "artifact-autoreact-ledger":
                rec = (short(d.get("accountUuid")), "", "ledger")
            else:
                continue
            if not rec[0]:
                continue
            own = C.ts_epoch(d.get("timestamp")) if isinstance(d.get("timestamp"), str) else None
            when = own if own is not None else last
            if when is not None:
                points.append((when,) + rec)
            else:
                waiting.append(rec)
    return points, entries


def _day(epoch):
    return time.strftime("%Y-%m-%d", time.localtime(epoch))


def classify(points, entries=None, in_app=False):
    """The points with the .claude.json login marked as "login" where the
    session ran in the desktop app.

    A hook line that does not say where it ran, and every ledger record, is
    "login" on a local day whose transcript records name a desktop entrypoint;
    on a day with no records, when any day does, or when no day names an
    entrypoint and `in_app` is true (no transcript was read and the app index
    lists the session). Otherwise a hook line is "hook" and a ledger record
    stays "ledger"."""
    entries = entries or {}
    desktop_days = set(day for day, names in entries.items() if any(is_desktop(e) for e in names))
    fallback = bool(desktop_days) if entries else bool(in_app)

    def desktop(ep):
        day = _day(ep)
        return day in desktop_days if day in entries else fallback

    out = []
    for ep, acct, org, kind in points:
        if kind in (_MAYBE, "ledger") and desktop(ep):
            kind = "login"
        elif kind == _MAYBE:
            kind = "hook"
        out.append((ep, acct, org, kind))
    return out


class Session(object):
    """The evidence for one session, and the answer per local day.

    "login" evidence never counts: a desktop session with nothing else is unknown."""

    def __init__(self, points=(), app=None):
        points = [p[:3] + ("hook",) if p[3] not in _RANK else p for p in points]
        points = [p for p in points if p[3] != "login"]
        self.points = sorted(points)                 # dated: (epoch, account, org, kind)
        self.app = app                               # (account, org) or None, not dated
        self._by_day = {}

    @property
    def known(self):
        return bool(self.points or self.app)

    def on(self, day):
        """{"account", "org", "kind"} for a local day (YYYY-MM-DD); account "unknown" without evidence."""
        if day in self._by_day:
            return self._by_day[day]
        best = None
        before = [p for p in self.points if _day(p[0]) <= day]
        if before:
            latest = max(_day(p[0]) for p in before)
            same = [p for p in before if _day(p[0]) == latest]
            best = min(same, key=lambda p: (_RANK[p[3]], -p[0]))
        else:
            cands = []
            for kind in KINDS:
                later = [p for p in self.points if p[3] == kind]
                if later:
                    cands.append(later[0])
            if self.app:
                cands.append((0, self.app[0], self.app[1], "app"))
            if cands:
                best = min(cands, key=lambda p: (_RANK[p[3]], p[0]))
        out = ({"account": best[1], "org": best[2], "kind": best[3]} if best
               else {"account": UNKNOWN, "org": "", "kind": "none"})
        self._by_day[day] = out
        return out

    def accounts(self):
        """Every short account form the evidence names."""
        out = set(p[1] for p in self.points)
        if self.app:
            out.add(self.app[0])
        return out


class Resolver(object):
    """Reads the hook log and the app index once, then answers per session."""

    def __init__(self, log_path=None, app_dir=None):
        self._log = _read_log(log_path)
        self._app = _read_app_index(app_dir)

    def session(self, session_id, transcript=None):
        points = list(self._log.get(session_id, ()))
        entries = {}
        if transcript:
            try:
                found, entries = transcript_evidence(transcript)
                points.extend(found)
            except Exception:
                pass                                 # evidence is optional
        app = self._app.get(session_id)
        points = classify(points, entries, in_app=bool(app) and not transcript)
        if app and app[2] is not None:
            # dated by the app's own createdAt: the session began in the app under this account
            points.append((app[2], app[0], app[1], "app"))
            app = None
        return Session(points, app[:2] if app else None)


def summary(projects_dir=None):
    """{"accounts": {short: sessions}, "unknown": n, "by_kind": {...}}: counts only."""
    res = Resolver()
    out = {"accounts": {}, "unknown": 0, "by_kind": {}}
    for path in glob.glob(os.path.join(projects_dir or C.PROJECTS_DIR, "*", "*.jsonl")):
        s = res.session(os.path.basename(path)[:-6], path)
        if not s.known:
            out["unknown"] += 1
            continue
        last = s.on("9999-12-31")
        for acct in s.accounts():
            out["accounts"][acct] = out["accounts"].get(acct, 0) + 1
        out["by_kind"][last["kind"]] = out["by_kind"].get(last["kind"], 0) + 1
    out["current"] = current()
    return out


def main(argv=None):
    words = list(sys.argv[1:] if argv is None else argv)
    if words == ["hook"]:
        return hook_main()
    if words == ["current"]:
        sys.stdout.write(json.dumps(current()) + "\n")
        return 0
    if words == ["summary"]:
        sys.stdout.write(json.dumps(summary(), indent=1, sort_keys=True) + "\n")
        return 0
    sys.stderr.write("use: hook | current | summary\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
