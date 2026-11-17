#!/usr/bin/env python3
"""Deterministic detectors over the digest that extract.py writes.

Each detector is one function registered with @detector. It returns a list of
findings: {pattern, severity, evidence, session, suggestion}.

To add one: write a function that takes a session dict (scope="session") or
the whole digest (scope="digest"), decorate it, done. Mark it cheap=True if it
only needs the counters that nudge.py keeps, so the Stop hook can run it too.

Examples:
  python3 coach/signals.py digest.json
  python3 coach/extract.py --since 7d | python3 coach/signals.py - --format text
"""
import json
import sys

DETECTORS = []
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

# Thresholds in one place so they are easy to tune.
T = {
    "fail_repeats": 3,
    "fail_repeats_high": 6,
    "error_kind_repeats": 5,
    "corrections_in_5": 3,
    "giant_result_chars": 100000,      # about 25k tokens in one result
    "big_results_total_chars": 400000,
    "compactions": 2,
    "interruptions": 3,
    "peak_context": 500000,
    "serial_agents": 3,
    "solo_active_min": 60,
    "solo_tool_calls": 200,
    "repeat_prompt_sessions": 3,
    "denials_same": 3,
    "permission_prompts_same": 3,
    "sprawl_turns": 60,
    "sprawl_titles": 3,
    "sprawl_days": 3,
    "sprawl_min_turns": 20,
    "long_paste_chars": 1500,
    "idle_gap_min": 60,
    "idle_context": 200000,
    "unused_min_range_days": 6,
}


def detector(pattern, scope="session", cheap=False):
    def wrap(fn):
        DETECTORS.append({"pattern": pattern, "scope": scope, "cheap": cheap, "fn": fn})
        return fn
    return wrap


def finding(pattern, severity, evidence, session, suggestion):
    return {"pattern": pattern, "severity": severity, "evidence": evidence,
            "session": session, "suggestion": suggestion}


def _sid(s):
    return (s.get("id") or "")[:8]


def _where(s):
    start = s.get("start") or ""
    return "%s, %s" % (s.get("project") or "?", start[:10])


def _k(n):
    return "%dk" % int(round(n / 1000.0))


# ---------- session detectors ----------

@detector("repeated_failing_command", cheap=True)
def repeated_failing_command(s):
    out = []
    rep = list(((s.get("errors") or {}).get("repeated")) or [])
    for r in ((s.get("subagents") or {}).get("repeated_fails")) or []:
        r = dict(r)
        r["sub"] = True
        rep.append(r)
    rep = [r for r in rep if r.get("fails", 0) >= T["fail_repeats"]]
    if not rep:
        return out
    worst = max(rep, key=lambda r: r["fails"])
    sev = "high" if worst["fails"] >= T["fail_repeats_high"] else "medium"
    who = "a subagent" if worst.get("sub") else "Claude"
    more = " (%d other calls also failed 3+ times)" % (len(rep) - 1) if len(rep) > 1 else ""
    out.append(finding(
        "repeated_failing_command", sev,
        "%s made the same call and it failed %d times: `%s`%s" % (
            who, worst["fails"], worst["cmd"][:90], more),
        _sid(s),
        "After the second identical failure, say: stop retrying, read the error and tell me the "
        "cause first. If this command fails for a known reason, put the fix in CLAUDE.md."))
    return out


@detector("repeated_error_kind")
def repeated_error_kind(s):
    kinds = (s.get("errors") or {}).get("kinds") or {}
    out = []
    for kind, n in kinds.items():
        if n >= T["error_kind_repeats"] and not kind.startswith("Exit code"):
            out.append(finding(
                "repeated_error_kind", "medium" if n >= 10 else "low",
                "The same kind of tool error came back %d times: \"%s\"" % (n, kind),
                _sid(s),
                "Add one CLAUDE.md line that tells Claude the right way to do this, so it stops "
                "hitting the same wall."))
            break
    return out


@detector("correction_loop", cheap=True)
def correction_loop(s):
    n = s.get("max_corrections_in_5_prompts", 0)
    if n < T["corrections_in_5"]:
        return []
    return [finding(
        "correction_loop", "high" if n >= 4 else "medium",
        "%d of 5 prompts in a row were corrections (%d corrections in the session)" % (
            n, s.get("corrections", n)),
        _sid(s),
        "Rewind to before the first miss (Esc twice or /rewind) and restate the whole "
        "requirement once, or switch to plan mode so the approach is agreed before any edit.")]


@detector("giant_tool_result", cheap=True)
def giant_tool_result(s):
    big = s.get("big_results") or {}
    top = big.get("top") or []
    worst = max(top, key=lambda r: r.get("chars", 0)) if top else None
    if worst and worst["chars"] >= T["giant_result_chars"]:
        return [finding(
            "giant_tool_result", "high" if worst["chars"] >= 3 * T["giant_result_chars"] else "medium",
            "One %s result put about %s tokens into context (%s)" % (
                worst.get("tool"), _k(worst["chars"] / 4.0), worst.get("what") or "no label"),
            _sid(s),
            "Ask for a filtered read (grep, head, a line range) or hand the reading to an Explore "
            "subagent so only the conclusion comes back.")]
    if big.get("chars", 0) >= T["big_results_total_chars"]:
        return [finding(
            "giant_tool_result", "medium",
            "%d large tool results added about %s tokens in total" % (
                big.get("count", 0), _k(big["chars"] / 4.0)),
            _sid(s),
            "Send bulk reading and searching to subagents so the main session keeps only the "
            "conclusions.")]
    return []


@detector("multiple_compactions", cheap=True)
def multiple_compactions(s):
    n = s.get("compactions_total") or len(s.get("compactions") or [])
    if n < T["compactions"]:
        return []
    return [finding(
        "multiple_compactions", "high" if n >= 3 else "medium",
        "The session compacted %d times" % n,
        _sid(s),
        "Each compaction drops detail. At a natural break, ask for a short handoff note and start "
        "a new session from it.")]


@detector("frequent_interruptions", cheap=True)
def frequent_interruptions(s):
    n = s.get("interruptions", 0)
    if n < T["interruptions"]:
        return []
    return [finding(
        "frequent_interruptions", "medium" if n >= 5 else "low",
        "You interrupted Claude %d times" % n,
        _sid(s),
        "Switch to plan mode for the next task of this kind: approve the plan first and "
        "there is less to interrupt.")]


@detector("context_bloat", cheap=True)
def context_bloat(s):
    peak = (s.get("tokens") or {}).get("peak_context", 0)
    if peak < T["peak_context"]:
        return []
    return [finding(
        "context_bloat", "medium",
        "Context reached about %s tokens, and every turn re-reads all of it" % _k(peak),
        _sid(s),
        "Run /compact with a focus line now, or start a new session for the next task.")]


@detector("permission_denials", cheap=True)
def permission_denials(s):
    top = (s.get("denials") or {}).get("top") or []
    out = []
    for d in top:
        if d.get("n", 0) >= T["denials_same"]:
            what = ("%s %s" % (d.get("tool"), d.get("cmd") or "")).strip()
            kind = d.get("kind")
            if kind == "user-rejected":
                tip = ("You keep rejecting the same kind of call. Add a CLAUDE.md rule or a deny "
                       "rule in permissions so Claude stops asking.")
            else:
                tip = ("If this action is safe, add an allow rule for it in settings.json "
                       "permissions. If it is not, add a CLAUDE.md line so Claude stops trying.")
            out.append(finding(
                "permission_denials", "medium",
                "`%s` was blocked %d times (%s)" % (what, d["n"], kind),
                _sid(s), tip))
            break
    return out


@detector("serial_agents")
def serial_agents(s):
    a = s.get("agents") or {}
    calls = a.get("calls", 0)
    if calls >= T["serial_agents"] and not a.get("parallel_batches") and not a.get("background"):
        return [finding(
            "serial_agents", "medium",
            "%d subagents were started one after another, never two at once" % calls,
            _sid(s),
            "When the parts do not depend on each other, ask for them in one message: "
            "start these as parallel agents now.")]
    if (not calls and not s.get("workflows")
            and s.get("active_min", 0) >= T["solo_active_min"]
            and s.get("tool_calls", 0) >= T["solo_tool_calls"]):
        return [finding(
            "serial_agents", "low",
            "%d minutes and %d tool calls in one thread with no subagents" % (
                s.get("active_min", 0), s.get("tool_calls", 0)),
            _sid(s),
            "Split independent parts (investigation, fixes, review) across parallel agents, or "
            "use a Workflow for a repeatable multi-step job.")]
    return []


@detector("sprawling_session")
def sprawling_session(s):
    if s.get("kind") == "scheduled":
        return []
    turns = s.get("turns", 0)
    titles = s.get("distinct_titles", 1)
    days = s.get("days", 1)
    wide = titles >= T["sprawl_titles"] or days >= T["sprawl_days"]
    if turns >= T["sprawl_turns"] or (wide and turns >= T["sprawl_min_turns"]):
        bits = ["%d prompts" % turns]
        if days > 1:
            bits.append("%d days" % days)
        if titles > 1:
            bits.append("%d different topics" % titles)
        comp = s.get("compactions_total") or len(s.get("compactions") or [])
        if comp:
            bits.append("%d compactions" % comp)
        return [finding(
            "sprawling_session", "medium" if (comp or turns >= 100) else "low",
            "One session covered %s" % ", ".join(bits),
            _sid(s),
            "Start a new session per task. Carry state over in a handoff file or memory, not in "
            "a week-long thread.")]
    return []


@detector("idle_then_resume")
def idle_then_resume(s):
    gaps = (s.get("idle_gaps") or {}).get("longest") or []
    long_gaps = [g for g in gaps if g.get("min", 0) >= T["idle_gap_min"]]
    peak = (s.get("tokens") or {}).get("peak_context", 0)
    if not long_gaps or peak < T["idle_context"]:
        return []
    return [finding(
        "idle_then_resume", "low",
        "Came back after a %d minute break to a session holding up to %s tokens" % (
            max(g["min"] for g in long_gaps), _k(peak)),
        _sid(s),
        "The prompt cache expires during a long break, so the first turn back re-sends the whole "
        "context at full price. After a break, start fresh or /compact first.")]


@detector("high_error_rate")
def high_error_rate(s):
    total = (s.get("errors") or {}).get("total", 0)
    calls = s.get("tool_calls", 0)
    if total >= 15 and calls and total / float(calls) >= 0.2:
        return [finding(
            "high_error_rate", "low",
            "%d of %d tool calls failed" % (total, calls),
            _sid(s),
            "Ask Claude what keeps failing and whether a setup step (install, login, env var) "
            "belongs in CLAUDE.md or a SessionStart hook.")]
    return []


# ---------- digest detectors ----------

@detector("repeated_prompt", scope="digest")
def repeated_prompt(d):
    out = []
    for g in d.get("repeated_prompts") or []:
        if g.get("session_count", 0) < T["repeat_prompt_sessions"]:
            continue
        if g.get("avg_len", 0) >= T["long_paste_chars"]:
            continue
        out.append(finding(
            "repeated_prompt", "medium",
            "Typed in %d sessions (%d times): \"%s\"" % (
                g["session_count"], g.get("count", 0), g.get("text", "")[:110]),
            ",".join(g.get("sessions") or []),
            "If it is a standing rule, make it a CLAUDE.md line. If it is a routine, make it a "
            "skill or slash command and call it by name."))
    return out[:3]


@detector("repeated_long_paste", scope="digest")
def repeated_long_paste(d):
    out = []
    for g in d.get("repeated_prompts") or []:
        if g.get("avg_len", 0) >= T["long_paste_chars"] and g.get("count", 0) >= 2:
            out.append(finding(
                "repeated_long_paste", "medium",
                "A %d character prompt was pasted %d times: \"%s\"" % (
                    g["avg_len"], g["count"], g.get("text", "")[:80]),
                ",".join(g.get("sessions") or []),
                "Save it once as a file or a skill and point at it (@path or the skill name) "
                "instead of pasting."))
    return out[:2]


@detector("permission_prompts", scope="digest")
def permission_prompts(d):
    out = []
    for p in d.get("permission_prompts") or []:
        if p.get("n", 0) < T["permission_prompts_same"]:
            continue
        cmd = p.get("cmd") or ""
        if p.get("read_only") and cmd:
            out.append(finding(
                "permission_prompts", "medium",
                "Asked permission %d times for the read-only command `%s`" % (p["n"], cmd),
                "",
                "Add \"Bash(%s:*)\" to permissions.allow in ~/.claude/settings.json." % cmd))
        elif p.get("n", 0) >= 2 * T["permission_prompts_same"]:
            out.append(finding(
                "permission_prompts", "low",
                "Asked permission %d times for `%s`" % (p["n"], (p.get("tool", "") + " " + cmd).strip()),
                "",
                "Decide once: an allow rule if you always say yes, a deny rule if you never do."))
    return out[:4]


@detector("unused_installed", scope="digest")
def unused_installed(d):
    if (d.get("range") or {}).get("days", 0) < T["unused_min_range_days"]:
        return []
    inv = d.get("inventory") or {}
    tot = d.get("totals") or {}
    used = set()
    for name in list((tot.get("skills_used") or {}).keys()) + list((tot.get("slash_commands") or {}).keys()):
        name = name.lstrip("/")
        used.add(name)
        used.update(name.split(":"))
    idle_plugins = [p for p in inv.get("plugins_enabled") or [] if p.split("@")[0] not in used]
    idle_skills = [k for k in inv.get("user_skills") or [] if k not in used]
    listed = inv.get("skills_listed_in_sessions") or []
    idle_listed = [k for k in listed if k not in used and k.split(":")[-1] not in used]
    out = []
    if idle_plugins or idle_skills:
        names = idle_plugins + idle_skills
        out.append(finding(
            "unused_installed", "low",
            "Installed but not called in this range: %s" % ", ".join(names[:12]),
            "",
            "Try each on real work this week, or turn it off: every enabled skill costs context "
            "in every session."))
    if listed and len(idle_listed) >= 10 and not out:
        out.append(finding(
            "unused_installed", "low",
            "%d of %d skills offered in sessions were never called" % (len(idle_listed), len(listed)),
            "",
            "Skim the list once and disable the ones you will not use."))
    if not tot.get("plan_mode_used") and tot.get("turns", 0) >= 50:
        out.append(finding(
            "unused_feature", "low",
            "Plan mode was not used in %d prompts" % tot.get("turns", 0),
            "",
            "For any task with more than one reasonable approach, switch to plan mode first."))
    return out


# ---------- running ----------

def run_session(session, cheap_only=False):
    out = []
    for det in DETECTORS:
        if det["scope"] != "session" or (cheap_only and not det["cheap"]):
            continue
        try:
            out.extend(det["fn"](session) or [])
        except Exception:  # one bad detector must not hide the rest
            continue
    return out


def run(digest):
    out = []
    for s in digest.get("sessions") or []:
        out.extend(run_session(s))
    for det in DETECTORS:
        if det["scope"] != "digest":
            continue
        try:
            out.extend(det["fn"](digest) or [])
        except Exception:
            continue
    meta = dict((_sid(s), s) for s in digest.get("sessions") or [])
    for f in out:
        s = meta.get(f["session"])
        if s:
            f["evidence"] = "%s [%s]" % (f["evidence"], _where(s))
    out.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Run coaching detectors over a digest.")
    ap.add_argument("digest", nargs="?", default="-", help="digest JSON file, or - for stdin")
    ap.add_argument("--format", choices=("json", "text"), default="json")
    ap.add_argument("--out", help="write here instead of stdout")
    ap.add_argument("--list", action="store_true", help="list the detectors and exit")
    args = ap.parse_args(argv)
    if args.list:
        for det in DETECTORS:
            print("%-26s %-8s %s" % (det["pattern"], det["scope"], "cheap" if det["cheap"] else ""))
        return 0
    if args.digest == "-":
        digest = json.load(sys.stdin)
    else:
        with open(args.digest) as fh:
            digest = json.load(fh)
    findings = run(digest)
    if args.format == "text":
        lines = ["%-6s %s (%s): %s -> %s" % (f["severity"], f["pattern"], f["session"] or "all",
                                             f["evidence"], f["suggestion"]) for f in findings]
        text = "\n".join(lines) if lines else "nothing notable"
    else:
        text = json.dumps(findings, indent=1, ensure_ascii=False)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text + "\n")
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
