#!/usr/bin/env python3
"""Stop hook: print one coaching line when something costly just happened.

Contract (verified against https://code.claude.com/docs/en/hooks, Stop section
and JSON output section):
  stdin : JSON with session_id, transcript_path, stop_hook_active, ...
  stdout: nothing, or exactly one JSON object {"systemMessage": "..."}.
          systemMessage is shown to the user. It does not block the stop and
          is not fed back to the model. We never send "decision", "continue"
          or "additionalContext", which would keep Claude working.
  exit  : always 0.

Rules: silent unless a cheap detector fires, one line per turn, each pattern at
most once per session. Create ~/.claude/coach/off to switch it off.

Speed: only the bytes added since the last run are read (offset kept in
~/.claude/coach/state/<session>.json). On first sight of a big transcript only
the tail is read. Transcript text is data and is never acted on.
"""
import json
import os
import sys
import time

FIRST_READ_BYTES = 4 * 1024 * 1024    # first run: look at the last 4 MB only
MAX_STEP_BYTES = 16 * 1024 * 1024     # more new data than this: skip to the tail
BIG_RESULT_CHARS = 20000
STATE_KEEP_DAYS = 30
WANTED = (b'"type":"user"', b'"type":"assistant"', b'"compact_boundary"', b'"queued_command"')


def _new_state():
    return {"v": 1, "offset": 0, "nudged": [], "turn": 0, "fails": {}, "pending": {},
            "corr": [], "max_corr": 0, "corrections": 0, "interruptions": 0,
            "compactions": 0, "big": [], "big_chars": 0, "big_count": 0,
            "denied": {}, "peak": 0}


def _load_state(path):
    try:
        with open(path) as fh:
            st = json.load(fh)
        if isinstance(st, dict) and st.get("v") == 1:
            base = _new_state()
            base.update(st)
            return base
    except (OSError, ValueError):
        pass
    return None


def _save_state(path, st):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(st, fh, separators=(",", ":"))
    os.replace(tmp, path)


def _prune(state_dir):
    cutoff = time.time() - STATE_KEEP_DAYS * 86400
    for name in os.listdir(state_dir):
        p = os.path.join(state_dir, name)
        try:
            if os.stat(p).st_mtime < cutoff:
                os.remove(p)
        except OSError:
            pass


def _prompt(st, C, text):
    text = C.strip_noise(text)
    if not text:
        return
    st["turn"] += 1
    corr = 1 if C.is_correction(text) else 0
    st["corr"] = (st["corr"] + [corr])[-5:]
    if corr:
        st["corrections"] += 1
    # the live window, not the all-time peak: a loop that ended needs no nudge
    st["max_corr"] = sum(st["corr"])


def _feed(st, C, d):
    t = d.get("type")
    if d.get("isSidechain"):
        return
    if t == "assistant":
        m = d.get("message") or {}
        u = m.get("usage")
        if isinstance(u, dict):
            ctx = ((u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
                   + (u.get("cache_creation_input_tokens") or 0))
            if ctx > st["peak"]:
                st["peak"] = ctx
        for b in m.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                name = b.get("name") or "?"
                st["pending"][str(b.get("id"))] = [name, C.tool_summary(name, b.get("input"))]
        while len(st["pending"]) > 60:
            st["pending"].pop(next(iter(st["pending"])))
    elif t == "user":
        content = (d.get("message") or {}).get("content")
        texts = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_result":
                    _result(st, C, d, b)
                elif b.get("type") == "text":
                    texts.append(b.get("text") or "")
        raw = "\n".join(texts).strip()
        if not raw:
            return
        if raw.startswith(C.INTERRUPT_PREFIX):
            st["interruptions"] += 1
            return
        if d.get("isMeta") or d.get("isCompactSummary") or d.get("isVisibleInTranscriptOnly"):
            return
        origin = d.get("origin")
        if isinstance(origin, dict):
            if origin.get("kind") != "human":
                return
        elif raw.startswith(C.NON_HUMAN_PREFIXES):
            return
        if raw.startswith("<"):
            return
        _prompt(st, C, raw)
    elif t == "attachment":
        a = d.get("attachment") or {}
        if a.get("type") == "queued_command" and isinstance(a.get("prompt"), str):
            if (a.get("origin") or {}).get("kind", "human") == "human" and not a["prompt"].startswith("<"):
                _prompt(st, C, a["prompt"])
    elif t == "system" and d.get("subtype") == "compact_boundary":
        st["compactions"] += 1
        st["peak"] = 0   # context was just reset


def _result(st, C, d, b):
    name, summ = st["pending"].pop(str(b.get("tool_use_id")), ["?", ""])
    content = b.get("content")
    n = C.result_text_len(content)
    if n >= BIG_RESULT_CHARS:
        st["big_count"] += 1
        st["big_chars"] += n
        st["big"].append({"tool": C.short_tool(name), "what": summ[:80], "chars": n})
        st["big"] = sorted(st["big"], key=lambda r: -r["chars"])[:3]
    head = C.result_head(content)
    key = summ if name == "Bash" and summ else "%s: %s" % (C.short_tool(name), C.error_kind(head))
    if not b.get("is_error"):
        if name == "Bash":
            st["fails"].pop(key, None)   # it worked in the end
        return
    kind = d.get("toolDenialKind")
    if not kind and head.startswith("The user doesn't want to proceed"):
        kind = "user-rejected"
    if kind:
        fam = C.command_family(summ) if name == "Bash" else ""
        dk = "%s|%s|%s" % (C.short_tool(name), fam, kind)
        st["denied"][dk] = st["denied"].get(dk, 0) + 1
        return
    rec = st["fails"].get(key)
    if rec is None:
        if len(st["fails"]) >= 150:
            st["fails"].pop(next(iter(st["fails"])))
        st["fails"][key] = [1, head[:100], st["turn"]]
    else:
        rec[0] += 1
        rec[2] = st["turn"]


def _read_new(path, st, C):
    """Feed the bytes added since the last run. Updates st['offset']."""
    size = os.stat(path).st_size
    off = st["offset"]
    fresh = off == 0
    skip_partial = False
    if off > size:                       # file was replaced or cut
        fresh_state = _new_state()
        fresh_state["nudged"] = st["nudged"]
        st.clear()
        st.update(fresh_state)
        off, fresh = 0, True
    if fresh and size > FIRST_READ_BYTES:
        off, skip_partial = size - FIRST_READ_BYTES, True
    elif size - off > MAX_STEP_BYTES:
        off, skip_partial = size - FIRST_READ_BYTES, True
    if off >= size:
        st["offset"] = size
        return
    with open(path, "rb") as fh:
        fh.seek(off)
        if skip_partial:
            off += len(fh.readline())
        for line in fh:
            if not line.endswith(b"\n"):
                break                    # half-written last line: pick it up next time
            off += len(line)
            hit = False
            for w in WANTED:
                if w in line:
                    hit = True
                    break
            if not hit:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict):
                _feed(st, C, d)
    st["offset"] = off


def _session_view(st, sid):
    """The slice of the digest shape that the cheap detectors read."""
    turn = st["turn"]
    repeated = [{"cmd": k, "fails": v[0], "err": v[1]}
                for k, v in st["fails"].items() if turn - v[2] <= 3]
    denied = []
    for k, n in st["denied"].items():
        tool, fam, kind = (k.split("|") + ["", "", ""])[:3]
        denied.append({"tool": tool, "cmd": fam, "kind": kind, "n": n})
    denied.sort(key=lambda x: -x["n"])
    return {
        "id": sid,
        "errors": {"repeated": repeated},
        "corrections": st["corrections"],
        "max_corrections_in_5_prompts": st["max_corr"],
        "interruptions": st["interruptions"],
        "compactions_total": st["compactions"],
        "tokens": {"peak_context": st["peak"]},
        "big_results": {"count": st["big_count"], "chars": st["big_chars"], "top": st["big"]},
        "denials": {"top": denied},
    }


def run(hook, coach_dir=None):
    """Returns the one-line message, or None. Never raises on bad input."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import common as C
    coach_dir = coach_dir or C.coach_home()
    if os.path.exists(os.path.join(coach_dir, "off")):
        return None
    if not isinstance(hook, dict) or hook.get("stop_hook_active"):
        return None
    path = os.path.expanduser(str(hook.get("transcript_path") or ""))
    if not path or not os.path.isfile(path):
        return None
    sid = "".join(ch for ch in str(hook.get("session_id") or "") if ch.isalnum() or ch in "-_")[:64]
    if not sid:
        sid = os.path.basename(path)[:-6][:64]
    state_dir = os.path.join(coach_dir, "state")
    state_path = os.path.join(state_dir, sid + ".json")
    st = _load_state(state_path)
    if st is None:
        if not os.path.isdir(state_dir):
            os.makedirs(state_dir)
        _prune(state_dir)
        st = _new_state()
    before = st["offset"]
    _read_new(path, st, C)
    message = None
    if st["offset"] != before:
        import signals
        found = [f for f in signals.run_session(_session_view(st, sid), cheap_only=True)
                 if f["pattern"] not in st["nudged"]]
        if found:
            found.sort(key=lambda f: signals.SEVERITY_ORDER.get(f["severity"], 9))
            f = found[0]
            st["nudged"].append(f["pattern"])
            message = "Coach: %s. %s" % (f["evidence"].rstrip("."), f["suggestion"])
            message = " ".join(message.split())
    st["updated"] = int(time.time())
    _save_state(state_path, st)
    return message


def main():
    try:
        try:
            hook = json.load(sys.stdin)
        except ValueError:
            hook = None
        message = run(hook)
        if message:
            sys.stdout.write(json.dumps({"systemMessage": message}) + "\n")
            sys.stdout.flush()
    except BaseException:  # a coaching hook must never break a session
        pass
    os._exit(0)


if __name__ == "__main__":
    main()
