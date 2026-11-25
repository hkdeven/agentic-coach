# Settings

Every choice the coach keeps for you, and where it writes things. All of it lives in one small file, `~/.claude/coach/config.json`, and the folder around it.

Where a command names `<plugin>`, put the path of the plugin folder in its place. `/agentic-coach:coach setup status` shows it as `plugin_root`.

## The coach page

### Publish it privately (optional)

By default the coach page is only a local file, `~/.claude/coach/site/index.html`. If you want to open it from your phone or another computer, you can publish it as a private page on your own Claude account. Only you can see it unless you share it yourself.

1. In Claude Code, type `/agentic-coach:coach setup` and press Enter.
2. When Claude asks "Publish the page as a private page on your own Claude account?", answer `yes`.
3. Claude first asks "Show short excerpts of your sessions under each item on the published page? Whoever can open the page can read them." They stay off unless you answer `yes`.
4. Claude publishes the page, shows you its link, and saves the link in `~/.claude/coach/config.json`. From then on every recap updates that same page and ends its reply with the link.

### Turn on notifications (optional)

1. In Claude Code, type `/agentic-coach:coach setup` and press Enter.
2. When Claude asks "Send one push notification when a recap is ready?", answer `yes`.

You then get one short notification per recap that has something in it, on the devices where your Claude push notifications are set up. A recap with nothing notable sends nothing.

### Turn either off

Your choices are stored in one small file, `~/.claude/coach/config.json`:

```json
{
 "notify": true,
 "page_url": "https://claude.ai/..."
}
```

- `page_url`: the link of your own published page. Absent means the page is not published.
- `notify`: `true` sends the notification. Absent or `false` sends none.

To stop publishing, type this in a terminal, with the path of the plugin folder in place of `<plugin>` (run `/agentic-coach:coach setup status` to see it as `plugin_root`):

```
python3 <plugin>/coach/config.py unset page_url
```

To stop notifications:

```
python3 <plugin>/coach/config.py set notify false
```

Or delete `~/.claude/coach/config.json` to turn both off (that also removes every other setting in it). A page you already published stays where it is until you delete it yourself on claude.ai. `/agentic-coach:coach setup status` shows whether the page is published and whether notifications are on.

### Change the name on the page (optional)

The page's title and header say "Agentic Coach". To show another name, type this in a terminal, with the plugin folder in place of `<plugin>`:

```
python3 <plugin>/coach/config.py set display_name My coach
```

The name is kept on one line, up to 40 characters, and the next build of the page uses it. To go back to "Agentic Coach":

```
python3 <plugin>/coach/config.py unset display_name
```

## Name your own projects (optional)

Costs are grouped by project (how a project gets its name is in the [Guide](GUIDE.md#cost)). To shape the names yourself, add a `projects` list to `~/.claude/coach/config.json`. Each rule has a name and one or more pieces of text to look for in the session's folder path or title. The first rule that matches wins. A `*` in the text works as a wildcard.

```json
{
 "projects": [
  {"name": "Shop website", "match": ["widget-shop", "shop-admin"]},
  {"name": "Client work", "match": ["*/clients/*"]}
 ]
}
```

Two more keys, both optional:

- `session_projects`: pin one session to a project by its id. It wins over every rule. The key is the full session id or its first 8 or more characters.
- `other_project`: one shared name for sessions that have no folder and match no rule, so one-off chats collapse into one row instead of one row per title.

```json
{
 "session_projects": {"1a2b3c4d": "Shop website"},
 "other_project": "One-off chats"
}
```

You can set both from a terminal, with the plugin folder in place of `<plugin>`:

```
python3 <plugin>/coach/config.py set session_project 1a2b3c4d Shop website
python3 <plugin>/coach/config.py unset session_project 1a2b3c4d
python3 <plugin>/coach/config.py set other_project One-off chats
python3 <plugin>/coach/config.py unset other_project
```

After any change to these, run:

```
python3 <plugin>/coach/ledger.py backfill --rebuild
```

It renames the sessions whose transcripts are still on disk and says how many days changed. Older sessions keep the names they were recorded with.

## Leaving work out

Some work you may never want to see in the coach. Three settings do that. Each one drops the work from every digest, total, cost figure, ledger row, report and page from then on. The coach never prints what you left out: setup status only gives counts, such as "2 exclusion rules, 5 sessions excluded".

- 🏷️ **By name:** `exclude`. Text to look for in a session's folder path, its title or its project name, without regard to case; a `*` works as a wildcard. You can also list session ids (the full id or its first 8 or more characters), under `sessions` or as a match text. Exclusion wins over every project rule and pin.
- 🪪 **By account:** `only_account`. If you used two Claude accounts on this machine, show one only. The value is the first 6 characters of the account id; `current` fills it in from the account you are logged in with. Where the machine's records say which account a session ran under on a given day, days under another account are left out. Many older sessions carry no such record: `unknown_account` decides them, `include` (the default) or `exclude`.
- 📅 **By date:** `exclude_dates`. Single days or ranges, both ends included. Use it for periods you know were on another account when the records cannot tell. The all-time "since" date moves accordingly.

```json
{
 "exclude": [
  {"match": ["side-project", "*/clients/acme/*"]},
  {"sessions": ["1a2b3c4d"]}
 ],
 "only_account": "0a1b2c",
 "unknown_account": "include",
 "exclude_dates": ["2026-01-05", "2026-02-01..2026-02-14", "..2025-12-31"]
}
```

From a terminal, with the plugin folder in place of `<plugin>`:

```
python3 <plugin>/coach/config.py add exclude side-project
python3 <plugin>/coach/config.py remove exclude side-project
python3 <plugin>/coach/config.py set only_account current
python3 <plugin>/coach/config.py unset only_account
python3 <plugin>/coach/config.py set unknown_account exclude
python3 <plugin>/coach/config.py add exclude_dates ..2025-12-31
python3 <plugin>/coach/config.py remove exclude_dates ..2025-12-31
python3 <plugin>/coach/config.py show
```

Then run this once, so that what is already recorded is removed too:

```
python3 <plugin>/coach/ledger.py backfill --rebuild
```

It says how many sessions went: `N excluded sessions removed`. (The next recap does the same on its own.)

Good to know:

- 🧽 Reports written before a rule existed are left as they are on disk. The coach page leaves out any item or line in them that names excluded work or cites an excluded session, and any daily report of an excluded date.
- 🧾 The page shows one quiet line under Cost, "Showing one account only", when the account filter is on. It never says which.
- ⏪ Remove an `exclude` rule and its sessions come back on the next run, as long as their transcripts are still on disk. Days dropped for the account or by date come back only where the whole day was dropped; a day that kept other sessions stays as it is.
- 🧱 If `config.json` cannot be read (a stray comma, or a leave-out setting with a value that cannot be used), the coach stops with a plain message and builds nothing, instead of guessing and showing what you hid. Fix the file and run again.
- 🤐 `config.py show` prints your settings as they are, leave-out rules included. It is meant for you in a terminal. After a change, the script prints counts only.
- 🗒️ Which account a session started under is noted from this version on, in `~/.claude/coach/state/session-accounts.jsonl`, in short form only. For older sessions the coach uses whatever the transcripts and the desktop app's own session list happen to record. None of that is documented by Claude Code, so every read is optional and a session without a record is simply "unknown".

## Where things are written

Everything goes under `~/.claude/coach/` in your home folder, never inside the plugin or a repo:

```
~/.claude/coach/
  reports/
    daily/YYYY-MM-DD.md        one per day
    weekly/YYYY-MM-DD.md       one per weekly review, named for the day it ran
    ranges/A_to_B.md           one per date range you asked for
    digests/                   the numbers each report was built from (these quote your prompts),
                               and trend.json, the 30-day totals the page draws
    baseline/                  optional: older reports the weekly review may use as a starting point
    evidence/                  the candidates and verdicts behind each report's items
    config-audit/              the saved "set up but unused" audits
  site/index.html              the coach page, rebuilt after every recap
  config.json                  your settings (see the list below); absent means all defaults
  ledger.json                  tokens per day, session, project and model, for the all-time totals
  advice.jsonl                 every item the coach gave, for follow-through (advice.lock guards it)
  applied.jsonl                fixes applied and undone, and advice that was acted on
  backups/                     each file as it was before a fix
  pending-fixes/               a fix preview waiting for your answer (kept for an hour)
  unattended                   a short-lived mark written by a scheduled run
  compare/                     stored period numbers, snapshots and the latest comparison
  evidence/                    session slices you asked to save with evidence.py show --out
  state/                       what the nudge and the status line have seen in each session
                               (cleared after 30 days), and session-accounts.jsonl
  coach_statusline.py          the status line launcher, only if you turned the status line on
  live.json                    the live feed (docs/LIVE-FEED.md), when it has been written
  live-cache.json              per-transcript figures behind the Overview, so a build reads only what changed
  live-mids.json               the message ids already counted, so none is counted twice
  coach_live.py, live.path     the live feed launcher and its note, only if you turned the runner on
  live.err.log                 the runner's errors, only if you turned the runner on
  live.lock                    keeps two live feed runs from overlapping
  permission-prompts.jsonl     a local log of permission prompts, used by the weekly review
  off                          exists only if you turned the nudge off
```

Every key `config.json` can hold, all optional:

| Key | What it does |
|---|---|
| `page_url` | the link of your own published page |
| `notify` | `true` sends one push notification per recap |
| `evidence_on_page` | `true` or `false`: session excerpts on the page (default: on for a local page, off for a published one) |
| `projects` | your own project rules |
| `session_projects` | pin a session to a project by id |
| `other_project` | one shared name for chats with no folder and no matching rule |
| `exclude` | work that is never shown |
| `only_account` | show one Claude account only (short form) |
| `unknown_account` | `include` or `exclude` sessions with no account record |
| `exclude_dates` | days or ranges that are never shown |
| `statusline` | optional status line settings |
| `display_name` | the name in the page title and header (default "Agentic Coach") |

`python3 <plugin>/coach/config.py show` prints them as the coach reads them.

To keep the data somewhere else, set the environment variable `COACH_HOME` to another folder.

## Turn the nudge off

In a terminal:

```
touch ~/.claude/coach/off
```

That silences the nudge and stops the permission log, at once, in every session. The status line keeps its numbers and drops its coaching. The recaps still work. To turn it back on:

```
rm ~/.claude/coach/off
```

To switch the whole plugin off instead, type `/plugin` in Claude Code, pick `agentic-coach`, and choose disable.
