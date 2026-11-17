#!/usr/bin/env python3
"""Work out which project a session belongs to. Python 3 stdlib only.

Order, first hit wins:
  a. the person's own rules (config.json key "projects": a list of
     {"name": "...", "match": ["substring or glob", ...]}), tested against the
     session's folder path and its title, without regard to case;
  b. a folder inside a git checkout or worktree: the main repository's folder
     name (read from the .git entry while the folder still exists, otherwise
     taken from well-known worktree path shapes);
  c. the folder's own name;
  d. no folder: the session's title, else "No folder".
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


def by_rule(rules, folder, title):
    for rule in rules or []:
        if not isinstance(rule, dict) or not rule.get("name"):
            continue
        match = rule.get("match") or []
        for pat in [match] if isinstance(match, str) else match:
            if _matches(pat, folder) or _matches(pat, title):
                return str(rule["name"])
    return None


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


def resolve(folder, title=None, rules=None, no_folder=False):
    """The project name for one session."""
    folder = folder or ""
    if no_folder or "/scratch-workspaces/" in folder:
        folder = ""
    name = by_rule(rules, folder, title)
    if not name and folder:
        name = by_folder(os.path.expanduser(folder))
    if not name:
        name = (title or "")[:200]
    # one line, no table bars: the name goes into report lines and a markdown table
    name = " ".join(str(name).replace("|", "/").split())[:60]
    return name or NO_FOLDER
