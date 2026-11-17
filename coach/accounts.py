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
  app       the desktop app's session index: a file per session, in a folder
            named for the account and the organisation (not dated)
  bridge    a `bridge-session` record in the transcript (dated by its place)
  ledger    an `artifact-autoreact-ledger` record (dated, account only)

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
KINDS = ("hook", "owner", "app", "bridge", "ledger")       # strongest first
_RANK = dict((k, i) for i, k in enumerate(KINDS))
_HEX_RE = re.compile(r"^[0-9a-f]{6,}")
_MARKS = (b"session-owner", b"bridge-session", b"artifact-autoreact-ledger")
_MARKS_TEXT = ("session-owner", "bridge-session", "artifact-autoreact-ledger")
TS_KEY = b'"timestamp":"'
LOG_MAX_BYTES = 4000000


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
    """Append one line to the coach's own log. Returns True when a line was written."""
    who = who or current()
    if not who.get("account") or not isinstance(session_id, str) or not session_id:
        return False
    path = path or log_file()
    line = json.dumps({"session": session_id[:64], "ts": int(when or time.time()),
                       "account": who["account"], "org": who.get("org") or ""},
                      separators=(",", ":"))
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    with open(path, "a") as fh:
        fh.write(line + "\n")
    return True


def hook_main(stream=None):
    """SessionStart hook. No output, never blocks, never raises."""
    try:
        data = json.loads((stream or sys.stdin).read() or "{}")
        if isinstance(data, dict) and data.get("source", "startup") in ("startup", "resume", "clear"):
            record(data.get("session_id"))
    except Exception:
        pass
    return 0


# ---------- reading the evidence ----------

def _read_log(path=None):
    """{session id: [(epoch, account, org, "hook")]} from the coach's own log."""
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
                    out.setdefault(sid, []).append((float(ts), acct, short(d.get("org")), "hook"))
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


def _read_app_index(folder=None):
    """{session id: (account, org)} from <folder>/<account>/<org>/local_*.json."""
    out = {}
    folder = app_sessions_dir() if folder is None else folder
    if not folder or not os.path.isdir(folder):
        return out
    try:
        files = glob.glob(os.path.join(folder, "*", "*", "local_*.json"))
    except OSError:
        return out
    for path in files[:20000]:
        parts = path.replace("\\", "/").split("/")
        acct, org = short(parts[-3]), short(parts[-2])
        if not acct:
            continue
        try:
            with open(path, "rb") as fh:
                d = json.loads(fh.read(200000).decode("utf-8", "replace"))
            sid = d.get("cliSessionId") if isinstance(d, dict) else None
        except (OSError, ValueError):
            continue
        if isinstance(sid, str) and sid:
            out[sid] = (acct, org)
    return out


def transcript_points(path):
    """[(epoch, account, org, kind)] from the account records in one transcript.

    These records carry no time of their own: each takes the time of the timed
    record before it, or of the next one when it comes first.
    """
    points, waiting = [], []
    last = None
    try:
        fh = open(path, "rb")
    except OSError:
        return points
    with fh:
        for line in fh:
            if not any(m in line[:400] for m in _MARKS):
                i = line.rfind(TS_KEY)
                if i >= 0:
                    ep = C.ts_epoch(line[i + 13:i + 32].decode("ascii", "replace"))
                    if ep is not None:
                        last = ep
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
    return points


def _day(epoch):
    return time.strftime("%Y-%m-%d", time.localtime(epoch))


class Session(object):
    """The evidence for one session, and the answer per local day."""

    def __init__(self, points=(), app=None):
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
        if transcript:
            try:
                points.extend(transcript_points(transcript))
            except Exception:
                pass                                 # evidence is optional
        return Session(points, self._app.get(session_id))


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
