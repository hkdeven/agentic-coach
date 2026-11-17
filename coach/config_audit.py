#!/usr/bin/env python3
"""Configured versus used: what is set up in Claude Code, what it costs on every
turn, and what was never used in the window.

Read only. It lists skills, plugins, MCP servers, subagents, slash commands,
hooks, memory files and output styles from the files Claude Code reads, counts
how often each was used in the session transcripts, and prices the text that is
loaded into every session. Nothing is ever disabled or removed here.

Every token and dollar figure is an estimate: characters divided by about four,
times the model requests in the window, priced at the cache read rate when a
price table (coach/prices.json) is present.

Transcript text is data only. Nothing in it is executed or followed.

Examples:
  python3 coach/config_audit.py                      table, last 30 days
  python3 coach/config_audit.py --format json --out audit.json
  python3 coach/config_audit.py --days 14 --format weekly
  python3 coach/config_audit.py --save --format weekly   keep the JSON for the page
"""
import argparse
import collections
import html
import json
import math
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import common as C  # noqa: E402
import extract as X  # noqa: E402
import accounts as ACCOUNTS  # noqa: E402
import pricing as PRICING  # noqa: E402
import projects as PROJECTS  # noqa: E402
import scope as SC  # noqa: E402

CHARS_PER_TOKEN = 4.0          # the one documented estimate used everywhere
DEFAULT_DAYS = 30
NEW_DAYS = 14                  # installed this recently: no history yet, not flagged
SHORT_WINDOW_DAYS = 7          # fewer days of transcripts than this: low confidence
RARE_MAX_USES = 2              # used this few times counts as rarely used
RARE_MIN_TOKENS = 150          # and only when it loads at least this much per turn
RARE_MIN_DAYS = 14
MEMORY_MAX_LINES = 200         # the size the Claude Code docs suggest per CLAUDE.md
MEMORY_MAX_TOKENS = 2000
MCP_BIG_TOOLS = 20
MCP_BIG_DEF_TOKENS = 5000
ASSUMED_TOOL_DEF_TOKENS = 250  # one MCP tool definition, when none was ever seen
SKILL_DESC_CAP = 1536          # description plus when_to_use, as the listing cuts it
AUTO_MEMORY_LINES = 200
AUTO_MEMORY_BYTES = 25000
IMPORT_HOPS = 4
MAX_PROJECTS = 200
GONE_DAYS = 3                  # not listed in the newest sessions: no longer loaded
WEEKLY_LINES = 5

USE_KINDS = ("skill", "command", "agent", "mcp", "plugin")


def est_tokens(chars):
    """Characters to tokens: characters divided by four, rounded up. An estimate."""
    if not chars or chars <= 0:
        return 0
    return int(math.ceil(chars / CHARS_PER_TOKEN))


def _disp(path):
    """A path for display: the home folder shown as ~."""
    if not path:
        return path
    home = os.path.expanduser("~")
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def _day(ep):
    return C.epoch_local(ep, "%Y-%m-%d") if ep else None


def _iso_epoch(text):
    if not isinstance(text, str):
        return None
    return C.ts_epoch(text)


def _file_time(path):
    """When a file was created, or None where the system does not record it.

    The time a file last changed is not used: editing an old skill would make it
    look newly installed.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return getattr(st, "st_birthtime", None) or None


def _changed_time(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _norm_server(name):
    """MCP server name as it appears inside a tool name."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", name or "")


def repo_root(path):
    """The main checkout a folder belongs to, so worktrees count as one project."""
    folder = path
    for _ in range(40):
        dot = os.path.join(folder, ".git")
        if os.path.isdir(dot):
            return folder
        if os.path.isfile(dot):
            try:
                with open(dot) as fh:
                    line = fh.read(2000).strip()
            except OSError:
                return folder
            marker = os.sep + ".git" + os.sep + "worktrees" + os.sep
            if line.startswith("gitdir:") and marker in line:
                return line[len("gitdir:"):].strip().split(marker)[0]
            return folder
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return path


def _under(path, folder):
    if not path or not folder:
        return False
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


# ---------- small readers ----------

class Inventory(object):
    """What is configured. items are JSON-serialisable dicts."""

    def __init__(self):
        self.items = []
        self.problems = []
        self.settings = {}      # scope label -> settings dict that was read
        self.projects = []
        self.tool_search_off = False
        self._mem_seen = {}

    def problem(self, path, why):
        self.problems.append({"path": _disp(path), "problem": why})

    def read_text(self, path, limit=2000000):
        try:
            with open(path, "rb") as fh:
                raw = fh.read(limit)
        except OSError:
            return None
        return raw.decode("utf-8", "replace")

    def read_json(self, path):
        """A JSON object, or None. A file that exists but cannot be read is a problem."""
        if not os.path.exists(path):
            return None
        try:
            with open(path, "rb") as fh:
                data = json.loads(fh.read().decode("utf-8", "replace"))
        except OSError as exc:
            self.problem(path, "cannot be read: %s" % exc.__class__.__name__)
            return None
        except (ValueError, RecursionError):
            self.problem(path, "is not valid JSON")
            return None
        if not isinstance(data, dict):
            self.problem(path, "is not a JSON object")
            return None
        return data

    def add(self, kind, name, scope, path=None, project=None, plugin=None, chars=0,
            always_on=True, basis="", installed=None, **extra):
        item = collections.OrderedDict()
        item["kind"] = kind
        item["name"] = name
        item["scope"] = scope
        item["project"] = project
        item["plugin"] = plugin
        item["path"] = _disp(path)
        item["always_on"] = bool(always_on)
        item["chars"] = int(chars) if always_on and chars else 0
        item["tokens_per_turn"] = est_tokens(item["chars"])
        item["cost_basis"] = basis
        item["installed"] = _day(installed)
        item["_installed"] = installed
        if installed is None and path and kind in ("skill", "command", "agent"):
            item["_changed"] = _changed_time(path)
        item.update(extra)
        item["id"] = "%s:%s:%s:%s" % (kind, scope, project or plugin or "", name)
        self.items.append(item)
        return item


_UNQUOTE_RE = re.compile(r"^(['\"])(.*)\1$")
_KEY_RE = re.compile(r"^([A-Za-z0-9_-]+):\s*(.*)$")


def parse_frontmatter(text):
    """The simple key: value block between --- lines. Returns (fields, body)."""
    if not text or not text.startswith("---"):
        return {}, text or ""
    lines = text.split("\n")
    end = None
    for i in range(1, min(len(lines), 400)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None or lines[0].strip() != "---":
        return {}, text
    fields = {}
    key = None
    for ln in lines[1:end]:
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        m = _KEY_RE.match(ln)
        if m and not ln[0].isspace():
            key = m.group(1)
            val = m.group(2).strip()
            if val in (">", "|", ">-", "|-", ">+", "|+"):
                val = ""
            um = _UNQUOTE_RE.match(val)
            fields[key] = um.group(2) if um else val
        elif key is not None and ln[0].isspace():
            fields[key] = (fields[key] + " " + ln.strip()).strip()
    return fields, "\n".join(lines[end + 1:])


def _truthy(val):
    return str(val).strip().lower() in ("true", "yes", "1")


def _listing_chars(name, fields, body=""):
    """Characters one skill or command adds to the listing: '- name: description'."""
    desc = (fields.get("description") or "").strip()
    more = (fields.get("when_to_use") or "").strip()
    if more:
        desc = (desc + " " + more).strip()
    if not desc:
        for ln in body.split("\n"):
            if ln.strip():
                desc = ln.strip().lstrip("# ")
                break
    return len(name) + len(desc[:SKILL_DESC_CAP]) + 5


# ---------- inventory: one kind at a time ----------

def _skills_in(inv, folder, scope, project=None, plugin=None, prefix="", applies=None):
    if not os.path.isdir(folder):
        return
    if os.path.isfile(os.path.join(folder, "SKILL.md")):
        names = [("", folder)]
    else:
        try:
            names = [(n, os.path.join(folder, n)) for n in sorted(os.listdir(folder))
                     if not n.startswith(".")]
        except OSError:
            return
    for n, path in names:
        md = os.path.join(path, "SKILL.md")
        text = inv.read_text(md, 60000) if os.path.isfile(md) else None
        if text is None:
            continue
        fields, body = parse_frontmatter(text)
        name = prefix + (fields.get("name") or n or os.path.basename(path.rstrip(os.sep)))
        manual = _truthy(fields.get("disable-model-invocation"))
        inv.add("skill", name, scope, md, project, plugin,
                chars=_listing_chars(name, fields, body), always_on=not manual,
                basis=("not in the listing: only runs when typed" if manual
                       else "estimate: name plus description in the skill listing"),
                installed=_file_time(md), applies=applies)


def _commands_in(inv, folder, scope, project=None, plugin=None, prefix="", applies=None):
    if os.path.isfile(folder) and folder.endswith(".md"):
        files = [folder]
    elif os.path.isdir(folder):
        files = []
        for root, dirs, names in os.walk(folder):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            files.extend(os.path.join(root, n) for n in sorted(names) if n.endswith(".md"))
    else:
        return
    for path in files:
        text = inv.read_text(path, 60000)
        if text is None:
            continue
        fields, body = parse_frontmatter(text)
        name = prefix + os.path.basename(path)[:-3]
        manual = _truthy(fields.get("disable-model-invocation"))
        inv.add("command", name, scope, path, project, plugin,
                chars=_listing_chars(name, fields, body), always_on=not manual,
                basis=("not in the listing: only runs when typed" if manual
                       else "estimate: name plus description in the skill listing"),
                installed=_file_time(path), applies=applies)


def _agents_in(inv, paths, scope, project=None, plugin=None, prefix="", applies=None):
    files = []
    for folder in paths:
        if os.path.isfile(folder) and folder.endswith(".md"):
            files.append((folder, ""))
        elif os.path.isdir(folder):
            for root, dirs, names in os.walk(folder):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                rel = os.path.relpath(root, folder)
                sub = "" if rel == "." else rel.replace(os.sep, ":") + ":"
                files.extend((os.path.join(root, n), sub) for n in sorted(names)
                             if n.endswith(".md"))
    for path, sub in files:
        text = inv.read_text(path, 60000)
        if text is None:
            continue
        fields, _body = parse_frontmatter(text)
        if not fields.get("name") and not fields.get("description"):
            continue
        base = fields.get("name") or os.path.basename(path)[:-3]
        name = prefix + (sub if plugin else "") + base
        desc = fields.get("description") or ""
        inv.add("agent", name, scope, path, project, plugin,
                chars=len(name) + len(desc) + 5,
                basis="estimate: name plus description in the agent listing",
                installed=_file_time(path), applies=applies)


def _styles_in(inv, folder, scope, active, project=None, plugin=None, applies=None):
    if os.path.isfile(folder) and folder.endswith(".md"):
        files = [folder]
    elif os.path.isdir(folder):
        try:
            files = [os.path.join(folder, n) for n in sorted(os.listdir(folder))
                     if n.endswith(".md")]
        except OSError:
            return
    else:
        return
    for path in files:
        text = inv.read_text(path)
        if text is None:
            continue
        fields, body = parse_frontmatter(text)
        name = fields.get("name") or os.path.basename(path)[:-3]
        on = bool(active) and active in (name, os.path.basename(path)[:-3])
        inv.add("output_style", name, scope, path, project, plugin,
                chars=len(body), always_on=on,
                basis=("estimate: the selected style is part of every request" if on
                       else "not selected: costs nothing until it is picked"),
                installed=_file_time(path), selected=on, applies=applies)


def _hooks_from(inv, events, scope, path, project=None, plugin=None, applies=None, off=False):
    if not isinstance(events, dict):
        return
    for event in sorted(events):
        groups = events[event]
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            matcher = group.get("matcher")
            matcher = matcher if isinstance(matcher, str) else ""
            handlers = group.get("hooks")
            for h in handlers if isinstance(handlers, list) else []:
                if not isinstance(h, dict):
                    continue
                kind = str(h.get("type") or "command")
                cmd = h.get("command") or h.get("url") or h.get("prompt") or h.get("tool") or ""
                cmd = cmd if isinstance(cmd, str) else ""
                label = "%s%s: %s" % (event, ("(" + matcher + ")") if matcher else "",
                                      C.one_line(cmd, 70) or kind)
                inv.add("hook", label, scope, path, project, plugin, always_on=False,
                        basis="no load of its own: a hook adds to context only when it prints",
                        event=event, matcher=matcher, hook_type=kind,
                        command=C.one_line(cmd, 200), _cmd=C.one_line(cmd, 0),
                        status_message=(h.get("statusMessage")
                                        if isinstance(h.get("statusMessage"), str) else None),
                        enabled=not off, applies=applies)


def _mcp_from(inv, servers, scope, path, project=None, plugin=None, applies=None,
              enabled=True, note=None):
    if not isinstance(servers, dict):
        return
    for name in sorted(servers):
        cfg = servers[name] if isinstance(servers[name], dict) else {}
        key = _norm_server(name)
        if plugin:
            key = "plugin_%s_%s" % (_norm_server(plugin.split("@")[0]), key)
        inv.add("mcp", name, scope, path, project, plugin, always_on=enabled is True,
                basis="unknown until the server is seen in a session",
                server_key=key, transport=str(cfg.get("type") or
                                              ("stdio" if cfg.get("command") else "http")),
                always_load=bool(cfg.get("alwaysLoad")), enabled=enabled, note=note,
                applies=applies)


_FENCE_RE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.S | re.M)
_SPAN_RE = re.compile(r"`[^`\n]*`")
_IMPORT_RE = re.compile(r"(?:^|(?<=\s))@((?:\\ |[^\s\\])+)", re.M)


def find_imports(text, base_dir):
    """Files a memory file pulls in with @path. Code blocks and spans are skipped."""
    plain = _SPAN_RE.sub("", _FENCE_RE.sub("", text or ""))
    out = []
    for m in _IMPORT_RE.finditer(plain):
        raw = m.group(1).replace("\\ ", " ").rstrip(".,;:)")
        path = os.path.expanduser(raw)
        if not os.path.isabs(path):
            path = os.path.join(base_dir, path)
        path = os.path.normpath(path)
        if os.path.isfile(path) and path not in out:
            out.append(path)
    return out


def _memory(inv, path, scope, role, project=None, applies=None, hops=0, via=None,
            on_demand=False):
    """One memory file and, up to four hops, the files it imports."""
    if not os.path.isfile(path):
        return False
    real = os.path.realpath(path)
    seen = inv._mem_seen.get(real)
    if seen is not None:
        if applies and seen.get("applies") is not None:
            for a in applies:
                if a not in seen["applies"]:
                    seen["applies"].append(a)
        elif applies is None:
            seen["applies"] = None
        return True
    text = inv.read_text(path)
    if text is None:
        inv.problem(path, "cannot be read")
        return False
    fields, _body = parse_frontmatter(text) if role == "rule" else ({}, text)
    gated = on_demand or bool(fields.get("paths"))
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    item = inv.add("memory", _disp(path), scope, path, project, chars=len(text),
                   always_on=not gated,
                   basis=("loads on demand: path scoped" if gated
                          else "estimate: the whole file is read into every session"),
                   installed=_file_time(path), role=role, lines=lines,
                   file_chars=len(text), imported_by=_disp(via),
                   applies=list(applies) if applies else None)
    inv._mem_seen[real] = item
    if hops < IMPORT_HOPS:
        for imp in find_imports(text, os.path.dirname(path)):
            _memory(inv, imp, scope, "import", project, applies, hops + 1, path, gated)
    return True


def _rules_in(inv, folder, scope, project=None, applies=None):
    if not os.path.isdir(folder):
        return
    for root, dirs, names in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for n in sorted(names):
            if n.endswith(".md"):
                _memory(inv, os.path.join(root, n), scope, "rule", project, applies)


def _auto_memory(inv, projects_dir):
    try:
        names = sorted(os.listdir(projects_dir))
    except OSError:
        return
    for pdir in names:
        path = os.path.join(projects_dir, pdir, "memory", "MEMORY.md")
        text = inv.read_text(path) if os.path.isfile(path) else None
        if text is None:
            continue
        head = "\n".join(text.split("\n")[:AUTO_MEMORY_LINES])[:AUTO_MEMORY_BYTES]
        inv.add("memory", "auto memory: " + pdir.strip("-")[-40:], "auto", path,
                chars=len(head),
                basis="estimate: the first 200 lines or 25KB of MEMORY.md load at session start",
                installed=_file_time(path), role="auto", lines=text.count("\n") + 1,
                file_chars=len(text), pdir=pdir)


def _inside(root, rel):
    """A manifest path, only if it stays inside the plugin folder."""
    if not isinstance(rel, str) or not rel:
        return None
    path = os.path.normpath(os.path.join(root, rel))
    real_root = os.path.realpath(root)
    if not _under(os.path.realpath(path), real_root):
        return None
    return path


def _as_list(val):
    if val is None:
        return []
    return val if isinstance(val, list) else [val]


def _names(val):
    """The strings in a settings list, whatever else is in it."""
    return [x for x in _as_list(val) if isinstance(x, str)]


def _plugin_parts(inv, pid, root, manifest, active_style, applies=None):
    """The skills, commands, agents, hooks, MCP servers and styles one plugin brings."""
    pname = manifest.get("name") if isinstance(manifest.get("name"), str) else pid.split("@")[0]
    pre = pname + ":"
    kw = {"plugin": pid, "applies": applies}
    skill_dirs = [os.path.join(root, "skills")]
    skill_dirs += [p for p in (_inside(root, r) for r in _as_list(manifest.get("skills"))) if p]
    if (not os.path.isdir(skill_dirs[0]) and "skills" not in manifest
            and os.path.isfile(os.path.join(root, "SKILL.md"))):
        skill_dirs.append(root)
    done = set()
    for d in skill_dirs:
        if os.path.realpath(d) not in done:
            done.add(os.path.realpath(d))
            _skills_in(inv, d, "plugin", prefix=pre, **kw)
    cmds = manifest.get("commands")
    if isinstance(cmds, dict):
        for cname in sorted(cmds):
            spec = cmds[cname] if isinstance(cmds[cname], dict) else {}
            desc = spec.get("description") if isinstance(spec.get("description"), str) else ""
            inv.add("command", pre + cname, "plugin", os.path.join(root, ".claude-plugin"),
                    chars=len(pre + cname) + len(desc[:SKILL_DESC_CAP]) + 5,
                    basis="estimate: name plus description in the skill listing", **kw)
    elif cmds is not None:
        for p in (_inside(root, r) for r in _as_list(cmds)):
            if p:
                _commands_in(inv, p, "plugin", prefix=pre, **kw)
    else:
        _commands_in(inv, os.path.join(root, "commands"), "plugin", prefix=pre, **kw)
    if manifest.get("agents") is not None:
        agent_paths = [p for p in (_inside(root, r) for r in _as_list(manifest["agents"])) if p]
    else:
        agent_paths = [os.path.join(root, "agents")]
    _agents_in(inv, agent_paths, "plugin", prefix=pre, **kw)
    hook_file = os.path.join(root, "hooks", "hooks.json")
    data = inv.read_json(hook_file)
    if data:
        _hooks_from(inv, data.get("hooks"), "plugin", hook_file, **kw)
    for h in _as_list(manifest.get("hooks")):
        if isinstance(h, dict):
            _hooks_from(inv, h, "plugin", os.path.join(root, ".claude-plugin"), **kw)
        else:
            p = _inside(root, h)
            data = inv.read_json(p) if p and os.path.realpath(p) != os.path.realpath(hook_file) else None
            if data:
                _hooks_from(inv, data.get("hooks"), "plugin", p, **kw)
    mcp_file = os.path.join(root, ".mcp.json")
    data = inv.read_json(mcp_file)
    if data:
        servers = data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else data
        _mcp_from(inv, servers, "plugin", mcp_file, **kw)
    for m in _as_list(manifest.get("mcpServers")):
        if isinstance(m, dict):
            _mcp_from(inv, m, "plugin", os.path.join(root, ".claude-plugin"), **kw)
        elif isinstance(m, str) and m.endswith(".json"):
            p = _inside(root, m)
            data = inv.read_json(p) if p else None
            if data:
                servers = data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else data
                _mcp_from(inv, servers, "plugin", p, **kw)
    if manifest.get("outputStyles") is not None:
        for p in (_inside(root, r) for r in _as_list(manifest["outputStyles"])):
            if p:
                _styles_in(inv, p, "plugin", active_style, **kw)
    else:
        _styles_in(inv, os.path.join(root, "output-styles"), "plugin", active_style, **kw)


def _plugins(inv, claude, enabled_map, active_style):
    path = os.path.join(claude, "plugins", "installed_plugins.json")
    data = inv.read_json(path)
    plugins = (data or {}).get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}
    for pid in sorted(plugins):
        entries = plugins[pid]
        entries = entries if isinstance(entries, list) else [entries]
        entries = [e for e in entries if isinstance(e, dict)]
        if not entries:
            continue
        entry = entries[-1]
        state = enabled_map.get(pid)
        enabled = state[0] if state else True
        where = [e["projectPath"] for e in entries if isinstance(e.get("projectPath"), str)]
        if state and state[2]:
            applies = list(state[2])        # switched on in these projects only
        elif where and len(where) == len(entries):
            applies = where                 # installed for these projects only
        else:
            applies = None
        dates = [d for d in (_iso_epoch(e.get("installedAt")) for e in entries) if d]
        root = entry.get("installPath") if isinstance(entry.get("installPath"), str) else ""
        manifest = {}
        if enabled and root and os.path.isdir(root):
            manifest = inv.read_json(os.path.join(root, ".claude-plugin", "plugin.json")) or {}
        scopes = sorted(set(str(e.get("scope") or "user") for e in entries))
        item = inv.add("plugin", pid, "user" if "user" in scopes else scopes[0], root or None,
                       project=_disp(applies[0]) if applies and len(applies) == 1 else None,
                       always_on=bool(enabled),
                       basis="estimate: the sum of what its parts load",
                       installed=min(dates) if dates else None,
                       version=entry.get("version"), enabled=bool(enabled),
                       enabled_by=state[1] if state else "default (not set in enabledPlugins)",
                       applies=applies)
        if not enabled:
            item["cost_basis"] = "disabled: loads nothing"
            continue
        if not root or not os.path.isdir(root):
            inv.problem(root or path, "plugin folder for %s is missing" % pid)
            continue
        _plugin_parts(inv, pid, root, manifest, active_style, applies)
    for pid in sorted(enabled_map):
        if pid not in plugins and enabled_map[pid][0]:
            inv.add("plugin", pid, "settings", None, always_on=False,
                    basis="enabled in settings but not in installed_plugins.json",
                    enabled=True, enabled_by=enabled_map[pid][1], not_installed=True)


def managed_dir():
    """Where an organisation's managed files live on this system."""
    if sys.platform == "darwin":
        return "/Library/Application Support/ClaudeCode"
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), "ClaudeCode")
    return "/etc/claude-code"


class _Folder(object):
    """Just enough of a session for extract.Naming: a folder and no titles."""

    def __init__(self, cwd):
        self.cwd, self.id = cwd, ""
        self.titles, self.custom_titles, self.custom_title = [], [], None


def home_json_path(claude=None):
    """The .claude.json file: in the home folder, or inside the config folder when
    that is not the default ~/.claude."""
    if claude is None or os.path.normpath(claude) == os.path.normpath(C.claude_dir()):
        return ACCOUNTS.home_json_path()         # one rule, shared with accounts.py
    return os.path.join(claude, ".claude.json")


def _project(inv, proj, claude, active_style, home_entry):
    """Everything one project folder adds, when the folder still exists."""
    label = _disp(proj)
    applies = [proj]
    dot = os.path.join(proj, ".claude")
    same_as_user = os.path.realpath(dot) == os.path.realpath(claude)
    found = False
    folder = proj
    chain = []
    while True:
        chain.append(folder)
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    for folder in chain:
        role = "project" if folder == proj else "ancestor"
        for rel in ("CLAUDE.md", os.path.join(".claude", "CLAUDE.md"), "CLAUDE.local.md"):
            path = os.path.join(folder, rel)
            if os.path.realpath(path) == os.path.realpath(os.path.join(claude, "CLAUDE.md")):
                continue
            if _memory(inv, path, "project", role, label, applies):
                found = True
    if not found:
        for folder in chain:
            for rel in ("AGENTS.md", os.path.join(".claude", "AGENTS.md")):
                _memory(inv, os.path.join(folder, rel), "project", "agents-md", label, applies)
    data = inv.read_json(os.path.join(proj, ".mcp.json"))
    settings = {}
    if not same_as_user:
        for fname in ("settings.json", "settings.local.json"):
            st = inv.read_json(os.path.join(dot, fname))
            if st:
                inv.settings["%s:%s" % (label, fname)] = st
                scope = "local" if "local" in fname else "project"
                _hooks_from(inv, st.get("hooks"), scope, os.path.join(dot, fname), label,
                            applies=applies, off=bool(st.get("disableAllHooks")))
                for k, v in st.items():
                    if isinstance(v, list) and isinstance(settings.get(k), list):
                        settings[k] = settings[k] + v
                    elif isinstance(v, dict) and isinstance(settings.get(k), dict):
                        merged = dict(settings[k])
                        merged.update(v)
                        settings[k] = merged
                    else:
                        settings[k] = v
        _rules_in(inv, os.path.join(dot, "rules"), "project", label, applies)
        _skills_in(inv, os.path.join(dot, "skills"), "project", label, applies=applies)
        _commands_in(inv, os.path.join(dot, "commands"), "project", label, applies=applies)
        _agents_in(inv, [os.path.join(dot, "agents")], "project", label, applies=applies)
        _styles_in(inv, os.path.join(dot, "output-styles"), "project",
                   settings.get("outputStyle") or active_style, label, applies=applies)
    if data:
        servers = data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else {}
        yes = set(_names(settings.get("enabledMcpjsonServers")) +
                  _names(home_entry.get("enabledMcpjsonServers")))
        no = set(_names(settings.get("disabledMcpjsonServers")) +
                 _names(home_entry.get("disabledMcpjsonServers")))
        all_on = bool(settings.get("enableAllProjectMcpServers"))
        for name in sorted(servers):
            if name in no:
                state, note = False, "turned off for this project"
            elif all_on or name in yes:
                state, note = True, None
            else:
                state, note = None, "not approved yet, so it may not load"
            _mcp_from(inv, {name: servers[name]}, "project", os.path.join(proj, ".mcp.json"),
                      label, applies=applies, enabled=state, note=note)
    _mcp_from(inv, home_entry.get("mcpServers"), "local", home_json_path(claude), label,
              applies=applies)
    return settings


def inventory(claude=None, home_json=None, project_dirs=(), managed=None):
    """Everything configured at user level and in each project folder that exists.

    claude: the Claude Code config folder. home_json: the .claude.json file.
    project_dirs: project folders to look in (missing ones are skipped).
    managed: the organisation's managed folder (the system default when None).
    """
    claude = claude or C.claude_dir()
    home_json = home_json or home_json_path(claude)
    managed = managed_dir() if managed is None else managed
    inv = Inventory()
    user = inv.read_json(os.path.join(claude, "settings.json")) or {}
    inv.settings["user"] = user
    home = inv.read_json(home_json) or {}
    active_style = user.get("outputStyle") if isinstance(user.get("outputStyle"), str) else None
    hooks_off = bool(user.get("disableAllHooks"))

    _memory(inv, os.path.join(claude, "CLAUDE.md"), "user", "user")
    _rules_in(inv, os.path.join(claude, "rules"), "user")
    _skills_in(inv, os.path.join(claude, "skills"), "user")
    _commands_in(inv, os.path.join(claude, "commands"), "user")
    _agents_in(inv, [os.path.join(claude, "agents")], "user")
    _styles_in(inv, os.path.join(claude, "output-styles"), "user", active_style)
    _hooks_from(inv, user.get("hooks"), "user", os.path.join(claude, "settings.json"),
                off=hooks_off)
    _mcp_from(inv, home.get("mcpServers"), "user", home_json)
    _auto_memory(inv, os.path.join(claude, "projects"))

    if managed and os.path.isdir(managed):
        _memory(inv, os.path.join(managed, "CLAUDE.md"), "managed", "managed")
        mm = inv.read_json(os.path.join(managed, "managed-mcp.json"))
        if mm:
            _mcp_from(inv, mm.get("mcpServers"), "managed",
                      os.path.join(managed, "managed-mcp.json"))
        ms = inv.read_json(os.path.join(managed, "managed-settings.json"))
        if ms:
            _hooks_from(inv, ms.get("hooks"), "managed",
                        os.path.join(managed, "managed-settings.json"))

    enabled_map = {}
    if isinstance(user.get("enabledPlugins"), dict):
        for pid, val in user["enabledPlugins"].items():
            enabled_map[pid] = (bool(val), "user settings", None)
    home_projects = home.get("projects") if isinstance(home.get("projects"), dict) else {}
    seen = set()
    todo = []
    hide_rules = SC.Scope().rules
    for proj in list(project_dirs) + sorted(home_projects):
        if not isinstance(proj, str) or not os.path.isabs(proj) or not os.path.isdir(proj):
            continue
        if hide_rules and PROJECTS.excluded(hide_rules, "", [proj, X.Naming().name(
                _Folder(proj), "")]):
            continue                  # the setup of work that is never shown is not listed
        real = os.path.realpath(proj)
        if real in seen or real == os.path.realpath(os.path.expanduser("~")):
            continue
        seen.add(real)
        todo.append(proj.rstrip(os.sep) or proj)
    for proj in todo[:MAX_PROJECTS]:
        entry = home_projects.get(proj)
        entry = entry if isinstance(entry, dict) else {}
        st = _project(inv, proj, claude, active_style, entry)
        ep = st.get("enabledPlugins")
        if isinstance(ep, dict):
            for pid, val in ep.items():
                cur = enabled_map.get(pid)
                if cur and cur[2] is None and (cur[0] or not val):
                    continue                # the user-level choice already covers it
                if val:
                    seen_in = (cur[2] if cur and cur[0] and cur[2] else []) + [proj]
                    enabled_map[pid] = (True, "project settings", seen_in)
                # a project that switches a plugin off does not switch it off elsewhere
        off = entry.get("disabledMcpServers")
        if isinstance(off, list) and off:
            inv.settings.setdefault("disabled_mcp", {})[proj] = [str(x) for x in off]
    _plugins(inv, claude, enabled_map, active_style)
    inv.projects = [_disp(p) for p in todo[:MAX_PROJECTS]]
    env = user.get("env") if isinstance(user.get("env"), dict) else {}
    inv.tool_search_off = str(env.get("ENABLE_TOOL_SEARCH", "")).lower() == "false"
    return inv


def lifetime_usage(home_json=None):
    """Claude Code's own last-used record, when .claude.json carries one.

    Not a documented file format, so every field is optional. Returns
    {"skills": {name: {"count", "last"}}, "plugins": {...}} with epoch seconds.
    """
    out = {"skills": {}, "plugins": {}}
    try:
        with open(home_json or home_json_path(), "rb") as fh:
            data = json.loads(fh.read().decode("utf-8", "replace"))
    except (OSError, ValueError, RecursionError):
        return out
    if not isinstance(data, dict):
        return out
    for src, dst in (("skillUsage", "skills"), ("pluginUsage", "plugins")):
        block = data.get(src)
        if not isinstance(block, dict):
            continue
        for name, rec in block.items():
            if not isinstance(rec, dict):
                continue
            n, last = rec.get("usageCount"), rec.get("lastUsedAt")
            if (isinstance(n, (int, float)) and isinstance(last, (int, float))
                    and math.isfinite(n) and math.isfinite(last) and n > 0):
                out[dst][str(name)] = {"count": int(n),
                                       "last": last / 1000.0 if last > 1e11 else float(last)}
    return out


# ---------- usage from transcripts ----------

_MCP_NAME_RE = re.compile(r"^mcp__(.+?)__(.+)$")


class AuditAcc(X.SessionAcc):
    """extract.SessionAcc plus what the audit needs: full MCP names, last-used
    times, hook runs and the listings a session was given."""

    def __init__(self, session_id, allow_sidechain=False):
        X.SessionAcc.__init__(self, session_id, allow_sidechain)
        self.mcp = collections.Counter()        # (server, tool) -> calls
        self.last_use = {}                      # (kind, name) -> epoch
        self.hook_runs = collections.Counter()  # (event, label, hook name) -> records
        self.stop_runs = collections.Counter()  # command -> runs
        self.listed_skills = {}                 # name -> [chars, first epoch]
        self.listed_agents = {}                 # type -> chars
        self.builtin_agents = set()
        self.mcp_tools = {}                     # server -> set of tool names
        self.mcp_instr = {}                     # server -> chars of its instructions
        self.tool_defs = {}                     # full tool name -> chars of its definition
        self.deferral = False
        self.req_days = collections.Counter()   # local day -> model requests
        self._req_mid = None

    def _mark(self, kind, name, ep):
        if ep is not None and ep > self.last_use.get((kind, name), 0):
            self.last_use[(kind, name)] = ep

    def _user(self, d, ep, prev):
        before = dict(self.slash)
        X.SessionAcc._user(self, d, ep, prev)
        for name, n in self.slash.items():
            if n != before.get(name, 0):
                self._mark("slash", name, ep)

    def feed(self, d):
        X.SessionAcc.feed(self, d)
        if d.get("isSidechain") and not self.allow_sidechain:
            return
        t = d.get("type")
        ep = C.ts_epoch(d.get("timestamp")) if d.get("timestamp") else None
        if t == "assistant":
            msg = d.get("message") if isinstance(d.get("message"), dict) else {}
            mid = msg.get("id")
            if isinstance(msg.get("usage"), dict):
                if (mid != self._req_mid or mid is None) and msg.get("model") not in (
                        None, "", "<synthetic>") and ep is not None:
                    self.req_days[C.epoch_local(ep, "%Y-%m-%d")] += 1
                self._req_mid = mid
            blocks = msg.get("content")
            for b in blocks if isinstance(blocks, list) else []:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                name = b.get("name") if isinstance(b.get("name"), str) else ""
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                m = _MCP_NAME_RE.match(name)
                if m:
                    self.mcp[(m.group(1), m.group(2))] += 1
                    self._mark("mcp", m.group(1), ep)
                elif name == "Skill":
                    self._mark("skill", str(inp.get("skill") or "?"), ep)
                elif name in ("Agent", "Task"):
                    self._mark("agent", inp.get("subagent_type") or "general-purpose", ep)
        elif t == "attachment":
            if isinstance(d.get("attachment"), dict):
                self._listing(d["attachment"], ep)
        elif t == "system" and d.get("subtype") == "stop_hook_summary":
            infos = d.get("hookInfos")
            for h in infos if isinstance(infos, list) else []:
                if isinstance(h, dict) and isinstance(h.get("command"), str):
                    self.stop_runs[h["command"][:300]] += 1

    def _listing(self, a, ep):
        at = a.get("type")
        if not isinstance(at, str):
            return
        if at.startswith("hook_"):
            label = a.get("command") if isinstance(a.get("command"), str) else ""
            self.hook_runs[(str(a.get("hookEvent") or "?"), label[:300],
                            str(a.get("hookName") or ""))] += 1
        elif at == "skill_listing" and isinstance(a.get("content"), str):
            for line in a["content"].split("\n"):
                if line.startswith("- ") and ": " in line[:160]:
                    name = line[2:].split(": ", 1)[0].strip()
                    rec = self.listed_skills.setdefault(name, [0, ep])
                    rec[0] = max(rec[0], len(line) + 1)
                    if ep is not None and (rec[1] is None or ep < rec[1]):
                        rec[1] = ep
        elif at == "agent_listing_delta":
            names, lines = a.get("addedTypes"), a.get("addedLines")
            if isinstance(names, list) and isinstance(lines, list):
                for n, ln in zip(names, lines):
                    if isinstance(n, str) and isinstance(ln, str):
                        self.listed_agents[n] = max(self.listed_agents.get(n, 0), len(ln) + 1)
            for key in ("builtInTypes", "builtInPluginTypes"):
                if isinstance(a.get(key), list):
                    self.builtin_agents.update(x for x in a[key] if isinstance(x, str))
        elif at == "mcp_instructions_delta":
            names, blocks = a.get("addedNames"), a.get("addedBlocks")
            if isinstance(names, list) and isinstance(blocks, list):
                for n, blk in zip(names, blocks):
                    if isinstance(n, str) and isinstance(blk, str):
                        key = _norm_server(n)
                        self.mcp_instr[key] = max(self.mcp_instr.get(key, 0), len(blk))
        elif at == "deferred_tools_delta":
            self.deferral = True
            added = a.get("addedNames")
            for n in added if isinstance(added, list) else []:
                m = _MCP_NAME_RE.match(n) if isinstance(n, str) else None
                if m:
                    self.mcp_tools.setdefault(m.group(1), set()).add(m.group(2))
        elif at == "deferred_tools_record":
            entries = a.get("entries")
            for e in entries if isinstance(entries, list) else []:
                if isinstance(e, dict) and isinstance(e.get("name"), str):
                    try:
                        size = len(json.dumps(e, separators=(",", ":")))
                    except (TypeError, ValueError, RecursionError):
                        continue
                    self.tool_defs[e["name"]] = max(self.tool_defs.get(e["name"], 0), size)


def _bump(table, name, n, last, cwd):
    rec = table.setdefault(name, {"n": 0, "last": None, "by_project": {}})
    rec["n"] += n
    if last and (rec["last"] is None or last > rec["last"]):
        rec["last"] = last
    if cwd:
        rec["by_project"][cwd] = rec["by_project"].get(cwd, 0) + n


def _scan(path, lo_iso, hi_iso, acc, dedupe, keep=None):
    """Read one transcript. A record in a shape nobody expected stops that file
    only: what was read before it still counts. Returns False when that happened."""
    try:
        X.scan_file(path, lo_iso, hi_iso, acc, dedupe, keep)
    except (AttributeError, TypeError, KeyError, IndexError, ValueError, RecursionError):
        return False
    return True


def scan_usage(since, until, projects_dir=None, subagents=True):
    """Count what was used between two epochs. Reuses extract.py's parsing.

    Returns a JSON-serialisable dict. "requests" is the number of model requests
    in main sessions: the multiplier for anything loaded on every turn.
    """
    projects_dir = projects_dir or os.path.join(C.claude_dir(), "projects")
    lo_iso, hi_iso = C.epoch_utc_iso(since), C.epoch_utc_iso(until)
    u = {"skills": {}, "slash": {}, "agents": {}, "mcp": {}, "by_project": {}, "by_pdir": {},
         "models": collections.Counter(), "sessions": 0, "prompts": 0, "requests": 0,
         "subagent_requests": 0, "first": None, "last": None, "files_skipped": 0}
    hook_runs = collections.Counter()
    stop_runs = collections.Counter()
    by_day = collections.Counter()
    seen = {"skills": {}, "agents": {}, "builtin_agents": set(), "mcp": {}, "listing_sessions": 0,
            "mcp_instr": {}, "tool_defs": {}, "deferral": False}
    mcp_tools_used = {}

    def sight(table, name, chars, first, last, reqs):
        rec = table.setdefault(name, {"chars": 0, "first": first, "last": last,
                                      "requests": 0, "sessions": 0})
        rec["chars"] = max(rec["chars"], chars)
        rec["requests"] += reqs
        rec["sessions"] += 1 if reqs else 0
        if first is not None and (rec["first"] is None or first < rec["first"]):
            rec["first"] = first
        if last is not None and (rec["last"] is None or last > rec["last"]):
            rec["last"] = last
        return rec

    def fold(acc, cwd, reqs):
        for name, n in acc.skills.items():
            _bump(u["skills"], name, n, acc.last_use.get(("skill", name)), cwd)
        for name, n in acc.slash.items():
            _bump(u["slash"], name.lstrip("/"), n, acc.last_use.get(("slash", name)), cwd)
        for name, n in acc.agent_types.items():
            _bump(u["agents"], name, n, acc.last_use.get(("agent", name)), cwd)
        for (server, tool), n in acc.mcp.items():
            _bump(u["mcp"], server, n, acc.last_use.get(("mcp", server)), cwd)
            mcp_tools_used.setdefault(server, collections.Counter())[tool] += n
        hook_runs.update(acc.hook_runs)
        stop_runs.update(acc.stop_runs)
        if acc.listed_skills and reqs:
            seen["listing_sessions"] += 1
        for name, (chars, first) in acc.listed_skills.items():
            sight(seen["skills"], name, chars, first, acc.last, reqs)
        for name, chars in acc.listed_agents.items():
            sight(seen["agents"], name, chars, acc.first, acc.last, reqs)
        seen["builtin_agents"].update(acc.builtin_agents)
        servers = set(acc.mcp_tools) | set(acc.mcp_instr) | set(k[0] for k in acc.mcp)
        for server in servers:
            rec = sight(seen["mcp"], server, 0, acc.first, acc.last, reqs)
            rec.setdefault("tools", set()).update(acc.mcp_tools.get(server, ()))
        for server, chars in acc.mcp_instr.items():
            seen["mcp_instr"][server] = max(seen["mcp_instr"].get(server, 0), chars)
        for name, chars in acc.tool_defs.items():
            seen["tool_defs"][name] = max(seen["tool_defs"].get(name, 0), chars)
        seen["deferral"] = seen["deferral"] or acc.deferral

    scope = SC.Scope()
    naming = X.Naming() if scope.rules else None
    for path, sid, pdir, _size in X.find_sessions(projects_dir, since):
        acc = AuditAcc(sid)
        dedupe = set()      # record ids, so a subagent file that repeats them adds nothing
        gate = scope.gate(sid, path)
        if not _scan(path, lo_iso, hi_iso, acc, dedupe, gate):
            u["files_skipped"] += 1
        acc.finish()
        if acc.first is None:
            continue
        if scope.rules and scope.hidden(acc, pdir, naming.name(acc, pdir)):
            continue                  # work config.json says is never to be shown
        cwd = acc.cwd or ""
        reqs = sum(acc.models.values())
        u["sessions"] += 1
        u["prompts"] += len(acc.prompts)
        u["requests"] += reqs
        u["models"].update(acc.models)
        by_day.update(acc.req_days)
        for table, key in ((u["by_project"], cwd), (u["by_pdir"], pdir)):
            rec = table.setdefault(key, {"sessions": 0, "requests": 0, "prompts": 0})
            rec["sessions"] += 1
            rec["requests"] += reqs
            rec["prompts"] += len(acc.prompts)
        if u["first"] is None or acc.first < u["first"]:
            u["first"] = acc.first
        if u["last"] is None or (acc.last or 0) > u["last"]:
            u["last"] = acc.last
        fold(acc, cwd, reqs)
        if not subagents:
            continue
        sub_dir = os.path.join(path[:-6], "subagents")
        if not os.path.isdir(sub_dir):
            continue
        try:
            names = sorted(os.listdir(sub_dir))
        except OSError:
            names = []
        for n in names:
            if not n.endswith(".jsonl"):
                continue
            sub = AuditAcc("sub", allow_sidechain=True)
            if not _scan(os.path.join(sub_dir, n), lo_iso, hi_iso, sub, dedupe, gate):
                u["files_skipped"] += 1
            sub.finish()
            u["subagent_requests"] += sum(sub.models.values())
            sub.slash.clear()
            fold(sub, cwd, 0)

    u["models"] = dict(u["models"])
    for server, tools in mcp_tools_used.items():
        u["mcp"][server]["tools"] = dict(tools.most_common(8))
    u["hooks"] = [{"event": k[0], "label": k[1], "name": k[2], "n": n}
                  for k, n in sorted(hook_runs.items(), key=lambda kv: -kv[1])]
    u["stop_hooks"] = dict(stop_runs)
    u["by_day"] = dict(by_day)
    for rec in seen["mcp"].values():
        rec["tools"] = sorted(rec.get("tools", ()))
        del rec["chars"]
    u["seen"] = {
        "skills": seen["skills"], "agents": seen["agents"],
        "builtin_agents": sorted(seen["builtin_agents"]),
        "mcp": seen["mcp"], "mcp_instructions": seen["mcp_instr"],
        "tool_defs": seen["tool_defs"], "deferral": seen["deferral"],
        "listing_sessions": seen["listing_sessions"],
    }
    return u


# ---------- prices ----------

_DATED_RE = re.compile(r"^(.+?)[-@]\d{8}$")


def load_prices(path=None):
    """The price table from 1.1.0 (coach/prices.json), or None when it is absent.

    Expected shape: {"models": {"<model id>": {"cache_read": x, "cache_write_5m": y}}},
    in dollars per million tokens.
    """
    try:
        if path:
            PRICING._cache.pop(path, None)     # a named file is read afresh each time
        data = PRICING.load_prices(path)       # the same table the cost figures use
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
        return None
    return data


def blended_rates(models, prices):
    """Cache read and cache write rates, weighted by the requests each model served.

    Returns None when there is no table or no model in the window is priced.
    """
    if not prices or not models:
        return None
    table = prices["models"]
    read = write = weight = 0.0
    total = float(sum(models.values()))
    for model, n in models.items():
        entry = PRICING._entry(model, prices)      # exact id, or a dated snapshot of one
        if not isinstance(entry, dict):
            continue
        r = entry.get("cache_read")
        w = entry.get("cache_write_5m", entry.get("cache_write"))
        if not isinstance(r, (int, float)) or not isinstance(w, (int, float)):
            continue
        read += r * n
        write += w * n
        weight += n
    if not weight:
        return None
    return {"cache_read": read / weight, "cache_write": write / weight,
            "priced_share": round(weight / total, 3) if total else 0.0,
            "retrieved": prices.get("retrieved")}


# ---------- joining inventory and usage ----------

def _turns_for(item, usage, real_since):
    """Model requests and sessions that carried this item.

    An item known only from session listings counts the sessions that listed it.
    An item on disk counts every session it applies to, cut down to the days
    since it was installed when that date falls inside the window.
    """
    if item.get("_seen") is not None:
        return item["_seen"]["requests"], item["_seen"]["sessions"]
    if item.get("pdir"):
        rec = usage["by_pdir"].get(item["pdir"]) or {}
        return rec.get("requests", 0), rec.get("sessions", 0)
    applies = item.get("applies")
    if not applies:
        reqs, sess = usage["requests"], usage["sessions"]
    else:
        reqs = sess = 0
        for cwd, rec in usage["by_project"].items():
            if any(_under(cwd, a) for a in applies):
                reqs += rec["requests"]
                sess += rec["sessions"]
    for cwd, rec in usage["by_project"].items():
        if any(_under(cwd, p) for p in item.get("_disabled_in") or ()) and (
                not applies or any(_under(cwd, a) for a in applies)):
            reqs -= rec["requests"]
            sess -= rec["sessions"]
    inst = item.get("_installed")
    total = sum(usage["by_day"].values())
    if inst and inst > real_since and total and item["kind"] in USE_KINDS:
        day = _day(inst)
        share = sum(n for d, n in usage["by_day"].items() if d >= day) / float(total)
        reqs, sess = int(round(reqs * share)), int(round(sess * share))
        item["turns_basis"] = "only the turns since it was installed on " + day
    return reqs, sess


def _use_of(item, usage, by_bare):
    """(uses, last epoch, uses per project) for one skill, command, agent or server."""
    kind, name = item["kind"], item["name"]
    recs = []
    if kind in ("skill", "command"):
        bare = name.split(":")[-1]
        keys = [name]
        if by_bare.get(bare, 0) == 1 and bare != name:
            keys.append(bare)
        listed_bare = bare in usage["seen"]["skills"]
        for k in keys:
            for table in (usage["skills"], usage["slash"]):
                # a typed /name counts for a prefixed skill only if sessions listed it bare:
                # otherwise it is a built-in command of the same name
                if k in table and (k == name or table is usage["skills"] or listed_bare):
                    recs.append(table[k])
            for k2, rec in usage["slash"].items():
                if len(k2) >= 39 and k.startswith(k2) and k != k2:
                    recs.append(rec)
    elif kind == "agent":
        if name in usage["agents"]:
            recs.append(usage["agents"][name])
    elif kind == "mcp":
        rec = usage["mcp"].get(item.get("server_key") or _norm_server(name))
        if rec:
            recs.append(rec)
    n = 0
    last = None
    by_project = {}
    applies = item.get("applies")
    for rec in recs:
        for cwd, c in rec["by_project"].items():
            if applies and not any(_under(cwd, a) for a in applies):
                continue
            n += c
            by_project[cwd] = by_project.get(cwd, 0) + c
            if rec["last"] and (last is None or rec["last"] > last):
                last = rec["last"]
        if not rec["by_project"] and not applies:
            n += rec["n"]
            last = max(last or 0, rec["last"] or 0) or None
    return n, last, by_project


def _same_hook(label, cmd, status):
    label = C.one_line(label, 0)
    if not label:
        return False
    return (label == cmd or label == status
            or (len(label) >= 250 and cmd.startswith(label)))


def _hook_runs(item, usage):
    """Runs recorded for one configured hook. Only runs that left a record count."""
    event, cmd, status = item.get("event"), item.get("_cmd") or "", item.get("status_message")
    n = sum(h["n"] for h in usage["hooks"]
            if h["event"] == event and _same_hook(h["label"], cmd, status))
    if event in ("Stop", "SubagentStop"):
        n = max(n, sum(c for k, c in usage["stop_hooks"].items() if _same_hook(k, cmd, None)))
    return n


def _mcp_load(item, seen, tool_search_off):
    """Fill in what one MCP server loads per turn, from what sessions were shown."""
    key = item.get("server_key") or _norm_server(item["name"])
    tools = (seen["mcp"].get(key) or {}).get("tools") or []
    instr = seen["mcp_instructions"].get(key, 0)
    item["_tools"] = tools[:3]
    prefix = "mcp__%s__" % key
    measured = [c for n, c in seen["tool_defs"].items() if n.startswith(prefix)]
    item["tool_count"] = len(tools)
    item["seen_in_sessions"] = bool(tools or instr or measured)
    if not item["seen_in_sessions"]:
        item["tokens_per_turn"] = None
        item["chars"] = 0
        item["cost_basis"] = "unknown: no session in the window recorded its tool list"
        return
    names_chars = sum(len(prefix) + len(t) + 1 for t in tools)
    if measured:
        avg = sum(measured) / float(len(measured))
        defs_chars = int(avg * max(len(tools), len(measured)))
        def_basis = "estimate from %d of its definitions seen in sessions" % len(measured)
    else:
        defs_chars = int(len(tools) * ASSUMED_TOOL_DEF_TOKENS * CHARS_PER_TOKEN)
        def_basis = "assumed %d tokens per tool: no definition was ever loaded" % ASSUMED_TOOL_DEF_TOKENS
    item["definition_tokens"] = est_tokens(defs_chars)
    item["definition_basis"] = def_basis
    item["instruction_tokens"] = est_tokens(instr)
    upfront = tool_search_off or item.get("always_load") or not seen["deferral"]
    item["deferred"] = not upfront
    if item.get("enabled") is not True:
        item["chars"] = 0
        item["cost_basis"] = ("turned off: loads nothing" if item.get("enabled") is False
                              else "not approved for this project yet: loads nothing")
    elif upfront:
        item["chars"] = defs_chars + instr
        item["cost_basis"] = "estimate: full tool definitions load up front (" + def_basis + ")"
    else:
        item["chars"] = names_chars + instr
        item["cost_basis"] = ("estimate: tool names and server instructions only; "
                              "definitions are deferred and load on demand")
    item["tokens_per_turn"] = est_tokens(item["chars"])


def _action(item, by_project):
    kind, scope = item["kind"], item["scope"]
    if scope == "builtin":
        return "none: part of Claude Code"
    if scope == "managed":
        return "none: set by your organisation"
    if scope == "session" and kind == "mcp":
        return ("turn the connector off where it was added (claude.ai connectors or the "
                "app that provides it); it is not in the local config files")
    if scope == "session":
        return ("turn it off where it was enabled (claude.ai or the desktop app); "
                "it is not in the local config files")
    if kind == "plugin":
        return "disable the plugin"
    if kind == "mcp":
        return "disable the server, or remove it"
    if scope in ("project", "local"):
        return "remove from the project"
    if len(by_project) == 1:
        return "move to project level: " + _disp(list(by_project)[0])
    return "remove, or move to the one project that needs it"


def _last_text(item, days):
    if item.get("last_used"):
        return "last used " + item["last_used"]
    return "never in %d days" % days


def build_report(inv, usage, since, until, lifetime=None, prices=None, new_days=NEW_DAYS,
                 now=None, standard=False):
    """Join what is configured with what was used. Returns the full report dict."""
    now = until if now is None else now
    lifetime = lifetime or {"skills": {}, "plugins": {}}
    seen = usage["seen"]
    first = usage.get("first")
    real_since = max(since, first) if first else since
    days_asked = int(round((until - since) / 86400.0))
    days_real = max(1, int(math.ceil((until - real_since) / 86400.0))) if first else 0
    items = inv.items

    # what sessions were shown that the config files do not explain
    plugin_names = {}
    for it in items:
        if it["kind"] == "plugin" and it.get("enabled"):
            plugin_names[it["name"].split("@")[0]] = it
    for it in items:
        if it.get("plugin") and it["kind"] in ("skill", "command", "agent"):
            plugin_names.setdefault(it["name"].split(":")[0], None)
    newest = {}
    for group in ("skills", "agents", "mcp"):
        newest[group] = max([r["last"] or 0 for r in seen[group].values()] or [0])

    def gone(rec, group):
        return bool(rec["last"]) and rec["last"] < newest[group] - GONE_DAYS * 86400

    known_skills = set(i["name"] for i in items if i["kind"] in ("skill", "command"))
    disk_bare = collections.Counter(i["name"].split(":")[-1] for i in items
                                    if i["kind"] in ("skill", "command"))
    for name in sorted(seen["skills"]):
        rec = seen["skills"][name]
        if name in known_skills or (":" not in name and disk_bare.get(name) == 1):
            continue
        owner = plugin_names.get(name.split(":")[0]) if ":" in name else None
        if owner is not None:
            it = inv.add("skill", name, "plugin", None, plugin=owner["name"], chars=rec["chars"],
                         basis="measured: its line in a session's skill listing",
                         installed=owner.get("_installed"), applies=owner.get("applies"),
                         note="listed in sessions, not found in the plugin folder on record")
        else:
            # a skill that ships with Claude Code is listed in nearly every session;
            # one listed in a few is a project skill whose folder was not found
            everywhere = rec["sessions"] * 10 >= seen.get("listing_sessions", 0) * 6
            it = inv.add("skill", name, "builtin" if ":" not in name and everywhere else "session",
                         None, chars=rec["chars"],
                         basis="measured: its line in a session's skill listing",
                         first_seen=_day(rec["first"]))
            if ":" not in name and not everywhere:
                it["note"] = "listed in some sessions only; its folder was not found"
            it["_first_seen"] = rec["first"]
            it["_seen"] = rec
        it["_gone"] = gone(rec, "skills")
    known_agents = set(i["name"] for i in items if i["kind"] == "agent")
    builtins = set(seen["builtin_agents"])
    for name in sorted(seen["agents"]):
        if name not in known_agents:
            rec = seen["agents"][name]
            it = inv.add("agent", name, "builtin" if name in builtins else "session", None,
                         chars=rec["chars"],
                         basis="measured: its line in a session's agent listing")
            it["_seen"] = rec
            it["_gone"] = gone(rec, "agents")
    known_servers = set(i.get("server_key") for i in items if i["kind"] == "mcp")
    for key in sorted(seen["mcp"]):
        if key not in known_servers:
            rec = seen["mcp"][key]
            it = inv.add("mcp", key[:8] if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-", key) else key,
                         "session", None, server_key=key, enabled=True,
                         note="connector or app-provided server, not in the local config files")
            it["_seen"] = rec
            it["_gone"] = gone(rec, "mcp")

    _mark_shadowed(items)
    by_bare = collections.Counter(i["name"].split(":")[-1] for i in items
                                  if i["kind"] in ("skill", "command"))
    plugin_of = {}
    for it in items:
        if it["kind"] == "plugin":
            plugin_of[it["name"]] = it
            it.update({"uses": 0, "_last": None, "parts": collections.Counter(),
                       "hook_runs": 0, "chars": 0, "_by_project": {}})
    for it in items:
        kind = it["kind"]
        if kind == "plugin":
            continue
        if kind == "mcp":
            _mcp_load(it, seen, inv.tool_search_off)
            rec = seen["mcp"].get(it.get("server_key"))
            if rec and it["scope"] != "session":
                it["_seen"] = rec
                it["_first_seen"] = rec["first"]
                it["turns_basis"] = "the sessions that listed or used it"
            elif rec:
                it["_first_seen"] = rec["first"]
            off = [p for p, names in (inv.settings.get("disabled_mcp") or {}).items()
                   if it["name"] in names]
            if off:
                it["disabled_in"] = [_disp(p) for p in off]
                it["_disabled_in"] = off
        elif kind in ("skill", "command") and it["always_on"] and it["name"] in seen["skills"]:
            it["chars"] = seen["skills"][it["name"]]["chars"]
            it.setdefault("_first_seen", seen["skills"][it["name"]]["first"])
            it["tokens_per_turn"] = est_tokens(it["chars"])
            it["cost_basis"] = "measured: its line in a session's skill listing"
        elif kind == "agent" and it["name"] in seen["agents"] and it["scope"] not in (
                "builtin", "session"):
            it["chars"] = seen["agents"][it["name"]]["chars"]
            it["tokens_per_turn"] = est_tokens(it["chars"])
            it["cost_basis"] = "measured: its line in a session's agent listing"
        if it.get("_shadowed"):
            it["shadowed_by"] = it["_shadowed"]
            it["always_on"] = False
            it["chars"] = 0
            it["tokens_per_turn"] = 0
            it["cost_basis"] = "never loads: the %s copy of the same name wins" % it["_shadowed"]
            it["uses"] = 0
            it["_by_project"] = {}
            it["used_in"] = []
            it["last_used"] = None
            continue
        if it.get("_gone"):
            it["gone"] = True
            it["last_listed"] = _day(it["_seen"]["last"]) if it.get("_seen") else None
            it["cost_basis"] += ("; not listed in the newest sessions, so it may be off already: "
                                 "left out of the per-turn total")
        by_project = {}
        if kind in ("skill", "command", "agent", "mcp"):
            n, last, by_project = _use_of(it, usage, by_bare)
            if not n and it.get("applies") and it["scope"] in ("project", "local"):
                if _use_of(dict(it, applies=None), usage, by_bare)[0]:
                    it["used_in_another_checkout"] = True
            it["uses"] = n
            it["_last"] = last
            life = lifetime["skills"].get(it["name"]) if kind in ("skill", "command") else None
            if life:
                it["lifetime_uses"] = life["count"]
                if not last or life["last"] > last:
                    it["_last"] = life["last"]
                    it["last_used_source"] = "Claude Code's own usage record"
                    if not n and since <= life["last"] <= until:
                        it["used_outside_transcripts"] = True
        elif kind == "hook":
            it["runs_recorded"] = _hook_runs(it, usage)
        it["_by_project"] = by_project
        it["used_in"] = sorted(_disp(p) for p in by_project)
        it["last_used"] = _day(it.get("_last"))
        parent = plugin_of.get(it.get("plugin"))
        if parent is not None:
            it["part_of"] = parent["name"]
            if parent.get("_installed"):
                it["_installed"] = parent["_installed"]
                it["installed"] = parent["installed"]
            parent["parts"][kind] += 1
            parent["chars"] += it["chars"] if it["always_on"] else 0
            if kind == "hook":
                parent["hook_runs"] += it["runs_recorded"]
            else:
                parent["uses"] += it.get("uses", 0)
                for p, c in by_project.items():
                    parent["_by_project"][p] = parent["_by_project"].get(p, 0) + c
                if it.get("_last") and (parent["_last"] or 0) < it["_last"]:
                    parent["_last"] = it["_last"]
                if it.get("used_outside_transcripts"):
                    parent["used_outside_transcripts"] = True
    for pid, it in plugin_of.items():
        it["parts"] = dict(it["parts"])
        it["tokens_per_turn"] = est_tokens(it["chars"]) if it["always_on"] else 0
        life = lifetime["plugins"].get(pid)
        if life and (not it["_last"] or life["last"] > it["_last"]):
            it["_last"] = life["last"]
            it["last_used_source"] = "Claude Code's own usage record"
            if not it["uses"] and since <= life["last"] <= until:
                it["used_outside_transcripts"] = True
        it["last_used"] = _day(it["_last"])
        it["used_in"] = sorted(_disp(p) for p in it["_by_project"])

    rates = blended_rates(usage["models"], prices)
    for it in items:
        turns, sessions = _turns_for(it, usage, real_since)
        it["turns"] = turns
        tpt = it.get("tokens_per_turn")
        it["window_tokens"] = tpt * turns if tpt is not None else None
        it["usd"] = (round(it["window_tokens"] * rates["cache_read"] / 1e6, 4)
                     if rates and it["window_tokens"] is not None else None)
        it["_sessions"] = sessions
        it["estimate"] = True
        inst = it.get("_installed")
        fs = it.get("_first_seen")
        changed = it.get("_changed")       # only where no creation time is recorded
        it["new"] = it["kind"] in USE_KINDS and bool(
            (inst and inst > until - new_days * 86400) or
            (changed and changed > until - new_days * 86400) or
            (fs and first and fs > first + 86400 and fs > until - new_days * 86400))

    findings = _findings(items, usage, days_real, real_since)
    leaves = [i for i in items if i["kind"] != "plugin" and i["always_on"]
              and i.get("tokens_per_turn")]
    every = [i for i in leaves if not i.get("applies") and not i.get("pdir")
             and not i.get("gone")]
    by_kind = collections.OrderedDict()
    for i in every:
        by_kind[i["kind"]] = by_kind.get(i["kind"], 0) + i["tokens_per_turn"]
    window_tokens = sum(i["window_tokens"] for i in leaves)
    write_tokens = sum(i["tokens_per_turn"] * i["_sessions"] for i in leaves)
    always = collections.OrderedDict()
    always["every_session_tokens_per_turn"] = sum(i["tokens_per_turn"] for i in every)
    always["by_kind"] = by_kind
    always["built_in_tokens_per_turn"] = sum(i["tokens_per_turn"] for i in every
                                             if i["scope"] == "builtin")
    always["project_only_tokens_per_turn"] = _project_extra(leaves)
    always["window_tokens"] = window_tokens
    always["usd_cache_read"] = round(window_tokens * rates["cache_read"] / 1e6, 2) if rates else None
    always["first_turn_write_tokens"] = write_tokens
    always["usd_first_turn_write"] = (round(write_tokens * rates["cache_write"] / 1e6, 2)
                                      if rates else None)
    always["unknown_load"] = sorted(i["name"] for i in items if i["kind"] == "mcp"
                                    and i["always_on"] and i.get("tokens_per_turn") is None)

    report = collections.OrderedDict()
    report["generated"] = C.epoch_local(now)
    report["window"] = collections.OrderedDict([
        ("asked_days", days_asked), ("real_days", days_real),
        ("since", _day(real_since)), ("until", _day(until)),
        ("first_transcript_record", _day(first)),
        ("capped_by_transcripts", bool(first and first > since + 86400)),
        ("sessions", usage["sessions"]), ("prompts", usage["prompts"]),
        ("turns", usage["requests"]), ("subagent_turns", usage["subagent_requests"]),
        ("transcripts_cut_short", usage.get("files_skipped", 0)),
        ("standard", bool(standard))])
    report["estimates"] = [
        "Every token figure is an estimate: characters divided by %d." % int(CHARS_PER_TOKEN),
        "A turn is one model request in a main session. Loaded text is re-read on each one. "
        "Subagent requests are not counted, so totals are a floor.",
        "Dollars use the cache read rate, weighted by the models that served the window. "
        "The first request of a session writes the cache at the higher write rate: shown "
        "separately, and a floor because a cache that expires is written again.",
        "MCP tool definitions are deferred by default, so an unused server costs its tool "
        "names and instructions, not its full definitions.",
        "Hook runs are counted only when the run left a record in the transcript. "
        "A guard hook that never ran is doing its job.",
    ]
    report["prices"] = ({"cache_read_per_mtok": round(rates["cache_read"], 4),
                         "cache_write_per_mtok": round(rates["cache_write"], 4),
                         "priced_share_of_turns": rates["priced_share"],
                         "retrieved": rates["retrieved"]} if rates else None)
    report["always_on"] = always
    report["findings"] = findings
    report["weekly_block"] = weekly_block(findings, days_real)
    report["items"] = [_public(i) for i in sorted(items, key=_cost_key)]
    report["projects_checked"] = len(inv.projects)
    report["projects_with_config"] = sorted(set(i["project"] for i in items if i.get("project")))
    report["problems"] = inv.problems
    return report


def _project_extra(leaves):
    extra = {}
    for i in leaves:
        for a in i.get("applies") or ([i["pdir"]] if i.get("pdir") else []):
            extra[_disp(a)] = extra.get(_disp(a), 0) + i["tokens_per_turn"]
    top = sorted(extra.items(), key=lambda kv: -kv[1])[:10]
    return collections.OrderedDict(top)


def _cost_key(item):
    wt = item.get("window_tokens")
    return (0 if wt is not None else 1, -(wt or 0), -(item.get("tokens_per_turn") or 0),
            item["kind"], item["name"])


def _public(item):
    out = collections.OrderedDict((k, v) for k, v in item.items() if not k.startswith("_"))
    if out.get("applies"):
        out["applies"] = [_disp(a) for a in out["applies"]]
    return out


def _finding(kind, item, days, action, why, **extra):
    f = collections.OrderedDict()
    f["type"] = kind
    f["kind"] = item["kind"]
    f["name"] = item["name"]
    f["scope"] = item["scope"]
    f["project"] = item.get("project")
    f["tokens_per_turn"] = item.get("tokens_per_turn")
    f["window_tokens"] = item.get("window_tokens")
    f["usd"] = item.get("usd")
    if item["kind"] in USE_KINDS:
        f["uses"] = item.get("uses", 0)
        f["last_used"] = item.get("last_used")
        f["last"] = _last_text(item, days)
    f["action"] = action
    f["why"] = why
    f["estimate"] = True
    if item.get("gone"):
        f["may_be_off_already"] = True
        f["last_listed"] = item.get("last_listed")
    if item.get("used_in_another_checkout"):
        f["used_in_another_checkout"] = True
    if item.get("_tools"):
        f["tools_like"] = item["_tools"]
    f.update(extra)
    return f


def _findings(items, usage, days, real_since):
    out = collections.OrderedDict((k, []) for k in (
        "never_used", "rarely_used", "single_project", "duplicates", "large_memory",
        "mcp_large_unused", "new_no_history", "hooks_no_run_recorded"))
    no_history = not usage["sessions"]
    low = days < SHORT_WINDOW_DAYS
    conf = ("low: only %d days of transcripts" % days) if low else "normal"
    roots = {}
    for p, r in usage["by_project"].items():
        if r["requests"]:
            roots[p] = repo_root(p) if p else p
    other_projects = len(set(roots.values()))
    for it in items:
        kind = it["kind"]
        if kind == "hook":
            if it.get("enabled", True) and not it.get("runs_recorded"):
                out["hooks_no_run_recorded"].append(collections.OrderedDict([
                    ("name", it["name"]), ("scope", it["scope"]), ("plugin", it.get("plugin")),
                    ("note", "no action: only runs that leave a record are counted, and a "
                             "guard that never runs is doing its job")]))
            continue
        if kind == "memory":
            if it["always_on"] and (it.get("lines", 0) > MEMORY_MAX_LINES
                                    or (it.get("tokens_per_turn") or 0) > MEMORY_MAX_TOKENS):
                out["large_memory"].append(_finding(
                    "large_memory", it, days,
                    "trim it, or move parts into path-scoped rules or a skill",
                    "%d lines, about %d tokens on every turn it applies to"
                    % (it.get("lines", 0), it["tokens_per_turn"]), lines=it.get("lines")))
            continue
        if kind not in USE_KINDS or it.get("part_of") or it["scope"] in ("builtin", "managed"):
            continue
        if it.get("enabled") in (False, None) and "enabled" in it:
            continue
        if it.get("not_installed") or it.get("shadowed_by"):
            continue
        if kind == "plugin" and not any(k in USE_KINDS for k in it.get("parts", {})):
            continue                      # hooks or an output style only: nothing to call
        if no_history:
            continue
        uses = it.get("uses", 0)
        tpt = it.get("tokens_per_turn")
        by_project = it.get("_by_project") or {}
        if uses == 0 and not it.get("used_outside_transcripts"):
            if it.get("new"):
                out["new_no_history"].append(collections.OrderedDict([
                    ("kind", kind), ("name", it["name"]),
                    ("installed", it.get("installed") or it.get("first_seen")),
                    ("note", "installed in the last %d days: no history yet, not flagged" % NEW_DAYS)]))
                continue
            if not it.get("turns"):
                continue                  # no session in the window carried it
            if it.get("used_in_another_checkout"):
                continue                  # the same file, used from another copy of the repo
            action = _action(it, by_project)
            if kind == "plugin" and it.get("parts", {}).get("hook"):
                action = "review before disabling: it also runs hooks"
            big = kind == "mcp" and (it.get("tool_count", 0) >= MCP_BIG_TOOLS
                                     or it.get("definition_tokens", 0) >= MCP_BIG_DEF_TOKENS)
            if big:
                why = ("%d tools, never called. %s. Full definitions would be about %d tokens (%s)"
                       % (it["tool_count"],
                          "Deferred, so each turn carries only the names" if it.get("deferred")
                          else "Loaded up front on every turn",
                          it["definition_tokens"], it["definition_basis"]))
                out["mcp_large_unused"].append(_finding(
                    "mcp_large_unused", it, days, action, why, confidence=conf,
                    tool_count=it["tool_count"], definition_tokens=it["definition_tokens"],
                    deferred=it.get("deferred")))
            else:
                why = "configured, and not used once in the window"
                if tpt is None:
                    why += "; its load is unknown because no session showed it"
                out["never_used"].append(_finding("never_used", it, days, action, why,
                                                  confidence=conf))
            continue
        if (0 < uses <= RARE_MAX_USES and (tpt or 0) >= RARE_MIN_TOKENS
                and days >= RARE_MIN_DAYS and not it.get("new")):
            out["rarely_used"].append(_finding(
                "rarely_used", it, days,
                "keep if it earns its place; otherwise shorten its description or "
                + _action(it, by_project),
                "used %d time%s in %d days against about %d tokens on every turn"
                % (uses, "" if uses == 1 else "s", days, tpt)))
        used_roots = set(roots.get(p, p) for p in by_project)
        if (uses > 0 and it["scope"] == "user" and len(used_roots) == 1
                and other_projects >= 3 and (tpt or 0) > 0 and not it.get("applies")):
            only = list(used_roots)[0]
            elsewhere = usage["requests"] - sum(
                usage["by_project"][p]["requests"] for p, root in roots.items() if root == only)
            if elsewhere > 0:
                out["single_project"].append(_finding(
                    "single_project", it, days, "move to project level: " + _disp(only),
                    "used only in one project, loaded in all %d" % other_projects,
                    wasted_tokens=tpt * elsewhere))
    out["duplicates"] = _duplicates(items)
    for key in ("never_used", "rarely_used", "mcp_large_unused", "large_memory"):
        out[key].sort(key=lambda f: -(f.get("window_tokens") or 0))
    out["single_project"].sort(key=lambda f: -(f.get("wasted_tokens") or 0))
    return out


_WINS = {"skill": ("managed", "user", "project"),
         "agent": ("managed", "project", "user"),
         "mcp": ("managed", "local", "project", "user")}


def _dup_groups(items):
    """Names set up at more than one level that can meet in one session.

    Yields (kind, name, levels, winner level, losing items). Two projects with
    the same name are not a duplicate, and neither are two plugins: plugin names
    carry the plugin prefix, so both are meant to load.
    """
    groups = collections.OrderedDict()
    for it in items:
        if it["scope"] in ("builtin", "session") or it.get("gone"):
            continue
        if "enabled" in it and it["enabled"] is not True:
            continue
        if it["kind"] in ("skill", "command"):
            key = ("skill", it["name"].split(":")[-1])
        elif it["kind"] == "agent":
            key = ("agent", it["name"].split(":")[-1])
        elif it["kind"] == "mcp":
            key = ("mcp", _norm_server(it["name"]))
        else:
            continue
        groups.setdefault(key, []).append(it)
    for (kind, name), group in groups.items():
        for proj in [None] + sorted(set(it["project"] for it in group if it.get("project"))):
            levels = collections.OrderedDict()
            for it in group:
                local = it["scope"] in ("project", "local")
                if local and it.get("project") != proj:
                    continue
                level = "plugin " + it["plugin"] if it.get("plugin") else it["scope"]
                levels.setdefault(level, it)
            plain = [lv for lv in levels if not lv.startswith("plugin ")]
            if len(levels) < 2 or not plain:
                continue
            if proj is not None and not any(lv in ("project", "local") for lv in levels):
                continue
            winner = next((lv for lv in _WINS[kind] if lv in levels), plain[0])
            losers = [levels[lv] for lv in plain if lv != winner]
            yield kind, name, levels, winner, losers


def _mark_shadowed(items):
    """A project copy that a higher level always beats never loads: cost it at nothing."""
    for _kind, _name, _levels, winner, losers in _dup_groups(items):
        for it in losers:
            if it["scope"] in ("project", "local"):
                it["_shadowed"] = winner


def _duplicates(items):
    """The same name set up at more than one level."""
    out = []
    for kind, name, levels, winner, losers in _dup_groups(items):
        plugins = [lv for lv in levels if lv.startswith("plugin ")]
        if losers:
            note = "the %s copy wins; the %s copy does not run where both apply" % (
                winner, " and ".join(it["scope"] for it in losers))
            action = "keep one copy and remove the other"
        else:
            note = "one plain copy and one in a plugin"
            action = "check that both are needed"
        if plugins:
            note += ("; the plugin copy loads as well under its own prefix, "
                     "so the listing pays for both")
        out.append(collections.OrderedDict([
            ("type", "duplicate"), ("kind", kind), ("name", name),
            ("levels", list(levels)), ("wins", winner),
            ("where", [it.get("path") or it.get("project") or it["scope"] for it in levels.values()]),
            ("tokens_per_turn", sum(it.get("tokens_per_turn") or 0 for it in levels.values())),
            ("action", action), ("why", note),
            ("estimate", True)]))
    return out


# ---------- output ----------

def _tok(n):
    if n is None:
        return "?"
    if n >= 1000000:
        return "%.1fM" % (n / 1e6)
    if n >= 10000:
        return "%dk" % round(n / 1000.0)
    if n >= 1000:
        return "%.1fk" % (n / 1000.0)
    return str(int(n))


def _usd(x):
    if x is None:
        return ""
    return "$%.2f" % x if x >= 0.005 else "<$0.01"


def weekly_block(findings, days, limit=WEEKLY_LINES):
    """The 'Set up but unused' lines for the weekly review: at most five."""
    rows = findings.get("never_used", []) + findings.get("mcp_large_unused", [])
    rows = [f for f in rows if not f.get("may_be_off_already")]
    rows = sorted(rows, key=lambda f: -(f.get("window_tokens") or 0))
    if len(rows) < limit:
        rows += findings.get("rarely_used", [])[:limit - len(rows)]
    out = []
    for f in rows[:limit]:
        load = ("about %s tokens per turn" % _tok(f["tokens_per_turn"])
                if f.get("tokens_per_turn") is not None else "load unknown")
        where = f["scope"] + (" " + f["project"] if f.get("project") else "")
        last = f["last"] if f["type"] != "rarely_used" else "used %d in %d days" % (f["uses"], days)
        name = f["name"]
        if f.get("tools_like"):
            name += " [tools like %s]" % ", ".join(f["tools_like"][:2])
        out.append("- %s %s (%s): %s (estimate), %s. Action: %s."
                   % (f["kind"], name, where, load, last, f["action"]))
    return out


def render_table(report, show_all=False, width=44, top=30):
    """A compact text table, most costly first. Every figure is an estimate."""
    w = report["window"]
    a = report["always_on"]
    out = []
    out.append("Configured versus used: %s to %s (%d days of transcripts%s), "
               "%d sessions, %d turns"
               % (w["since"], w["until"], w["real_days"],
                  ", asked for %d" % w["asked_days"] if w["capped_by_transcripts"] else "",
                  w["sessions"], w["turns"]))
    line = ("Always on, every session: about %s tokens per turn (estimate). Window: about %s tokens"
            % (_tok(a["every_session_tokens_per_turn"]), _tok(a["window_tokens"])))
    if a["usd_cache_read"] is not None:
        line += ", about %s at the cache read rate, plus about %s for first-turn cache writes" % (
            _usd(a["usd_cache_read"]), _usd(a["usd_first_turn_write"]))
    out.append(line + ".")
    if a["by_kind"]:
        out.append("By kind: " + ", ".join("%s %s" % (k, _tok(v)) for k, v in a["by_kind"].items()))
    out.append("")
    head = ("kind", "name", "scope", "tok/turn", "turns", "est tok", "est $", "uses", "last used", "note")
    rows = [head]
    for it in report["items"]:
        if it["kind"] in ("hook",) or (it.get("part_of") and not show_all):
            continue
        if not show_all and not it["always_on"] and not it.get("uses"):
            continue
        note = ""
        if it["scope"] == "builtin":
            note = "built in"
        elif it.get("gone"):
            note = "gone"
        elif it.get("new"):
            note = "new"
        elif it["kind"] == "memory":
            note = "%d lines" % it.get("lines", 0)
        elif it.get("enabled") is False:
            note = "off"
        elif it["kind"] in USE_KINDS and not it.get("uses"):
            note = "UNUSED"
        name = it["name"] if len(it["name"]) <= width else it["name"][:width - 3] + "..."
        rows.append((it["kind"], name, it["scope"], _tok(it.get("tokens_per_turn")),
                     str(it["turns"]), _tok(it.get("window_tokens")), _usd(it.get("usd")),
                     str(it.get("uses", "")) if it["kind"] in USE_KINDS else "",
                     it.get("last_used") or ("never" if it["kind"] in USE_KINDS else ""), note))
    more = 0
    if not show_all and len(rows) > top + 1:
        more = len(rows) - top - 1
        rows = rows[:top + 1]
    widths = [max(len(r[i]) for r in rows) for i in range(len(head))]
    for n, r in enumerate(rows):
        out.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip())
        if n == 0:
            out.append("  ".join("-" * widths[i] for i in range(len(head))))
    if more:
        out.append("(%d more rows: --all shows them)" % more)
    out.append("")
    out.append("Set up but unused:")
    out.extend(report["weekly_block"] or ["- nothing flagged"])
    f = report["findings"]
    extra = []
    for key, label in (("duplicates", "duplicate"), ("large_memory", "large memory file"),
                       ("single_project", "used in one project only"),
                       ("new_no_history", "new, no history yet"),
                       ("hooks_no_run_recorded", "hook with no run recorded (no action)")):
        if f.get(key):
            extra.append("%d %s" % (len(f[key]), label))
    if extra:
        out.append("Also: " + ", ".join(extra) + ". See the JSON for details.")
    for p in report["problems"]:
        out.append("Could not read %s: %s" % (p["path"], p["problem"]))
    out.append("All token and dollar figures are estimates (characters divided by %d)."
               % int(CHARS_PER_TOKEN))
    return "\n".join(out).replace(u"\u2014", "-").replace(u"\u2013", "-")


def _esc(val):
    if isinstance(val, str):
        return html.escape(val, quote=True)
    if isinstance(val, list):
        return [_esc(v) for v in val]
    if isinstance(val, dict):
        return collections.OrderedDict((html.escape(str(k), quote=True), _esc(v))
                                       for k, v in val.items())
    return val


def page_data(report, top=15, escape=True):
    """An HTML-safe block for the coach page: every string is already escaped.

    The page builder can drop these strings straight into markup. Pass
    escape=False when the page sets them with textContent instead, or the
    entities would show as text.
    """
    rows = []
    for it in report["items"]:
        if it["kind"] == "hook" or it.get("part_of") or not it.get("tokens_per_turn"):
            continue
        rows.append(collections.OrderedDict([
            ("kind", it["kind"]), ("name", it["name"]), ("scope", it["scope"]),
            ("tokens_per_turn", it["tokens_per_turn"]), ("window_tokens", it["window_tokens"]),
            ("usd", it["usd"]), ("uses", it.get("uses") if it["kind"] in USE_KINDS else None),
            ("last_used", it.get("last_used")),
            ("unused", bool(it["kind"] in USE_KINDS and not it.get("uses")
                            and it["scope"] != "builtin"))]))
        if len(rows) >= top:
            break
    a = report["always_on"]
    block = collections.OrderedDict([
        ("title", "Set up but unused"),
        ("window", "%s to %s, %d days" % (report["window"]["since"], report["window"]["until"],
                                          report["window"]["real_days"])),
        ("turns", report["window"]["turns"]),
        ("tokens_per_turn", a["every_session_tokens_per_turn"]),
        ("window_tokens", a["window_tokens"]),
        ("usd_cache_read", a["usd_cache_read"]),
        ("usd_first_turn_write", a["usd_first_turn_write"]),
        ("by_kind", a["by_kind"]),
        ("lines", [ln[2:] for ln in report["weekly_block"]]),
        ("rows", rows),
        ("note", "Every figure is an estimate: characters divided by %d, times turns."
                 % int(CHARS_PER_TOKEN))])
    return _esc(block) if escape else block


def page_json(report, top=15, escape=True):
    """page_data as JSON text that is safe inside a script tag."""
    blob = json.dumps(page_data(report, top, escape), separators=(",", ":"))
    return (blob.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace(u"\u2028", "\\u2028").replace(u"\u2029", "\\u2029"))


def audit(days=DEFAULT_DAYS, since=None, until=None, claude=None, home_json=None,
          projects_dir=None, managed=None, prices_path=None, new_days=NEW_DAYS,
          subagents=True, now=None):
    """Run the whole audit. Returns the report dict (JSON-serialisable)."""
    now = time.time() if now is None else now
    standard = since is None and until is None and days == DEFAULT_DAYS
    until = now if until is None else until
    since = until - days * 86400 if since is None else since
    claude = claude or C.claude_dir()
    home_json = home_json or home_json_path(claude)
    usage = scan_usage(since, until, projects_dir or os.path.join(claude, "projects"), subagents)
    inv = inventory(claude, home_json, sorted(p for p in usage["by_project"] if p), managed)
    return build_report(inv, usage, since, until, lifetime_usage(home_json),
                        load_prices(prices_path), new_days, now, standard)


def audit_dir(root=None):
    """Where saved audits live: <coach home>/reports/config-audit."""
    return os.path.join(root or C.reports_dir(), "config-audit")


def save(report, root=None):
    """Write the report as <reports>/config-audit/YYYY-MM-DD.json. Returns the path.

    Only the standard run (the default days, ending now) gets that name. Any other
    window is saved beside it with the window in its name, so it never replaces
    the one that the page reads.
    """
    folder = audit_dir(root)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    w = report["window"]
    name = report["generated"][:10]
    if not w.get("standard"):
        name += "-window-%s-to-%s" % (w["since"], w["until"])
    path = os.path.join(folder, name + ".json")
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as fh:
        fh.write(X._dump(report, 1) + "\n")
    os.replace(tmp, path)
    return path


def latest(root=None):
    """The newest saved report, or None. For the page builder."""
    folder = audit_dir(root)
    try:
        names = sorted(n for n in os.listdir(folder)
                       if re.match(r"^\d{4}-\d{2}-\d{2}\.json$", n))
    except OSError:
        return None
    for name in reversed(names):
        try:
            with open(os.path.join(folder, name)) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and "always_on" in data and "items" in data:
            return data
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="What is configured in Claude Code, what it loads on every turn, and "
                    "what was never used. Read only. All figures are estimates.")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help="window length in days (default %d), capped by the transcripts that exist"
                         % DEFAULT_DAYS)
    ap.add_argument("--since", help="start instead of --days: YYYY-MM-DD, Nd, yesterday")
    ap.add_argument("--until", default="now", help="end, exclusive (default now)")
    ap.add_argument("--format", choices=("table", "json", "weekly", "page"), default="table")
    ap.add_argument("--all", action="store_true", help="table: also list plugin parts and idle items")
    ap.add_argument("--out", help="write here instead of stdout")
    ap.add_argument("--save", action="store_true",
                    help="also keep the JSON under <coach home>/reports/config-audit/")
    ap.add_argument("--new-days", type=int, default=NEW_DAYS,
                    help="installed this recently counts as new, not unused (default %d)" % NEW_DAYS)
    ap.add_argument("--no-subagents", action="store_true", help="skip subagent transcripts")
    ap.add_argument("--claude-dir", help="override the Claude Code config folder (for tests)")
    ap.add_argument("--home-json", help="override the .claude.json file (for tests)")
    ap.add_argument("--projects-dir", help="override the transcripts folder (for tests)")
    ap.add_argument("--managed-dir", help="override the managed settings folder (for tests)")
    ap.add_argument("--prices", help="price table (default coach/prices.json when present)")
    args = ap.parse_args(argv)
    if args.days < 1:
        ap.error("--days must be at least 1")
    try:
        until = None if args.until.strip().lower() == "now" else C.parse_when(args.until)
        since = C.parse_when(args.since) if args.since else None
    except ValueError as exc:
        ap.error(str(exc))
    if since is not None and (time.time() if until is None else until) <= since:
        ap.error("--until must be after --since")
    report = audit(args.days, since, until, args.claude_dir, args.home_json, args.projects_dir,
                   args.managed_dir, args.prices, args.new_days, not args.no_subagents)
    if args.save:
        sys.stderr.write("saved %s\n" % save(report))
    if args.format == "json":
        text = X._dump(report, 1)
    elif args.format == "weekly":
        text = "\n".join(["Set up but unused"] + (report["weekly_block"] or ["- nothing flagged"]))
    elif args.format == "page":
        text = page_json(report)
    else:
        text = render_table(report, args.all)
    if args.out:
        folder = os.path.dirname(os.path.abspath(args.out))
        if not os.path.isdir(folder):
            os.makedirs(folder)
        with open(args.out, "w") as fh:
            fh.write(text + "\n")
        sys.stderr.write("wrote %s\n" % args.out)
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(C.run_main(main))
