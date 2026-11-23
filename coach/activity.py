#!/usr/bin/env python3
"""What kind of work a turn was, worked out from its tools and its prompt. No model call.

A turn is one prompt and everything Claude did until the next prompt. Each
turn gets exactly one of thirteen activity types (CATEGORIES). The rules are
those of the open-source CodeBurn project (MIT licence,
https://github.com/getagentseal/codeburn, src/classifier.ts and
src/bash-utils.ts), written again here in Python; no CodeBurn code is copied.
Credit for the rule set goes to the CodeBurn authors.

The rules, in order:
  1. Tools decide first.
     a. EnterPlanMode was called: Planning.
     b. An agent was started (Agent or Task): Delegation.
     c. A shell command ran and nothing was edited: Testing, Git Ops or
        Build/Deploy when the prompt names tests, a git command, or a build or
        install command.
     d. Something was edited: Coding.
     e. Shell and file reads: Exploration. Shell alone: Coding.
     f. Web search, web fetch, tool search or an MCP tool: Exploration.
     g. File reads only: Exploration. Task list tools only: Planning.
     h. The Skill tool only: General.
  2. The prompt then refines two of those. Coding becomes Refactoring,
     Feature Dev or Debugging, whichever keyword comes first in the prompt
     (a tie goes to Refactoring, then Feature Dev, then Debugging).
     Exploration becomes Debugging when the prompt has a debugging word and no
     research word.
  3. A turn with no tools, or with tools none of the rules know, is read from
     the prompt alone: Brainstorming, Exploration, Feature Dev or Debugging
     (first keyword wins), Coding when it names a file type or a script, API
     or database, Exploration for a link, else Conversation.

First-try edits: a turn that edited a file is an edit turn. A retry is the
same file edited again with a shell command in between (Edit foo, Bash, Edit
foo). A different file after the shell command is not a retry, and neither is
a shell command that only reads (grep, cat, git log and the like). The
first-try rate is the share of edit turns with no retry.

The prompt is reduced to a small set of yes or no answers and positions
(prompt_flags) so that callers can keep a turn open across runs without
storing any prompt text.

Python 3 stdlib only.
"""
import hashlib
import os
import re

CATEGORIES = ("Coding", "Debugging", "Feature Dev", "Refactoring", "Testing", "Exploration",
              "Planning", "Delegation", "Git Ops", "Build/Deploy", "Brainstorming",
              "Conversation", "General")

_I = re.I
_TEST = re.compile(r"\b(test|pytest|vitest|jest|mocha|spec|coverage|npm\s+test|npx\s+vitest|"
                   r"npx\s+jest)\b", _I)
_GIT = re.compile(r"\bgit\s+(push|pull|commit|merge|rebase|checkout|branch|stash|log|diff|status|"
                  r"add|reset|cherry-pick|tag)\b", _I)
_BUILD = re.compile(r"\b(npm\s+run\s+build|npm\s+publish|pip\s+install|docker|deploy|make\s+build|"
                    r"npm\s+run\s+dev|npm\s+start|pm2|systemctl|brew|cargo\s+build)\b", _I)
_INSTALL = re.compile(r"\b(npm\s+install|pip\s+install|brew\s+install|apt\s+install|cargo\s+add)\b",
                      _I)
_DEBUG = re.compile(r"\b(fix|bug|error|broken|failing|crash|issue|debug|traceback|exception|"
                    r"stack\s*trace|not\s+working|wrong|unexpected|status\s+code|404|500|401|403)\b",
                    _I)
_FEATURE = re.compile(r"\b(add|create|implement|new|build|feature|introduce|set\s*up|scaffold|"
                      r"generate|make\s+(?:a|me|the)|write\s+(?:a|me|the))\b", _I)
_REFACTOR = re.compile(r"\b(refactor|clean\s*up|rename|reorganize|simplify|extract|restructure|"
                       r"move|migrate|split)\b", _I)
_BRAINSTORM = re.compile(r"\b(brainstorm|idea|what\s+if|explore|think\s+about|approach|strategy|"
                         r"design|consider|how\s+should|what\s+would|opinion|suggest|recommend)\b",
                         _I)
_RESEARCH = re.compile(r"\b(research|investigate|look\s+into|find\s+out|check|search|analyze|"
                       r"review|understand|explain|how\s+does|what\s+is|show\s+me|list|compare)\b",
                       _I)
_FILE = re.compile(r"\.(py|js|ts|tsx|jsx|json|yaml|yml|toml|sql|sh|go|rs|java|rb|php|css|html|md|"
                   r"csv|xml)\b", _I)
_SCRIPT = re.compile(r"\b(run\s+\S+\.\w+|execute|scrip?t|curl|api\s+\S+|endpoint|request\s+url|"
                     r"fetch\s+\S+|query|database|db\s+\S+)\b", _I)
_URL = re.compile(r"https?://\S+", _I)

EDIT_TOOLS = frozenset(("Edit", "Write", "FileEditTool", "FileWriteTool", "NotebookEdit",
                        "cursor:edit", "cursor:strreplace", "cursor:write", "edit", "write"))
READ_TOOLS = frozenset(("Read", "Grep", "Glob", "FileReadTool", "GrepTool", "GlobTool"))
BASH_TOOLS = frozenset(("Bash", "BashTool", "PowerShell", "PowerShellTool"))
TASK_TOOLS = frozenset(("TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskOutput",
                        "TaskStop", "TodoWrite"))
SEARCH_TOOLS = frozenset(("WebSearch", "WebFetch", "ToolSearch"))
AGENT_TOOLS = frozenset(("Agent", "Task"))
PLAN_TOOL = "EnterPlanMode"

# One letter per kind of tool, so a turn's tools fit in a short string.
EDIT, READ, BASH, TASK, SEARCH, MCP, SKILL, PLAN, AGENT, OTHER = "ERBTSMKPAO"

# The answers kept from a prompt, in this order (see prompt_flags).
FLAG_KEYS = ("test", "git", "build", "install", "brainstorm", "research", "file", "script", "url",
             "refactor", "feature", "debug")
NO_FLAGS = [0, 0, 0, 0, 0, 0, 0, 0, 0, -1, -1, -1]


def tool_kind(name):
    """The one-letter kind of a tool name."""
    name = name or ""
    if name == PLAN_TOOL:
        return PLAN
    if name in AGENT_TOOLS:
        return AGENT
    if name in EDIT_TOOLS:
        return EDIT
    if name in READ_TOOLS:
        return READ
    if name in BASH_TOOLS:
        return BASH
    if name in TASK_TOOLS:
        return TASK
    if name in SEARCH_TOOLS:
        return SEARCH
    if name.startswith("mcp__"):
        return MCP
    if name == "Skill":
        return SKILL
    return OTHER


def tool_kinds(names):
    """The kinds of a list of tool names, as a sorted string without repeats."""
    return "".join(sorted(set(tool_kind(n) for n in names or ())))


def _pos(rx, text):
    m = rx.search(text)
    return m.start() if m else -1


def prompt_flags(text):
    """Everything the rules need from a prompt, as a list of twelve numbers.

    The first nine are 1 or 0 (FLAG_KEYS order). The last three are where the
    first refactoring, feature and debugging keyword starts, or -1.
    """
    text = text or ""
    return [1 if rx.search(text) else 0 for rx in
            (_TEST, _GIT, _BUILD, _INSTALL, _BRAINSTORM, _RESEARCH, _FILE, _SCRIPT, _URL)] + \
           [_pos(_REFACTOR, text), _pos(_FEATURE, text), _pos(_DEBUG, text)]


def _flags(flags):
    if isinstance(flags, str):
        flags = prompt_flags(flags)
    if not isinstance(flags, (list, tuple)) or len(flags) != len(NO_FLAGS):
        flags = NO_FLAGS
    return dict(zip(FLAG_KEYS, flags))


def _first(candidates):
    """The name whose keyword starts earliest; the one listed first wins a tie."""
    best = None
    for order, (pos, name) in enumerate(candidates):
        if pos is None or pos < 0:
            continue
        if best is None or pos < best[0]:
            best = (pos, order, name)
    return best[2] if best else None


def _by_tools(kinds, f):
    if not kinds:
        return None
    if PLAN in kinds:
        return "Planning"
    if AGENT in kinds:
        return "Delegation"
    edits, reads, bash = EDIT in kinds, READ in kinds, BASH in kinds
    if bash and not edits:
        if f["test"]:
            return "Testing"
        if f["git"]:
            return "Git Ops"
        if f["build"] or f["install"]:
            return "Build/Deploy"
    if edits:
        return "Coding"
    if bash and reads:
        return "Exploration"
    if bash:
        return "Coding"
    if SEARCH in kinds or MCP in kinds:
        return "Exploration"
    if reads:
        return "Exploration"
    if TASK in kinds:
        return "Planning"
    if SKILL in kinds:
        return "General"
    return None


def _by_prompt(f):
    if f["brainstorm"]:
        return "Brainstorming"
    if f["research"]:
        return "Exploration"
    hit = _first([(f["feature"], "Feature Dev"), (f["debug"], "Debugging")])
    if hit:
        return hit
    if f["file"] or f["script"]:
        return "Coding"
    if f["url"]:
        return "Exploration"
    return "Conversation"


def classify(kinds, flags):
    """The activity type of one turn.

    kinds: the turn's tool kinds (tool_kinds), or a list of tool names.
    flags: prompt_flags of the prompt, or the prompt text itself.
    """
    if not isinstance(kinds, str):
        kinds = tool_kinds(kinds)
    f = _flags(flags)
    cat = _by_tools(kinds, f)
    if cat is None:
        return _by_prompt(f)
    if cat == "Coding":
        return _first([(f["refactor"], "Refactoring"), (f["feature"], "Feature Dev"),
                       (f["debug"], "Debugging")]) or "Coding"
    if cat == "Exploration" and not f["research"] and f["debug"] >= 0:
        return "Debugging"
    return cat


# ---------- shell commands that only read ----------

_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")
_SPLIT = re.compile(r"&&|;|\|")
_ASSIGN = re.compile(r"^\w+=")
_PREFIXES = frozenset(("sudo", "doas", "npx", "bunx", "time", "nice", "nohup", "stdbuf", "rtk"))
_READ_ONLY = frozenset(("rg", "grep", "egrep", "fgrep", "ag", "cat", "head", "tail", "less", "more",
                        "ls", "find", "fd", "tree", "wc", "stat", "file", "du", "df", "which",
                        "type", "pwd", "printenv", "env", "readlink", "realpath", "basename",
                        "dirname", "jq", "diff"))
_GIT_READS = frozenset(("log", "diff", "status", "show", "blame", "grep", "shortlog", "describe",
                        "rev-parse", "ls-files"))


def is_read_command(command):
    """True when every part of a shell command line only looks at things.

    One part that could change anything, or that is not known, makes the whole
    line a command that checks work, which is what a retry needs in between.
    """
    if not isinstance(command, str) or not command.strip():
        return False
    stripped = _QUOTED.sub(lambda m: " " * len(m.group(0)), command)
    saw = False
    for part in _SPLIT.split(stripped):
        tokens = part.split()
        i = 0
        while i < len(tokens) and (_ASSIGN.match(tokens[i])
                                   or os.path.basename(tokens[i]) in _PREFIXES):
            i += 1
        base = os.path.basename(tokens[i]) if i < len(tokens) else ""
        if not base:
            continue
        saw = True
        if base == "git":
            sub = tokens[i + 1] if i + 1 < len(tokens) else ""
            if sub not in _GIT_READS:
                return False
            continue
        if base not in _READ_ONLY:
            return False
    return saw


# ---------- first-try edits ----------

def new_retry_state():
    """Fresh state for one turn. Plain JSON, so it can be kept between runs."""
    return {"step": 0, "verify": -1, "edits": {}, "retries": 0, "has_edits": False, "last": None}


def file_key(path):
    """A short stand-in for a file path, so no path has to be stored."""
    if not isinstance(path, str) or not path:
        return "-"
    return hashlib.md5(path.encode("utf-8", "replace")).hexdigest()[:10]


def retry_feed(state, tool, file_path=None, command=None, step=None):
    """Fold one tool call into a turn's retry state. Returns the state.

    step: anything that names the assistant message the call came in. Calls
    of one message are one step, as they run side by side.
    """
    if step is None or step != state.get("last"):
        state["step"] += 1
        state["last"] = step
    i = state["step"]
    if tool in BASH_TOOLS:
        if not command or not is_read_command(command):
            state["verify"] = i
    if tool in EDIT_TOOLS:
        state["has_edits"] = True
        key = file_key(file_path)
        prev = state["edits"].get(key)
        if prev is not None and prev < state["verify"] < i:
            state["retries"] += 1
        state["edits"][key] = i
    return state


def count_retries(calls):
    """Retries in one turn. calls: [(tool, file path or None, command or None, step)]."""
    state = new_retry_state()
    for n, call in enumerate(calls):
        tool, path, command = call[0], call[1], call[2]
        retry_feed(state, tool, path, command, call[3] if len(call) > 3 else n)
    return state["retries"]


def first_try_rate(edit_turns, clean_turns):
    """Share of edit turns with no retry, or None when there were no edit turns."""
    if not edit_turns:
        return None
    return round(float(clean_turns) / edit_turns, 4)
