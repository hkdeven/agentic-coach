#!/usr/bin/env python3
"""Show or change the coach's per-person settings in <coach home>/config.json.

  python3 coach/config.py show
  python3 coach/config.py set page_url https://claude.ai/...   the person's own page link
  python3 coach/config.py unset page_url                       stop publishing
  python3 coach/config.py set notify true|false                push notification per run

The file holds only these two keys. Other keys already in it are kept.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import common as C  # noqa: E402

USAGE = "use: show | set page_url <https://claude.ai/... link> | unset page_url | set notify true|false"


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
    else:
        return USAGE
    _write(raw)
    return None


def main(argv=None):
    words = list(sys.argv[1:] if argv is None else argv)
    if words and words != ["show"]:
        err = change(words)
        if err:
            sys.stderr.write("coach: %s\n" % err)
            return 2
    out = C.load_config()
    out["config_file"] = C.config_file()
    sys.stdout.write(json.dumps(out, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
