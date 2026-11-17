#!/usr/bin/env python3
"""PermissionRequest hook: log each permission prompt, decide nothing.

Transcripts record denials but not prompts you approved, so without this log
the "frequent permission prompt" detector only sees half the picture.

stdin : PermissionRequest hook JSON (session_id, tool_name, tool_input, ...).
stdout: nothing, so the normal permission prompt is left unchanged.
exit  : always 0.

Appends one line per prompt to ~/.claude/coach/permission-prompts.jsonl.
Respects the same kill switch as nudge.py (~/.claude/coach/off).
"""
import json
import os
import sys
import time

MAX_LOG_BYTES = 5 * 1024 * 1024


def main():
    try:
        claude = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
        home = os.environ.get("COACH_HOME") or os.path.join(claude, "coach")
        if os.path.exists(os.path.join(home, "off")):
            os._exit(0)
        d = json.load(sys.stdin)
        inp = d.get("tool_input") if isinstance(d.get("tool_input"), dict) else {}
        cmd = inp.get("command") or inp.get("file_path") or inp.get("url") or ""
        rec = {"ts": int(time.time()), "session": str(d.get("session_id") or "")[:64],
               "tool": str(d.get("tool_name") or "?")[:80], "cmd": str(cmd)[:300],
               "mode": d.get("permission_mode")}
        if not os.path.isdir(home):
            os.makedirs(home)
        path = os.path.join(home, "permission-prompts.jsonl")
        try:
            if os.stat(path).st_size > MAX_LOG_BYTES:
                os.replace(path, path + ".1")
        except OSError:
            pass
        with open(path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except BaseException:
        pass
    os._exit(0)


if __name__ == "__main__":
    main()
