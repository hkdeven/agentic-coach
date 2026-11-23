#!/usr/bin/env python3
"""Build the one-page Agentic Coach site from the reports and digests.

Reads daily, weekly, baseline and digests under the coach reports root
(~/.claude/coach/reports, or under $COACH_HOME) and writes one self-contained
file, ~/.claude/coach/site/index.html. Never writes into the plugin folder.
Python 3 stdlib only.

Report and transcript text is untrusted data. It is HTML-escaped on the way
into the page and is never executed or followed.

Examples:
  python3 coach/build_site.py
  python3 coach/build_site.py --card
  python3 coach/build_site.py --reports-dir r --out out/index.html --no-trends
"""
import argparse
import bisect
import glob
import html
import importlib
import json
import math
import os
import re
import sys
import textwrap
import time

try:
    from . import common as C
    from . import extract as E
    from . import ledger as L
    from . import pricing as P
    from . import projects as PR
    from . import scope as SC
except ImportError:  # run as a script
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import common as C
    import extract as E
    import ledger as L
    import pricing as P
    import projects as PR
    import scope as SC


def optional(name):
    """One of the coach modules the page shows data from, or None when it cannot
    be loaded. The page is built without it then; it never fails to build."""
    try:
        if __package__:
            return importlib.import_module("." + name, __package__)
        return importlib.import_module(name)
    except Exception:
        return None


def guarded(fn, *args, **kwargs):
    """fn(...) or None. A failure in one module leaves the rest of the page intact."""
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


EVIDENCE_DAILIES = 7      # newest dailies whose items carry their evidence
TREND_DAYS = 30
MAX_DAILIES = 60
MAX_WEEKLIES = 26
CONTEXT_LINE = 600000
DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
SID_RE = re.compile(r"\b[0-9a-f]{8}\b")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ---------- small helpers ----------

def esc(text):
    return html.escape("" if text is None else str(text), quote=True)


def clean(text):
    """Long dashes to hyphens, so the page never carries them."""
    return (text or "").replace(chr(0x2014), "-").replace(chr(0x2013), "-")


def cut(text, limit):
    text = (text or "").strip()
    if len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


def cap(text):
    return text[:1].upper() + text[1:] if text else text


def read_text(path):
    try:
        with open(path, "rb") as fh:
            return clean(fh.read().decode("utf-8", "replace"))
    except OSError:
        return None


def read_json(path):
    try:
        with open(path, "rb") as fh:
            return json.loads(clean(fh.read().decode("utf-8", "replace")))
    except (OSError, ValueError):
        return None


def date_parts(day):
    return int(day[0:4]), int(day[5:7]), int(day[8:10])


def real_date(day):
    try:
        y, m, d = date_parts(day)
        return time.strftime("%Y-%m-%d", time.localtime(time.mktime((y, m, d, 12, 0, 0, 0, 0, -1)))) == day
    except (ValueError, OverflowError, IndexError):
        return False


def short_date(day):
    y, m, d = date_parts(day)
    return "%d %s" % (d, MONTHS[m - 1])


def long_date(day):
    y, m, d = date_parts(day)
    wd = time.localtime(time.mktime((y, m, d, 12, 0, 0, 0, 0, -1))).tm_wday
    return "%s %d %s %d" % (WEEKDAYS[wd], d, MONTHS[m - 1], y)


def day_shift(day, delta):
    y, m, d = date_parts(day)
    return time.strftime("%Y-%m-%d", time.localtime(time.mktime((y, m, d + delta, 12, 0, 0, 0, 0, -1))))


def json_for_script(obj):
    """JSON that is safe inside a script element: no raw <, >, &, quotes in data."""
    s = json.dumps(obj, ensure_ascii=True, separators=(",", ":"))
    s = re.sub(r"\\(.)", lambda m: "\\u0022" if m.group(1) == '"' else m.group(0), s)
    return (s.replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026").replace("'", "\\u0027"))


# ---------- work that is never shown ----------

class Hide(object):
    """The person's exclude rules, as the page applies them.

    rules: the "exclude" list from config.json. ids: first 8 characters of the
    sessions the ledger found to be excluded. With neither, nothing is hidden
    and every check is False.
    """

    def __init__(self, rules=None, ids=(), scope=None):
        self.rules = [r for r in rules or [] if isinstance(r, dict)]
        self.ids = set(str(i)[:8].lower() for i in ids or () if i)
        self.on = bool(self.rules or self.ids)
        # the account and date settings work on transcripts and the ledger, not on text
        self.scope = scope if scope is not None else SC.Scope(cfg={}, exclude=self.rules)

    def plus(self, ids):
        return Hide(self.rules, self.ids | set(ids), self.scope) if ids else self

    def text(self, *texts):
        """True when any of the texts names excluded work."""
        if not self.on:
            return False
        return any(PR.text_hidden(self.rules, t, self.ids)
                   for t in texts if isinstance(t, str) and t)

    def deep(self, obj):
        """text() over every string inside a dict or list, keys included."""
        if not self.on:
            return False
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, dict):
            return any(self.deep(k) or self.deep(v) for k, v in obj.items())
        if isinstance(obj, (list, tuple)):
            return any(self.deep(v) for v in obj)
        return False

    def session(self, sid, texts=()):
        """True for a session the rules cover, by id or by a text it carries."""
        if not self.on:
            return False
        sid = str(sid or "").lower()
        if sid and sid[:8] in self.ids:
            return True
        return PR.excluded(self.rules, sid, [t for t in texts if isinstance(t, str) and t])


NO_HIDE = Hide()


def load_hide(ledger_path=None):
    """The exclude rules from config.json plus the ledger's list of excluded sessions."""
    cfg = guarded(C.load_config) or {}
    rules = cfg.get("exclude") or []
    got = guarded(L.load, ledger_path)
    led = got[0] if isinstance(got, tuple) and isinstance(got[0], dict) else {}
    ids = led.get("excluded")
    return Hide(rules, ids if isinstance(ids, list) else (), SC.Scope(cfg))


def visible_md(text, hide):
    """A markdown text without the lines and bullets that name excluded work.

    A hidden bullet goes with the lines nested under it. If what is left still
    names excluded work (a match that runs across lines), nothing is shown.
    """
    if not text or not hide.text(text):
        return text
    out = []
    skip = None
    for line in text.split("\n"):
        indent = len(line) - len(line.lstrip())
        if skip is not None:
            if not line.strip():
                out.append(line)
                continue
            if indent > skip:
                continue
            skip = None
        if hide.text(line):
            if _LIST_RE.match(line):
                skip = indent
            continue
        out.append(line)
    text = "\n".join(out)
    return "" if hide.text(text) else text


_USD_CLAUSE_RE = re.compile(r"^.+ \$\d[\d,]*(?:\.\d+)?$")


def visible_totals(text, hide):
    """A `Totals:` line without the clause of an excluded project.

    Only a clause of the `Top projects:` list can be cut cleanly. A hidden name
    anywhere else leaves the whole line out.
    """
    if not hide.text(text):
        return text
    out = []
    for sent in re.split(r"(?<=\.)\s+(?=[A-Z])", text):
        if not hide.text(sent):
            out.append(sent)
            continue
        m = re.match(r"^(Top projects:)\s*(.*?)\.?$", sent)
        clauses = m.group(2).split(", ") if m else []
        if not clauses or not all(_USD_CLAUSE_RE.match(c) for c in clauses):
            return ""
        keep = [c for c in clauses if not hide.text(c)]
        if keep:
            out.append("%s %s." % (m.group(1), ", ".join(keep)))
    text = " ".join(out)
    return "" if hide.text(text) else text


def visible_sentences(text, hide):
    """A line of prose without the sentences that name excluded work."""
    if not hide.text(text):
        return text
    text = " ".join(x for x in re.split(r"(?<=[.;])\s+", text) if not hide.text(x))
    return "" if hide.text(text) else text


# ---------- markdown subset to HTML ----------

_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_LIST_RE = re.compile(r"^(\s*)([-*]|\d+[.)])\s+(.*)$")
_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _link(m):
    url = m.group(2)
    if url.startswith("https://") or url.startswith("http://"):
        return '<a href="%s" target="_blank" rel="noopener noreferrer">%s</a>' % (url, m.group(1))
    return m.group(1)


def md_inline(text):
    """Inline markdown: code spans, bold, http links. Everything is escaped first."""
    out = []
    for part in re.split(r"(`[^`]+`)", text or ""):
        if len(part) > 2 and part.startswith("`") and part.endswith("`"):
            out.append("<code>%s</code>" % esc(part[1:-1]))
        elif part:
            s = esc(part)
            s = _BOLD_RE.sub(r"<strong>\1</strong>", s)
            s = _LINK_RE.sub(_link, s)
            out.append(s)
    return "".join(out)


def _split_row(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]


def _render_list(items, collapsible, depth=0):
    """items: list of dicts {ordered, num, text, children}."""
    if not items:
        return ""
    ordered = items[0]["ordered"]
    for i, it in enumerate(items):
        if it["ordered"] != ordered:  # a different kind of list starts here
            return (_render_list(items[:i], collapsible, depth)
                    + _render_list(items[i:], collapsible, depth))
    fold = collapsible and depth == 0 and ordered
    tag = "ol" if ordered else "ul"
    cls = ' class="patterns"' if fold else ""
    out = ["<%s%s>" % (tag, cls)]
    for it in items:
        kids = _render_list(it["children"], collapsible, depth + 1)
        text = it["text"]
        if fold:
            num = '<span class="num">%s</span>' % esc(it["num"])
            if kids:
                out.append('<li><details><summary>%s<span class="ptext">%s</span></summary>%s</details></li>'
                           % (num, md_inline(text), kids))
            else:
                out.append('<li><div class="pflat">%s<span class="ptext">%s</span></div></li>'
                           % (num, md_inline(text)))
            continue
        m = re.match(r"^(Fix|Do instead|Proposal):\s*(.*)$", text, re.S)
        if m:
            body = '<span class="pill fix">%s</span> %s' % (esc(m.group(1)), md_inline(m.group(2)))
            li = '<li class="fixline">'
        else:
            body = md_inline(text)
            li = '<li value="%s">' % esc(it["num"]) if ordered and it["num"] else "<li>"
        out.append("%s%s%s</li>" % (li, body, kids))
    out.append("</%s>" % tag)
    return "".join(out)


def _nest(flat):
    root = []
    stack = [(-1, root)]
    for indent, ordered, num, text in flat:
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        node = {"ordered": ordered, "num": num, "text": text, "children": []}
        stack[-1][1].append(node)
        stack.append((indent, node["children"]))
    return root


def md_to_html(text, collapsible=False, skip_h1=False):
    """Headings, paragraphs, nested lists, tables, fenced code, bold, inline code."""
    lines = clean(text or "").replace("\r\n", "\n").split("\n")
    out = []
    para = []
    i = 0
    n = len(lines)

    def flush():
        if para:
            out.append("<p>%s</p>" % md_inline(" ".join(para)))
            del para[:]

    while i < n:
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            flush()
            i += 1
            code = []
            while i < n and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            out.append('<div class="scroll"><pre><code>%s</code></pre></div>' % esc("\n".join(code)))
            continue
        m = _HEAD_RE.match(line)
        if m:
            flush()
            level = len(m.group(1))
            if not (skip_h1 and level == 1):
                out.append('<h%d class="md-h%d">%s</h%d>'
                           % (min(level + 2, 6), level, md_inline(m.group(2)), min(level + 2, 6)))
            i += 1
            continue
        if stripped.startswith("|") and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]) \
                and "|" in lines[i + 1]:
            flush()
            head = _split_row(line)
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(_split_row(lines[i]))
                i += 1
            t = ['<div class="scroll"><table><thead><tr>']
            t.extend("<th>%s</th>" % md_inline(c) for c in head)
            t.append("</tr></thead><tbody>")
            for r in rows:
                t.append("<tr>%s</tr>" % "".join("<td>%s</td>" % md_inline(c) for c in r))
            t.append("</tbody></table></div>")
            out.append("".join(t))
            continue
        m = _LIST_RE.match(line)
        if m:
            flush()
            flat = []
            while i < n:
                cur = lines[i]
                lm = _LIST_RE.match(cur)
                if lm:
                    marker = lm.group(2)
                    ordered = marker[0].isdigit()
                    flat.append([len(lm.group(1).expandtabs(4)), ordered,
                                 marker[:-1] if ordered else "", lm.group(3).strip()])
                    i += 1
                elif not cur.strip():
                    j = i + 1
                    while j < n and not lines[j].strip():
                        j += 1
                    if j < n and (_LIST_RE.match(lines[j]) or lines[j][:1] in (" ", "\t")) \
                            and not lines[j].strip().startswith("```"):
                        i = j
                    else:
                        break
                elif cur[:1] in (" ", "\t") and not cur.strip().startswith("```"):
                    flat[-1][3] += " " + cur.strip()
                    i += 1
                else:
                    break
            out.append(_render_list(_nest(flat), collapsible))
            continue
        if not stripped:
            flush()
            i += 1
            continue
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            flush()
            out.append("<hr>")
            i += 1
            continue
        para.append(stripped)
        i += 1
    flush()
    return "\n".join(out)


def md_title(text, fallback):
    for line in (text or "").split("\n"):
        m = _HEAD_RE.match(line)
        if m and len(m.group(1)) == 1:
            return m.group(2)
    return fallback


# ---------- report parsing ----------

_ITEM_RE = re.compile(r"^\s*(\d+)[.)]\s+(?:What happened:\s*)?(.*)$")
_FIELD_RE = re.compile(r"^\s*(?:[-*]\s+)?(Do instead|Trend|Evidence):\s*(.*)$")
_FIELD_KEYS = {"Do instead": "do", "Trend": "trend", "Evidence": "evidence_cmd"}
_REPEAT_RE = re.compile(r"\(repeat\b|\brepeat of\b|\ba repeat\b|\brepeat from\b|^\W*repeat\b", re.I)
_TREND_RE = re.compile(r"\b(new|repeat(?:ing|ed|s)?|better|worse)\b", re.I)
_TAG_PAREN_RE = re.compile(r"\((session [0-9a-f]{8}\b[^()]*)\)")

FT = optional("followthrough")
EV = optional("evidence")
# The repeat marker: "(repeat of DATE)" or "(repeat of DATE, given N times)", and
# on a weekly Trend line "repeat since DATE, given N times".
_MARKER_RE = getattr(FT, "MARKER_RE", None) or re.compile(
    r"\(repeat of (\d{4}-\d{2}-\d{2})(?:, given (\d+) times)?\)")
_TREND_MARKER_RE = getattr(FT, "TREND_MARKER_RE", None) or re.compile(
    r"\brepeat since (\d{4}-\d{2}-\d{2})(?:, given (\d+) times)?")


def find_tag(text):
    """The evidence tag of an item, the text inside its round brackets, or None."""
    for m in _TAG_PAREN_RE.finditer(text or ""):
        tag = m.group(1).strip()
        if EV is None or guarded(lambda: EV.parse_tag(tag)):
            return tag
    return None


def find_marker(what, trend=""):
    """(first date, times given or None) from an item's repeat marker, or (None, None)."""
    m = _MARKER_RE.search(what or "") or _TREND_MARKER_RE.search(trend or "")
    if not m:
        return None, None
    return m.group(1), int(m.group(2)) if m.group(2) else None


def find_ids(text):
    out = []
    for sid in SID_RE.findall(text or ""):
        if sid not in out and re.search(r"\d", sid) and re.search(r"[a-f]", sid):
            out.append(sid)
    return out


def trend_state(text):
    m = _TREND_RE.search(text or "")
    if not m:
        return ""
    word = m.group(1).lower()
    return "repeat" if word.startswith("repeat") else word


def parse_items(lines):
    """Numbered items with What happened, Do instead and optional Trend and
    Evidence lines. Each item also gets its evidence tag and repeat marker."""
    items = []
    notes = []
    cur = None
    field = None
    for raw in lines:
        line = raw.replace("**", "")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _FIELD_RE.match(line)
        if m and cur is not None:
            field = _FIELD_KEYS[m.group(1)]
            cur[field] = (cur[field] + " " + m.group(2)).strip()
            continue
        m = _ITEM_RE.match(line)
        if m:
            cur = {"n": int(m.group(1)), "what": m.group(2).strip(), "do": "", "trend": "",
                   "evidence_cmd": ""}
            items.append(cur)
            field = "what"
            continue
        if cur is not None:
            cur[field] = (cur[field] + " " + line.strip()).strip()
        else:
            notes.append(line.strip())
    for it in items:
        it["ids"] = find_ids(it["what"] + " " + it["do"])[:6]
        it["tag"] = find_tag(it["what"])
        it["repeat_since"], it["times"] = find_marker(it["what"], it["trend"])
        it["evidence_cmd"] = cut(it["evidence_cmd"], 300) or None
        it["repeat"] = bool(_REPEAT_RE.search(it["what"]) or _REPEAT_RE.search(it["do"]))
        it["state"] = trend_state(it["trend"]) if it["trend"] else ""
        for k in ("what", "do", "trend"):
            it[k] = cut(it[k], 900)
            if k != "what" and not SID_RE.match(it[k]):
                it[k] = cap(it[k])
    return items, " ".join(notes)


_TOTALS_RE = re.compile(r"^\s*(?:[-*]\s+)?Totals:\s*(.*)$")
_CHECKED_RE = re.compile(r"^\s*(?:[-*]\s+)?Checked:\s*(.*)$")


def take_checked(lines):
    """(the other lines, the text of the `Checked:` line or "")."""
    rest, checked = [], ""
    for line in lines:
        m = _CHECKED_RE.match(line.replace("**", ""))
        if m:
            checked = m.group(1).strip()
        else:
            rest.append(line)
    return rest, cut(checked, 300)


def renumber(items, kept):
    """kept, numbered from 1 again when something was taken out of items."""
    if len(kept) != len(items):
        for i, it in enumerate(kept):
            it["n"] = i + 1
    return kept


def visible_items(items, hide):
    if not hide.on:
        return items
    return renumber(items, [it for it in items if not hide.text(
        it["what"], it["do"], it["trend"], it.get("evidence_cmd"))])


def parse_daily(text, hide=NO_HIDE):
    """Items, the note, the closing `Totals:` line (tokens and cost for the day)
    and the `Checked:` line that may follow it. Excluded work is left out."""
    lines, totals = [], ""
    for line in (text or "").split("\n"):
        m = _TOTALS_RE.match(line.replace("**", ""))
        if m:
            totals = m.group(1).strip()
        else:
            lines.append(line)
    lines, checked = take_checked(lines)
    items, note = parse_items(lines)
    if hide.on and hide.text(text or ""):
        # A report that names excluded work was written with that work in its
        # totals. A total it is part of is not shown, with or without the name.
        totals = ""
    return {"items": visible_items(items, hide)[:5],
            "note": "" if hide.text(note) else cut(note, 400),
            "totals": cut(visible_totals(totals, hide), 500),
            "checked": "" if hide.text(checked) else checked}


_SECTION_RE = re.compile(r"^##\s+(.*)$")
_PROP_RE = re.compile(r"^###\s+(P\d+)\s*[:.]?\s*(.*)$")
_PFIELD_RE = re.compile(r"^\s*(?:[-*]\s+)?(Why|Change|Where):\s?(.*)$")


def split_change(raw):
    """Change text to (note, code). Fenced blocks become the code to copy."""
    lines = raw.split("\n")
    code, note = [], []
    in_fence = False
    saw_fence = False
    for line in lines:
        if line.strip().startswith("```"):
            if in_fence:
                code.append("")
            in_fence = not in_fence
            saw_fence = True
            continue
        (code if in_fence else note).append(line)
    if saw_fence:
        return (cut(" ".join(x.strip() for x in note if x.strip()), 600),
                cut(textwrap.dedent("\n".join(code)).strip("\n"), 6000))
    body = textwrap.dedent("\n".join(lines)).strip()
    if len(body) > 2 and body.startswith("`") and body.endswith("`") and "`" not in body[1:-1]:
        body = body[1:-1]
    return "", cut(body, 6000)


def _is_fence(line):
    """A line that opens or closes a fenced block (not a one-line ```x``` span)."""
    s = line.strip()
    return s.startswith("```") and "```" not in s[3:]


_KNOWN_SECTION_RE = re.compile(
    r"^##\s+(follow-through|week in numbers|cost by project|before and after|what cost"
    r"|set up but unused|proposed)", re.I)
FT_STATUSES = ("worse", "recurred", "same", "better", "not seen again", "cannot measure", "no data")
_FT_RE = re.compile(r"^(%s)(?:,\s*applied on (\d{4}-\d{2}-\d{2}))?\s*:\s*(.*)$"
                    % "|".join(FT_STATUSES), re.I | re.S)
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")


def bullet_lines(lines):
    """(bullets, other lines): each bullet with the indented lines under it joined on."""
    bullets, rest = [], []
    for raw in lines:
        line = raw.replace("**", "")
        if not line.strip():
            continue
        m = _BULLET_RE.match(line)
        if m:
            bullets.append(m.group(1).strip())
        elif bullets and line[:1] in (" ", "\t"):
            bullets[-1] += " " + line.strip()
        else:
            rest.append(line.strip())
    return bullets, rest


def parse_followthrough(lines):
    """`## Follow-through` bullets to {status, applied, text}. The status is one of FT_STATUSES."""
    out = []
    for text in bullet_lines(lines)[0][:8]:
        m = _FT_RE.match(text)
        if m:
            out.append({"status": m.group(1).lower(), "applied": m.group(2),
                        "text": cut(m.group(3), 500)})
        else:
            out.append({"status": "", "applied": None, "text": cut(text, 500)})
    return out


def parse_unused(lines):
    """`## Set up but unused`: at most five lines, then the `Always on:` sentence."""
    bullets, rest = bullet_lines(lines)
    total = ""
    for line in rest:
        if line.lower().startswith("always on:"):
            total = line
        else:
            bullets.append(line)
    return {"lines": [cut(b, 400) for b in bullets[:5]], "total": cut(total, 400)}


def parse_proposals(lines):
    props = []
    cur = None
    field = None
    in_fence = False
    for line in lines:
        m = _PROP_RE.match(line.replace("**", ""))     # also ends a fence left open
        if m:
            cur = {"id": m.group(1), "name": cut(m.group(2), 160), "why": [], "change": [], "where": []}
            props.append(cur)
            field = None
            in_fence = False
            continue
        if cur is None:
            continue
        if _is_fence(line):
            in_fence = not in_fence
        m = None if in_fence else _PFIELD_RE.match(line.replace("**", ""))
        if m:
            field = m.group(1).lower()
            rest = m.group(2)
            if _is_fence(rest):                        # "Change: ```" opens the block
                in_fence = True
                cur[field].append("")
                cur[field].append(rest.strip())
                continue
            cur[field].append(rest)
        elif field:
            cur[field].append(line)
    out = []
    for p in props[:5]:
        note, code = split_change("\n".join(p["change"]))
        out.append({"id": p["id"], "name": p["name"],
                    "why": cut(" ".join(x.strip() for x in p["why"] if x.strip()), 700),
                    "note": note, "change": code,
                    "where": cut(" ".join(x.strip() for x in p["where"] if x.strip()), 300)})
    return out


def parse_weekly(text, hide=NO_HIDE):
    sections = {}
    name = ""
    in_fence = False
    for line in (text or "").split("\n"):
        m = _KNOWN_SECTION_RE.match(line)
        if m and not any(k.startswith(m.group(1).lower()) for k in sections):
            in_fence = False                   # a report heading ends a fence left open,
                                               # the first time it is met
        if _is_fence(line):
            in_fence = not in_fence
        elif not in_fence:
            m = _SECTION_RE.match(line)
            if m:
                name = m.group(1).strip().lower()
                sections[name] = []
                continue
            if line.startswith("# "):
                continue
        sections.setdefault(name, []).append(line)

    def pick(prefix):
        for k, v in sections.items():
            if k.startswith(prefix):
                return v
        return []

    number_lines, checked = take_checked(pick("week in numbers"))
    numbers = " ".join(x.strip() for x in number_lines if x.strip())
    cost_lines, late = take_checked(pick("what cost"))
    checked = checked or late
    items, _note = parse_items(cost_lines)
    note = " ".join(x.strip() for x in sections.get("", []) if x.strip())
    compare_lines = [cut(re.sub(r"^\s*[-*]\s+", "", x.replace("**", "")).strip(), 400)
                     for x in pick("before and after") if x.strip()][:6]
    unused = parse_unused(pick("set up but unused"))
    if hide.on and hide.text(text or ""):
        numbers = ""       # written with excluded work in its figures: not shown
    week = {"numbers": cut(visible_sentences(numbers, hide), 900),
            "items": visible_items(items, hide)[:5],
            "proposals": [p for p in parse_proposals(pick("proposed")) if not hide.deep(p)],
            "note": "" if hide.text(note) else cut(note, 400),
            "cost": parse_cost_block(pick("cost by project"), hide),
            "checked": "" if hide.text(checked) else checked,
            "followthrough": [f for f in parse_followthrough(pick("follow-through"))
                              if not hide.text(f["text"])],
            "compare_lines": [x for x in compare_lines if not hide.text(x)],
            "unused": {"lines": [x for x in unused["lines"] if not hide.text(x)],
                       "total": "" if hide.text(unused["total"]) else unused["total"]}}
    return week


# ---------- loading ----------

def dated_reports(folder):
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for name in names:
        m = DATE_RE.match(name)
        if m and real_date(m.group(1)):
            out.append((m.group(1), os.path.join(folder, name)))
    out.sort(reverse=True)
    return out


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _session_figures(full, s):
    """What one digest session adds to the digest totals. None where it does not say."""
    tok = s.get("tokens") if isinstance(s.get("tokens"), dict) else {}
    sub = s.get("subagents") if isinstance(s.get("subagents"), dict) else {}
    out = {"sessions": 1, "scheduled_sessions": 1 if s.get("kind") == "scheduled" else 0,
           "turns": _num(s.get("turns"))}
    if not full:                      # a small session: only prompts, output and cost
        out.update(active_min=None, errors=None, corrections=None, compactions=None,
                   out=None, sub_out=None, usd=_num(s.get("usd")))
        return out
    errors = s.get("errors") if isinstance(s.get("errors"), dict) else {}
    comp = s.get("compactions") if isinstance(s.get("compactions"), list) else []
    cost = s.get("cost") if isinstance(s.get("cost"), dict) else {}
    sub_cost = sub.get("cost") if isinstance(sub.get("cost"), dict) else {}
    usd = _num(cost.get("usd"))
    out.update(active_min=_num(s.get("active_min")), errors=_num(errors.get("total")) or 0,
               corrections=_num(s.get("corrections")) or 0,
               compactions=_num(s.get("compactions_total")) or len(comp),
               out=_num(tok.get("out")) or 0,
               sub_out=_num((sub.get("tokens") or {}).get("out")
                            if isinstance(sub.get("tokens"), dict) else 0) or 0,
               usd=None if usd is None else usd + (_num(sub_cost.get("usd")) or 0))
    return out


def _totals_without(totals, dropped):
    """Digest totals with the dropped sessions taken off again.

    A figure stays only when every dropped session carries its own number for
    it; otherwise it becomes None and the page prints nothing for it, never a
    total that still has excluded work in it.
    """
    t = dict(totals) if isinstance(totals, dict) else {}
    figs = [_session_figures(full, s) for full, s in dropped]

    def less(have, key):
        vals = [f[key] for f in figs]
        if _num(have) is None or any(v is None for v in vals):
            return None
        return max(0, have - sum(vals))

    for key in ("sessions", "scheduled_sessions", "turns", "active_min", "errors",
                "corrections", "compactions"):
        if key in t:
            t[key] = less(t[key], key)
    tok = t.get("tokens") if isinstance(t.get("tokens"), dict) else {}
    sub = t.get("subagent_tokens") if isinstance(t.get("subagent_tokens"), dict) else {}
    out = less(tok.get("out", 0), "out")
    sub_out = less(sub.get("out", 0), "sub_out")
    t["tokens"] = None if out is None or sub_out is None else dict(tok, out=out)
    t["subagent_tokens"] = None if t["tokens"] is None else dict(sub, out=sub_out)
    cost = t.get("cost") if isinstance(t.get("cost"), dict) else None
    if cost is not None:
        usd = less(cost.get("usd"), "usd")
        t["cost"] = None if usd is None else dict(cost, usd=round(usd, 2))
    return t


def visible_digest(digest, hide):
    """(the digest without excluded sessions and projects, ids of the sessions dropped).

    A digest written before an exclude rule existed still holds that work. Its
    totals are worked out again from what the dropped sessions say about themselves.
    """
    if not hide.on or not isinstance(digest, dict):
        return digest, set()
    out = dict(digest)
    dropped = []
    names = set()
    for key in ("sessions", "minor_sessions"):
        rows = digest.get(key)
        if not isinstance(rows, list):
            continue
        keep = []
        for s in rows:
            if isinstance(s, dict) and hide.session(s.get("id"), [
                    s.get(k) for k in ("project", "project_name", "title", "cwd",
                                       "scheduled_task")]):
                dropped.append((key == "sessions", s))
                names.add(str(s.get("project_name") or ""))
            else:
                keep.append(s)
        out[key] = keep
    if isinstance(digest.get("projects"), list):
        out["projects"] = [p for p in digest["projects"] if not (
            isinstance(p, dict) and (hide.text(str(p.get("name") or ""))
                                     or str(p.get("name") or "") in names))]
    if dropped:
        out["totals"] = _totals_without(digest.get("totals"), dropped)
    return out, set(str(s.get("id") or "")[:8].lower() for _full, s in dropped) - set([""])


def visible_findings(findings, hide):
    """Detector findings without those of, or about, excluded sessions."""
    if not hide.on or not isinstance(findings, list):
        return findings
    return [f for f in findings if not (isinstance(f, dict) and (
        str(f.get("session") or "")[:8].lower() in hide.ids or hide.deep(f)))]


def digest_counts(digest):
    t = (digest or {}).get("totals") or {}
    if not t:
        return {}
    tok = t.get("tokens") or {}
    sub = t.get("subagent_tokens") or {}
    known = t.get("tokens", {}) is not None        # None: not known without excluded work
    return {"sessions": t.get("sessions", 0), "scheduled": t.get("scheduled_sessions", 0),
            "prompts": t.get("turns", 0), "active_min": t.get("active_min", 0),
            "out_tokens": int(tok.get("out", 0)) + int(sub.get("out", 0)) if known else None,
            "errors": t.get("errors", 0), "corrections": t.get("corrections", 0),
            "compactions": t.get("compactions", 0)}


def session_facts(s):
    tok = s.get("tokens") or {}
    sub = ((s.get("subagents") or {}).get("tokens") or {})
    return {"project": cut(s.get("project") or "?", 120), "title": cut(s.get("title") or "", 80),
            "kind": s.get("kind") or "interactive", "start": s.get("start") or "",
            "active_min": s.get("active_min", 0), "prompts": s.get("turns", 0),
            "out": int(tok.get("out", 0)), "sub_out": int(sub.get("out", 0)),
            "peak": int(tok.get("peak_context", 0))}


def finding_view(f):
    return {"pattern": cut(str(f.get("pattern") or "?"), 60),
            "severity": str(f.get("severity") or "low")[:10],
            "evidence": cut(str(f.get("evidence") or ""), 320),
            "suggestion": cut(str(f.get("suggestion") or ""), 260)}


def attach_evidence(entry, digest, findings):
    """Add session facts and detector findings for the ids named in the items."""
    named = []
    for it in entry["items"]:
        for sid in it["ids"]:
            if sid not in named:
                named.append(sid)
    sessions = {}
    for s in (digest or {}).get("sessions") or []:
        sid = (s.get("id") or "")[:8]
        if sid in named:
            sessions[sid] = session_facts(s)
    for s in (digest or {}).get("minor_sessions") or []:
        sid = (s.get("id") or "")[:8]
        if sid in named and sid not in sessions:
            sessions[sid] = {"project": cut(s.get("project") or "?", 120), "title": "",
                             "kind": s.get("kind") or "interactive", "start": "",
                             "active_min": None, "prompts": s.get("turns", 0),
                             "out": int(s.get("out_tokens", 0)), "sub_out": 0, "peak": 0}
    by_id = {}
    other = []
    for f in findings if isinstance(findings, list) else []:
        if not isinstance(f, dict):
            continue
        sid = str(f.get("session") or "")[:8]
        view = finding_view(f)
        if sid in named:
            by_id.setdefault(sid, []).append(view)
        else:
            view["session"] = sid
            other.append(view)
    for sid in by_id:
        by_id[sid] = by_id[sid][:8]
    entry["sessions"] = sessions
    entry["findings"] = by_id
    entry["other"] = other[:12]
    entry["counts"] = digest_counts(digest)


def read_digest(path, hide):
    """(digest, exclude rules widened by the sessions dropped from it)."""
    raw = read_json(path)
    if (isinstance(raw, dict) and hide.scope.by_day
            and (raw.get("scope_sig") or "") != hide.scope.day_sig()):
        return None, hide          # counted under other account or date settings
    try:
        digest, gone = visible_digest(raw, hide)
    except (AttributeError, TypeError, ValueError, KeyError, OverflowError):
        if hide.on:
            return None, hide          # cannot be checked, so it is not shown
        return read_json(path), hide
    return digest, hide.plus(gone)


def load_dailies(reports_dir, hide=NO_HIDE):
    out = []
    digests = os.path.join(reports_dir, "digests")
    for day, path in dated_reports(os.path.join(reports_dir, "daily"))[:MAX_DAILIES]:
        if hide.scope.date_hidden(day):
            continue                      # a day the person left out is not shown at all
        digest, seen = read_digest(os.path.join(digests, "daily-%s.json" % day), hide)
        entry = parse_daily(read_text(path), seen)
        entry["date"] = day
        entry["label"] = long_date(day)
        entry["short"] = short_date(day)
        try:
            entry["cost"] = digest_cost(digest)
        except (AttributeError, TypeError, ValueError, KeyError, OverflowError):
            entry["cost"] = {}
        try:
            attach_evidence(entry, digest, visible_findings(
                read_json(os.path.join(digests, "daily-%s.findings.json" % day)), seen))
        except (AttributeError, TypeError, ValueError, KeyError, OverflowError):
            entry.update(sessions={}, findings={}, other=[], counts={})   # malformed digest
        out.append(entry)
    return out


def load_weeklies(reports_dir, hide=NO_HIDE):
    out = []
    digests = os.path.join(reports_dir, "digests")
    for day, path in dated_reports(os.path.join(reports_dir, "weekly"))[:MAX_WEEKLIES]:
        raw = read_json(os.path.join(digests, "weekly-%s.json" % day))
        stale = ((raw.get("scope_sig") or "") != hide.scope.day_sig()
                 if isinstance(raw, dict) else True)
        if stale and any(hide.scope.date_hidden(day_shift(day, -i)) for i in range(1, 8)):
            continue        # a review of days the person has since left out is not shown
        digest, seen = read_digest(os.path.join(digests, "weekly-%s.json" % day), hide)
        entry = parse_weekly(read_text(path), seen)
        entry["date"] = day
        entry["label"] = "Week to " + long_date(day)
        try:
            entry["counts"] = digest_counts(digest)
        except (AttributeError, TypeError, ValueError, KeyError, OverflowError):
            entry["counts"] = {}
        out.append(entry)
    return out


def load_baseline(reports_dir, hide=NO_HIDE):
    folder = os.path.join(reports_dir, "baseline")
    summary = visible_md(read_text(os.path.join(folder, "SUMMARY.md")), hide)
    details = []
    newest = 0
    for path in sorted(glob.glob(os.path.join(folder, "*.md"))):
        try:
            newest = max(newest, os.stat(path).st_mtime)
        except OSError:
            pass
        if os.path.basename(path) == "SUMMARY.md":
            continue
        text = visible_md(read_text(path) or "", hide)
        title = md_title(text, os.path.basename(path))
        if hide.text(title) or (hide.on and not text.strip()):
            continue
        details.append((title, text))
    return summary, details, newest


# ---------- trends ----------

def _day_edges(days):
    los, his = [], []
    for day in days:
        y, m, d = date_parts(day)
        los.append(C.epoch_utc_iso(time.mktime((y, m, d, 0, 0, 0, 0, 0, -1))))
        his.append(C.epoch_utc_iso(time.mktime((y, m, d + 1, 0, 0, 0, 0, 0, -1))))
    return los, his


def _route_file(path, los, his, get_acc, seen=None, meta=None, keep=None, days=None):
    """Like extract.scan_file, but feeds each record to the accumulator of its local day.

    meta: an accumulator for the session's title records, which belong to the
    whole session. keep: an optional function from a local day to True or False
    (scope.Gate); days is the list of local days the slots stand for.
    """
    def slot(ts):
        k = bisect.bisect_right(los, ts) - 1
        if k < 0 or ts >= his[k]:
            return None
        if keep is not None and days is not None and not keep(days[k]):
            return None
        return k

    cur = None
    try:
        fh = open(path, "rb")
    except OSError:
        return
    with fh:
        for line in fh:
            if line.startswith(E.UNTIMED_TYPES):
                if meta is not None:
                    try:
                        d = json.loads(line)
                        if isinstance(d, dict) and d.get("type") in ("ai-title", "custom-title"):
                            meta.feed({"type": d["type"], "aiTitle": d.get("aiTitle"),
                                       "customTitle": d.get("customTitle")})
                    except (ValueError, AttributeError, TypeError, KeyError):
                        pass
                continue
            i = line.rfind(E.TS_KEY)
            if i < 0:
                continue
            cur = slot(line[i + 13:i + 32].decode("ascii", "replace"))
            if cur is None:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            k = cur
            ts = d.get("timestamp")
            if isinstance(ts, str):
                k = slot(ts[:19])
                if k is None:
                    continue
            if seen is not None:
                uid = d.get("uuid")
                if uid:
                    if uid in seen:
                        continue
                    seen.add(uid)
            try:
                get_acc(k).feed(d)
            except (AttributeError, TypeError, ValueError, KeyError):
                continue


def empty_day():
    return {"sessions": 0, "scheduled": 0, "prompts": 0, "short": 0, "active_min": 0,
            "out": 0, "sub_out": 0, "corrections": 0, "errors": 0, "compactions": 0, "peak": 0}


def _acc_hidden(a, pdir, hide, naming):
    """True when the session this accumulator read is covered by an exclude rule."""
    try:
        if E.is_excluded(a, pdir, hide.rules):
            return True
        return PR.excluded(hide.rules, "", [naming.name(a, pdir)])
    except Exception:
        return True                     # cannot be checked, so it is not counted


def compute_days(days, projects_dir, scheduled, hide=NO_HIDE):
    """Per-day totals for the given local days, from one pass over the transcripts.

    scheduled: set of 8-character ids already known to be scheduled-task runs;
    it is updated in place. Scheduled sessions only add to the "scheduled" count.
    Sessions covered by an exclude rule are left out of every count.
    """
    days = sorted(days)
    out = dict((d, empty_day()) for d in days)
    if not days:
        return out
    los, his = _day_edges(days)
    y, m, d = date_parts(days[0])
    lo = time.mktime((y, m, d, 0, 0, 0, 0, 0, -1))
    claimed = {}           # message ids already counted: resumed copies count once
    naming = E.Naming() if hide.rules else None
    for path, sid, pdir, _size in E.oldest_first(E.find_sessions(projects_dir, lo)):
        if hide.on and sid[:8].lower() in hide.ids:
            continue
        accs = {}
        gate = hide.scope.gate(sid, path)
        meta = E.SessionAcc(sid)

        def get_acc(k, accs=accs, sid=sid, path=path):
            if k not in accs:
                accs[k] = E.SessionAcc(sid, claimed=claimed, owner=path)
            return accs[k]

        _route_file(path, los, his, get_acc, meta=meta, keep=gate, days=days)
        subs = {}
        sub_dir = os.path.join(path[:-6], "subagents")
        if accs and os.path.isdir(sub_dir):
            seen = set()

            def get_sub(k, subs=subs, path=path):
                if k not in subs:
                    subs[k] = E.SessionAcc("sub", allow_sidechain=True, claimed=claimed,
                                           owner=path + "/sub")
                return subs[k]

            for p in glob.glob(os.path.join(sub_dir, "*.jsonl")):
                try:
                    if os.stat(p).st_mtime < lo:
                        continue
                except OSError:
                    continue
                _route_file(p, los, his, get_sub, seen, keep=gate, days=days)
                for a in subs.values():
                    a._commit_usage()
                    a._mid = None
        for a in accs.values():
            a.finish()
        if hide.rules and accs:
            meta.cwd = next((a.cwd for a in accs.values() if a.cwd), None)
            if _acc_hidden(meta, pdir, hide, naming):
                continue
        for a in accs.values():
            if a.kind == "scheduled":
                scheduled.add(sid[:8])
        for k, a in accs.items():
            if a.first is None or (not a.prompts and not a.tool_calls):
                continue
            day = out[days[k]]
            if sid[:8] in scheduled:
                day["scheduled"] += 1
                continue
            day["sessions"] += 1
            day["prompts"] += len(a.prompts)
            day["short"] += sum(1 for p in a.prompts if len(p["text"].split()) <= 3)
            day["active_min"] += int(round(a.active / 60.0))
            day["out"] += a.tok["out"]
            day["corrections"] += a.corrections
            day["errors"] += a.errors_total
            day["compactions"] += len(a.compactions)
            day["peak"] = max(day["peak"], a.peak_context)
            if k in subs:
                day["sub_out"] += subs[k].tok["out"]
    return out


def update_trend(reports_dir, projects_dir, today, scan=True, hide=NO_HIDE):
    """Load the cache, compute missing days plus the last two, save, return all days.

    The cache keeps a fingerprint of the exclude rules. When the rules change,
    every day is counted again, so that no day keeps excluded work in its totals.
    A day whose transcripts are gone by then comes back empty.
    """
    path = os.path.join(reports_dir, "digests", "trend.json")
    cache = read_json(path)
    if not isinstance(cache, dict) or not isinstance(cache.get("days"), dict):
        cache = {"version": 1, "days": {}, "scheduled": []}
    days = {}
    for day, row in cache["days"].items():      # keep only well-formed days
        if isinstance(day, str) and real_date(day) and isinstance(row, dict):
            full = empty_day()
            full.update((k, v) for k, v in row.items()
                        if k in full and isinstance(v, (int, float)) and not isinstance(v, bool)
                        and math.isfinite(v) and abs(v) < 1e15)
            days[day] = full
    cache["days"] = days
    if not isinstance(cache.get("scheduled"), list):
        cache["scheduled"] = []
    cache["scheduled"] = [s for s in cache["scheduled"]
                          if isinstance(s, str) and s[:8].lower() not in hide.ids]
    sig = hide.scope.sig()
    stale = (cache.get("exclude_sig") or "") != sig
    if not scan:
        return {} if stale else cache["days"]      # never show totals counted under other rules
    window = [day_shift(today, -i) for i in range(TREND_DAYS, 0, -1)]
    need = [d for d in window if d not in cache["days"]]
    for d in window[-2:]:
        if d not in need:
            need.append(d)
    if stale:
        need = sorted(set(need) | set(cache["days"]))
        cache.pop("exclude_sig", None)
        if sig:
            cache["exclude_sig"] = sig
    scheduled = set(cache.get("scheduled") or [])
    cache["days"].update(compute_days(need, projects_dir, scheduled, hide))
    cache["scheduled"] = sorted(scheduled)
    blob = json.dumps(cache, sort_keys=True, separators=(",", ":"))
    try:
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        old = (read_text(path) or "").strip()
        if old != blob:
            tmp = "%s.%d.tmp" % (path, os.getpid())
            try:
                with open(tmp, "w") as fh:
                    fh.write(blob + "\n")
                os.replace(tmp, path)    # never leave a half-written cache
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
    except OSError:
        pass
    return cache["days"]


def fmt_num(v, kind):
    if v is None:
        return "no data"
    if kind == "tok":
        if v >= 999500:
            return ("%.1fM" % (v / 1e6)).replace(".0M", "M")
        if v >= 1000:
            return "%dk" % int(round(v / 1000.0))
        return "%d" % v
    if kind == "usd":
        return "$0" if not v else P.fmt_usd(v)
    if kind == "pct":
        return "%d%%" % int(round(v))
    if kind == "dec":
        return ("%.1f" % v).replace(".0", "") if v < 100 else "%d" % round(v)
    return "%d" % round(v)


def nice_max(v):
    if v <= 0:
        return 1.0
    p = 1.0
    while p * 10 <= v:
        p *= 10
    while p > v:
        p /= 10.0
    for step in (1, 2, 2.5, 4, 5, 8, 10):
        if step * p >= v:
            return step * p
    return 10 * p


def _prompts_per_hour(d):
    return d["prompts"] * 60.0 / d["active_min"] if d["active_min"] >= 10 else None


SERIES = [
    # key, title, number kind, value getter, note
    ("peak", "Peak context, tokens", "tok",
     lambda d: d["peak"] if d["sessions"] else None),
    ("short", "Prompts of 3 words or fewer, share", "pct",
     lambda d: 100.0 * d["short"] / d["prompts"] if d["prompts"] else None),
    ("pph", "Prompts per active hour", "dec", _prompts_per_hour),
    ("corrections", "Corrections", "int", lambda d: d["corrections"]),
    ("prompts", "Typed prompts", "int", lambda d: d["prompts"]),
    ("active", "Active hours", "dec", lambda d: d["active_min"] / 60.0),
    ("out", "Output tokens, with subagents", "tok", lambda d: d["out"] + d["sub_out"]),
    ("errors", "Tool errors", "int", lambda d: d["errors"]),
    ("compactions", "Compactions", "int", lambda d: d["compactions"]),
    ("sessions", "Sessions", "int", lambda d: d["sessions"]),
]
SERIES_BY_KEY = dict((s[0], s) for s in SERIES)


def chart_svg(days, values, kind, title, threshold=None):
    """Bar chart of one daily series. One scale places bars, grid and labels."""
    W, H = 320.0, 132.0
    L, R, T, B = 38.0, 8.0, 10.0, 20.0
    pw, ph = W - L - R, H - T - B
    present = [v for v in values if v is not None]
    top = max(present) if present else 0
    if threshold:
        top = max(top, threshold * 1.1)
    ymax = nice_max(top)
    if kind == "int" and ymax < 4:
        ymax = 4.0

    def y(v):
        return T + ph - ph * (v / ymax)

    n = max(1, len(days))
    slot = pw / n
    bw = max(2.0, slot - 2.0)
    parts = ['<svg class="ch" viewBox="0 0 %d %d" role="img" aria-label="%s, last %d days">'
             % (W, H, esc(title), len(days))]
    for frac in (0.0, 0.5, 1.0):
        gy = y(ymax * frac)
        parts.append('<line class="ch-grid" x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/>'
                     % (L, gy, W - R, gy))
        parts.append('<text class="ch-lab" x="%.1f" y="%.1f" text-anchor="end">%s</text>'
                     % (L - 5, gy + 3.5, esc(fmt_num(ymax * frac, kind))))
    last = max([i for i, v in enumerate(values) if v is not None] or [-1])
    for i, day in enumerate(days):
        v = values[i]
        x = L + slot * i + (slot - bw) / 2.0
        label = "%s: %s" % (short_date(day), fmt_num(v, kind))
        parts.append('<g class="b" data-r="%s">' % esc(label))
        parts.append('<rect class="ch-hit" x="%.1f" y="%.1f" width="%.1f" height="%.1f"/>'
                     % (L + slot * i, T, slot, ph))
        if v is not None and v > 0:
            bh = max(1.5, ph * (v / ymax))
            parts.append('<rect class="ch-bar%s" x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="1.5"/>'
                         % (" now" if i == last else "", x, T + ph - bh, bw, bh))
        elif v is not None:
            parts.append('<rect class="ch-zero" x="%.1f" y="%.1f" width="%.1f" height="1.5"/>'
                         % (x, T + ph - 1.5, bw))
        parts.append("</g>")
    if threshold:
        ty = y(threshold)
        parts.append('<line class="ch-limit" x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/>'
                     % (L, ty, W - R, ty))
        lab = "%s line" % fmt_num(threshold, kind)
        lw = 6.0 * len(lab) + 6
        parts.append('<rect class="ch-limit-bg" x="%.1f" y="%.1f" width="%.1f" height="12" rx="2"/>'
                     % (L + 3, ty - 6, lw))
        parts.append('<text class="ch-limit-lab" x="%.1f" y="%.1f">%s</text>'
                     % (L + 6, ty + 3.5, esc(lab)))
    if days:
        parts.append('<text class="ch-lab" x="%.1f" y="%.1f">%s</text>' % (L, H - 5, esc(short_date(days[0]))))
        parts.append('<text class="ch-lab" x="%.1f" y="%.1f" text-anchor="end">%s</text>'
                     % (W - R, H - 5, esc(short_date(days[-1]))))
    parts.append("</svg>")
    return "".join(parts)


def fmt_usd2(v):
    """Dollars with cents and thousands separators, as the Overview prints money."""
    return "${:,.2f}".format(max(0.0, float(v or 0)))


def line_x(i, n, width=1000.0, pad=4.0):
    """Where point i of n sits across a line graph of the given width."""
    return width / 2.0 if n <= 1 else pad + (width - 2 * pad) * i / float(n - 1)


def line_svg(values, labels=None, title="", mark=None, size=(1000, 210),
             grid=True, area=True, hits=True, zero=True, avg=None, ceiling=None, limit=None):
    """Line graph of one series: a line with a soft area under it, dashed grid
    lines, a ring on the last point and, with mark, a ring on that point too.

    It is drawn to be stretched to any width (preserveAspectRatio none). Strokes
    keep their width and every ring is a round line cap, so nothing is distorted
    and the graph can fill a phone or an extra wide screen from one drawing.
    values: numbers, None for a point with no data. labels: one readout text per
    point; with hits each point gets a hover and focus target that carries it.
    zero: the scale starts at 0; without it the scale runs from the lowest value.
    avg: a second series on the same scale, drawn as a dashed line (a running average).
    ceiling: the top of the scale when it should sit above the highest value (a round figure).
    limit: a threshold on the same scale, drawn as a dashed line across the graph.
    """
    W, H = float(size[0]), float(size[1])
    top, base = (16.0, H - 10.0) if grid else (7.0, H - 7.0)
    n = len(values)
    pts = [(i, float(v)) for i, v in enumerate(values)
           if _num(v) is not None and math.isfinite(v)]
    hi = max([v for _i, v in pts] or [0.0])
    if _num(ceiling) is not None and math.isfinite(ceiling):
        hi = max(hi, float(ceiling))
    lo = 0.0 if zero else min([v for _i, v in pts] or [0.0])
    if zero and hi <= 0:
        hi = 1.0

    def xy(i, v):
        y = (top + base) / 2.0 if hi == lo else base - (base - top) * (v - lo) / (hi - lo)
        return line_x(i, n, W), y

    at = dict((i, xy(i, v)) for i, v in pts)
    parts = ['<svg class="lg" viewBox="0 0 %d %d" preserveAspectRatio="none" role="img" '
             'aria-label="%s"%s>' % (W, H, esc(title), ' tabindex="0"' if hits and pts else "")]
    if grid:
        for gy in (top, (top + base) / 2.0):
            parts.append('<line class="lg-grid" x1="0" y1="%.1f" x2="%d" y2="%.1f"/>' % (gy, W, gy))
        parts.append('<line class="lg-base" x1="0" y1="%.1f" x2="%d" y2="%.1f"/>' % (base, W, base))
    if _num(limit) is not None and math.isfinite(limit) and lo <= limit <= hi and hi > lo:
        ly = xy(0, float(limit))[1]
        parts.append('<line class="lg-limit" x1="0" y1="%.1f" x2="%d" y2="%.1f"/>' % (ly, W, ly))
    line =" ".join("%.1f,%.1f" % at[i] for i, _v in pts)
    if len(pts) > 1:
        if area:
            parts.append('<polygon class="lg-area" points="%.1f,%.1f %s %.1f,%.1f"/>'
                         % (at[pts[0][0]][0], base, line, at[pts[-1][0]][0], base))
        parts.append('<polyline class="lg-line" points="%s"/>' % line)
    smooth = [(i, float(v)) for i, v in enumerate(avg or [])
              if i < n and _num(v) is not None and math.isfinite(v)]
    if len(smooth) > 1:
        parts.append('<polyline class="lg-avg" points="%s"/>'
                     % " ".join("%.1f,%.1f" % xy(i, v) for i, v in smooth))
    if hits:
        slot = W if n <= 1 else (W - 8.0) / (n - 1)
        for i, _v in pts:
            x, y = at[i]
            left = max(0.0, x - slot / 2.0)
            label = labels[i] if labels and i < len(labels) else ""
            parts.append('<g class="b" data-r="%s"><rect class="ch-hit" x="%.1f" y="0" width="%.1f" '
                         'height="%d"/><path class="lg-dot" d="M%.1f %.1fh0"/></g>'
                         % (esc(label), left, min(W, x + slot / 2.0) - left, H, x, y))
    rings = [(pts[-1][0], "")] if pts else []
    if mark in at:
        rings = [r for r in rings if r[0] != mark] + [(mark, " peak")]
    for i, cls in rings:
        parts.append('<path class="lg-ring%s" d="M%.1f %.1fh0"/><path class="lg-hole" d="M%.1f %.1fh0"/>'
                     % ((cls,) + at[i] + at[i]))
    parts.append("</svg>")
    return "".join(parts)


def running_average(values, span=7):
    """For each point, the mean of it and the points before it, up to `span` in all."""
    out = []
    for i in range(len(values)):
        window = values[max(0, i - span + 1):i + 1]
        out.append(sum(window) / float(len(window)))
    return out


def axis_usd(v):
    """A dollar figure for the side of a graph: $0, $450, $1.2k, $12k."""
    v = max(0.0, float(v or 0))
    if v >= 10000:
        return "$%dk" % _half_up(v / 1000.0)
    if v >= 1000:
        return ("$%.1fk" % (v / 1000.0)).replace(".0k", "k")
    if v >= 10 or v == 0:
        return "$%d" % _half_up(v)
    return "$%.2f" % v


def _clean(v, kind):
    """True when fmt_num prints v exactly, so a scale label never rounds."""
    def whole(x):
        return abs(x - round(x)) < 1e-9
    if kind == "tok":
        return whole(v / 1e5) if v >= 999500 else whole(v / 1000.0) if v >= 1000 else whole(v)
    if kind == "dec":
        return whole(v * 10)
    return whole(v)


def scale_top(top, kind):
    """The top of a Trends scale: the first round figure at or above top whose half,
    the middle label, is printed exactly too (5 would label its middle 2, so 8 is used)."""
    first = max(nice_max(top), 4.0 if kind == "int" else 0.0)
    p = 10.0 ** math.floor(math.log10(first))
    for mult in (p, p * 10, p * 100):
        for step in (1, 2, 2.5, 4, 5, 8):
            v = round(step * mult, 9)
            if v >= first and _clean(v, kind) and _clean(v / 2.0, kind):
                return v
    return first


def trend_graph(days, values, kind, title, threshold=None):
    """Line graph of one daily series, drawn like the Overview's spend graph: the scale
    beside it (a round top, its half and 0), the first and last day under it, and the
    threshold, when the series has one, as a dashed line with its figure on it."""
    present = [v for v in values if v is not None]
    top = max(present) if present else 0
    if threshold:
        top = max(top, threshold * 1.1)
    ymax = scale_top(top, kind)
    labels =["%s: %s" % (short_date(d), fmt_num(v, kind)) for d, v in zip(days, values)]
    svg = line_svg(values, labels, "Line graph of %s, last %d days" % (title, len(days)),
                   ceiling=ymax, limit=threshold or None)
    # the same 210-high drawing as line_svg: the top grid line at 16, the base line at 200
    scale = "".join('<span style="top:%.1f%%">%s</span>' % (100.0 * y / 210.0, esc(fmt_num(v, kind)))
                    for y, v in ((16.0, ymax), (108.0, ymax / 2.0), (200.0, 0)))
    lab = ""
    if threshold and 0 <= threshold <= ymax:
        ty = 200.0 - 184.0 * threshold / ymax
        lab = ('<span class="lg-limit-lab" aria-hidden="true" style="top:%.1f%%">%s line</span>'
               % (100.0 * ty / 210.0, esc(fmt_num(threshold, kind))))
    axis = ""
    if days:
        axis = "".join('<span style="left:%.1f%%">%s</span>' % (line_x(i, len(days)) / 10.0,
                                                                 esc(short_date(days[i])))
                       for i in sorted(set((0, len(days) - 1))))
    return ('<div class="lg-wrap"><div class="lg-plot"><div class="lg-y" aria-hidden="true">%s</div>'
            '%s%s</div><div class="lg-x" aria-hidden="true">%s</div></div>' % (scale, svg, lab, axis))


def chart_card(key, days, data, threshold=None):
    _k, title, kind, fn = SERIES_BY_KEY[key]
    values = [fn(data[d]) if d in data else None for d in days]
    latest = "no data"
    for d, v in zip(reversed(days), reversed(values)):
        if v is not None:
            latest = "%s: %s" % (short_date(d), fmt_num(v, kind))
            break
    return ('<figure class="chart trend"><figcaption><span class="ch-title">%s</span>'
            '<span class="readout" data-default="%s" aria-live="polite">%s</span></figcaption>%s</figure>'
            % (esc(title), esc(latest), esc(latest), trend_graph(days, values, kind, title, threshold)))


def _ratio(rows, num, den, scale):
    a = sum(num(r) for r in rows)
    b = sum(den(r) for r in rows)
    return scale * a / b if b else None


HEADLINES = [
    # name, baseline line, metrics [(label, kind, window fn)], chart keys
    ("Context size", "Long sessions that mix topics carry a large context on every turn.",
     [("average daily peak context", "tok",
       lambda rows: sum(r["peak"] for r in rows) / float(len(rows)) if rows else None)],
     [("peak", CONTEXT_LINE)]),
    ("Drip prompting", "Many short prompts in a row, each sent at full context.",
     [("share of prompts of 3 words or fewer", "pct",
       lambda rows: _ratio(rows, lambda r: r["short"], lambda r: r["prompts"], 100.0)),
      ("prompts per active hour", "dec",
       lambda rows: _ratio(rows, lambda r: r["prompts"], lambda r: r["active_min"], 60.0))],
     [("short", None), ("pph", None)]),
    ("Rework", "Answers you had to correct, counted from prompts that open with a correction.",
     [("corrections per 100 prompts", "dec",
       lambda rows: _ratio(rows, lambda r: r["corrections"], lambda r: r["prompts"], 100.0))],
     [("corrections", None)]),
]


def verdict(all_days, metrics):
    """Compare the first week on record with the latest active days. Lower is better."""
    active = [d for d in sorted(all_days) if all_days[d].get("sessions")]
    if len(active) < 8:
        return ("early", "Too early",
                "Too few days to compare yet: %d active %s on record, 8 needed."
                % (len(active), "day" if len(active) == 1 else "days"))
    first = active[:7]
    last = active[-min(7, len(active) - 7):]
    scores = []
    lines = []
    for label, kind, fn in metrics:
        a = fn([all_days[d] for d in first])
        b = fn([all_days[d] for d in last])
        if a is None or b is None:
            continue
        lines.append("%s went from %s to %s" % (label, fmt_num(a, kind), fmt_num(b, kind)))
        if a == 0 and b == 0:
            scores.append(0)
        elif a == 0:
            scores.append(1)
        else:
            change = (b - a) / float(a)
            scores.append(-1 if change <= -0.1 else 1 if change >= 0.1 else 0)
    if not lines:
        return ("early", "Too early", "No prompts in one of the two periods, so nothing to compare.")
    total = sum(scores)
    state, word = (("better", "Improving") if total < 0 else
                   ("worse", "Getting worse") if total > 0 else ("same", "About the same"))
    span = "first week on record (%s to %s) against the last %d active days (%s to %s)" % (
        short_date(first[0]), short_date(first[-1]), len(last),
        short_date(last[0]), short_date(last[-1]))
    text = "%s: %s. Compared: %s." % (word, "; ".join(lines), span)
    return (state, word, text[0].upper() + text[1:])


def render_trends(all_days, today):
    days = [day_shift(today, -i) for i in range(TREND_DAYS, 0, -1)]
    if not any(all_days.get(d, {}).get("sessions") for d in days):
        return ('<div class="empty"><p class="empty-title">No trend data yet.</p>'
                '<p>Charts appear once the daily run has sessions to count. '
                'Run a daily recap to start.</p></div>')
    out = ['<p class="lede">Daily totals for the last %d days, %s to %s. Scheduled-task sessions '
           'are left out, so these are your own habits. The ringed point is the latest day with data.</p>'
           % (len(days), esc(short_date(days[0])), esc(short_date(days[-1])))]
    used = set()
    for name, base, metrics, charts in HEADLINES:
        state, word, text = verdict(all_days, metrics)
        out.append('<section class="headline"><div class="hl-text"><div class="hl-head">'
                   '<h3>%s</h3><span class="pill %s">%s</span></div><p>%s</p>'
                   '<p class="muted">%s</p></div><div class="hl-charts">'
                   % (esc(name), state, esc(word), esc(text), esc(base)))
        for key, limit in charts:
            used.add(key)
            out.append(chart_card(key, days, all_days, limit))
        out.append("</div></section>")
    out.append('<h3 class="sub">Volume</h3><div class="chart-grid">')
    for key, _t, _k, _f in SERIES:
        if key not in used:
            out.append(chart_card(key, days, all_days))
    out.append("</div>")
    out.append('<details class="fold"><summary>All numbers as a table</summary>'
               '<div class="scroll"><table class="num"><thead><tr><th>Day</th>')
    out.extend("<th>%s</th>" % esc(s[1]) for s in SERIES)
    out.append("</tr></thead><tbody>")
    for d in reversed(days):
        row = all_days.get(d)
        out.append("<tr><td>%s</td>" % esc(short_date(d)))
        for _key, _t, kind, fn in SERIES:
            v = fn(row) if row else None
            out.append("<td>%s</td>" % ("" if v is None else esc(fmt_num(v, kind))))
        out.append("</tr>")
    out.append("</tbody></table></div></details>")
    return "".join(out)




# ---------- cost ----------

def digest_cost(digest):
    """Day cost and top project from a digest. Empty for a digest written before costs
    existed, or when the cost cannot be given without excluded work in it."""
    t = (digest or {}).get("totals") or {}
    c = t.get("cost")
    if not isinstance(c, dict) or not isinstance(c.get("usd"), (int, float)):
        return {}
    out = {"usd": round(float(c["usd"]), 2), "text": P.fmt_usd(float(c["usd"]))}
    projects = (digest or {}).get("projects")
    if isinstance(projects, list) and projects and isinstance(projects[0], dict):
        top = projects[0]
        if isinstance(top.get("usd"), (int, float)):
            out["top_project"] = {"name": cut(str(top.get("name") or "?"), 80),
                                  "usd": round(float(top["usd"]), 2),
                                  "text": P.fmt_usd(float(top["usd"]))}
    return out


def _tok_sum(block):
    block = block if isinstance(block, dict) else {}
    return sum(int(block.get(k) or 0) for k in ("in", "out", "cache_read", "cache_write"))


def digest_sessions(digest, limit=15):
    """The day's sessions, most costly first. Old digests have tokens but no dollars."""
    rows = []
    for s in (digest or {}).get("sessions") or []:
        if not isinstance(s, dict):
            continue
        sub = s.get("subagents") if isinstance(s.get("subagents"), dict) else {}
        usd = sub_usd = None
        if isinstance(s.get("cost"), dict) and isinstance(s["cost"].get("usd"), (int, float)):
            usd = float(s["cost"]["usd"])
            sub_usd = float((sub.get("cost") or {}).get("usd") or 0)
        rows.append({"id": str(s.get("id") or "")[:8],
                     "project": cut(str(s.get("project_name") or s.get("project") or "?"), 60),
                     "title": cut(str(s.get("title") or ""), 60),
                     "kind": str(s.get("kind") or "interactive")[:12],
                     "tokens": _tok_sum(s.get("tokens")) + _tok_sum(sub.get("tokens")),
                     "usd": None if usd is None else round(usd + sub_usd, 2),
                     "sub_usd": None if usd is None else round(sub_usd, 2)})
    for s in (digest or {}).get("minor_sessions") or []:
        if isinstance(s, dict):
            usd = s.get("usd")
            rows.append({"id": str(s.get("id") or "")[:8],
                         "project": cut(str(s.get("project_name") or s.get("project") or "?"), 60),
                         "title": "", "kind": str(s.get("kind") or "interactive")[:12],
                         "tokens": None,
                         "usd": round(float(usd), 2) if isinstance(usd, (int, float)) else None,
                         "sub_usd": None})
    rows.sort(key=lambda r: (-(r["usd"] or 0), -(r["tokens"] or 0)))
    return rows[:limit]


def _period_view(t):
    return {"usd": round(t["usd"], 2), "main_usd": round(t["main_usd"], 2),
            "sub_usd": round(t["sub_usd"], 2), "tokens": t["tokens"], "out": t["out"],
            "days": t["days"], "unpriced_tokens": t["unpriced_tokens"],
            "by_type": [{"type": r["type"], "tokens": r["tokens"], "usd": round(r["usd"], 2)}
                        for r in t["by_type"]],
            "by_project": [{"name": cut(r["name"], 80), "usd": round(r["usd"], 2),
                            "main_usd": round(r["main_usd"], 2), "sub_usd": round(r["sub_usd"], 2),
                            "tokens": r["tokens"], "sessions": r["sessions"],
                            "scheduled": r["scheduled"], "unpriced_tokens": r["unpriced_tokens"]}
                           for r in t["by_project"][:60]],
            "by_model": [{"name": cut(r["name"], 60), "tokens": r["tokens"],
                          "usd": round(r["usd"], 2), "priced": r["priced"]}
                         for r in t["by_model"][:30]]}


def load_cost(reports_dir, today, ledger_path=None, hide=NO_HIDE):
    """Everything the Cost view shows, from the ledger. None when there is no ledger data yet."""
    try:
        led, _existed = L.load(ledger_path)
        led = L.visible(led, hide.scope)
        days = sorted(d for d in led["days"] if d < today) or sorted(led["days"])
        if not days:
            return None, ""
        anchor = days[-1]
        per = L.periods(led, anchor)
        window = [day_shift(today, -i) for i in range(TREND_DAYS, 0, -1)]
        series = L.daily_series(led, window)
    except (AttributeError, TypeError, ValueError, KeyError, OverflowError, OSError):
        return None, ""
    digest, _seen = read_digest(os.path.join(reports_dir, "digests", "daily-%s.json" % anchor), hide)
    try:
        sessions = digest_sessions(digest)
    except (AttributeError, TypeError, ValueError, KeyError, OverflowError):
        sessions = []
    data = {"since": per["since"], "pricesDate": per["prices_date"], "day": anchor,
            "periods": [
                {"key": "day", "label": "Day", "title": long_date(anchor), "t": _period_view(per["day"])},
                {"key": "week7", "label": "Last 7 days",
                 "title": "%s to %s" % (short_date(day_shift(anchor, -6)), short_date(anchor)),
                 "t": _period_view(per["week7"])},
                {"key": "calweek", "label": "This week",
                 "title": "Monday %s to %s" % (short_date(L.monday(anchor)), short_date(anchor)),
                 "t": _period_view(per["calweek"])},
                {"key": "all", "label": "All time",
                 "title": "Since %s" % long_date(per["since"]), "t": _period_view(per["all"])}],
            "sessions": sessions}
    if hide.scope.only:
        data["oneAccount"] = True          # a quiet line; never which account
    values = [s["usd"] for s in series]
    title = "Cost per day, API-equivalent"
    latest = "no data"
    for d, v in zip(reversed(window), reversed(values)):
        if v is not None:
            latest = "%s: %s" % (short_date(d), fmt_num(v, "usd"))
            break
    chart = ('<figure class="chart cost-chart"><figcaption><span class="ch-title">%s</span>'
             '<span class="readout" data-default="%s">%s</span></figcaption>%s</figure>'
             % (esc(title), esc(latest), esc(latest), chart_svg(window, values, "usd", title)))
    return data, chart


def parse_cost_block(lines, hide=NO_HIDE):
    """The weekly `## Cost by project` table to rows of text cells, plus its note line.

    The row of an excluded project is dropped, and with it the Total row, which
    was added up with that project in it.
    """
    rows, note = [], []
    for line in lines:
        s = line.strip()
        if s.startswith("|"):
            if not _TABLE_SEP_RE.match(s):
                rows.append([cut(c, 80) for c in _split_row(s)][:6])
        elif s:
            note.append(s)
    head, body = (rows[0] if rows else []), rows[1:13]
    kept = [r for r in body if not hide.deep(r)]
    if len(kept) != len(body):
        kept = [r for r in kept if (r[0] if r else "").strip().lower() != "total"]
    if hide.deep(head):
        head, kept = [], []
    return {"head": head, "rows": kept, "note": cut(visible_sentences(" ".join(note), hide), 300)}


# ---------- page ----------

def render_baseline(summary, details):
    if not summary and not details:
        return ('<div class="empty"><p class="empty-title">No baseline report found.</p>'
                '<p>It is read from baseline/SUMMARY.md in the reports folder.</p></div>')
    out = []
    if summary:
        out.append('<h2 class="view-title">%s</h2>' % md_inline(md_title(summary, "Baseline")))
        out.append('<div class="md summary">%s</div>' % md_to_html(summary, collapsible=True, skip_h1=True))
    if details:
        out.append('<h3 class="sub">Detail reports</h3>')
        for title, text in details:
            out.append('<details class="fold report"><summary>%s</summary><div class="md">%s</div></details>'
                       % (md_inline(title), md_to_html(text, skip_h1=True)))
    return "".join(out)


# ---------- sections from the other coach modules ----------
# Every string from a module or a report goes through esc() here. Nothing is
# placed in the page as markup except the excerpt evidence.page_block() built.

def plain(text):
    """Text from a module that hands out HTML-escaped strings, as plain text again."""
    return html.unescape("" if text is None else str(text))


def show_day(day):
    day = str(day or "")
    return short_date(day) + " " + day[:4] if real_date(day) else day


def home_as_tilde(path):
    path = str(path or "")
    for home in (C.HOME, os.path.expanduser("~")):
        if home and home != "/" and (path == home or path.startswith(home + os.sep)):
            return "~" + path[len(home):]
    return path


def table_html(head, rows, cls=""):
    """head: [(label, kind)], rows: lists of cells already made safe.

    kind: True for a number (right-aligned), "nw" for a cell that must not wrap
    (a date), "wide" for prose that needs room, False for the rest.
    """
    def attr(kind):
        return ' class="%s"' % ("n" if kind is True else kind) if kind else ""

    out = ['<div class="panel-t scroll"><table%s><thead><tr>' % (' class="%s"' % cls if cls else "")]
    out.extend('<th scope="col"%s>%s</th>' % (attr(kind), esc(label)) for label, kind in head)
    out.append("</tr></thead><tbody>")
    for row in rows:
        out.append("<tr>%s</tr>" % "".join(
            "<td%s>%s</td>" % (attr(head[i][1]), cell) for i, cell in enumerate(row)))
    out.append("</tbody></table></div>")
    return "".join(out)


FT_GOOD = ("better", "not seen again")
FT_BAD = ("worse", "recurred")


def status_pill(status):
    """The status word in a chip. The colour only repeats what the word says."""
    word = str(status or "").strip().lower()
    cls = " better" if word in FT_GOOD else " worse" if word in FT_BAD else ""
    return '<span class="pill%s">%s</span>' % (cls, esc(word)) if word else ""


def followthrough_list(lines):
    out = ['<ul class="ft panel">']
    for f in lines:
        applied = ('<span class="ft-applied">applied on %s</span>' % esc(show_day(f["applied"]))
                   if f.get("applied") else "")
        out.append('<li><span class="ft-head">%s%s</span><span class="ft-text">%s</span></li>'
                   % (status_pill(f["status"]), applied, md_inline(f["text"])))
    out.append("</ul>")
    return "".join(out)


def render_followthrough(weekly, data, hide=NO_HIDE):
    """The latest weekly's follow-through lines, then every piece of advice on record."""
    out = []
    lines = (weekly or {}).get("followthrough") or []
    if lines:
        out.append('<p class="muted">From the weekly review: %s.</p>' % esc(weekly["label"]))
        out.append(followthrough_list(lines))
    rows = []
    advice = data.get("advice") if isinstance(data, dict) else None
    for r in advice if isinstance(advice, list) else []:
        if not isinstance(r, dict) or hide.deep(r) or not r.get("do"):
            continue
        times = r.get("times") if isinstance(r.get("times"), int) else 1
        rows.append([md_inline(cut(str(r["do"]), 300))
                     + (' <span class="pill repeat">given again</span>' if times > 1 else ""),
                     esc(times), esc(show_day(r.get("first"))), esc(show_day(r.get("last"))),
                     esc(show_day(r["applied"])) if r.get("applied") else ""])
    if rows:
        out.append('<h4 class="sub2">Advice given so far</h4>')
        out.append(table_html([("Advice", "wide"), ("Times given", True), ("First", "nw"),
                               ("Last", "nw"), ("Applied", "nw")], rows[:40]))
        if len(rows) > 40:
            out.append('<p class="muted">The 40 pieces of advice given most often.</p>')
    if not out:
        return ""
    return '<section class="block"><h3 class="sub">Follow-through</h3>%s</section>' % "".join(out)


def _cmp_num(v, kind):
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return "-"
    if kind == "cost":
        return "$%.2f" % v
    if kind == "share":
        return "%.1f%%" % v
    a = abs(v)
    if a >= 1e4:
        return fmt_num(v, "tok")
    if isinstance(v, float) and v != int(v):
        return "%.2f" % v if a < 100 else "%.0f" % v
    return "%d" % v


def _cmp_pct(v):
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return ""
    return "%+d%%" % int(round(v))


CMP_STATES = {"improvement": " better", "regression": " worse"}


def _cmp_state(row):
    word = str(row.get("state") or "").replace("_", " ")
    return '<span class="pill%s">%s</span>' % (CMP_STATES.get(row.get("state"), ""), esc(word))


def _cmp_period(name, p):
    p = p if isinstance(p, dict) else {}
    bits = []
    if isinstance(p.get("days"), (int, float)):
        bits.append("%d %s" % (p["days"], "day" if p["days"] == 1 else "days"))
    if isinstance(p.get("sessions_seen"), int):
        bits.append("%d %s" % (p["sessions_seen"], "session" if p["sessions_seen"] == 1 else "sessions"))
    return "<li><b>%s</b> %s to %s%s</li>" % (
        esc(name), esc(show_day(p.get("first_day"))), esc(show_day(p.get("last_day"))),
        esc(" (%s)" % ", ".join(bits)) if bits else "")


def render_compare(block, hide=NO_HIDE):
    """Before and after, from the stored comparison: highlights, then the full table folded."""
    data = block.get("data") if isinstance(block, dict) else None
    if not isinstance(data, dict):
        return ""
    def shown(key):
        got = data.get(key)
        return [r for r in got if isinstance(got, list) and isinstance(r, dict) and r.get("key")
                and not hide.text(str(r.get("label") or ""), str(r["key"]))] \
            if isinstance(got, list) else []

    metrics = shown("metrics")
    by_key = dict((r["key"], r) for r in metrics + shown("findings"))
    out = ['<ul class="periods">%s%s</ul>' % (_cmp_period("Before", data.get("before")),
                                              _cmp_period("After", data.get("after")))]
    for note in data.get("notes") or []:
        if isinstance(note, str) and not hide.text(note):
            out.append('<p class="muted">%s</p>' % esc(cut(note, 400)))
    high = []
    for key in data.get("highlights") if isinstance(data.get("highlights"), list) else []:
        r = by_key.get(key) if isinstance(key, str) else None
        if not r:
            continue
        basis = r.get("basis")
        if basis:
            move = "%s to %s %s" % (_cmp_num(r.get("norm_before"), r.get("kind")),
                                    _cmp_num(r.get("norm_after"), r.get("kind")), basis)
        else:
            move = "%s to %s" % (_cmp_num(r.get("before"), r.get("kind")),
                                 _cmp_num(r.get("after"), r.get("kind")))
        high.append([esc(cut(str(r.get("label") or key), 80)), esc(move),
                     esc(_cmp_pct(r.get("norm_pct") if basis else r.get("pct"))), _cmp_state(r)])
    if high:
        out.append(table_html([("Measure", False), ("Before to after", "wide"), ("Change", True),
                               ("Result", False)], high))
    full = []
    for r in metrics:
        norm = ""
        if r.get("basis"):
            norm = "%s to %s %s" % (_cmp_num(r.get("norm_before"), r.get("kind")),
                                    _cmp_num(r.get("norm_after"), r.get("kind")), r["basis"])
        full.append([esc(cut(str(r.get("label") or r["key"]), 80)),
                     esc(_cmp_num(r.get("before"), r.get("kind"))),
                     esc(_cmp_num(r.get("after"), r.get("kind"))),
                     esc(_cmp_pct(r.get("norm_pct") if r.get("basis") else r.get("pct"))),
                     esc(norm), _cmp_state(r)])
    if full:
        out.append('<details class="fold"><summary>Every measure as a table</summary>%s</details>'
                   % table_html([("Measure", False), ("Before", True), ("After", True),
                                 ("Change", True), ("Normalised", False), ("Result", False)], full))
    if not high and not full:
        return ""
    return '<section class="block"><h3 class="sub">Before and after</h3>%s</section>' % "".join(out)


def render_unused(data, hide=NO_HIDE):
    """Set up but unused, from the saved config audit (strings are plain text here)."""
    if not isinstance(data, dict):
        return ""
    out = []
    if isinstance(data.get("tokens_per_turn"), (int, float)):
        total = "Always on: about %s tokens on every turn" % fmt_num(data["tokens_per_turn"], "tok")
        if isinstance(data.get("window_tokens"), (int, float)) and data.get("window"):
            total += ", about %s tokens over %s" % (fmt_num(data["window_tokens"], "tok"),
                                                    data["window"])
            if isinstance(data.get("turns"), int):
                total += " (%d turns)" % data["turns"]
        if isinstance(data.get("usd_cache_read"), (int, float)):
            total += ", about %s at the cache read rate" % fmt_num(data["usd_cache_read"], "usd")
        out.append('<p class="lede">%s.</p>' % esc(total))
    lines = [x for x in data.get("lines") or [] if isinstance(x, str) and not hide.text(x)]
    if lines:
        out.append('<ul class="plain">%s</ul>'
                   % "".join("<li>%s</li>" % md_inline(cut(x, 400)) for x in lines[:5]))
    rows = []
    for r in data.get("rows") or []:
        if not isinstance(r, dict) or hide.deep(r):
            continue
        name = esc(cut(str(r.get("name") or "?"), 80))
        if r.get("unused"):
            name += ' <span class="pill warn">unused</span>'
        rows.append([esc(r.get("kind") or ""), name, esc(cut(str(r.get("scope") or ""), 60)),
                     esc(fmt_num(r["tokens_per_turn"], "tok"))
                     if isinstance(r.get("tokens_per_turn"), (int, float)) else "",
                     "" if r.get("uses") is None else esc(r["uses"]),
                     esc(cut(str(r.get("last_used") or ""), 40))])
    if rows:
        out.append(table_html([("Kind", False), ("Name", False), ("Scope", False),
                               ("Tokens per turn", True), ("Uses", True), ("Last used", False)], rows))
    if not lines and not rows:
        return ""
    if data.get("note"):
        out.append('<p class="muted">%s</p>' % esc(cut(str(data["note"]), 300)))
    return '<section class="block"><h3 class="sub">Set up but unused</h3>%s</section>' % "".join(out)


def render_fixes(data, hide=NO_HIDE):
    """Fixes the person said yes to. Absent when there are none."""
    rows = []
    items = data.get("applied") if isinstance(data, dict) else None
    for r in items if isinstance(items, list) else []:
        if not isinstance(r, dict) or hide.deep(r):
            continue
        status = str(r.get("status") or "")
        rows.append([esc(show_day(str(r.get("date") or "")[:10])), esc(cut(str(r.get("type") or ""), 40)),
                     "<code>%s</code>" % esc(cut(home_as_tilde(r.get("target")), 160)),
                     esc(cut(str(r.get("summary") or ""), 300)),
                     esc(cut(str(r.get("reason") or ""), 300)),
                     '<span class="pill%s">%s</span>' % (" better" if status == "applied" else "",
                                                         esc(status))])
    if not rows:
        return ""
    return ('<section class="block"><h3 class="sub">Fixes applied</h3>%s</section>'
            % table_html([("Date", "nw"), ("Type", False), ("File", "wide"), ("Change", "wide"),
                          ("Why", "wide"), ("Status", False)], rows[:60]))


STATUSLINE_MODES = {
    "coach": "On, as the whole status line.",
    "coach_wrap": "On, added to the status line you already had.",
    "unset": "Off: no status line is set.",
    "other": "Off: another status line is set.",
    "unreadable": "Off: the settings file could not be read.",
}


def render_statusline(data):
    """A small card: on or off and the mode, what each part means, the thresholds."""
    if not isinstance(data, dict):
        return ""
    state = plain(data.get("state"))
    on = bool(data.get("on"))
    mode = STATUSLINE_MODES.get(state, "On." if on else "Off.")
    if on and data.get("coach_off"):
        mode += " The coach is switched off, so it shows nothing for now."
    out = ['<div class="hl-head"><h3 class="sub">Status line</h3>'
           '<span class="pill%s">%s</span></div><p>%s</p>'
           % (" better" if on else "", "on" if on else "off", esc(mode))]
    legend = [["<code>%s</code>" % esc(plain(r.get("part"))), esc(plain(r.get("meaning")))]
              for r in data.get("legend") or [] if isinstance(r, dict)]
    if legend:
        out.append(table_html([("It shows", False), ("Meaning", False)], legend, "wrapcol"))
    marks = [[esc(plain(r.get("name"))), esc(plain(r.get("value")))]
             for r in data.get("thresholds") or [] if isinstance(r, dict)]
    if marks:
        out.append('<h4 class="sub2">Thresholds</h4>')
        out.append(table_html([("Marker", False), ("Shows at", False)], marks, "wrapcol"))
    return '<section class="block card">%s</section>' % "".join(out)


def module_sections(reports_dir, today, weeklies, hide=NO_HIDE):
    """(progress html, setup html). Each part is built on its own: a module that
    is missing or fails leaves its part out and nothing else."""
    def part(name, build):
        mod = optional(name)
        return (guarded(build, mod) or "") if mod is not None else ""

    progress = [
        part("followthrough", lambda m: render_followthrough(
            weeklies[0] if weeklies else None, guarded(m.page_data, reports_dir, today) or {}, hide)),
        part("compare", lambda m: render_compare(m.page_block(), hide)),
    ]
    def unused(m):
        report = m.latest(reports_dir)
        return render_unused(m.page_data(report, escape=False), hide) if report is not None else ""

    setup = [
        part("config_audit", unused),
        part("applyfix", lambda m: render_fixes(m.page_data(), hide)),
        part("statusline", lambda m: render_statusline(m.page_data())),
    ]
    return "".join(progress), "".join(setup)


def attach_item_evidence(dailies, weeklies, projects_dir, hide=NO_HIDE):
    """Give the items of the newest dailies and the newest weekly their evidence:
    it["ev"] = {"tag", "open", "tpl"}. Returns the <template> elements that hold
    the excerpts. An excerpt is the markup evidence.page_block() made, already
    escaped, and it goes into the page as it is."""
    if EV is None:
        return ""
    out = []
    groups = [("d%d" % i, "daily-%s" % d["date"], d) for i, d in enumerate(dailies[:EVIDENCE_DAILIES])]
    groups += [("w%d" % i, "weekly-%s" % w["date"], w) for i, w in enumerate(weeklies[:1])]
    for prefix, stem, entry in groups:
        for it in entry["items"]:
            if not it.get("tag"):
                continue
            blk = guarded(lambda: EV.page_block(it["what"], projects_dir, on_page=None, stem=stem))
            if not isinstance(blk, dict) or hide.text(str(blk.get("tag") or "")):
                continue
            ev = {"tag": cut(str(blk.get("tag") or it["tag"]), 200),
                  "open": cut(str(blk.get("open") or ""), 300), "tpl": None}
            excerpt = blk.get("html")
            if isinstance(excerpt, str) and excerpt and not hide.text(excerpt, html.unescape(excerpt)):
                ev["tpl"] = "ev-%s-%d" % (prefix, it["n"])
                out.append('<template id="%s">%s</template>' % (ev["tpl"], excerpt))
            it["ev"] = ev
    return "".join(out)


# ---------- overview (the live feed) ----------
# The feed is live.snapshot(), or the live.json it writes (docs/LIVE-FEED.md). Every value is checked and cut here, so the script in the page only
# ever meets the shapes below, with None where the feed has nothing to say.

LIVE_VERSION = 1
LIVE_PERIODS = (("today", "Today"), ("week", "Last 7 days"), ("month", "Last 30 days"),
                ("all", "All time"))
LIVE_SERIES_MAX = 600       # All time draws a point a week: over eleven years of them
LIVE_LEVELS = ("ok", "warn", "crit")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def load_live():
    """The live feed as a dict, or None when there is none.

    First live.snapshot(), when the coach has that module. When it has not, or
    the call fails or hands back something else, the live.json in the coach
    folder ($COACH_HOME moves it). Neither: None, and the Overview stays empty.
    """
    mod = optional("live")
    snap = getattr(mod, "snapshot", None) if mod is not None else None
    feed = guarded(snap) if callable(snap) else None
    if not isinstance(feed, dict):
        feed = read_json(os.path.join(C.coach_home(), "live.json"))
    if not isinstance(feed, dict) or feed_is_empty(feed):
        return None
    return feed


def feed_is_empty(feed):
    """True when a current-version feed holds nothing to show: no session or spend
    in any period, no limits and nothing running (a fresh install, or 30 quiet
    days). The Overview then says there are no live numbers yet instead of a page
    of zeros. A newer-version feed is never empty here, so "Update the coach" shows."""
    version = feed.get("version")
    if not isinstance(version, (int, float)) or isinstance(version, bool) or version > 1:
        return False
    periods = feed.get("periods")
    if isinstance(periods, dict):
        for p in periods.values():
            if isinstance(p, dict) and ((_fin(p.get("sessions")) or 0) > 0
                                        or (_fin(p.get("cost_usd")) or 0) > 0):
                return False
    limits = feed.get("limits")
    if isinstance(limits, dict) and limits.get("rows"):
        return False
    return not feed.get("running")


def _fin(v):
    v = _num(v)
    try:
        return v if v is not None and math.isfinite(v) and abs(v) < 1e15 else None
    except OverflowError:          # an integer too large to be a float
        return None


def _half_up(v):
    """Round as the script in the page does (Math.round), so both print the same figure."""
    return int(math.floor(v + 0.5))


def _rate(v):
    v = _fin(v)
    return v if v is not None and 0 <= v <= 1 else None


def _iso(v):
    return v[:32] if isinstance(v, str) and _ISO_RE.match(v) else None


def _txt(v, limit):
    return cut(clean(v), limit) if isinstance(v, str) and v.strip() else None


def _part(v):
    return v if isinstance(v, dict) else {}


def _rows(v, limit):
    return [r for r in v if isinstance(r, dict)][:limit] if isinstance(v, list) else []


def heat_pct(value, top):
    """Length of a heat bar as a whole percent of its track: the row's value as a
    share of the largest row in its table. A row with anything at all keeps a
    sliver (2), a row with nothing has no bar (0)."""
    v, t = _fin(value), _fin(top)
    if v is None or t is None or v <= 0 or t <= 0:
        return 0
    return max(2, min(100, int(round(100.0 * v / t))))


def add_heat(rows, key):
    """Give each row its heat bar length, measured on the given figure."""
    top = max([r[key] for r in rows if r.get(key) is not None] or [0])
    for r in rows:
        r["heat"] = heat_pct(r.get(key), top)
    return rows


def live_version(v):
    """None for a feed of this version (or one that does not say), else the newer
    version it claims: a number above ours, however it is written ("2", "2.0.0")."""
    if isinstance(v, str):
        m = re.match(r"\s*v?(\d+(?:\.\d+)?)", v)
        v = float(m.group(1)) if m else None
    v = _num(v)
    if v is None or v <= LIVE_VERSION:
        return None
    return v if _fin(v) is not None else LIVE_VERSION + 1


def _trend(v, check):
    """A measure's own series: up to 12 steps, None where a step has no data.
    None when the feed carries no series for it."""
    return [check(x) for x in v[:12]] if isinstance(v, list) else None


def live_period(p, label, hide=NO_HIDE):
    series = []
    for r in _rows(p.get("series"), LIVE_SERIES_MAX):
        if _iso(r.get("t")):
            series.append({"t": _iso(r["t"]), "tokens": max(0, _fin(r.get("tokens")) or 0),
                           "usd": max(0, _fin(r.get("usd")) or 0)})
    peak = _part(p.get("peak"))
    ft, ch, co = _part(p.get("first_try")), _part(p.get("cache_hit")), _part(p.get("corrections"))
    # All time: activities, first-try, cache hit, corrections and MCP servers only reach
    # back as far as the transcripts do. When that is later than the period's first day,
    # those blocks say so ("Detail from Nov 9").
    since, detail = _iso(p.get("since")), _iso(p.get("detail_since"))
    unit = p.get("series_unit")
    return {
        "label": _txt(p.get("label"), 40) or label,
        "detail_from": nice_day(detail) if since and detail and detail[:10] > since[:10] else None,
        "cost_usd": _fin(p.get("cost_usd")), "prev_cost_usd": _fin(p.get("prev_cost_usd")),
        "calls": _fin(p.get("calls")), "sessions": _fin(p.get("sessions")),
        "tokens": _fin(p.get("tokens")),
        "series_unit": unit if unit in ("hour", "week") else "day",
        "series": series,
        "peak": ({"t": _iso(peak["t"]), "tokens": _fin(peak.get("tokens")),
                  "usd": _fin(peak.get("usd"))} if _iso(peak.get("t")) else None),
        "first_try": {"rate": _rate(ft.get("rate")), "prev_rate": _rate(ft.get("prev_rate")),
                      "edit_turns": _fin(ft.get("edit_turns")),
                      "series": _trend(ft.get("series"), _rate)},
        "cache_hit": {"rate": _rate(ch.get("rate")), "prev_rate": _rate(ch.get("prev_rate")),
                      "series": _trend(ch.get("series"), _rate)},
        "corrections": {"count": _fin(co.get("count")), "prev_count": _fin(co.get("prev_count")),
                        "series": _trend(co.get("series"), _fin)},
        "activities": add_heat([
            {"name": _txt(r.get("name"), 60) or "?", "usd": _fin(r.get("usd")),
             "turns": _fin(r.get("turns")), "share": _rate(r.get("share")),
             "first_try": _rate(r.get("first_try"))}
            for r in _rows(p.get("activities"), 30)], "usd"),
        "projects": add_heat([
            {"name": _txt(r.get("name"), 60) or "?", "sessions": _fin(r.get("sessions")),
             "usd": _fin(r.get("usd")), "share": _rate(r.get("share"))}
            for r in _rows(p.get("projects"), 4)], "usd"),
        "mcp_servers": add_heat([
            {"name": _txt(r.get("name"), 60) or "?", "calls": _fin(r.get("calls")),
             "tools": _fin(r.get("tools")), "errors": _fin(r.get("errors"))}
            for r in _rows(p.get("mcp_servers"), 30)
            if not hide.text(r.get("name"))][:8], "calls"),      # a server's name is the person's
    }


def live_view(feed, hide=NO_HIDE):
    """The feed as the page embeds it, or None when there is no feed.

    A feed of a later version is cut down to its version number, and the page
    asks for an update. A feed written before an exclude rule existed may still
    hold that work: a period with a project the rules cover was counted with it
    and is left out whole, and so is a running session or a tip that names it.
    Only names are checked, never the feed's own words (labels, activity types).
    """
    if not isinstance(feed, dict):
        return None
    newer = live_version(feed.get("version"))
    if newer is not None:
        return {"version": newer}
    periods = {}
    for key, label in LIVE_PERIODS:
        p = _part(feed.get("periods")).get(key)
        # "Other" is the feed's word for the projects it folds together, not a name
        if isinstance(p, dict) and not any(hide.text(r.get("name"))
                                           for r in _rows(p.get("projects"), 60)
                                           if r.get("name") != "Other"):
            periods[key] = live_period(p, label, hide)
    limits = _part(feed.get("limits"))
    rows = []
    for r in _rows(limits.get("rows"), 8):
        used = _fin(r.get("used_pct"))
        if used is None:
            continue
        level = r.get("level") if r.get("level") in LIVE_LEVELS else (
            "crit" if used >= 90 else "warn" if used >= 70 else "ok")
        rows.append({"key": _txt(r.get("key"), 40) or "", "label": _txt(r.get("label"), 40) or "Limit",
                     "used_pct": used, "resets_at": _iso(r.get("resets_at")),
                     "pace_pct": _fin(r.get("pace_pct")), "runs_out_at": _iso(r.get("runs_out_at")),
                     "level": level})
    running = []
    for r in _rows(feed.get("running"), 12):
        if not _txt(r.get("project"), 60) or hide.text(r.get("project"), r.get("branch")):
            continue
        running.append({"project": _txt(r["project"], 60), "branch": _txt(r.get("branch"), 60),
                        "model": _txt(r.get("model"), 40), "started": _iso(r.get("started")),
                        "context_pct": _fin(r.get("context_pct")),
                        "context_left_tokens": _fin(r.get("context_left_tokens")),
                        "level": r.get("level") if r.get("level") in LIVE_LEVELS else "ok"})
    tips = []
    for r in _rows(feed.get("tips"), 3):
        if _txt(r.get("title"), 200) and not hide.text(r.get("title"), r.get("advice")):
            tips.append({"title": _txt(r["title"], 200), "advice": _txt(r.get("advice"), 300) or "",
                         "pattern": _txt(r.get("pattern"), 60) or ""})
    return {"version": LIVE_VERSION, "updated": _iso(feed.get("updated")), "periods": periods,
            "limits": {"as_of": _iso(limits.get("as_of")) if rows else None, "rows": rows},
            "running": running, "tips": tips}


def nice_day(iso):
    """Nov 23, from a local ISO time."""
    return "%s %d" % (MONTHS[max(1, min(12, int(iso[5:7]))) - 1], int(iso[8:10]))


def point_name(iso, hour):
    """What a point of the spend graph is called: its hour, or its day (a week's
    point carries its Monday, so it reads like a day)."""
    return iso[11:16] if hour else nice_day(iso)


def axis_marks(n):
    """Which points of n get a label under the graph: up to five, evenly spread."""
    if n <= 5:
        return list(range(n))
    for k in (5, 4, 3):
        if (n - 1) % (k - 1) == 0:
            return [i * (n - 1) // (k - 1) for i in range(k)]
    return sorted(set(int(i * (n - 1) / 4.0 + 0.5) for i in range(5)))


def overview_graphs(live):
    """The Overview's line graphs as <template> elements, which the script in the
    page puts in place: the spend graph of each period (ov-g-<period>) and a
    small trend line for each measure (ov-s-<period>-<m>): drawn from the measure's
    own series when the feed carries one with two or more steps of data, else from
    the period before and now, when it has both."""
    out = []
    updated = (live or {}).get("updated") or ""
    for key, _label in LIVE_PERIODS:
        p = ((live or {}).get("periods") or {}).get(key)
        if not p:
            continue
        hour = p["series_unit"] == "hour"
        week = p["series_unit"] == "week"
        series = p["series"]
        if series:
            values = [r["usd"] for r in series]
            names = [point_name(r["t"], hour) for r in series]
            mark = values.index(max(values)) if max(values) > 0 else None
            title = "Line graph of spend by %s, %s" % (p["series_unit"], p["label"])
            if mark is not None:
                title += ", peaking %s %s" % ("at" if hour else "in the week of" if week else "on",
                                              names[mark])
            # under the graph a week reads like a day (Nov 9); in the readout it says what it is
            said = ["Week of %s" % n if week else n for n in names]
            # by day, with more than a week of days: the 7-day average as a dashed line
            smooth = running_average(values) if not hour and not week and len(values) > 7 else None
            if smooth:
                title += ", with a dashed 7-day average"
            svg = line_svg(values, ["%s: %s" % (n, fmt_usd2(v)) for n, v in zip(said, values)],
                           title, mark=mark, avg=smooth)
            # the scale beside the graph: the top line is the highest point, the base line is $0
            top = max(values) if max(values) > 0 else 1.0
            scale = "".join('<span style="top:%.1f%%">%s</span>' % (100.0 * y / 210.0, esc(axis_usd(v)))
                            for y, v in ((16.0, top), (108.0, top / 2.0), (200.0, 0)))
            axis = []
            last = len(series) - 1
            for i in axis_marks(len(series)):
                name = names[i]
                if week:
                    # the last week is the one today is in
                    if i == last and series[i]["t"][:10] <= updated[:10]:
                        name = "Today"
                elif i == last and series[i]["t"][:13 if hour else 10] == updated[:13 if hour else 10]:
                    name = "Now" if hour else "Today"
                axis.append('<span style="left:%.1f%%">%s</span>'
                            % (line_x(i, len(series)) / 10.0, esc(name)))
            out.append('<template id="ov-g-%s"><div class="lg-wrap"><div class="lg-plot">'
                       '<div class="lg-y" aria-hidden="true">%s</div>%s</div><div class="lg-x" '
                       'aria-hidden="true">%s</div></div></template>' % (key, scale, svg, "".join(axis)))
        ft, ch, co = p["first_try"], p["cache_hit"], p["corrections"]
        for mid, name, before, now, trend, pct in (
                ("ft", "First-try edits", ft["prev_rate"], ft["rate"], ft["series"], True),
                ("ch", "Cache hit", ch["prev_rate"], ch["rate"], ch["series"], True),
                ("co", "Corrections", co["prev_count"], co["count"], co["series"], False)):
            unit = "%" if pct else ""
            steps = [None if v is None else v * 100 if pct else v for v in trend or []]
            have = [v for v in steps if v is not None]
            if len(have) > 1:
                title = "%s over the period: from %d%s to %d%s" % (
                    name, _half_up(have[0]), unit, _half_up(have[-1]), unit)
                out.append('<template id="ov-s-%s-%s">%s</template>' % (key, mid, line_svg(
                    steps, title=title, size=(200, 40), grid=False, area=False, hits=False,
                    zero=False)))
                continue
            if before is None or now is None:
                continue
            a, b = (before * 100, now * 100) if pct else (before, now)
            title = "%s: %d%s the period before, %d%s now" % (name, _half_up(a), unit,
                                                              _half_up(b), unit)
            out.append('<template id="ov-s-%s-%s">%s</template>' % (key, mid, line_svg(
                [a, b], title=title, size=(200, 40), grid=False, area=False, hits=False, zero=False)))
    return "".join(out)


def card(dailies):
    if not dailies:
        return {"date": None, "items": [], "counts": {}}
    d = dailies[0]
    items = [C.one_line(it["do"] or it["what"], 160) for it in d["items"][:5]]
    if not items and d.get("note"):
        items = [C.one_line(d["note"], 160)]
    counts = dict((k, v) for k, v in (d.get("counts") or {}).items() if v is not None)
    counts["items"] = len(d["items"])
    counts["findings"] = sum(len(v) for v in d.get("findings", {}).values()) + len(d.get("other", []))
    out = {"date": d["date"], "items": items, "counts": counts}
    cost = d.get("cost") or {}
    if cost:
        out["cost"] = {"usd": cost["usd"], "text": cost["text"],
                       "meaning": "API-equivalent at list prices, not a bill"}
        if cost.get("top_project"):
            out["cost"]["top_project"] = cost["top_project"]
    return out


def build_page(reports_dir, projects_dir=None, today=None, scan=True, ledger_path=None):
    """Returns (html, card dict)."""
    today = today or time.strftime("%Y-%m-%d")
    hide = load_hide(ledger_path)
    dailies = load_dailies(reports_dir, hide)
    weeklies = load_weeklies(reports_dir, hide)
    summary, details, base_mtime = load_baseline(reports_dir, hide)
    trend = update_trend(reports_dir, projects_dir or C.PROJECTS_DIR, today, scan, hide)
    excerpts = guarded(attach_item_evidence, dailies, weeklies, projects_dir, hide) or ""
    progress, setup = guarded(module_sections, reports_dir, today, weeklies, hide) or ("", "")
    if not setup:
        setup = ('<div class="empty"><p class="empty-title">Nothing to show yet.</p>'
                 '<p>What is set up but unused, the fixes applied and the status line appear here.</p></div>')
    if not progress:
        progress = ('<div class="empty"><p class="empty-title">Nothing to follow up yet.</p>'
                    '<p>This fills in once advice has been given and a later week can be '
                    'set against it.</p></div>')

    dates = [d["date"] for d in dailies[:1]] + [w["date"] for w in weeklies[:1]]
    dates += [d for d in trend if trend[d].get("sessions") and d < today]
    if dates:
        newest = "Data to " + long_date(max(dates))
    elif base_mtime:
        newest = "Baseline of " + long_date(time.strftime("%Y-%m-%d", time.localtime(base_mtime)))
    else:
        newest = "No data yet"

    one = ""
    if dailies and dailies[0]["items"] and dailies[0]["items"][0]["do"]:
        one = ('<p class="onething"><span class="one-label">Do today</span>'
               '<span class="one-text">%s</span></p>' % md_inline(cap(dailies[0]["items"][0]["do"])))

    cost, cost_chart = load_cost(reports_dir, today, ledger_path, hide)
    if cost:
        cost_html = cost_chart
    else:
        cost_html = ('<div class="empty"><p class="empty-title">No cost data yet.</p>'
                     '<p>Costs appear after the next recap, which also records every day still '
                     'in your transcripts.</p></div>')
    live = guarded(live_view, guarded(load_live), hide)
    data = {"dailies": dailies, "weeklies": weeklies,
            "contextLine": CONTEXT_LINE, "cost": cost, "live": live}
    name = (guarded(C.load_config) or {}).get("display_name") or C.DEFAULT_DISPLAY_NAME
    values = {"NAME": esc(name), "NEWEST": esc(newest), "ONE": one, "TRENDS": render_trends(trend, today),
              "BASELINE": render_baseline(summary, details), "DATA": json_for_script(data),
              "COST": cost_html, "PROGRESS": progress, "SETUP": setup, "EXCERPTS": excerpts,
              "GRAPHS": guarded(overview_graphs, live) or ""}
    page = re.sub(r"\{\{(NAME|NEWEST|ONE|TRENDS|BASELINE|DATA|COST|PROGRESS|SETUP|EXCERPTS|GRAPHS)\}\}",
                  lambda m: values[m.group(1)], PAGE)
    return clean(page), card(dailies)


PAGE = r"""<title>{{NAME}}</title>
<meta name="claude:viewport-fit-sides" content="cover">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
/* Layout: one left-aligned column, sticky name and tabs, summary rows first, evidence folded under each row. */
/* Colours follow the page mockup (docs/mockups/PageMM.dc.html), light and dark.
   --good, --warn and --crit are for words and are dark enough to read on a card; the
   -mark ones are for dots, edges and bars. */
:root {
  --bg: #f3f2ee; --surface: #fcfcfb; --sunken: #eeede8; --raised: #ffffff;
  --ink: #0b0b0b; --ink-2: #52514e; --ink-3: #7c7b75; --line: #dedcd5; --grid: #e9e7e1;
  --accent: #2a78d6; --accent-ink: #ffffff; --accent-soft: #e6effb;
  --good: #065206; --good-soft: rgba(12, 163, 12, 0.12); --good-mark: #0ca30c;
  --warn: #7d590c; --warn-soft: rgba(250, 178, 25, 0.2); --warn-mark: #fab219;
  --crit: #a32c2c; --crit-soft: rgba(208, 59, 59, 0.12); --crit-mark: #d03b3b;
  --heat-mid: #eda100;
  --bar: #9cc0ea; --bar-now: #2a78d6;
  --pill: #0b0b0b; --pill-text: #fcfcfb; --hover: #eeede8;
  --sans: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif;
  --mono: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif;
  --code: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  --r: 10px;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #111110; --surface: #1a1a19; --sunken: #232321; --raised: #232321;
    --ink: #f0efec; --ink-2: #c3c2b7; --ink-3: #8f8e85; --line: #33332f; --grid: #2a2a27;
    --accent: #3987e5; --accent-ink: #ffffff; --accent-soft: #17304f;
    --good: #0ca30c; --good-soft: rgba(12, 163, 12, 0.18); --good-mark: #0ca30c;
    --warn: #fab219; --warn-soft: rgba(250, 178, 25, 0.16); --warn-mark: #fab219;
    --crit: #ec6a6a; --crit-soft: rgba(208, 59, 59, 0.2); --crit-mark: #d03b3b;
    --heat-mid: #c98500;
    --bar: #25507f; --bar-now: #3987e5;
    --pill: #f0efec; --pill-text: #1a1a19; --hover: #262624;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #111110; --surface: #1a1a19; --sunken: #232321; --raised: #232321;
  --ink: #f0efec; --ink-2: #c3c2b7; --ink-3: #8f8e85; --line: #33332f; --grid: #2a2a27;
  --accent: #3987e5; --accent-ink: #ffffff; --accent-soft: #17304f;
  --good: #0ca30c; --good-soft: rgba(12, 163, 12, 0.18); --good-mark: #0ca30c;
  --warn: #fab219; --warn-soft: rgba(250, 178, 25, 0.16); --warn-mark: #fab219;
  --crit: #ec6a6a; --crit-soft: rgba(208, 59, 59, 0.2); --crit-mark: #d03b3b;
  --heat-mid: #c98500;
  --bar: #25507f; --bar-now: #3987e5;
  --pill: #f0efec; --pill-text: #1a1a19; --hover: #262624;
  color-scheme: dark;
}
*, *::before, *::after { box-sizing: border-box; }
body { background: var(--bg); color: var(--ink); font-family: var(--sans); font-size: 14px; line-height: 1.4; font-variant-numeric: tabular-nums; -webkit-text-size-adjust: 100%; }
body::before { content: ""; position: fixed; inset: 0 0 auto; height: env(safe-area-inset-top, 0px); z-index: 10; background: var(--bg); }
h1, h2, h3, h4, h5, h6, p, ol, ul, dl, dd, figure, pre { margin: 0; }
h1, h2, h3, h4 { text-wrap: balance; line-height: 1.25; }
ol, ul { padding: 0; }
a { color: var(--accent); }
code, pre { font-family: var(--code); font-size: 0.9em; }
.mono { font-family: var(--mono); font-size: 0.9em; }
code { background: var(--sunken); border-radius: 3px; padding: 0.05em 0.3em; overflow-wrap: anywhere; }
pre code { background: none; padding: 0; overflow-wrap: normal; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }
button, select { font: inherit; color: inherit; }

.top { position: sticky; top: env(safe-area-inset-top, 0px); z-index: 5; background: var(--bg); }
/* one column for every tab, header and tab bar included, so nothing moves on a tab switch */
.wrap { max-width: 1320px; margin-inline: auto; padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); }
.top .wrap { padding-block: 16px 10px; display: flex; flex-direction: column; gap: 14px; }
.brand { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: 4px 16px; }
.brand h1 { font-size: 17px; font-weight: 650; }
.brand .newest { display: block; color: var(--ink-2); font-size: 12.5px; }
.conn { display: none; align-items: center; gap: 6px; font-size: 12.5px; color: var(--ink-2); white-space: nowrap; }
.conn.on { display: inline-flex; }
.conn .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--good-mark); flex: none; }
.conn.stale .dot { background: var(--warn-mark); }
/* Tabs and periods are one kind of control: a row of buttons, the chosen one an inverted pill. */
.tabs { display: inline-flex; flex-wrap: wrap; gap: 2px; padding: 2px; background: var(--surface); border: 1px solid var(--line); border-radius: 8px; }
.tabs a { padding: 5px 11px; border-radius: 6px; color: var(--ink-2); text-decoration: none; font-weight: 550; font-size: 12.5px; white-space: nowrap; }
.tabs a:hover { background: var(--hover); }
.tabs a[aria-current="page"] { background: var(--pill); color: var(--pill-text); }
main.wrap { padding-block: 4px 48px; display: flex; flex-direction: column; gap: 14px; }
.onething { display: flex; flex-wrap: wrap; gap: 4px 12px; align-items: baseline; background: var(--accent-soft); border-radius: 8px; padding: 10px 14px; }
.one-label { font-size: 12px; font-weight: 600; color: var(--accent); white-space: nowrap; }
.one-text { flex: 1 1 260px; min-width: 0; font-weight: 500; }
.view { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.view > div[id$="-root"] { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.view[hidden] { display: none; }
.view-title { font-size: 17px; font-weight: 650; }
.sub { font-size: 13px; font-weight: 600; color: var(--ink); margin-top: 8px; }
.lede, .muted { color: var(--ink-2); max-width: 70ch; }
.muted { color: var(--ink-3); font-size: 13px; }

.daybar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; }
.daybar .view-title { margin-right: auto; }
.btn { background: var(--raised); border: 1px solid var(--line); border-radius: 7px; padding: 4px 10px; min-height: 32px; cursor: pointer; font-size: 12.5px; font-weight: 550; }
.btn:hover:not(:disabled) { background: var(--hover); }
.btn:disabled { color: var(--ink-3); cursor: default; opacity: 0.6; }
.btn[aria-pressed="true"] { background: var(--pill); border-color: var(--pill); color: var(--pill-text); }
.chips { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.chips .lab { font-size: 13px; color: var(--ink-3); margin-right: 2px; }
.chips .btn { font-variant-numeric: tabular-nums; }
select.btn { padding-right: 6px; max-width: 100%; }
.stats { display: flex; flex-wrap: wrap; gap: 6px 22px; color: var(--ink-2); font-size: 13px; }
.stats b { color: var(--ink); font-weight: 600; font-variant-numeric: tabular-nums; margin-right: 4px; font-size: 15px; }

.panel { background: var(--surface); border: 1px solid var(--line); border-radius: var(--r); }
.items { list-style: none; }
.items > li { display: grid; grid-template-columns: 30px minmax(0, 1fr); gap: 4px 8px; padding: 14px 16px; border-top: 1px solid var(--line); }
.items > li:first-child { border-top: 0; }
.rank { font-family: var(--mono); font-size: 14px; color: var(--ink-3); font-variant-numeric: tabular-nums; padding-top: 1px; }
.item-body { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.field { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
.field .k { font-size: 12px; font-weight: 600; color: var(--ink-3); display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.field .v { max-width: 78ch; overflow-wrap: anywhere; }
.field.do .v { font-weight: 500; }
.field.what .v { color: var(--ink-2); }
.pill { display: inline-block; font-family: var(--sans); font-size: 11.5px; font-weight: 600; line-height: 1; padding: 4px 7px; border-radius: 999px; background: var(--sunken); color: var(--ink-2); white-space: nowrap; }
.pill::first-letter { text-transform: uppercase; }
.pill.repeat, .pill.medium, .pill.warn { background: var(--warn-soft); color: var(--warn); }
.pill.worse, .pill.high, .pill.crit { background: var(--crit-soft); color: var(--crit); }
.pill.better, .pill.fix { background: var(--good-soft); color: var(--good); }
.pill.new { background: var(--accent-soft); color: var(--accent); }

details > summary { cursor: pointer; }
.evidence > summary { font-size: 13px; color: var(--accent); font-weight: 500; width: fit-content; padding-block: 8px; }
.evidence[open] > summary { margin-bottom: 10px; }
.ev-body { display: flex; flex-direction: column; gap: 12px; }
.sess { background: var(--bg); border-radius: 8px; padding: 10px 12px; display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.sess-head { display: flex; flex-wrap: wrap; gap: 4px 10px; align-items: baseline; min-width: 0; }
.sid { font-weight: 500; }
.sess-head .proj { color: var(--ink-2); font-size: 13px; overflow-wrap: anywhere; min-width: 0; }
.facts { display: flex; flex-wrap: wrap; gap: 6px 20px; font-size: 13px; }
.facts div { display: flex; flex-direction: column; }
.facts dt { color: var(--ink-3); font-size: 12px; }
.facts dd { font-variant-numeric: tabular-nums; font-weight: 500; display: flex; gap: 6px; align-items: center; }
.finds { list-style: none; display: flex; flex-direction: column; gap: 8px; font-size: 13px; }
.finds li { display: flex; flex-direction: column; gap: 2px; min-width: 0; overflow-wrap: anywhere; }
.finds .fh { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.finds .pat { font-family: var(--mono); font-size: 12px; }
.finds .sug { color: var(--ink-2); }
.ev-where { display: flex; flex-wrap: wrap; gap: 4px 10px; align-items: baseline; font-size: 13px; min-width: 0; }
.ev-where .lab { color: var(--ink-3); font-size: 12px; }
.ev-where code, td code, .finds code, .codebox pre code { font-size: 12px; }
.evidence-slice { background: var(--bg); border-radius: 8px; min-width: 0; }
.evidence-slice > summary { padding: 9px 12px; font-size: 13px; font-weight: 500; overflow-wrap: anywhere; }
.evidence-note { color: var(--ink-3); font-size: 12px; margin: 0 12px 8px; }
.evidence-slice.gone .evidence-note { margin-bottom: 0; padding-bottom: 10px; }
.evidence-rows { list-style: none; margin: 0 12px 12px; display: flex; flex-direction: column; gap: 6px; font-size: 13px; }
.ev-row { display: flex; flex-direction: column; gap: 1px; padding: 6px 8px; border-left: 3px solid var(--line); min-width: 0; }
.ev-row.ev-focus { border-left-color: var(--accent); background: var(--accent-soft); }
.ev-meta { font-family: var(--mono); font-size: 12px; color: var(--ink-3); overflow-wrap: anywhere; }
.ev-focus .ev-meta { color: var(--ink-2); }
.ev-text { color: var(--ink-2); overflow-wrap: anywhere; white-space: pre-wrap; }
.ev-focus .ev-text { color: var(--ink); }
.evidence-open { display: none; } /* the same command sits above the excerpt with its copy button */
.fold { background: var(--surface); border: 1px solid var(--line); border-radius: var(--r); min-width: 0; }
.fold > summary { padding: 10px 14px; font-weight: 500; }
.fold > :not(summary) { margin: 0 14px 14px; }

.block { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.block.card { background: var(--surface); border: 1px solid var(--line); border-radius: var(--r); padding: 14px 16px; }
.block.card .sub { margin-top: 0; }
.sub2 { font-size: 13px; font-weight: 600; color: var(--ink-2); margin-top: 4px; }
.ft { list-style: none; }
.ft > li { display: flex; flex-direction: column; gap: 6px; padding: 12px 16px; border-top: 1px solid var(--line); min-width: 0; }
.ft > li:first-child { border-top: 0; }
.ft-head { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: center; }
.ft-applied { font-size: 12px; color: var(--ink-3); }
.ft-text { max-width: 78ch; overflow-wrap: anywhere; }
.periods { list-style: none; display: flex; flex-wrap: wrap; gap: 4px 28px; font-size: 14px; color: var(--ink-2); font-variant-numeric: tabular-nums; }
.periods b { color: var(--ink); font-weight: 600; margin-right: 6px; }
.plain { padding-left: 20px; display: flex; flex-direction: column; gap: 4px; max-width: 80ch; }
.plain li { overflow-wrap: anywhere; }
table.wrapcol td:first-child { min-width: 16ch; overflow-wrap: anywhere; }
td.nw, th.nw { white-space: nowrap; }
td.wide { min-width: 20ch; overflow-wrap: anywhere; }
.checked { color: var(--ink-3); font-size: 12px; }

.empty { background: var(--surface); border: 1px dashed var(--line); border-radius: var(--r); padding: 22px 18px; display: flex; flex-direction: column; gap: 4px; color: var(--ink-2); }
.empty-title { color: var(--ink); font-weight: 600; font-size: 16px; }

.props { display: grid; grid-template-columns: minmax(0, 1fr); gap: 12px; }
.prop { background: var(--surface); border: 1px solid var(--line); border-radius: var(--r); padding: 14px 16px; display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.prop-head { display: flex; gap: 10px; align-items: baseline; }
.prop-head .pid { font-family: var(--mono); font-size: 13px; font-weight: 500; color: var(--accent); }
.prop-head h4 { font-size: 16px; font-weight: 600; min-width: 0; }
.codebox { border: 1px solid var(--line); border-radius: 8px; background: var(--sunken); min-width: 0; }
.codebar { display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 4px 4px 4px 10px; border-bottom: 1px solid var(--line); font-size: 12px; color: var(--ink-3); }
.codebar .btn { padding: 3px 9px; font-size: 12px; }
.scroll { overflow-x: auto; max-width: 100%; }
.codebox pre { padding: 10px; white-space: pre; font-size: 13px; line-height: 1.45; }

.headline { display: grid; grid-template-columns: minmax(0, 5fr) minmax(0, 7fr); gap: 14px 24px; padding-block: 16px; border-top: 1px solid var(--line); align-items: start; }
.hl-text { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.hl-head { display: flex; flex-wrap: wrap; gap: 8px 10px; align-items: center; }
.hl-head h3 { font-size: 17px; font-weight: 600; }
.hl-charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; min-width: 0; }
.chart-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }
.chart { background: var(--surface); border: 1px solid var(--line); border-radius: var(--r); padding: 10px 10px 4px; min-width: 0; }
.chart figcaption { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 2px 10px; align-items: baseline; padding: 0 2px 4px; }
.ch-title { font-size: 13px; font-weight: 600; }
.readout { font-size: 12px; color: var(--ink-2); font-variant-numeric: tabular-nums; font-family: var(--mono); }
svg.ch { display: block; width: 100%; height: auto; }
.ch-grid { stroke: var(--grid); stroke-width: 1; }
.ch-lab { fill: var(--ink-3); font-family: var(--mono); font-size: 9.5px; }
.ch-hit { fill: transparent; }
.ch-bar { fill: var(--bar); }
.ch-bar.now { fill: var(--bar-now); }
.ch-zero { fill: var(--line); }
.b:hover .ch-bar { fill: var(--bar-now); }
.b:hover .ch-hit { fill: var(--sunken); }
.ch-limit { stroke: var(--warn); stroke-width: 1.2; stroke-dasharray: 4 3; }
.ch-limit-bg { fill: var(--surface); }
.ch-limit-lab { fill: var(--warn); font-family: var(--mono); font-size: 9.5px; font-weight: 500; }

table { border-collapse: collapse; font-size: 13px; width: 100%; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--grid); vertical-align: top; }
th { color: var(--ink-3); font-weight: 550; font-size: 11.5px; white-space: nowrap; }
td { font-variant-numeric: tabular-nums; min-width: 6ch; }
table.num td, table.num th { white-space: nowrap; }
td.n, th.n { text-align: right; white-space: nowrap; }
th button.sort { all: unset; cursor: pointer; font: inherit; color: inherit; display: inline-block; min-height: 36px; line-height: 36px; min-width: 36px; }
th button.sort:hover { color: var(--accent); }
th button.sort:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }
th[aria-sort="ascending"] button.sort::after { content: " \2191"; }
th[aria-sort="descending"] button.sort::after { content: " \2193"; }
td.barcell { width: 22%; min-width: 60px; vertical-align: middle; }
.share { display: block; height: 8px; border-radius: 0 4px 4px 0; background: var(--bar); min-width: 2px; }
.cost-chart { max-width: 520px; }
.panel-t { background: var(--surface); border: 1px solid var(--line); border-radius: var(--r); padding: 4px 4px 0; min-width: 0; }
.panel-t table tr:last-child td { border-bottom: 0; }
.panel-t td { vertical-align: middle; }
.cost-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px 16px; align-items: start; }
.cost-grid > div { min-width: 0; display: flex; flex-direction: column; gap: 6px; }
.split { display: flex; gap: 2px; height: 10px; max-width: 520px; }
.split span { display: block; height: 100%; min-width: 2px; background: var(--bar-now); border-radius: 2px; }
.split span.s2 { background: var(--bar); }
.split-key { display: flex; flex-wrap: wrap; gap: 4px 18px; font-size: 13px; color: var(--ink-2); }
.split-key i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; background: var(--bar-now); margin-right: 6px; }
.split-key i.s2 { background: var(--bar); }
.split-key b { color: var(--ink); font-variant-numeric: tabular-nums; }
.totals-line { color: var(--ink-2); font-size: 14px; max-width: 80ch; }
td a.sid { font-family: var(--mono); }
.md { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.md > p, .md li { max-width: 80ch; overflow-wrap: anywhere; }
.md .md-h2 { font-size: 16px; font-weight: 600; margin-top: 10px; }
.md .md-h3 { font-size: 15px; font-weight: 600; margin-top: 6px; }
.md .md-h4, .md .md-h5, .md .md-h6 { font-size: 14px; font-weight: 600; }
.md ul, .md ol { padding-left: 22px; display: flex; flex-direction: column; gap: 4px; }
.md li > ul, .md li > ol { margin-top: 4px; }
.md pre { background: var(--sunken); border-radius: 8px; padding: 10px; font-size: 13px; }
.md hr { border: 0; border-top: 1px solid var(--line); width: 100%; margin: 4px 0; }
.md ol.patterns { list-style: none; padding: 0; gap: 0; background: var(--surface); border: 1px solid var(--line); border-radius: var(--r); }
.patterns > li { border-top: 1px solid var(--line); max-width: none; }
.patterns > li:first-child { border-top: 0; }
.patterns summary, .patterns .pflat { display: flex; gap: 10px; padding: 10px 14px; align-items: baseline; font-weight: 500; }
.patterns summary::before { content: "+"; font-family: var(--mono); color: var(--accent); order: 3; margin-left: auto; padding-left: 10px; }
.patterns details[open] > summary::before { content: "-"; }
.patterns summary::-webkit-details-marker { display: none; }
.patterns summary { list-style: none; }
.patterns .num { font-family: var(--mono); color: var(--ink-3); font-size: 13px; min-width: 1.4em; font-variant-numeric: tabular-nums; }
.patterns .ptext { min-width: 0; }
.patterns details > ul { margin: 0 14px 12px 38px; padding-left: 16px; color: var(--ink-2); font-size: 14px; }
.md li.fixline { list-style: none; margin-left: -16px; color: var(--ink); margin-top: 2px; }

/* Overview (docs/mockups/PageMM.dc.html): a row of stat tiles, the spend graph beside what is
   running and the coach's tips, then By activity beside Projects and MCP servers. */
.tabrow { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; }
.seg { display: none; gap: 2px; padding: 2px; background: var(--surface); border: 1px solid var(--line); border-radius: 8px; }
.ov-live .seg { display: inline-flex; }
.seg button { border: 0; background: none; border-radius: 6px; padding: 5px 11px; min-width: 40px; font-size: 12.5px; font-weight: 550; color: var(--ink-2); cursor: pointer; white-space: nowrap; }
.seg button:hover { background: var(--hover); }
.seg button[aria-pressed="true"] { background: var(--pill); color: var(--pill-text); }
#overview-root.ov { display: flex; flex-direction: column; gap: 10px; }
.ov-card { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 11px 12px 8px; display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.ov-card .sub { margin-top: 0; }
.ov-card [title] { cursor: help; }
.ov-head { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline; gap: 4px 14px; padding: 0; }
.ov-hint { font-size: 12px; font-weight: 400; color: var(--ink-3); margin-left: 8px; }
.ov-note { font-size: 12px; color: var(--ink-3); }
.is-good { color: var(--good); }
.is-warn, .lvl-warn { color: var(--warn); }
.is-crit, .lvl-crit { color: var(--crit); }
.is-flat { color: var(--ink-3); }
/* tiles: label, figure, one or two lines of detail. Six, three or two equal columns, so the
   six figures fill one row from 1180px and a row never ends half empty; limits use the same grid. */
.ov-tiles { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.ov-tiles.lims-1 .ov-lim { grid-column: span 2; }
.ov-tiles.lims-3 .ov-lim:last-child { grid-column: span 2; }
@media (min-width: 600px) {
  .ov-tiles { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .ov-tiles.lims-1 .ov-lim { grid-column: span 3; }
  .ov-tiles.lims-3 .ov-lim:last-child { grid-column: auto; }
  .ov-tiles.lims-4 .ov-lim:last-child { grid-column: span 3; }
}
@media (min-width: 1180px) {
  .ov-tiles { grid-template-columns: repeat(6, minmax(0, 1fr)); }
  .ov-tiles.lims-1 .ov-lim { grid-column: span 6; }
  .ov-tiles.lims-2 .ov-lim { grid-column: span 3; }
  .ov-tiles.lims-3 .ov-lim { grid-column: span 2; }
  .ov-tiles.lims-3 .ov-lim:last-child { grid-column: span 2; }
  .ov-tiles.lims-4 .ov-lim:nth-last-child(n+3) { grid-column: span 2; }
  .ov-tiles.lims-4 .ov-lim:last-child { grid-column: auto; }
}
.ov-tile { padding: 11px 13px 12px; gap: 0; }
.ov-tile.warning { box-shadow: inset 3px 0 0 var(--warn-mark); }
.ov-tile.critical { box-shadow: inset 3px 0 0 var(--crit-mark); }
.ov-tile .k { font-size: 12px; color: var(--ink-2); }
.ov-tile .v { font-size: 22px; font-weight: 620; letter-spacing: -0.01em; margin-top: 3px; line-height: 1.2; overflow-wrap: anywhere; }
.ov-tile .v.words { font-size: 16px; line-height: 1.65; }
.ov-tile .s { font-size: 12px; color: var(--ink-3); margin-top: 3px; overflow-wrap: anywhere; }
.ov-tile .s b { font-weight: 600; }
.ov-lim.warning .v { color: var(--warn); }
.ov-lim.critical .v { color: var(--crit); }
.ov-spark { margin-top: 6px; color: var(--accent); }
.ov-spark.is-good { color: var(--good-mark); }
.ov-spark.is-warn { color: var(--warn-mark); }
.ov-spark .lg { height: 26px; }
.ov-spark .lg-line { stroke: currentColor; stroke-width: 1.5; }
.ov-spark .lg-hole { stroke: currentColor; stroke-width: 5; }
.ov-spark .lg-ring { stroke-width: 7; }
/* the graph beside the running sessions and tips */
.ov-row2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(420px, 100%), 1fr)); gap: 10px; }
.ov-graph .ov-gt { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 14px; }
.ov-legend { display: flex; flex-wrap: wrap; gap: 3px 12px; font-size: 12px; color: var(--ink-2); }
.ov-legend span { display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
.ov-legend i { width: 12px; height: 3px; border-radius: 2px; background: var(--accent); flex: none; }
.ov-legend i.avg { height: 0; background: none; border-top: 1.25px dashed var(--ink-2); border-radius: 0; }
.lg-plot { position: relative; padding-left: 44px; }
.lg-y { position: absolute; left: 0; top: 0; bottom: 0; width: 38px; font-size: 11px; color: var(--ink-3); }
.lg-y span { position: absolute; right: 0; transform: translateY(-50%); white-space: nowrap; }
.lg { display: block; width: 100%; height: 190px; overflow: visible; }
.lg:focus-visible { outline-offset: 6px; }
.lg * { vector-effect: non-scaling-stroke; }
.lg-grid, .lg-base { stroke: var(--grid); stroke-width: 1; }
.lg-area { display: none; }
.lg-line { fill: none; stroke: var(--accent); stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
.lg-avg { fill: none; stroke: var(--ink-2); stroke-width: 1.25; stroke-dasharray: 3 3; }
/* a marker is a round line cap: a ring of the card's colour with a dot of the line's colour on it */
.lg-ring, .lg-hole, .lg-dot { fill: none; stroke-linecap: round; }
.lg-ring { stroke: var(--surface); stroke-width: 8; }
.lg-hole { stroke: var(--accent); stroke-width: 6; }
.lg-dot { stroke: var(--accent); stroke-width: 8; opacity: 0; }
/* the spend graph marks its peak only */
.ov-graph .lg-ring:not(.peak), .ov-graph .lg-ring:not(.peak) + .lg-hole { display: none; }
.lg-area, .lg-line, .lg-avg, .lg-ring, .lg-hole { pointer-events: none; }
.lg .b:hover .ch-hit, .lg .b.on .ch-hit { fill: var(--accent); fill-opacity: 0.08; }
.lg .b:hover .lg-dot, .lg .b.on .lg-dot { opacity: 1; }
/* a threshold: a dashed amber line with its figure on it, at the left of the plot */
.lg-limit { stroke: var(--warn); stroke-width: 1.2; stroke-dasharray: 4 3; pointer-events: none; }
.lg-limit-lab { position: absolute; left: 48px; transform: translateY(-50%); font-size: 11px; font-weight: 500; color: var(--warn); background: var(--surface); padding: 0 3px; border-radius: 2px; white-space: nowrap; pointer-events: none; }
/* the Trends graphs: the spend graph's look, shorter to sit in a grid of cards */
.chart.trend { padding: 10px 12px 6px; }
.chart.trend .lg { height: 140px; }
.lg-x { position: relative; height: 16px; margin: 4px 0 0 44px; font-size: 11px; color: var(--ink-3); }
.lg-x span { position: absolute; top: 0; transform: translateX(-50%); white-space: nowrap; }
.lg-x span:first-child { transform: none; }
.lg-x span:last-child { transform: translateX(-100%); }
.ov-run { display: flex; gap: 10px; align-items: center; border-left: 3px solid var(--good-mark); padding: 6px 10px; background: var(--sunken); border-radius: 6px; }
.ov-run.run-warn { border-left-color: var(--warn-mark); }
.ov-run.run-crit { border-left-color: var(--crit-mark); }
.ov-run .who { flex: 1; min-width: 0; }
.ov-run .t { font-size: 13px; font-weight: 550; overflow-wrap: anywhere; }
.ov-run .m { font-size: 12px; color: var(--ink-2); }
.ov-run .ctx { text-align: right; white-space: nowrap; }
.ov-run .ctx .p { font-size: 15px; font-weight: 620; }
.ov-run .ctx .m { color: var(--ink-3); }
.ov-run.run-warn .ctx .p { color: var(--warn); }
.ov-run.run-crit .ctx .p { color: var(--crit); }
.ov-tips-h { margin-top: 6px; }
.ov-tip { border-left: 3px solid var(--warn-mark); padding: 4px 10px; min-width: 0; }
.ov-tip .t { font-size: 13px; font-weight: 550; overflow-wrap: anywhere; }
.ov-tip .a { font-size: 12px; color: var(--ink-2); overflow-wrap: anywhere; }
/* three tables; the first figure of each row sits over a thin heat bar */
.ov-row3 { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr); gap: 10px; align-items: start; }
.ov-col { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.ov-acts, .ov-projects, .ov-mcp { padding-bottom: 6px; }
.ov-t { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.ov-t th, .ov-t td { border: 0; border-top: 1px solid var(--grid); padding: 5px 0 5px 12px; text-align: right; vertical-align: top; white-space: nowrap; min-width: 0; font-size: 12.5px; }
.ov-t thead th { border-top: 0; padding-block: 4px; font-size: 11.5px; font-weight: 550; color: var(--ink-3); }
.ov-t thead th.main { color: var(--ink); }
.ov-t th:first-child { text-align: left; padding-left: 0; }
.ov-t tbody th { color: var(--ink); font-weight: 400; white-space: normal; overflow-wrap: anywhere; width: 99%; }
.ov-t td.mid { color: var(--ink-2); }
.ov-t td.dim { color: var(--ink-3); }
.ov-t td.barcol { width: 110px; min-width: 110px; }
/* Heat bar: one accent to amber to red gradient laid across the whole 110px track and clipped by the bar. */
.hb { width: 110px; height: 4px; border-radius: 2px; background: var(--grid); margin: 3px 0 0 auto; overflow: hidden; }
.hb i { display: block; height: 4px; border-radius: 2px; background: linear-gradient(90deg, var(--accent), var(--heat-mid), var(--crit-mark)) 0 0 / 110px 4px no-repeat; }
@media (max-width: 900px) {
  .ov-row3 { grid-template-columns: minmax(0, 1fr); }
}

@media (max-width: 1024px) {
  /* finger-sized targets wherever a thumb may be the pointer */
  .seg button { min-height: 44px; }
  .tabs a { padding-block: 13px; }
  .btn { min-height: 44px; padding-inline: 14px; }
  .codebar .btn { min-height: 44px; }
  th button.sort { min-height: 44px; line-height: 44px; min-width: 44px; }
  td a.sid { display: inline-block; min-height: 44px; line-height: 44px; }
  .fold > summary, .evidence > summary, .evidence-slice > summary, .patterns summary { padding-block: 13px; }
}
@media (max-width: 720px) {
  td.barcell, th.barcell, .opt { display: none; }
  th, td { padding: 6px 8px; }
  .headline { grid-template-columns: minmax(0, 1fr); }
  .items > li { grid-template-columns: 22px minmax(0, 1fr); padding: 12px; }
  .patterns details > ul { margin-left: 14px; }
  /* a table wider than a phone scrolls sideways inside its card */
  .ov-acts, .ov-projects, .ov-mcp { overflow-x: auto; }
  .ov-t td.barcol { width: 84px; min-width: 84px; }
  .hb { width: 84px; }
  .hb i { background-size: 84px 4px; }
  .lg-plot { padding-left: 38px; }
  .lg-x { margin-left: 38px; }
  .lg-limit-lab { left: 42px; }
}
@media (prefers-reduced-motion: reduce) { * { transition: none !important; animation: none !important; scroll-behavior: auto !important; } }
</style>

<header class="top">
  <div class="wrap">
    <div class="brand"><div><h1>{{NAME}}</h1><span class="newest">{{NEWEST}}</span></div><span class="conn" id="ov-conn"><i class="dot"></i><span></span></span></div>
    <div class="tabrow">
    <nav class="tabs" aria-label="Views">
      <a href="#overview" id="tab-overview">Overview</a>
      <a href="#today" id="tab-today">Today</a>
      <a href="#week" id="tab-week">This week</a>
      <a href="#cost" id="tab-cost">Cost</a>
      <a href="#trends" id="tab-trends">Trends</a>
      <a href="#progress" id="tab-progress">Progress</a>
      <a href="#setup" id="tab-setup">Setup</a>
      <a href="#baseline" id="tab-baseline">Baseline</a>
    </nav>
    <div class="seg" id="ov-period" role="group" aria-label="Period">
      <button type="button" id="ov-p-today" aria-pressed="false">Today</button>
      <button type="button" id="ov-p-week" aria-pressed="false">7 days</button>
      <button type="button" id="ov-p-month" aria-pressed="false">30 days</button>
      <button type="button" id="ov-p-all" aria-pressed="false">All time</button>
    </div>
    </div>
  </div>
</header>

<main class="wrap">
  {{ONE}}
  <section class="view" id="view-overview" aria-labelledby="tab-overview">
    <div id="overview-root">
      <div class="empty"><p class="empty-title">No live numbers yet.</p><p>This fills in after the coach next records your usage.</p></div>
    </div>
  </section>
  <section class="view" id="view-today" aria-labelledby="tab-today">
    <div id="today-root">
      <div class="empty"><p class="empty-title">No daily recap yet.</p><p>The first one arrives after the next daily run.</p></div>
    </div>
  </section>
  <section class="view" id="view-week" aria-labelledby="tab-week">
    <div id="week-root">
      <div class="empty"><p class="empty-title">No weekly review yet.</p><p>The first one arrives after the next weekly run.</p></div>
    </div>
  </section>
  <section class="view" id="view-cost" aria-labelledby="tab-cost">
    <h2 class="view-title">Cost</h2>
    <p class="lede">What your tokens would cost at API list prices. <strong>API-equivalent, not a bill:</strong> on a Claude subscription you are not charged this.</p>
    {{COST}}
    <div id="cost-root"></div>
  </section>
  <section class="view" id="view-trends" aria-labelledby="tab-trends">
    <h2 class="view-title">Trends</h2>
    {{TRENDS}}
  </section>
  <section class="view" id="view-progress" aria-labelledby="tab-progress">
    <h2 class="view-title">Progress</h2>
    {{PROGRESS}}
  </section>
  <section class="view" id="view-setup" aria-labelledby="tab-setup">
    <h2 class="view-title">Setup</h2>
    {{SETUP}}
  </section>
  <section class="view" id="view-baseline" aria-labelledby="tab-baseline">
    {{BASELINE}}
  </section>
</main>
{{EXCERPTS}}
{{GRAPHS}}

<script type="application/json" id="coach-data">{{DATA}}</script>
<script>
(function () {
  "use strict";
  var D = JSON.parse(document.getElementById("coach-data").textContent);
  var VIEWS = ["overview", "today", "week", "cost", "trends", "progress", "setup", "baseline"];
  var dayIdx = 0, weekIdx = 0, costIdx = 0;
  var LIVE = D.live || null, liveOk = !!(LIVE && !(LIVE.version > 1));
  var PERIODS = ["today", "week", "month", "all"], period = "today";

  function h(tag, attrs) {
    var e = document.createElement(tag), i, c, k;
    if (attrs) {
      for (k in attrs) {
        if (attrs[k] == null) { continue; }
        if (k === "class") { e.className = attrs[k]; }
        else if (k === "text") { e.textContent = attrs[k]; }
        else if (attrs[k] === true) { e.setAttribute(k, ""); }
        else if (attrs[k] !== false && attrs[k] != null) { e.setAttribute(k, attrs[k]); }
      }
    }
    for (i = 2; i < arguments.length; i++) {
      c = arguments[i];
      if (c == null || c === false) { continue; }
      e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return e;
  }
  /* Report text is data: it only ever becomes text nodes and code elements. */
  function rich(el, text) {
    String(text || "").split(/(`[^`]+`)/).forEach(function (p) {
      if (p.length > 2 && p.charAt(0) === "`" && p.charAt(p.length - 1) === "`") {
        el.appendChild(h("code", { text: p.slice(1, -1) }));
      } else if (p) { el.appendChild(document.createTextNode(p)); }
    });
    return el;
  }
  function tok(n) {
    if (n == null) { return "?"; }
    if (n >= 999500000) { return (n / 1e9).toFixed(1).replace(".0", "") + "B"; }
    if (n >= 999500) { return (n / 1e6).toFixed(1).replace(".0", "") + "M"; }
    if (n >= 1000) { return Math.round(n / 1000) + "k"; }
    return String(n);
  }
  function mins(n) {
    if (n == null) { return "?"; }
    return n >= 60 ? Math.floor(n / 60) + " h " + (n % 60) + " min" : n + " min";
  }
  function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }
  function field(cls, label, text, extra) {
    return h("div", { class: "field " + cls }, h("div", { class: "k" }, label, extra),
      rich(h("div", { class: "v" }), text));
  }
  function emptyBox(title, line) {
    return h("div", { class: "empty" }, h("p", { class: "empty-title", text: title }), h("p", { text: line }));
  }
  function stats(c) {
    if (!c || c.sessions == null) { return null; }
    var box = h("div", { class: "stats" });
    [[c.sessions, "sessions" + (c.scheduled ? " (" + c.scheduled + " scheduled)" : "")],
     [c.prompts, "typed prompts"], [mins(c.active_min), "active"],
     [tok(c.out_tokens), "output tokens"], [c.errors, "tool errors"],
     [c.corrections, "corrections"], [c.compactions, "compactions"]].forEach(function (p) {
      if (p[0] == null || p[0] === "?") { return; }
      box.appendChild(h("span", null, h("b", { text: String(p[0]) }), p[1]));
    });
    return box;
  }
  function findingList(list, withSession) {
    var ul = h("ul", { class: "finds" });
    list.forEach(function (f) {
      ul.appendChild(h("li", null,
        h("div", { class: "fh" },
          h("span", { class: "pill " + (f.severity === "high" || f.severity === "medium" ? f.severity : ""), text: f.severity }),
          h("span", { class: "pat", text: f.pattern.replace(/_/g, " ") }),
          withSession && f.session ? h("code", { text: f.session }) : null),
        h("div", { text: f.evidence }),
        h("div", { class: "sug", text: "Suggested: " + f.suggestion })));
    });
    return ul;
  }
  /* One Evidence disclosure per item: where it happened and the command to open it,
     the excerpt built from the transcript, then the digest facts for the sessions named. */
  function evidence(d, it) {
    var ev = it.ev || null;
    var ids = d.sessions && it.ids ? it.ids : [];
    if (!ids.length && !ev) { return null; }
    var n = 0, found = d.findings || {};
    ids.forEach(function (id) { n += (found[id] || []).length; });
    var body = h("div", { class: "ev-body" });
    if (ev) {
      body.appendChild(h("div", { class: "ev-where" }, h("span", { class: "lab", text: "Where" }), h("code", { text: ev.tag })));
      var cmd = ev.open || it.evidence_cmd;
      if (cmd) { body.appendChild(copyBox(cmd, "Command to open the evidence")); }
      var tpl = ev.tpl ? document.getElementById(ev.tpl) : null;
      if (tpl && tpl.content) { body.appendChild(tpl.content.cloneNode(true)); }
    }
    ids.forEach(function (id) {
      var s = d.sessions[id], box = h("div", { class: "sess" });
      box.appendChild(h("div", { class: "sess-head" }, h("code", { class: "sid", text: id }),
        h("span", { class: "proj", text: s ? s.project + (s.title ? ", " + s.title : "") : "Not in the digest for this day" }),
        s && s.kind === "scheduled" ? h("span", { class: "pill", text: "scheduled" }) : null));
      if (s) {
        var dl = h("dl", { class: "facts" });
        [["Start", s.start || "?"], ["Active", mins(s.active_min)], ["Prompts", String(s.prompts)],
         ["Output tokens", tok(s.out) + (s.sub_out ? " + " + tok(s.sub_out) + " subagents" : "")]].forEach(function (p) {
          dl.appendChild(h("div", null, h("dt", { text: p[0] }), h("dd", { text: p[1] })));
        });
        if (s.peak) {
          dl.appendChild(h("div", null, h("dt", { text: "Peak context" }),
            h("dd", null, tok(s.peak), s.peak >= D.contextLine ? h("span", { class: "pill warn", text: "over " + tok(D.contextLine) }) : null)));
        }
        box.appendChild(dl);
      }
      var fs = found[id] || [];
      box.appendChild(fs.length ? findingList(fs, false) : h("div", { class: "muted", text: "No detector findings for this session." }));
      body.appendChild(box);
    });
    var parts = [];
    if (ev) { parts.push(ev.tag); }
    if (ids.length) { parts.push(plural(ids.length, "session") + ", " + plural(n, "detector finding")); }
    return h("details", { class: "evidence" }, h("summary", { text: "Evidence: " + parts.join("; ") }), body);
  }
  function itemList(d, items, weekly) {
    var ol = h("ol", { class: "items panel" });
    items.forEach(function (it) {
      var mark = null;
      if (weekly && it.state) { mark = h("span", { class: "pill " + it.state, text: it.state }); }
      else if (!weekly && it.repeat) { mark = h("span", { class: "pill repeat", text: "repeat" }); }
      var body = h("div", { class: "item-body" },
        field("do", "Do instead", it["do"] || "No action given.", mark),
        field("what", "What happened", it.what));
      if (weekly && it.trend) { body.appendChild(field("what", "Trend", it.trend)); }
      var ev = evidence(d, it); if (ev) { body.appendChild(ev); }
      ol.appendChild(h("li", null, h("span", { class: "rank", text: String(it.n) }), body));
    });
    return ol;
  }
  function refocus(id) { var e = id && document.getElementById(id); if (e) { e.focus(); } }
  function button(id, label, fn, opts) {
    var b = h("button", { type: "button", class: "btn", id: id, text: label });
    if (opts && opts.disabled) { b.disabled = true; }
    if (opts && opts.pressed != null) { b.setAttribute("aria-pressed", opts.pressed ? "true" : "false"); }
    if (opts && opts.label) { b.setAttribute("aria-label", opts.label); }
    b.addEventListener("click", function () { fn(); refocus(id); });
    return b;
  }

  function renderToday() {
    var root = document.getElementById("today-root");
    if (!D.dailies.length) { return; }
    root.textContent = "";
    var d = D.dailies[dayIdx], last = D.dailies.length - 1;
    function go(i) { dayIdx = Math.max(0, Math.min(last, i)); renderToday(); }
    root.appendChild(h("div", { class: "daybar" },
      h("h2", { class: "view-title", text: d.label }),
      button("day-prev", "Previous day", function () { go(dayIdx + 1); }, { disabled: dayIdx >= last }),
      button("day-next", "Next day", function () { go(dayIdx - 1); }, { disabled: dayIdx <= 0 })));
    if (D.dailies.length > 1) {
      var chips = h("div", { class: "chips" }, h("span", { class: "lab", text: "Recent" }));
      D.dailies.slice(0, 10).forEach(function (x, i) {
        chips.appendChild(button("day-" + x.date, x.short, function () { go(i); },
          { pressed: i === dayIdx, label: "Show " + x.label }));
      });
      if (D.dailies.length > 10) {
        var sel = h("select", { id: "day-select", class: "btn", "aria-label": "Earlier days" });
        D.dailies.forEach(function (x, i) {
          var o = h("option", { value: String(i), text: x.label });
          if (i === dayIdx) { o.selected = true; }
          sel.appendChild(o);
        });
        sel.addEventListener("change", function () { go(parseInt(sel.value, 10)); refocus("day-select"); });
        chips.appendChild(sel);
      }
      root.appendChild(chips);
    }
    var st = stats(d.counts);
    if (st) { root.appendChild(st); }
    if (d.totals) { root.appendChild(h("p", { class: "totals-line", text: "Totals: " + d.totals })); }
    if (!d.items.length) {
      root.appendChild(emptyBox(d.note || "Nothing notable on this day.", "No items were raised in this recap."));
    } else {
      root.appendChild(itemList(d, d.items, false));
    }
    if (d.checked) { root.appendChild(h("p", { class: "checked", text: "Checked: " + d.checked })); }
    if (d.other && d.other.length) {
      root.appendChild(h("details", { class: "fold" },
        h("summary", { text: "Other detector findings (" + d.other.length + ")" }), findingList(d.other, true)));
    }
  }

  function copyBox(text, label) {
    var code = h("code", { text: text });
    var btn = h("button", { type: "button", class: "btn", text: "Copy" });
    btn.addEventListener("click", function () {
      function selectIt() {
        var r = document.createRange(); r.selectNodeContents(code);
        var s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
        btn.textContent = "Selected, now copy";
      }
      btn.textContent = "Copying";
      try {
        navigator.clipboard.writeText(text).then(function () { btn.textContent = "Copied"; }, selectIt);
      } catch (e) { selectIt(); }
      setTimeout(function () { btn.textContent = "Copy"; }, 2500);
    });
    return h("div", { class: "codebox" },
      h("div", { class: "codebar" }, h("span", { text: label || "Change, ready to paste" }), btn),
      h("div", { class: "scroll" }, h("pre", null, code)));
  }
  function renderWeek() {
    var root = document.getElementById("week-root");
    if (!D.weeklies.length) { return; }
    root.textContent = "";
    var w = D.weeklies[weekIdx];
    var bar = h("div", { class: "daybar" }, h("h2", { class: "view-title", text: w.label }));
    if (D.weeklies.length > 1) {
      var sel = h("select", { id: "week-select", class: "btn", "aria-label": "Choose a week" });
      D.weeklies.forEach(function (x, i) {
        var o = h("option", { value: String(i), text: x.label });
        if (i === weekIdx) { o.selected = true; }
        sel.appendChild(o);
      });
      sel.addEventListener("change", function () { weekIdx = parseInt(sel.value, 10); renderWeek(); refocus("week-select"); });
      bar.appendChild(sel);
    }
    root.appendChild(bar);
    var st = stats(w.counts);
    if (st) { root.appendChild(st); }
    if (w.followthrough && w.followthrough.length) {
      root.appendChild(h("h3", { class: "sub", text: "Follow-through" }));
      var ft = h("ul", { class: "ft panel" });
      w.followthrough.forEach(function (f) {
        var good = f.status === "better" || f.status === "not seen again";
        var bad = f.status === "worse" || f.status === "recurred";
        ft.appendChild(h("li", null,
          h("span", { class: "ft-head" },
            f.status ? h("span", { class: "pill" + (good ? " better" : bad ? " worse" : ""), text: f.status }) : null,
            f.applied ? h("span", { class: "ft-applied", text: "applied on " + f.applied }) : null),
          rich(h("span", { class: "ft-text" }), f.text)));
      });
      root.appendChild(ft);
    }
    if (w.numbers) { root.appendChild(rich(h("p", { class: "lede" }), w.numbers)); }
    if (w.cost && w.cost.rows && w.cost.rows.length) {
      root.appendChild(h("h3", { class: "sub", text: "Cost by project" }));
      var ct = h("table"), chead = h("tr");
      w.cost.head.forEach(function (c, i) { chead.appendChild(h("th", { class: i ? "n" : null, text: c })); });
      ct.appendChild(h("thead", null, chead));
      var cb = h("tbody");
      w.cost.rows.forEach(function (r) {
        var tr = h("tr");
        r.forEach(function (c, i) { tr.appendChild(h("td", { class: i ? "n" : null, text: c })); });
        cb.appendChild(tr);
      });
      ct.appendChild(cb);
      root.appendChild(h("div", { class: "panel-t scroll" }, ct));
      if (w.cost.note) { root.appendChild(h("p", { class: "muted", text: w.cost.note })); }
    }
    if (w.compare_lines && w.compare_lines.length) {
      root.appendChild(h("h3", { class: "sub", text: "Before and after" }));
      var cl = h("ul", { class: "plain" });
      w.compare_lines.forEach(function (x) { cl.appendChild(rich(h("li"), x)); });
      root.appendChild(cl);
    }
    if (!w.items.length && !w.proposals.length) {
      root.appendChild(emptyBox(w.note || "A clean week.", "No costly patterns and no proposed changes in this review."));
    }
    if (w.items.length) {
      root.appendChild(h("h3", { class: "sub", text: "What cost the most" }));
      root.appendChild(itemList(w, w.items, true));
    }
    if (w.checked) { root.appendChild(h("p", { class: "checked", text: "Checked: " + w.checked })); }
    if (w.unused && (w.unused.lines.length || w.unused.total)) {
      root.appendChild(h("h3", { class: "sub", text: "Set up but unused" }));
      if (w.unused.lines.length) {
        var ul = h("ul", { class: "plain" });
        w.unused.lines.forEach(function (x) { ul.appendChild(rich(h("li"), x)); });
        root.appendChild(ul);
      }
      if (w.unused.total) { root.appendChild(rich(h("p", { class: "muted" }), w.unused.total)); }
    }
    if (w.proposals.length) {
      root.appendChild(h("h3", { class: "sub", text: "Proposed changes, not applied" }));
      var grid = h("div", { class: "props" });
      w.proposals.forEach(function (p) {
        var art = h("article", { class: "prop" },
          h("div", { class: "prop-head" }, h("span", { class: "pid", text: p.id }), h("h4", { text: p.name || "Proposal" })));
        if (p.why) { art.appendChild(field("what", "Why", p.why)); }
        var ch = h("div", { class: "field" }, h("div", { class: "k" }, "Change"));
        if (p.note) { ch.appendChild(rich(h("div", { class: "v" }), p.note)); }
        if (p.change) { ch.appendChild(copyBox(p.change)); }
        art.appendChild(ch);
        if (p.where) { art.appendChild(field("where", "Where", p.where)); }
        grid.appendChild(art);
      });
      root.appendChild(grid);
    }
  }

  function usd(x) {
    if (x == null) { return "no price"; }
    if (x < 0) { x = 0; }
    if (Math.round(x * 100) / 100 >= 10) { return "$" + Math.round(x).toLocaleString("en-US"); }
    return "$" + x.toFixed(2);
  }
  /* A table whose numeric columns sort on click. cols: {k, label, num, fmt, opt, bar}. */
  function sortable(id, cols, rows, sortKey) {
    var state = { k: sortKey, dir: -1 };
    var wrap = h("div", { class: "panel-t scroll" });
    function draw() {
      wrap.textContent = "";
      var max = 0;
      rows.forEach(function (r) { if (typeof r.usd === "number" && r.usd > max) { max = r.usd; } });
      var sorted = rows.slice().sort(function (a, b) {
        var x = a[state.k], y = b[state.k];
        if (x == null) { x = typeof y === "string" ? "" : -1; }
        if (y == null) { y = typeof x === "string" ? "" : -1; }
        if (typeof x === "string") { return state.dir * String(x).localeCompare(String(y)); }
        return state.dir * (x - y);
      });
      var tr = h("tr");
      cols.forEach(function (c) {
        if (c.bar) { tr.appendChild(h("th", { class: "barcell", "aria-hidden": "true" })); return; }
        var cls = (c.num ? "n" : "") + (c.opt ? " opt" : "");
        var th = h("th", { class: cls || null, scope: "col",
          "aria-sort": state.k === c.k ? (state.dir > 0 ? "ascending" : "descending") : null });
        var b = h("button", { type: "button", class: "sort", id: id + "-sort-" + c.k, text: c.label });
        b.addEventListener("click", function () {
          if (state.k === c.k) { state.dir = -state.dir; } else { state.k = c.k; state.dir = c.num ? -1 : 1; }
          draw(); refocus(id + "-sort-" + c.k);
        });
        th.appendChild(b);
        tr.appendChild(th);
      });
      var table = h("table", null, h("thead", null, tr)), body = h("tbody");
      sorted.forEach(function (r) {
        var row = h("tr");
        cols.forEach(function (c) {
          if (c.bar) {
            var w = max > 0 && r.usd ? Math.max(1, Math.round(100 * r.usd / max)) : 0;
            row.appendChild(h("td", { class: "barcell", "aria-hidden": "true" },
              w ? h("span", { class: "share", style: "width:" + w + "%" }) : null));
            return;
          }
          var cls = (c.num ? "n" : "") + (c.opt ? " opt" : "");
          var cell = h("td", { class: cls || null });
          var v = c.fmt ? c.fmt(r[c.k], r) : r[c.k];
          if (v && v.nodeType) { cell.appendChild(v); } else { cell.textContent = v == null ? "" : String(v); }
          row.appendChild(cell);
        });
        body.appendChild(row);
      });
      table.appendChild(body);
      wrap.appendChild(table);
    }
    draw();
    return wrap;
  }
  function costBlock(title, table, note) {
    return h("div", null, h("h3", { class: "sub", text: title }), table, note ? h("p", { class: "muted", text: note }) : null);
  }
  function renderCost() {
    var root = document.getElementById("cost-root"), C = D.cost;
    if (!C) { return; }
    root.textContent = "";
    var p = C.periods[costIdx], t = p.t;
    var chips = h("div", { class: "chips", role: "group", "aria-label": "Period" }, h("span", { class: "lab", text: "Period" }));
    C.periods.forEach(function (x, i) {
      chips.appendChild(button("cost-" + x.key, x.label, function () { costIdx = i; renderCost(); },
        { pressed: i === costIdx, label: "Show " + x.label + ", " + x.title }));
    });
    root.appendChild(chips);
    var st = h("div", { class: "stats" });
    [[usd(t.usd), "API-equivalent, not a bill"], [tok(t.tokens), "tokens"], [tok(t.out), "output tokens"],
     [String(t.days), t.days === 1 ? "day with data" : "days with data"]].forEach(function (x) {
      st.appendChild(h("span", null, h("b", { text: x[0] }), x[1]));
    });
    root.appendChild(h("h3", { class: "sub", text: p.label + ": " + p.title }));
    root.appendChild(st);
    if (t.unpriced_tokens) {
      root.appendChild(h("p", { class: "muted", text: tok(t.unpriced_tokens) + " tokens are unpriced: their model is not in the price table, so they are counted but not costed." }));
    }
    if (t.usd > 0) {
      var a = Math.max(1, Math.round(100 * t.main_usd / t.usd));
      root.appendChild(h("div", null,
        h("div", { class: "split", role: "img", "aria-label": "Main sessions " + usd(t.main_usd) + ", subagents " + usd(t.sub_usd) },
          h("span", { style: "width:" + a + "%" }), h("span", { class: "s2", style: "width:" + (100 - a) + "%" })),
        h("div", { class: "split-key" },
          h("span", null, h("i"), "Main sessions ", h("b", { text: usd(t.main_usd) })),
          h("span", null, h("i", { class: "s2" }), "Subagents ", h("b", { text: usd(t.sub_usd) })))));
    }
    root.appendChild(costBlock("By project", sortable("cp", [
      { k: "name", label: "Project", fmt: function (v, r) {
          var s = h("span", null, v);
          if (r.scheduled) { s.appendChild(document.createTextNode(" ")); s.appendChild(h("span", { class: "pill", text: r.scheduled === r.sessions ? "scheduled" : r.scheduled + " scheduled" })); }
          return s; } },
      { k: "sessions", label: "Sessions", num: true, opt: true },
      { k: "tokens", label: "Tokens", num: true, fmt: tok },
      { k: "main_usd", label: "Main", num: true, opt: true, fmt: usd },
      { k: "sub_usd", label: "Subagents", num: true, opt: true, fmt: usd },
      { k: "usd", label: "Cost", num: true, fmt: usd },
      { bar: true }], t.by_project, "usd"),
      t.days > 1 ? "A session that runs over two days counts once on each day." : null));
    var grid = h("div", { class: "cost-grid" });
    grid.appendChild(costBlock("By model", sortable("cm", [
      { k: "name", label: "Model" }, { k: "tokens", label: "Tokens", num: true, fmt: tok },
      { k: "usd", label: "Cost", num: true, fmt: function (v, r) { return r.priced ? usd(v) : "unpriced"; } },
      { bar: true }], t.by_model, "usd")));
    grid.appendChild(costBlock("By token type", sortable("ct", [
      { k: "type", label: "Type" }, { k: "tokens", label: "Tokens", num: true, fmt: tok },
      { k: "usd", label: "Cost", num: true, fmt: usd }, { bar: true }], t.by_type, "usd")));
    root.appendChild(grid);
    if (p.key === "day" && C.sessions && C.sessions.length) {
      root.appendChild(costBlock("By session, top " + C.sessions.length, sortable("cs", [
        { k: "id", label: "Session", fmt: function (v) {
            var link = h("a", { class: "sid", href: "#today", text: v, title: "Show this day's recap" });
            link.addEventListener("click", function () { D.dailies.forEach(function (x, i) { if (x.date === C.day) { dayIdx = i; } }); renderToday(); });
            return link; } },
        { k: "project", label: "Project", fmt: function (v, r) {
            var s = h("span", null, v);
            if (r.kind === "scheduled") { s.appendChild(document.createTextNode(" ")); s.appendChild(h("span", { class: "pill", text: "scheduled" })); }
            return s; } },
        { k: "tokens", label: "Tokens", num: true, opt: true, fmt: function (v) { return v == null ? "" : tok(v); } },
        { k: "sub_usd", label: "Subagents", num: true, opt: true, fmt: function (v) { return v == null ? "" : usd(v); } },
        { k: "usd", label: "Cost", num: true, fmt: usd }, { bar: true }], C.sessions, "usd"),
        "The session id opens that day's recap."));
    }
    root.appendChild(h("p", { class: "muted", text: "List prices as of " + C.pricesDate + ". All time means since " + C.since + ", the first day on record." }));
    if (C.oneAccount) root.appendChild(h("p", { class: "muted", text: "Showing one account only." }));
  }

  /* A graph's readout follows the pointer; a line graph also takes focus and the arrow keys. */
  function bindReadout(fig) {
    var out = fig.querySelector(".readout"), svg = fig.querySelector("svg[tabindex]");
    var gs = svg ? svg.querySelectorAll("g.b") : [], at = -1;
    if (!out) { return; }
    function mark(i) {
      if (at >= 0 && gs[at]) { gs[at].classList.remove("on"); }
      at = i;
      if (i >= 0 && gs[i]) { gs[i].classList.add("on"); out.textContent = gs[i].getAttribute("data-r"); }
      else { out.textContent = out.getAttribute("data-default"); }
    }
    function over(ev) {
      var g = ev.target.closest ? ev.target.closest("g.b") : null;
      if (!g) { return; }
      if (svg) { mark(Array.prototype.indexOf.call(gs, g)); } else { out.textContent = g.getAttribute("data-r"); }
    }
    fig.addEventListener("pointermove", over);
    fig.addEventListener("pointerdown", over);
    fig.addEventListener("pointerleave", function () {
      if (svg) { mark(-1); } else { out.textContent = out.getAttribute("data-default"); }
    });
    if (svg && gs.length) {
      svg.addEventListener("focus", function () { if (at < 0) { mark(gs.length - 1); } });
      svg.addEventListener("blur", function () { mark(-1); });
      svg.addEventListener("keydown", function (ev) {
        var step = ev.key === "ArrowLeft" ? -1 : ev.key === "ArrowRight" ? 1 : 0;
        if (!step) { return; }
        ev.preventDefault();
        mark(Math.max(0, Math.min(gs.length - 1, (at < 0 ? gs.length - 1 : at) + step)));
      });
    }
  }

  /* Overview: the live feed, one period at a time. Times are printed as the feed wrote them. */
  var MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  /* All time has no period before it, so it has no entry here and no change line is drawn. */
  var BEFORE = { today: "yesterday", week: "the 7 days before", month: "the 30 days before" };
  var TIPS = {
    pace: "Pace is the share of the limit used minus the share of its window that has passed.",
    ft: "First-try edits is the share of turns that edited files and needed no retry or correction afterwards.",
    ch: "Cache hit is the share of input tokens read from the prompt cache instead of being sent again at full price.",
    co: "Corrections counts your prompts that open by correcting the answer before.",
    share: "Share is this row's part of the cost of the period.",
    cost: "Cost is API-equivalent at list prices, not a bill.",
    turns: "Turns counts your prompts answered in this kind of work.",
    first: "First try is the share of this activity's editing turns that needed no retry.",
    sessions: "Sessions counts the sessions that ran in this project.",
    mcp: "MCP stands for Model Context Protocol, the way Claude Code connects to outside tools and services.",
    calls: "Calls counts every time a tool of this server was used.",
    tools: "Tools counts the different tools of this server that were used.",
    errors: "Errors counts the calls to this server that failed."
  };
  function isNum(x) { return typeof x === "number" && isFinite(x); }
  function isoParts(s) {
    var m = /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)/.exec(s || "");
    return m ? { y: +m[1], mo: +m[2], d: +m[3], hm: m[4] + ":" + m[5] } : null;
  }
  function monDay(s) { var p = isoParts(s); return p ? MON[Math.max(1, Math.min(12, p.mo)) - 1] + " " + p.d : ""; }
  function clock(s) { var p = isoParts(s); return p ? p.hm : ""; }
  function asOf(s) { return isoParts(s) ? "As of " + monDay(s) + ", " + clock(s) : null; }
  function span(ms) {
    var m = Math.max(0, Math.round(ms / 60000));
    if (m >= 1440) { return Math.floor(m / 1440) + "d " + Math.floor((m % 1440) / 60) + "h"; }
    if (m >= 60) { return Math.floor(m / 60) + "h " + (m % 60) + "m"; }
    return m + "m";
  }
  function about(ms) {
    var m = Math.max(1, Math.round(ms / 60000));
    return m >= 2880 ? Math.round(m / 1440) + "d" : m >= 60 ? Math.round(m / 60) + "h" : m + "m";
  }
  function money(x) { return "$" + Math.max(0, x).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function whole(n) { return Math.round(n).toLocaleString("en-US"); }
  function pct(r) { return Math.round(r * 100) + "%"; }
  function points(r) { return isNum(r) ? Math.round(r * 100) : null; }
  function graft(id) { var t = document.getElementById(id); return t && t.content ? t.content.cloneNode(true) : null; }
  function clockSec(s) { var m = /T(\d\d:\d\d:\d\d)/.exec(s || ""); return m ? m[1] : clock(s); }
  /* Beside the name: a green dot and "Live" while the numbers are under 15 minutes old. */
  function liveLine() {
    var el = document.getElementById("ov-conn"), at = Date.parse((LIVE && LIVE.updated) || "");
    if (!el || !liveOk || isNaN(at)) { return; }
    var age = Date.now() - at, fresh = age <= 15 * 60000;
    el.className = "conn on" + (fresh ? "" : " stale");
    el.lastChild.textContent = fresh ? "Live \u00b7 updated " + clockSec(LIVE.updated)
      : "Not live \u00b7 updated " + monDay(LIVE.updated) + " at " + clock(LIVE.updated);
  }
  function ovCard(cls, title, hint, tip) {
    var name = h("h3", { class: "sub", text: title, title: tip });
    if (hint) { name.appendChild(h("span", { class: "ov-hint", text: hint })); }
    return h("section", { class: "ov-card " + cls }, h("div", { class: "ov-head" }, name));
  }
  /* How a figure moved against the period before: the words that say so (lead, coloured)
     and the rest of the line. up: true when more is better. */
  function change(now, prev, up, unit) {
    if (!isNum(now) || !isNum(prev) || !BEFORE[period]) { return null; }
    var d = Math.round(now - prev);
    if (!d) { return { cls: "is-flat", lead: "", rest: "same as " + BEFORE[period] }; }
    return { cls: (d > 0) === up ? "is-good" : "is-warn",
      lead: (d > 0 ? "Up " : "Down ") + Math.abs(d) + (unit || ""), rest: "on " + BEFORE[period] };
  }
  function sLine(text) { return text ? h("div", { class: "s", text: text }) : null; }
  function chgLine(c, plain) {
    if (!c) { return null; }
    return h("div", { class: "s" }, c.lead ? h("b", { class: plain ? null : c.cls, text: c.lead }) : null, (c.lead ? " " : "") + c.rest);
  }
  /* All time: these figures only reach back as far as the transcripts still on this Mac. */
  function detailNote(p) { return p.detail_from ? h("div", { class: "s ov-detail", text: "Detail from " + p.detail_from }) : null; }
  function withNote(card, p) {
    if (p.detail_from) { card.appendChild(h("div", { class: "ov-note ov-detail", text: "Detail from " + p.detail_from })); }
    return card;
  }
  /* One tile: label, figure (or a few words where there is no figure), lines of detail. */
  function tile(cls, k, v, tip, subs) {
    var t = h("section", { class: "ov-card ov-tile" + (cls ? " " + cls : "") },
      h("div", { class: "k", text: k, title: tip }),
      h("div", { class: "v" + (/\d/.test(v) ? "" : " words"), text: v }));
    (subs || []).forEach(function (s) { if (s) { t.appendChild(s); } });
    return t;
  }
  function spendName(p) {
    var l = p.label || "";
    return period === "today" ? "Spend today" : "Spend, " + l.charAt(0).toLowerCase() + l.slice(1);
  }
  function peakWords(p) {
    var k = p.peak;
    if (!k || !isNum(k.tokens) || !(k.tokens > 0) || !isoParts(k.t)) { return null; }
    return "peak " + tok(k.tokens) + (p.series_unit === "hour" ? " at " + clock(k.t)
      : p.series_unit === "week" ? " in the week of " + monDay(k.t) : " on " + monDay(k.t));
  }
  /* Limits used: one tile per limit, with its pace and when it resets. */
  function ovLimitTiles() {
    var L = LIVE.limits || {}, rows = L.rows || [], now = Date.now(), seen = asOf(L.as_of || LIVE.updated);
    return rows.map(function (r) {
      var at = Date.parse(r.resets_at || ""), over = at <= now, subs = [];   /* reset since: the pace no longer holds */
      if (isNum(r.pace_pct) && !over) {
        var pc = Math.round(r.pace_pct), out = Date.parse(r.runs_out_at || ""), text, cls;
        if (pc < 0) { text = -pc + "% in reserve"; cls = "is-good"; }
        else if (!pc) { text = "On pace"; cls = "is-flat"; }
        else {
          text = pc + "% over pace" + (!isNaN(out) && out > now ? ", runs out in about " + about(out - now) : "");
          cls = r.level === "crit" ? "is-crit" : "is-warn";
        }
        subs.push(h("div", { class: "s" }, h("b", { class: cls, text: text, title: TIPS.pace })));
      }
      if (!isNaN(at)) {
        var when = monDay(r.resets_at) + " at " + clock(r.resets_at);
        subs.push(sLine(at <= now ? "Reset " + when : at - now < 864e5 ? "Resets in " + span(at - now) : "Resets " + when));
      }
      return tile("ov-lim" + (r.level === "crit" ? " critical" : r.level === "warn" ? " warning" : ""),
        "Limit used, " + r.label + (r.level === "crit" ? ", near limit" : ""), Math.round(r.used_pct) + "%", seen, subs);
    });
  }
  /* The limit tiles go in the tiles' grid on a row of their own; the grid is told how many
     there are, so they span whole columns and fill that row (two limits: three columns each). */
  function limitRow(box) {
    var lims = ovLimitTiles();
    lims.forEach(function (t) { box.appendChild(t); });
    if (lims.length) { box.className += " lims-" + Math.min(lims.length, 4); }
  }
  function ovTiles(p) {
    var box = h("div", { class: "ov-tiles" }), cost = null;
    var ft = p.first_try || {}, ch = p.cache_hit || {}, co = p.corrections || {};
    if (isNum(p.cost_usd) && isNum(p.prev_cost_usd) && p.prev_cost_usd > 0) {
      cost = change(100 * p.cost_usd / p.prev_cost_usd, 100, false, "%");
    }
    /* a day still running is not judged against a whole day, and more spend is not a fault: no colour then */
    box.appendChild(tile("ov-cost", spendName(p), isNum(p.cost_usd) ? money(p.cost_usd) : "No data", TIPS.cost,
      [sLine("API-equivalent, not a bill"), chgLine(cost, period === "today" || (cost && cost.cls === "is-warn"))]));
    box.appendChild(tile("ov-calls", "Calls", isNum(p.calls) ? whole(p.calls) : "No data", null,
      [isNum(p.sessions) ? sLine("in " + whole(p.sessions) + (p.sessions === 1 ? " session" : " sessions")) : null, detailNote(p)]));
    box.appendChild(tile("ov-tokens", "Tokens", isNum(p.tokens) ? tok(p.tokens) : "No data", null, [sLine(peakWords(p))]));
    [["ft", "First-try edits", isNum(ft.rate) ? pct(ft.rate) : "No edits", points(ft.rate), points(ft.prev_rate), true],
     ["ch", "Cache hit", isNum(ch.rate) ? pct(ch.rate) : "No data", points(ch.rate), points(ch.prev_rate), true],
     ["co", "Corrections", isNum(co.count) ? whole(co.count) : "No data", co.count, co.prev_count, false]].forEach(function (m) {
      var chg = change(m[3], m[4], m[5]), spark = graft("ov-s-" + period + "-" + m[0]);
      box.appendChild(tile("ov-how" + (chg && chg.cls === "is-warn" ? " warning" : ""), m[1], m[2], TIPS[m[0]],
        [chgLine(chg), detailNote(p), spark ? h("div", { class: "ov-spark " + (chg ? chg.cls : "") }, spark) : null]));
    });
    limitRow(box);
    return box;
  }
  function dayBefore(s) {
    var u = isoParts(LIVE.updated), i;
    if (!u) { return null; }
    var d = new Date(u.y, u.mo - 1, u.d - 1);
    var key = d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2);
    for (i = 0; i < s.length; i++) { if (s[i].t.slice(0, 10) === key) { return s[i].usd; } }
    return null;
  }
  function ovGraph(p) {
    var hour = p.series_unit === "hour", week = p.series_unit === "week", s = p.series || [], sum = 0, peak = -1, said = [];
    function name(r) { return hour ? clock(r.t) : monDay(r.t); }
    s.forEach(function (r, i) { sum += r.usd; if (r.usd > 0 && (peak < 0 || r.usd > s[peak].usd)) { peak = i; } });
    if (s.length) { said.push("average " + money(sum / s.length)); }
    if (peak >= 0) { said.push("peak " + money(s[peak].usd) + (hour ? " at " : week ? " in the week of " : " on ") + name(s[peak])); }
    var y = hour ? p.prev_cost_usd : week ? null : dayBefore(s);
    if (isNum(y)) { said.push("yesterday " + money(y)); }
    var last = s.length ? (week ? "Week of " : "") + name(s[s.length - 1]) + ": " + money(s[s.length - 1].usd) : "";
    var plot = graft("ov-g-" + period);
    var legend = h("div", { class: "ov-legend", "aria-hidden": "true" }, h("span", null, h("i"), "Spend"));
    if (plot && plot.querySelector(".lg-avg")) { legend.appendChild(h("span", null, h("i", { class: "avg" }), "7-day average")); }
    var title = h("h3", { class: "sub", text: hour ? "Spend by hour" : week ? "Spend by week" : "Spend by day" });
    if (said.length) { title.appendChild(h("span", { class: "ov-hint", text: said.join(", ") })); }
    var fig = h("figure", { class: "ov-card ov-graph" },
      h("figcaption", { class: "ov-head" },
        h("div", { class: "ov-gt" }, title,
          h("span", { class: "readout", "data-default": last, "aria-live": "polite", text: last })),
        legend),
      plot || h("p", { class: "muted", text: "No spend recorded in this period." }));
    bindReadout(fig);
    return fig;
  }
  /* Running now, with the coach's tips under it in the same card. */
  function ovRunTips() {
    var runs = LIVE.running || [], upd = Date.parse(LIVE.updated || ""), tips = LIVE.tips || [];
    var c = ovCard("ov-running", "Running now", runs.length ? plural(runs.length, "session") : null);
    if (!runs.length) { c.appendChild(h("p", { class: "muted", text: "Nothing is running." })); }
    runs.forEach(function (r) {
      var ran = upd - Date.parse(r.started || ""), sub = [], left = [];
      if (r.model) { sub.push(r.model); }
      if (ran >= 0) { sub.push((sub.length ? "running " : "Running ") + span(ran)); }
      var lvl = r.level === "crit" || r.level === "warn" ? r.level : "";
      if (isNum(r.context_left_tokens)) { left.push(tok(r.context_left_tokens) + " left"); }
      if (lvl) { left.push(lvl === "crit" ? "context nearly full" : "context filling up"); }
      c.appendChild(h("div", { class: "ov-run" + (lvl ? " run-" + lvl : "") },
        h("div", { class: "who" }, h("div", { class: "t", text: r.project + (r.branch ? ", " + r.branch : "") }),
          sub.length ? h("div", { class: "m", text: sub.join(", ") }) : null),
        isNum(r.context_pct) || left.length ? h("div", { class: "ctx" },
          isNum(r.context_pct) ? h("div", { class: "p", text: Math.round(r.context_pct) + "% context" }) : null,
          left.length ? h("div", { class: "m", text: left.join(", ") }) : null) : null));
    });
    c.appendChild(h("h3", { class: "sub ov-tips-h", text: "Coach tips" }));
    if (!tips.length) { c.appendChild(h("p", { class: "muted", text: "No tips right now." })); }
    tips.forEach(function (t) {
      c.appendChild(h("div", { class: "ov-tip" }, h("div", { class: "t", text: t.title }),
        t.advice ? h("div", { class: "a", text: t.advice }) : null));
    });
    return c;
  }
  /* A table whose first figure sits over a thin heat bar. first: the name column's heading;
     heads: [label, tooltip]; cells(row): [text, class]. */
  function ovTable(cls, title, tip, first, heads, rows, cells, none) {
    var c = ovCard(cls, title, null, tip);
    if (!rows || !rows.length) { c.appendChild(h("p", { class: "muted", text: none })); return c; }
    var head = h("tr", { role: "row" }, h("th", { scope: "col", role: "columnheader", text: first }));
    var body = h("tbody", { role: "rowgroup" });
    heads.forEach(function (x, i) {
      head.appendChild(h("th", { scope: "col", role: "columnheader", class: i ? null : "main", text: x[0], title: x[1] }));
    });
    rows.forEach(function (r) {
      var tr = h("tr", { role: "row" }, h("th", { scope: "row", role: "rowheader", text: r.name }));
      cells(r).forEach(function (x, i) {
        var td = h("td", { role: "cell", class: x[1] + (i ? "" : " barcol") }, h("div", { text: x[0] }));
        if (!i) { td.appendChild(h("div", { class: "hb", "aria-hidden": "true" }, r.heat ? h("i", { style: "width:" + r.heat + "%" }) : null)); }
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
    c.appendChild(h("table", { class: "ov-t", role: "table" }, h("thead", { role: "rowgroup" }, head), body));
    return c;
  }
  function num(v, fmt) { return isNum(v) ? fmt(v) : ""; }
  function ovTables(p) {
    return h("div", { class: "ov-row3" },
      withNote(ovTable("ov-acts", "By activity", null, "Activity",
        [["Cost", TIPS.cost], ["Turns", TIPS.turns], ["First try", TIPS.first], ["Share", TIPS.share]], p.activities,
        function (r) {
          return [[num(r.usd, money), "main"], [num(r.turns, whole), "mid"],
            [isNum(r.first_try) ? pct(r.first_try) : "No edits", isNum(r.first_try) ? "mid" : "dim"], [num(r.share, pct), "dim"]];
        }, "No activity in this period"), p),
      h("div", { class: "ov-col" },
        ovTable("ov-projects", "Projects", null, "Project",
          [["Cost", TIPS.cost], ["Sessions", TIPS.sessions], ["Share", TIPS.share]], p.projects,
          function (r) { return [[num(r.usd, money), "main"], [num(r.sessions, whole), "mid"], [num(r.share, pct), "dim"]]; },
          "No projects in this period"),
        withNote(ovTable("ov-mcp", "MCP servers", TIPS.mcp, "Server",
          [["Calls", TIPS.calls], ["Tools", TIPS.tools], ["Errors", TIPS.errors]], p.mcp_servers,
          function (r) {
            return [[num(r.calls, whole), "main"], [num(r.tools, whole), "mid"], [num(r.errors, whole), r.errors ? "is-warn" : "dim"]];
          }, "No MCP calls in this period"), p)));
  }
  function renderOverview() {
    var root = document.getElementById("overview-root");
    if (!LIVE) { return; }
    root.textContent = "";
    if (!liveOk) {
      root.appendChild(emptyBox("Update the coach", "These numbers were written by a newer coach than the one that built this page."));
      return;
    }
    var p = (LIVE.periods || {})[period], row = h("div", { class: "ov-row2" });
    root.className = "ov";
    if (p) { root.appendChild(ovTiles(p)); }
    else {
      /* no figures for this period: the limits still stand, and so does what is running */
      var lims = h("div", { class: "ov-tiles" });
      limitRow(lims);
      if (lims.childNodes.length) { root.appendChild(lims); }
      var none = emptyBox("No data for this period.", "Pick another period.");
      none.className += " ov-nodata";
      root.appendChild(none);
    }
    if (p) { row.appendChild(ovGraph(p)); }
    row.appendChild(ovRunTips());
    root.appendChild(row);
    if (p) { root.appendChild(ovTables(p)); }
  }
  function setPeriod(p, keep) {
    if (PERIODS.indexOf(p) < 0) { return; }
    period = p;
    PERIODS.forEach(function (n) {
      document.getElementById("ov-p-" + n).setAttribute("aria-pressed", n === p ? "true" : "false");
    });
    try { renderOverview(); } catch (err) {
      /* a feed the page cannot draw never takes the other views down with it */
      var root = document.getElementById("overview-root");
      root.className = "";
      root.textContent = "";
      root.appendChild(emptyBox("Update the coach", "These numbers could not be drawn by this page."));
    }
    try { localStorage.setItem("coach.period", p); } catch (e) { /* storage is optional */ }
    if (keep) {
      try { history.replaceState(null, "", "#overview/" + p); } catch (e2) { /* the address is optional */ }
    }
  }
  PERIODS.forEach(function (n) {
    document.getElementById("ov-p-" + n).addEventListener("click", function () { setPeriod(n, true); });
  });

  function stored(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }
  function show(v) {
    VIEWS.forEach(function (n) {
      document.getElementById("view-" + n).hidden = n !== v;
      var t = document.getElementById("tab-" + n);
      if (n === v) { t.setAttribute("aria-current", "page"); } else { t.removeAttribute("aria-current"); }
    });
    document.documentElement.classList.toggle("ov-live", v === "overview" && liveOk);
    try { localStorage.setItem("coach.view", v); } catch (e) { /* storage is optional */ }
  }
  /* The address wins (#cost, #overview/week, #overview/all), then the view and period last open. With
     nothing to go on the page opens on the Overview, or on Today when there is no live feed. */
  function route() {
    var hash = (location.hash || "").slice(1).split("/"), v = hash[0];
    if (VIEWS.indexOf(v) < 0) {
      v = stored("coach.view");
      if (v === "overview" && !liveOk) { v = null; }
    }
    if (VIEWS.indexOf(v) < 0) { v = liveOk ? "overview" : "today"; }
    var p = hash[0] === "overview" && PERIODS.indexOf(hash[1]) >= 0 ? hash[1] : stored("coach.period");
    setPeriod(PERIODS.indexOf(p) < 0 ? period : p);
    show(v);
  }
  VIEWS.forEach(function (n) {
    document.getElementById("tab-" + n).addEventListener("click", function () { show(n); });
  });

  Array.prototype.forEach.call(document.querySelectorAll("figure.chart"), bindReadout);

  renderToday();
  renderWeek();
  renderCost();
  liveLine();
  /* a page left open stops saying "Live" once its numbers are over 15 minutes old */
  if (typeof setInterval === "function") { setInterval(liveLine, 30000); }
  window.addEventListener("hashchange", route);
  route();
})();
</script>
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the Agentic Coach page.")
    ap.add_argument("--reports-dir", default=C.reports_dir(),
                    help="reports root (default: the coach reports folder)")
    ap.add_argument("--out", default=C.site_file(),
                    help="page file to write (default: the coach site folder)")
    ap.add_argument("--projects-dir", help="override ~/.claude/projects (for tests)")
    ap.add_argument("--today", help="YYYY-MM-DD, the day the build runs (default: today)")
    ap.add_argument("--no-trends", action="store_true",
                    help="do not scan transcripts, draw trends from the cache only")
    ap.add_argument("--ledger", help="ledger file to read (default: the one in the coach folder)")
    ap.add_argument("--card", action="store_true",
                    help="also print one JSON line with the latest daily's top items")
    args = ap.parse_args(argv)
    if C.load_config().get("damaged"):
        # never rebuild (or let a recap publish) a page when the rules for what
        # to leave out cannot be read; the page already on disk is left as it is
        sys.stderr.write("coach: %s\n" % C.config_problem())
        return 2
    page, card_data = build_page(args.reports_dir, args.projects_dir, args.today, not args.no_trends,
                                 args.ledger)
    out_dir = os.path.dirname(os.path.abspath(args.out))
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(page)
    sys.stderr.write("wrote %s: %d bytes\n" % (args.out, len(page.encode("utf-8"))))
    if args.card:
        sys.stdout.write(json.dumps(card_data, separators=(",", ":"), ensure_ascii=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
