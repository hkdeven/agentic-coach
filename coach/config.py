#!/usr/bin/env python3
"""Show or change the coach's per-person settings in <coach home>/config.json.

  python3 coach/config.py show
  python3 coach/config.py set page_url https://claude.ai/...   the person's own page link
  python3 coach/config.py unset page_url                       stop publishing
  python3 coach/config.py set notify true|false                push notification per run
  python3 coach/config.py set evidence_on_page true|false      session excerpts on the page
  python3 coach/config.py unset evidence_on_page               back to the default (off once published)
  python3 coach/config.py set session_project <id> <name>      pin one session to a project
  python3 coach/config.py unset session_project <id>
  python3 coach/config.py add exclude <text>                   never show work that matches
  python3 coach/config.py remove exclude <text>
  python3 coach/config.py set only_account <short id>|current  show one Claude account only
  python3 coach/config.py unset only_account
  python3 coach/config.py set unknown_account include|exclude  sessions with no account evidence
  python3 coach/config.py add exclude_dates <day or range>     2026-01-05, 2026-01-01..2026-01-31, ..2026-01-31
  python3 coach/config.py remove exclude_dates <day or range>
  python3 coach/config.py set other_project <name>             one row for chats with no folder
  python3 coach/config.py unset other_project
  python3 coach/config.py set display_name <text>              the name in the page title and header
  python3 coach/config.py unset display_name                   back to "Agentic Coach"

A short account id is the first 6 or more hex characters of the account id;
`current` takes it from the account this Claude Code session runs under (the
desktop app's own account in the Code tab), else from the Terminal login in
.claude.json. Full ids are never stored.

<id> is a session id or its first 8 or more characters. <text> is part of a
folder path, a title or a project name (a glob such as *client* also works),
or the first 8 or more characters of a session id.
Project rules ("projects") are edited in the file by hand. Keys this script
does not know are kept as they are.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import common as C  # noqa: E402

USAGE = ("use: show | set page_url <https://claude.ai/... link> | unset page_url | "
         "set notify true|false | set evidence_on_page true|false | unset evidence_on_page | "
         "set session_project <id> <name> | unset session_project <id> | "
         "add exclude <text> | remove exclude <text> | set only_account <short id>|current | "
         "unset only_account | set unknown_account include|exclude | "
         "add exclude_dates <day or range> | remove exclude_dates <day or range> | "
         "set other_project <name> | unset other_project | "
         "set display_name <text> | unset display_name")
ID_RE = re.compile(r"^[0-9A-Za-z-]{8,64}$")


def _read_raw():
    """The file's own keys. None when it exists but cannot be read as a JSON object."""
    try:
        with open(C.config_file()) as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _write(raw):
    path = C.config_file()
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as fh:
        fh.write(json.dumps(raw, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


def change(words):
    """Apply one change. Returns an error line, or None when it was applied."""
    raw = _read_raw()
    if raw is None:      # do not rewrite a damaged file: that would drop the page link
        return "%s is not valid JSON. Fix or delete it, then try again." % C.config_file()
    if len(words) == 3 and words[:2] == ["set", "page_url"]:
        if not C.valid_page_url(words[2]):
            return "not a claude.ai page link: %s" % words[2]
        raw["page_url"] = words[2]
    elif words == ["unset", "page_url"]:
        raw.pop("page_url", None)
    elif len(words) == 3 and words[:2] == ["set", "notify"] and words[2] in ("true", "false"):
        raw["notify"] = words[2] == "true"
    elif (len(words) == 3 and words[:2] == ["set", "evidence_on_page"]
          and words[2] in ("true", "false")):
        raw["evidence_on_page"] = words[2] == "true"
    elif words == ["unset", "evidence_on_page"]:
        raw.pop("evidence_on_page", None)
    elif len(words) >= 4 and words[:2] == ["set", "session_project"]:
        name = " ".join(" ".join(words[3:]).split())[:80]
        if not ID_RE.match(words[2]):
            return "not a session id (want the id or its first 8 or more characters): %s" % words[2]
        if not name:
            return USAGE
        pins = raw.get("session_projects")
        pins = pins if isinstance(pins, dict) else {}
        pins[words[2].lower()] = name
        raw["session_projects"] = pins
    elif len(words) == 3 and words[:2] == ["unset", "session_project"]:
        pins = raw.get("session_projects")
        if not isinstance(pins, dict) or not any(k.lower() == words[2].lower() for k in pins):
            return "no session_project entry for %s" % words[2]
        raw["session_projects"] = dict((k, v) for k, v in pins.items()
                                       if k.lower() != words[2].lower())
        if not raw["session_projects"]:
            del raw["session_projects"]
    elif len(words) >= 3 and words[1] == "exclude" and words[0] in ("add", "remove") \
            and "exclude" in raw and not C._clean_exclude(raw["exclude"])[1]:
        # never rewrite a value this script cannot read in full: that would drop it
        return ("the exclude key in %s has a part that cannot be used. Fix it by hand first; "
                "nothing was changed." % C.config_file())
    elif len(words) >= 3 and words[:2] == ["add", "exclude"]:
        text = " ".join(" ".join(words[2:]).split())
        if len(text) < 3:
            return "an exclude text needs at least 3 characters"
        rules = raw.get("exclude")
        rules = [r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else []
        if not any(text.lower() == str(m).lower() for r in rules for m in (r.get("match") or [])
                   if isinstance(r.get("match"), list)):
            for r in rules:
                if isinstance(r.get("match"), list):
                    r["match"].append(text)
                    break
            else:
                rules.append({"match": [text]})
        raw["exclude"] = rules
    elif len(words) >= 3 and words[:2] == ["remove", "exclude"]:
        text = " ".join(" ".join(words[2:]).split()).lower()
        rules = raw.get("exclude")
        rules = [r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else []
        hit = False
        kept = []
        for r in rules:
            for key in ("match", "sessions"):
                if isinstance(r.get(key), list):
                    left = [m for m in r[key] if str(m).lower() != text]
                    hit = hit or len(left) != len(r[key])
                    if left:
                        r[key] = left
                    else:
                        del r[key]
            if r.get("match") or r.get("sessions"):
                kept.append(r)
        if not hit:
            return "no exclude entry reads: %s" % text
        if kept:
            raw["exclude"] = kept
        else:
            raw.pop("exclude", None)
    elif len(words) == 3 and words[:2] == ["set", "only_account"]:
        value = words[2].strip().lower()
        if value == "current":
            import accounts
            who = accounts.session_account(os.environ.get("CLAUDE_CODE_SESSION_ID") or "")
            value = (who or {}).get("account") or ""
            if not value:
                return "the account of this session is not known (no account from Claude Code and no login in .claude.json)"
        if not re.fullmatch(r"[0-9a-f]{6,64}", value):
            return "not a short account id (want 6 or more hex characters, or: current)"
        raw["only_account"] = value[:6]          # the short form only, never a full id
    elif words == ["unset", "only_account"]:
        raw.pop("only_account", None)
    elif (len(words) == 3 and words[:2] == ["set", "unknown_account"]
          and words[2] in ("include", "exclude")):
        raw["unknown_account"] = words[2]
    elif len(words) == 3 and words[1] == "exclude_dates" and words[0] in ("add", "remove"):
        rng = C.parse_date_range(words[2])
        if not rng:
            return ("not a day or a range of days (want 2026-01-05, 2026-01-01..2026-01-31, "
                    "..2026-01-31 or 2026-01-01..): %s" % words[2])
        text = rng[0] if rng[0] == rng[1] else "%s..%s" % (rng[0] or "", rng[1] or "")
        dates = raw.get("exclude_dates")
        dates = [d for d in ([dates] if isinstance(dates, str) else dates or [])
                 if isinstance(d, str)] if isinstance(dates, (list, str)) else []
        same = [d for d in dates if C.parse_date_range(d) == rng]
        if words[0] == "add":
            if not same:
                dates.append(text)
        else:
            if not same:
                return "no exclude_dates entry reads: %s" % text
            dates = [d for d in dates if d not in same]
        if dates:
            raw["exclude_dates"] = dates
        else:
            raw.pop("exclude_dates", None)
    elif len(words) >= 3 and words[:2] == ["set", "other_project"]:
        name = " ".join(" ".join(words[2:]).replace("|", "/").split())[:60]
        if not name:
            return USAGE
        raw["other_project"] = name
    elif words == ["unset", "other_project"]:
        raw.pop("other_project", None)
    elif len(words) >= 3 and words[:2] == ["set", "display_name"]:
        name = C.clean_display_name(" ".join(words[2:]))
        if not name:
            return USAGE
        raw["display_name"] = name
    elif words == ["unset", "display_name"]:
        raw.pop("display_name", None)
    else:
        return USAGE
    _write(raw)
    return None


def main(argv=None):
    words = list(sys.argv[1:] if argv is None else argv)
    changed = bool(words) and words != ["show"]
    if changed:
        err = change(words)
        if err:
            sys.stderr.write("coach: %s\n" % err)
            return 2
    out = C.load_config()
    out["exclude_dates"] = [a if a == b else "%s..%s" % (a or "", b or "")
                            for a, b in out["exclude_dates"]]
    if changed:
        # after a change, counts only: what is left out is printed by `show` alone,
        # which the person runs themselves
        out["exclude"] = "%d rules" % sum(len(r.get("match") or []) + len(r.get("sessions") or [])
                                          for r in out["exclude"])
        out["exclude_dates"] = "%d ranges" % len(out["exclude_dates"])
        out["session_projects"] = "%d sessions" % len(out["session_projects"])
        out["only_account"] = "set" if out["only_account"] else None
    if out.pop("damaged", False):
        sys.stderr.write("coach: %s\n" % C.config_problem())
    out["config_file"] = C.config_file()
    sys.stdout.write(json.dumps(out, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
