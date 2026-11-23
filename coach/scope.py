"""What the coach is allowed to show: one place for every "leave this out" rule.

config.json can say that some work is never to be shown:
  exclude          rules matched against a session's folder, titles, id and project
  only_account     show one Claude account only (see accounts.py)
  unknown_account  "include" (default) or "exclude" sessions with no account evidence
  exclude_dates    days or ranges of days dropped whole

A Scope is built once per run. `gate(session id, transcript)` gives a
function that says, per local day, whether that session's records of the day
count. `hidden(acc, folder)` says whether a whole session is covered by an
exclude rule. The scope keeps counts of what it dropped (numbers only, never a
name) and the session-days it dropped, so the ledger can purge them.
"""
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import common as C  # noqa: E402
import projects as PR  # noqa: E402

REASONS = ("rule", "account", "unknown", "date")
ACCOUNT_RULES = 2       # 2: 1.3.0 tags desktop sessions by the desktop app's account


class Gate(object):
    """Per-day answer for one session. Call it with a local day (YYYY-MM-DD)."""

    def __init__(self, scope, sid, who):
        self.scope, self.sid, self.who = scope, sid, who
        self.dropped = False
        self._days = {}

    def __call__(self, day):
        if day in self._days:
            return self._days[day]
        ok = True
        sc = self.scope
        if day is None:
            ok = True
        elif sc.date_hidden(day):
            sc.stats["date"].add(self.sid)
            ok = False
        elif self.who is not None:
            acct = self.who.on(day)["account"]
            if acct == "unknown":
                sc.stats["unknown"].add(self.sid)
                ok = not sc.unknown_out
            elif acct != sc.only:
                sc.stats["account"].add(self.sid)
                ok = False
            if not ok:
                sc.dropped_days.add((self.sid, day))
        if not ok:
            self.dropped = True
        self._days[day] = ok
        return ok


class Scope(object):
    def __init__(self, cfg=None, exclude=None, resolver=None):
        cfg = cfg if cfg is not None else C.load_config()
        if cfg.get("damaged"):
            raise C.ConfigError(C.config_problem())
        self.rules = exclude if exclude is not None else (cfg.get("exclude") or [])
        # exclusion also tests the project name a session resolves to, so the
        # naming settings are part of the fingerprint whenever a rule is in force
        self._naming = [cfg.get("projects"), cfg.get("session_projects"),
                        cfg.get("other_project")] if self.rules else None
        self._gone_ids = None
        self.only = (cfg.get("only_account") or "")[:6].lower()
        self.unknown_out = bool(self.only) and cfg.get("unknown_account") == "exclude"
        self.dates = [tuple(r) for r in cfg.get("exclude_dates") or []]
        self._resolver = resolver
        self.stats = dict((k, set()) for k in REASONS)
        self.dropped_days = set()          # (session id, day) dropped because of the account
        self.gone = set()                  # sessions with nothing left to show

    @property
    def on(self):
        return bool(self.rules or self.only or self.dates)

    @property
    def by_day(self):
        return bool(self.only or self.dates)

    def sig(self):
        """A short fingerprint of every rule in force ("" when there is none)."""
        if not self.on:
            return ""
        blob = json.dumps([self.rules, self.only, self.unknown_out, self.dates, self._naming],
                          sort_keys=True).lower().encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:16]

    def day_sig(self):
        """A fingerprint of the settings that work by day (account, dates) only.
        A digest is stamped with it: digests counted under other day settings
        are not reused, while a renamed project does not make them stale."""
        if not self.by_day:
            return ""
        # ACCOUNT_RULES changes when the way sessions are tagged with an account
        # changes, so digests counted under the old tags are worked out again.
        blob = json.dumps([self.only, self.unknown_out, self.dates, ACCOUNT_RULES],
                          sort_keys=True).lower().encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:16]

    def date_hidden(self, day):
        for lo, hi in self.dates:
            if (lo is None or day >= lo) and (hi is None or day <= hi):
                return True
        return False

    def resolver(self):
        if self._resolver is None:
            import accounts
            self._resolver = accounts.Resolver()
        return self._resolver

    def gate(self, sid, transcript=None):
        """The per-day test for one session, or None when no rule works by day."""
        if not self.by_day:
            return None
        who = None
        if self.only:
            try:
                who = self.resolver().session(sid, transcript)
            except Exception:
                import accounts
                who = accounts.Session()   # evidence could not be read: an unknown session
        return Gate(self, sid, who)

    def gone_ids(self):
        """First 8 characters of the sessions the ledger lists as left out (read once)."""
        if self._gone_ids is None:
            ids = set()
            try:
                with open(os.path.join(C.coach_home(), "ledger.json"), "rb") as fh:
                    raw = json.loads(fh.read().decode("utf-8", "replace"))
                ids = set(x for x in raw.get("excluded") or [] if isinstance(x, str))
            except (OSError, ValueError, AttributeError, TypeError):
                pass
            self._gone_ids = ids
        return self._gone_ids

    def hidden(self, acc, pdir, project=None):
        """True when an exclude rule covers this session (by folder, title, id or project)."""
        if not self.rules:
            return False
        texts = [getattr(acc, "cwd", None) or "", pdir or ""]
        texts += list(getattr(acc, "custom_titles", ())) + list(getattr(acc, "titles", ()))
        hit = PR.excluded(self.rules, acc.id, texts)
        if not hit and project:
            hit = PR.excluded(self.rules, "", [project])
        if hit:
            self.stats["rule"].add(acc.id)
            self.gone.add(acc.id)
        return hit

    def emptied(self, sid, gate):
        """Note a session that a day rule left with nothing. Returns True when it did."""
        if gate is not None and gate.dropped:
            self.gone.add(sid)
            return True
        return False

    def counts(self):
        """Numbers only: how many sessions each kind of rule touched."""
        out = dict((k, len(v)) for k, v in self.stats.items())
        out["sessions_gone"] = len(self.gone)
        return out


def stored(digest, findings=None, day=None):
    """A digest (and its findings) read from disk, without work that may not be shown now.

    A digest written before a rule existed still holds that work. Returns
    (digest, findings); (None, None) for a day in exclude_dates or when the
    digest cannot be checked.
    """
    try:
        import build_site as B
        hide = B.load_hide()
        if day and hide.scope.date_hidden(day):
            return None, None
        if not isinstance(digest, dict):
            return digest, findings
        if hide.scope.by_day and (digest.get("scope_sig") or "") != hide.scope.day_sig():
            # (with no account or date setting in force, a digest counted under
            # one only holds less than may be shown, so it is still usable)
            return None, None          # counted under other account or date settings
        if not hide.on:
            return digest, findings
        clean, gone = B.visible_digest(digest, hide)
        return clean, B.visible_findings(findings, hide.plus(gone))
    except Exception:
        return None, None


def from_args(scope=None, exclude=None):
    """The scope a scan runs under: the one given, or one built from config.json."""
    if scope is not None:
        return scope
    return Scope(exclude=exclude)
