#!/usr/bin/env python3
"""Work out which project a session belongs to. Python 3 stdlib only.

Before any of that, two things from config.json:
  - "exclude": rules for work that must never show anywhere. A session that
    matches is dropped from every digest, total, ledger row and page (see
    `excluded`). Exclusion wins over everything below.
  - "session_projects": a map of session id (the full id, or its first 8 or
    more characters) to a project name. It wins over every rule below.

Order, first hit wins:
  a. the person's own rules (config.json key "projects": a list of
     {"name": "...", "match": ["substring or glob", ...]}), tested against the
     session's folder path and its title, without regard to case;
  b. a folder inside a git checkout or worktree: the main repository's folder
     name (read from the .git entry while the folder still exists, otherwise
     taken from well-known worktree path shapes);
  c. the folder's own name;
  d. no folder: the name in config.json key "other_project" when it is set,
     else the session's title, else "No folder".
Nothing here runs git or changes anything.
"""
import fnmatch
import os
import re

NO_FOLDER = "No folder"
HOME_FOLDER = "Home folder"
_GLOB_CHARS = re.compile(r"[*?\[]")
# <repo>/.claude/worktrees/<name>[/...] and <repo>/.worktrees/<name>[/...]
_WORKTREE_SHAPES = (re.compile(r"^(.*?)/\.claude/worktrees/[^/]+(?:/.*)?$"),
                    re.compile(r"^(.*?)/\.worktrees/[^/]+(?:/.*)?$"))
_cache = {}


def _matches(pattern, text):
    pattern = str(pattern).lower()
    text = (text or "").lower()
    if not pattern or not text:
        return False
    if _GLOB_CHARS.search(pattern):
        return fnmatch.fnmatchcase(text, pattern) or fnmatch.fnmatchcase(text, "*" + pattern + "*")
    return pattern in text


def by_rule(rules, folder, title, titles=()):
    """The first rule that matches the folder path, the title, or any other title
    the session carried (a session can be retitled while it runs)."""
    texts = [folder, title] + [t for t in titles or () if t and t != title]
    for rule in rules or []:
        if not isinstance(rule, dict) or not rule.get("name"):
            continue
        match = rule.get("match") or []
        for pat in [match] if isinstance(match, str) else match:
            if any(_matches(pat, t) for t in texts):
                return str(rule["name"])
    return None


MIN_ID = 8      # a session id prefix shorter than this is not accepted anywhere


def session_override(session_projects, session_id):
    """The project a session was pinned to by id in config.json, or None.

    Keys are full session ids or prefixes of at least 8 characters; the longest
    matching key wins.
    """
    sid = str(session_id or "").lower()
    best = None
    if not sid or not isinstance(session_projects, dict):
        return None
    for key, name in session_projects.items():
        key = str(key).strip().lower()
        if len(key) >= MIN_ID and sid.startswith(key) and isinstance(name, str) and name.strip():
            if best is None or len(key) > len(best[0]):
                best = (key, name.strip())
    return best[1] if best else None


def excluded(rules, session_id="", texts=()):
    """True when an exclude rule covers this session.

    rules: a list of {"match": ["substring or glob", ...]} and or
    {"sessions": ["id or prefix of 8 or more characters", ...]}. Match strings
    are tested without regard to case against every text given (the folder
    path, the titles). A match text of 8 or more characters also counts when the
    session id starts with it; shorter text is never tested against ids, so a
    short word cannot hide unrelated sessions whose id happens to contain it.
    """
    sid = str(session_id or "").lower()
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        for pat in rule.get("match") or []:
            if any(_matches(pat, t) for t in texts or ()):
                return True
            key = str(pat).strip().lower()
            if len(key) >= MIN_ID and sid and sid.startswith(key):
                return True                  # an id typed as a match text: a prefix only
        for key in rule.get("sessions") or []:
            key = str(key).strip().lower()
            if len(key) >= MIN_ID and sid and sid.startswith(key):
                return True
    return False


def text_hidden(rules, text, ids=()):
    """True when a line of an old report must not be shown: it contains an
    exclude match string, or cites an excluded session by its first 8 characters."""
    low = (text or "").lower()
    if not low:
        return False
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        for pat in rule.get("match") or []:
            if _matches(pat, low):
                return True
        for key in rule.get("sessions") or []:
            key = str(key).strip().lower()
            if len(key) >= MIN_ID and key[:MIN_ID] in low:
                return True
    return any(i and i.lower() in low for i in ids or ())


def _git_main(folder):
    """The main repository folder for a folder inside a checkout, or None."""
    cur = folder
    for _ in range(40):
        dot = os.path.join(cur, ".git")
        if os.path.isdir(dot):
            return cur
        if os.path.isfile(dot):
            try:
                with open(dot) as fh:
                    line = fh.read(2000).strip()
            except OSError:
                return cur
            m = re.match(r"^gitdir:\s*(.+)$", line)
            if m:
                gitdir = m.group(1).strip()
                if not os.path.isabs(gitdir):
                    gitdir = os.path.normpath(os.path.join(cur, gitdir))
                common = gitdir
                try:
                    with open(os.path.join(gitdir, "commondir")) as fh:
                        common = os.path.normpath(os.path.join(gitdir, fh.read(2000).strip()))
                except OSError:
                    hit = re.match(r"^(.*?)/\.git/(?:worktrees|modules)/", gitdir + "/")
                    if hit:
                        common = hit.group(1) + "/.git"
                if os.path.basename(common) == ".git":
                    return os.path.dirname(common)
                return common          # a bare repository
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent
    return None


def by_folder(folder):
    """Rules b and c for a folder path."""
    folder = folder.rstrip("/") or "/"
    if folder in _cache:
        return _cache[folder]
    name = None
    if os.path.isdir(folder):
        main = _git_main(folder)
        if main:
            name = os.path.basename(main.rstrip("/"))
            if name.endswith(".git"):
                name = name[:-4]
    if not name:
        for shape in _WORKTREE_SHAPES:
            m = shape.match(folder)
            if m and m.group(1):
                name = os.path.basename(m.group(1))
                break
    if not name:
        if os.path.normpath(folder) == os.path.normpath(os.path.expanduser("~")):
            name = HOME_FOLDER
        else:
            name = os.path.basename(folder) or folder
    _cache[folder] = name
    return name


def resolve(folder, title=None, rules=None, no_folder=False, session_id=None,
            session_projects=None, titles=(), other=None):
    """The project name for one session."""
    folder = folder or ""
    if no_folder or "/scratch-workspaces/" in folder:
        folder = ""
    name = session_override(session_projects, session_id) or by_rule(rules, folder, title, titles)
    if not name and folder:
        name = by_folder(os.path.expanduser(folder))
    if not name and other:
        name = other                  # no folder, no rule: one shared row for one-off chats
    if not name:
        name = (title or "")[:200]
    # one line, no table bars: the name goes into report lines and a markdown table
    name = " ".join(str(name).replace("|", "/").split())[:60]
    return name or NO_FOLDER
