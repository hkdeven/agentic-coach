"""Shared helpers for agentic-coach. Python 3 stdlib only.

Transcript text is data. Nothing here ever acts on what a transcript says.
"""
import calendar
import json
import os
import re
import time

HOME = os.path.expanduser("~")


def claude_dir():
    """The current user's Claude Code folder: $CLAUDE_CONFIG_DIR, else ~/.claude."""
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")


PROJECTS_DIR = os.path.join(claude_dir(), "projects")


def coach_home():
    """Runtime folder for state, the kill switch, the permission log and reports.

    $COACH_HOME overrides it (tests use this). Never inside the plugin folder:
    the plugin cache path changes with every version.
    """
    return os.environ.get("COACH_HOME") or os.path.join(claude_dir(), "coach")


def reports_dir():
    """Where digests and reports are written: <coach home>/reports."""
    return os.path.join(coach_home(), "reports")


def site_file():
    """The coach page, one self-contained file: <coach home>/site/index.html."""
    return os.path.join(coach_home(), "site", "index.html")


def config_file():
    """Per-person settings: <coach home>/config.json. Absent means all defaults."""
    return os.path.join(coach_home(), "config.json")


PAGE_URL_PREFIX = "https://claude.ai/"


def valid_page_url(url):
    """Only a plain claude.ai link (letters, digits, / _ . -) counts as a page link."""
    return (isinstance(url, str) and len(url) <= 300
            and bool(re.fullmatch(r"https://claude\.ai/[A-Za-z0-9/_.-]+", url)))


def load_config():
    """{"page_url": str or None, "notify": bool, "projects": [rules]}. A missing or broken
    file is all defaults.

    projects: the person's own project rules, a list of
    {"name": "...", "match": ["substring or glob", ...]}; see projects.py.

    page_url: the person's own private page link, set only when they chose to
    publish the page. notify: send one push notification per run, default off.
    """
    cfg = {"page_url": None, "notify": False, "projects": []}
    try:
        with open(config_file()) as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return cfg
    if isinstance(raw, dict):
        if valid_page_url(raw.get("page_url")):
            cfg["page_url"] = raw["page_url"]
        cfg["notify"] = raw.get("notify") is True
        for rule in raw.get("projects") if isinstance(raw.get("projects"), list) else []:
            if (isinstance(rule, dict) and isinstance(rule.get("name"), str) and rule["name"].strip()
                    and isinstance(rule.get("match"), list)):
                match = [m for m in rule["match"] if isinstance(m, str) and m.strip()]
                if match:
                    cfg["projects"].append({"name": rule["name"].strip()[:80], "match": match})
    return cfg


# ---------- time ----------

def ts_epoch(ts):
    """'2026-11-16T10:55:49.778Z' (UTC) to epoch seconds. Returns None if bad."""
    try:
        return calendar.timegm((int(ts[0:4]), int(ts[5:7]), int(ts[8:10]),
                                int(ts[11:13]), int(ts[14:16]), int(ts[17:19])))
    except (ValueError, TypeError, IndexError):
        return None


def epoch_utc_iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch))


def epoch_local(epoch, fmt="%Y-%m-%d %H:%M"):
    return time.strftime(fmt, time.localtime(epoch))


def parse_when(text, now=None):
    """Local date or time to epoch seconds.

    Accepts: today, yesterday, now, Nd (N days ago, midnight), YYYY-MM-DD,
    YYYY-MM-DDTHH:MM.
    """
    now = time.time() if now is None else now
    t = text.strip().lower()
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    if t == "now":
        return now
    if t == "today":
        return midnight
    if t == "yesterday":
        t = "1d"
    m = re.match(r"^(\d+)d$", t)
    if m:
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - int(m.group(1)),
                            0, 0, 0, 0, 0, -1))
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[t ](\d{2}):(\d{2}))?$", t)
    if m:
        hh = int(m.group(4) or 0)
        mm = int(m.group(5) or 0)
        return time.mktime((int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            hh, mm, 0, 0, 0, -1))
    raise ValueError("cannot read date: %r" % text)


# ---------- prompt text ----------

_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_NOISE_BLOCK_RE = re.compile(
    r"<(local-command-caveat|local-command-stdout|local-command-stderr|"
    r"command-message|command-args|task-notification|ci-monitor-event|"
    r"user-prompt-submit-hook)>.*?</\1>", re.S)
_NOISE_SINGLE_RE = re.compile(r"<artifact-content-authored-by-others/>")
_WS_RE = re.compile(r"\s+")

INTERRUPT_PREFIX = "[Request interrupted by user"

# Text that sits in a user record but was not typed by the person.
NON_HUMAN_PREFIXES = (
    "<task-notification", "<local-command", "<command-name", "<scheduled-task",
    "<ci-monitor-event", "<system-reminder", "<artifact-content",
    "This session is being continued from a previous",
    INTERRUPT_PREFIX,
)


def strip_noise(text):
    """Remove system reminders and harness wrappers from a prompt."""
    if not text:
        return ""
    text = _REMINDER_RE.sub("", text)
    text = _NOISE_BLOCK_RE.sub("", text)
    text = _NOISE_SINGLE_RE.sub("", text)
    return text.strip()


def one_line(text, limit):
    text = _WS_RE.sub(" ", text or "").strip()
    if limit and len(text) > limit:
        return text[: max(0, limit - 3)] + "..."
    return text


_CORRECTION_RE = re.compile(
    r"^\W*(?:"
    r"no\b(?!\s+(?:problem|worries|rush|need))|nope\b|stop\b|wrong\b|incorrect\b|"
    r"that(?:'|’)?s\s+(?:not|wrong|incorrect)|that\s+is\s+(?:not|wrong)|"
    r"this\s+is\s+(?:not|wrong|still)|"
    r"not\s+what\b|i\s+said\b|i\s+told\s+you\b|i\s+asked\b|i\s+already\b|"
    r"as\s+i\s+said\b|like\s+i\s+said\b|again\b|once\s+again\b|"
    r"still\s+(?:not|broken|wrong|failing|the\s+same|doesn|isn|does\s+not)|"
    r"it(?:'|’)?s\s+still\b|it\s+still\b|"
    r"you\s+(?:didn|did\s+not|missed|forgot|ignored|broke|removed|keep|still)|"
    r"why\s+(?:did|are|do)\s+you\b|undo\b|revert\b|"
    r"don(?:'|’)?t\b|do\s+not\b|never\s+mind\b|"
    r"you(?:'|’)?re\s+(?:wrong|not)|didn(?:'|’)?t\s+work|does\s+not\s+work|"
    r"doesn(?:'|’)?t\s+work"
    r")", re.I)


def is_correction(text):
    """Heuristic: does this prompt open by correcting the previous answer?"""
    if not text:
        return False
    return bool(_CORRECTION_RE.match(text[:80]))


_NORM_RE = re.compile(r"[^a-z0-9]+")


def norm_prompt(text):
    """Lowercase letters and digits only, single spaces. For near-match keys."""
    return _NORM_RE.sub(" ", (text or "").lower()).strip()


# ---------- commands ----------

_CD_PREFIX_RE = re.compile(r"^cd\s+(?:\"[^\"]*\"|'[^']*'|\S+)\s*(?:&&|;)\s*")
_ENV_PREFIX_RE = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")


def norm_command(cmd, limit=160):
    """Collapse a shell command to a stable short key."""
    cmd = _WS_RE.sub(" ", cmd or "").strip()
    cmd = _CD_PREFIX_RE.sub("", cmd)
    return cmd[:limit]


_TWO_WORD = {"git", "npm", "pnpm", "yarn", "npx", "docker", "kubectl", "supabase",
             "firebase", "brew", "cargo", "go", "uv", "pip", "pip3", "vercel",
             "netlify", "az", "aws", "gcloud", "xcrun", "make"}
_THREE_WORD = {"gh"}


def command_family(cmd):
    """'gh pr view 12 --json x' to 'gh pr view'. Used for allowlist entries."""
    cmd = norm_command(cmd, 400)
    cmd = _ENV_PREFIX_RE.sub("", cmd)
    parts = cmd.split(" ")
    if not parts or not parts[0]:
        return ""
    head = parts[0]
    want = 3 if head in _THREE_WORD else 2 if head in _TWO_WORD else 1
    out = []
    for p in parts[:want]:
        if out and (p.startswith("-") or not re.match(r"^[A-Za-z0-9_.:/-]+$", p)):
            break
        out.append(p)
    return " ".join(out)


_READ_ONLY_FAMILIES = {
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "stat", "du", "df",
    "tree", "file", "which", "pwd", "echo", "jq", "date", "whoami", "sort", "uniq",
    "diff", "basename", "dirname", "realpath", "env", "printenv", "ps", "lsof",
    "git status", "git log", "git diff", "git show", "git branch", "git remote",
    "git rev-parse", "git ls-files", "git blame", "git stash list", "git worktree",
    "git fetch", "git describe", "git tag",
    "gh pr view", "gh pr list", "gh pr checks", "gh pr diff", "gh pr status",
    "gh run list", "gh run view", "gh run watch", "gh issue view", "gh issue list",
    "gh repo view", "gh repo list", "gh api", "gh auth status", "gh release list",
    "gh release view", "gh workflow list", "gh workflow view",
    "npm ls", "npm view", "npm outdated", "npm list", "pnpm ls", "pnpm list",
    "docker ps", "docker images", "docker logs", "brew list", "brew info",
    "supabase status", "firebase projects:list",
}
_UNSAFE_CHARS_RE = re.compile(r"(?:>|\brm\b|\bmv\b|\bsudo\b|-X\s*(?:POST|PUT|PATCH|DELETE)|"
                              r"--method\s+(?:POST|PUT|PATCH|DELETE)|\bxargs\b|\|\s*sh\b)")


def is_read_only(cmd):
    """Conservative guess that a shell command only reads."""
    fam = command_family(cmd)
    if fam not in _READ_ONLY_FAMILIES:
        return False
    return not _UNSAFE_CHARS_RE.search(cmd or "")


_MCP_RE = re.compile(r"^mcp__(.+?)__(.+)$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-")


def short_tool(name):
    """Group MCP tools by server so counts stay small."""
    m = _MCP_RE.match(name or "")
    if not m:
        return name or "?"
    server = m.group(1)
    if _UUID_RE.match(server):
        server = server[:6]
    return "mcp:" + server


def result_text_len(content):
    """Characters of text in a tool_result content. Images are not counted."""
    if isinstance(content, str):
        return len(content)
    n = 0
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                n += len(b.get("text") or "")
    return n


def result_head(content, limit=120):
    if isinstance(content, str):
        return one_line(content[:400], limit)
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                return one_line((b.get("text") or "")[:400], limit)
    return ""


_DIGITS_RE = re.compile(r"\d+")


def error_kind(text):
    """First words of an error with numbers blanked, to group similar errors."""
    t = one_line((text or "")[:200], 0)
    t = t.replace("<tool_use_error>", "").strip()
    return _DIGITS_RE.sub("N", t)[:48]


def tool_summary(name, inp):
    """Short human label for a tool call: the command, the file, the query."""
    if not isinstance(inp, dict):
        return ""
    if name == "Bash":
        return norm_command(inp.get("command") or "")
    for k in ("file_path", "path", "url", "pattern", "query", "description", "skill"):
        v = inp.get(k)
        if isinstance(v, str) and v:
            if k == "file_path":
                v = "/".join(v.split("/")[-2:])
            return one_line(v, 120)
    return ""
