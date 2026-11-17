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
CONTEXT_LINE = 300000
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


def chart_card(key, days, data, threshold=None):
    _k, title, kind, fn = SERIES_BY_KEY[key]
    values = [fn(data[d]) if d in data else None for d in days]
    latest = "no data"
    for d, v in zip(reversed(days), reversed(values)):
        if v is not None:
            latest = "%s: %s" % (short_date(d), fmt_num(v, kind))
            break
    return ('<figure class="chart"><figcaption><span class="ch-title">%s</span>'
            '<span class="readout" data-default="%s">%s</span></figcaption>%s</figure>'
            % (esc(title), esc(latest), esc(latest), chart_svg(days, values, kind, title, threshold)))


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
           'are left out, so these are your own habits. The highlighted bar is the latest day with data.</p>'
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
    data = {"dailies": dailies, "weeklies": weeklies,
            "contextLine": CONTEXT_LINE, "cost": cost}
    values = {"NEWEST": esc(newest), "ONE": one, "TRENDS": render_trends(trend, today),
              "BASELINE": render_baseline(summary, details), "DATA": json_for_script(data),
              "COST": cost_html, "PROGRESS": progress, "SETUP": setup, "EXCERPTS": excerpts}
    page = re.sub(r"\{\{(NEWEST|ONE|TRENDS|BASELINE|DATA|COST|PROGRESS|SETUP|EXCERPTS)\}\}",
                  lambda m: values[m.group(1)], PAGE)
    return clean(page), card(dailies)


PAGE = r"""<title>Agentic Coach</title>
<meta name="claude:viewport-fit-sides" content="cover">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
/* Layout: one left-aligned column, sticky name and tabs, summary rows first, evidence folded under each row. */
:root {
  --bg: #f2f5f7; --surface: #ffffff; --sunken: #e8edf1;
  --ink: #17212b; --ink-2: #485664; --ink-3: #64717e; --line: #d3dbe2;
  --accent: #17698a; --accent-ink: #ffffff; --accent-soft: #dcebf2;
  --good: #1f7045; --good-soft: #dbefe3;
  --warn: #8a5600; --warn-soft: #f7e9cf;
  --crit: #ae2f27; --crit-soft: #f8dfdc;
  --bar: #7fb0c5; --bar-now: #17698a;
  --sans: "IBM Plex Sans", "Segoe UI", system-ui, -apple-system, "Helvetica Neue", Arial, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  --r: 6px;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #11161b; --surface: #19212a; --sunken: #0c1115;
    --ink: #e3e9ee; --ink-2: #b0bbc5; --ink-3: #8a97a4; --line: #2c3843;
    --accent: #6ebbd7; --accent-ink: #0a1a22; --accent-soft: #1b3440;
    --good: #6cc693; --good-soft: #163124;
    --warn: #e2aa56; --warn-soft: #372a11;
    --crit: #f08b81; --crit-soft: #3d1c19;
    --bar: #3d7187; --bar-now: #6ebbd7;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #11161b; --surface: #19212a; --sunken: #0c1115;
  --ink: #e3e9ee; --ink-2: #b0bbc5; --ink-3: #8a97a4; --line: #2c3843;
  --accent: #6ebbd7; --accent-ink: #0a1a22; --accent-soft: #1b3440;
  --good: #6cc693; --good-soft: #163124;
  --warn: #e2aa56; --warn-soft: #372a11;
  --crit: #f08b81; --crit-soft: #3d1c19;
  --bar: #3d7187; --bar-now: #6ebbd7;
  color-scheme: dark;
}
*, *::before, *::after { box-sizing: border-box; }
body { background: var(--bg); color: var(--ink); font-family: var(--sans); font-size: 15px; line-height: 1.5; }
body::before { content: ""; position: fixed; inset: 0 0 auto; height: env(safe-area-inset-top, 0px); z-index: 10; background: var(--bg); }
h1, h2, h3, h4, h5, h6, p, ol, ul, dl, dd, figure, pre { margin: 0; }
h1, h2, h3, h4 { text-wrap: balance; line-height: 1.25; }
ol, ul { padding: 0; }
a { color: var(--accent); }
code, pre, .mono { font-family: var(--mono); font-size: 0.9em; }
code { background: var(--sunken); border-radius: 3px; padding: 0.05em 0.3em; overflow-wrap: anywhere; }
pre code { background: none; padding: 0; overflow-wrap: normal; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }
button, select { font: inherit; color: inherit; }

.top { position: sticky; top: env(safe-area-inset-top, 0px); z-index: 5; background: var(--bg); border-bottom: 1px solid var(--line); }
.wrap { max-width: 980px; margin-inline: auto; padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); }
.top .wrap { padding-block: 10px 0; display: flex; flex-direction: column; gap: 6px; }
.brand { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 14px; }
.brand h1 { font-size: 18px; font-weight: 600; letter-spacing: -0.01em; }
.brand .newest { color: var(--ink-3); font-size: 13px; font-variant-numeric: tabular-nums; }
.tabs { display: flex; flex-wrap: wrap; gap: 0 2px; margin-left: -10px; }
.tabs a { padding: 8px 10px; color: var(--ink-2); text-decoration: none; font-weight: 500; font-size: 14px; border-bottom: 2px solid transparent; white-space: nowrap; }
.tabs a:hover { color: var(--ink); }
.tabs a[aria-current="page"] { color: var(--accent); border-bottom-color: var(--accent); }
main.wrap { padding-block: 16px 48px; display: flex; flex-direction: column; gap: 20px; }
.onething { display: flex; flex-wrap: wrap; gap: 4px 12px; align-items: baseline; background: var(--accent-soft); border-radius: var(--r); padding: 10px 14px; }
.one-label { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: var(--accent); white-space: nowrap; }
.one-text { flex: 1 1 260px; min-width: 0; font-weight: 500; }
.view { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.view > div[id$="-root"] { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.view[hidden] { display: none; }
.view-title { font-size: 20px; font-weight: 600; }
.sub { font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: var(--ink-3); margin-top: 8px; }
.lede, .muted { color: var(--ink-2); max-width: 70ch; }
.muted { color: var(--ink-3); font-size: 13px; }

.daybar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; }
.daybar .view-title { margin-right: auto; }
.btn { background: var(--surface); border: 1px solid var(--line); border-radius: var(--r); padding: 5px 11px; min-height: 36px; cursor: pointer; font-size: 13px; font-weight: 500; }
.btn:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.btn:disabled { color: var(--ink-3); cursor: default; opacity: 0.6; }
.btn[aria-pressed="true"] { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
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
.field .k { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: var(--ink-3); display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.field .v { max-width: 78ch; overflow-wrap: anywhere; }
.field.do .v { font-weight: 500; }
.field.what .v { color: var(--ink-2); }
.pill { display: inline-block; font-family: var(--sans); font-size: 12px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; line-height: 1; padding: 4px 7px; border-radius: 999px; background: var(--sunken); color: var(--ink-2); white-space: nowrap; }
.pill.repeat, .pill.medium, .pill.warn { background: var(--warn-soft); color: var(--warn); }
.pill.worse, .pill.high, .pill.crit { background: var(--crit-soft); color: var(--crit); }
.pill.better, .pill.fix { background: var(--good-soft); color: var(--good); }
.pill.new { background: var(--accent-soft); color: var(--accent); }

details > summary { cursor: pointer; }
.evidence > summary { font-size: 13px; color: var(--accent); font-weight: 500; width: fit-content; padding-block: 8px; }
.evidence[open] > summary { margin-bottom: 10px; }
.ev-body { display: flex; flex-direction: column; gap: 12px; }
.sess { background: var(--bg); border-radius: var(--r); padding: 10px 12px; display: flex; flex-direction: column; gap: 8px; min-width: 0; }
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
.evidence-slice { background: var(--bg); border-radius: var(--r); min-width: 0; }
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
.codebox { border: 1px solid var(--line); border-radius: var(--r); background: var(--sunken); min-width: 0; }
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
.ch-grid { stroke: var(--line); stroke-width: 1; }
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
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--ink-3); font-weight: 600; font-size: 12px; white-space: nowrap; }
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
.md pre { background: var(--sunken); border-radius: var(--r); padding: 10px; font-size: 13px; }
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

@media (max-width: 1024px) {
  /* finger-sized targets wherever a thumb may be the pointer */
  .tabs a { padding-block: 12px; }
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
}
@media (prefers-reduced-motion: reduce) { * { transition: none !important; animation: none !important; scroll-behavior: auto !important; } }
</style>

<header class="top">
  <div class="wrap">
    <div class="brand"><h1>Agentic Coach</h1><span class="newest">{{NEWEST}}</span></div>
    <nav class="tabs" aria-label="Views">
      <a href="#today" id="tab-today">Today</a>
      <a href="#week" id="tab-week">This week</a>
      <a href="#cost" id="tab-cost">Cost</a>
      <a href="#trends" id="tab-trends">Trends</a>
      <a href="#progress" id="tab-progress">Progress</a>
      <a href="#setup" id="tab-setup">Setup</a>
      <a href="#baseline" id="tab-baseline">Baseline</a>
    </nav>
  </div>
</header>

<main class="wrap">
  {{ONE}}
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

<script type="application/json" id="coach-data">{{DATA}}</script>
<script>
(function () {
  "use strict";
  var D = JSON.parse(document.getElementById("coach-data").textContent);
  var VIEWS = ["today", "week", "cost", "trends", "progress", "setup", "baseline"];
  var dayIdx = 0, weekIdx = 0, costIdx = 0;

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

  function show(v) {
    VIEWS.forEach(function (n) {
      document.getElementById("view-" + n).hidden = n !== v;
      var t = document.getElementById("tab-" + n);
      if (n === v) { t.setAttribute("aria-current", "page"); } else { t.removeAttribute("aria-current"); }
    });
    try { localStorage.setItem("coach.tab", v); } catch (e) { /* storage is optional */ }
  }
  function route() {
    var v = (location.hash || "").slice(1);
    if (VIEWS.indexOf(v) < 0) {
      try { v = localStorage.getItem("coach.tab"); } catch (e) { v = null; }
    }
    show(VIEWS.indexOf(v) < 0 ? "today" : v);
  }
  VIEWS.forEach(function (n) {
    document.getElementById("tab-" + n).addEventListener("click", function () { show(n); });
  });

  Array.prototype.forEach.call(document.querySelectorAll("figure.chart"), function (fig) {
    var out = fig.querySelector(".readout");
    function over(ev) {
      var g = ev.target.closest ? ev.target.closest("g.b") : null;
      if (g) { out.textContent = g.getAttribute("data-r"); }
    }
    fig.addEventListener("pointermove", over);
    fig.addEventListener("pointerdown", over);
    fig.addEventListener("pointerleave", function () { out.textContent = out.getAttribute("data-default"); });
  });

  renderToday();
  renderWeek();
  renderCost();
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
