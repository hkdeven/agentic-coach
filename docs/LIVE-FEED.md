# The live feed: live.json

The coach can keep one small JSON file with its latest numbers: `~/.claude/coach/live.json` (the folder moves with `$COACH_HOME`). The Overview tab of the coach page is drawn from the same figures, and other tools on your machine can read the file too. This page describes what is in it.

The feed is built on your machine from your own transcripts, the coach's ledger and the plan limits the coach status line last saw. It makes no network call and never reads your login token or the Keychain. Work you left out in `config.json` (`exclude`, `only_account`, `exclude_dates`) is never in it.

## Turning it on

Write it by hand, from the plugin folder:

```
python3 coach/live.py write            # write the feed (skipped when it is under a minute old)
python3 coach/live.py write --force    # write it now
python3 coach/live.py show             # print the feed without writing live.json
python3 coach/live.py page             # rebuild the coach page with fresh figures and print its path
python3 coach/live.py time             # cold and warm run times on this machine
```

To keep it fresh on its own (macOS only), `/agentic-coach:coach setup` offers a per-user LaunchAgent, `com.agentic-coach.live`, that runs `live.py write` once a minute. It asks first, and you can take it out again at any time:

```
python3 coach/setup_check.py live-agent status      # read-only
python3 coach/setup_check.py live-agent install     # writes the LaunchAgent and loads it
python3 coach/setup_check.py live-agent uninstall   # unloads it and removes what install wrote
```

What it costs: one short background run a minute, about a tenth of a second of CPU once the cache is warm (the first run reads every transcript once and can take half a minute). The files it keeps are all under the coach folder: `live.json`, `live-cache.json` and `live-mids.json` (what was already counted), `coach_live.py` (a small launcher that finds the current plugin version), `live.path`, `live.lock` (one run at a time) and `live.err.log`. The only file outside it is the LaunchAgent itself, `~/Library/LaunchAgents/com.agentic-coach.live.plist`.

## How it is written

- Written whole: a temporary file beside it, then a rename, so a reader never sees half a file.
- At most once a minute, unless `--force`.
- All times are local ISO 8601 with the UTC offset, for example `2026-11-23T11:41:00+02:00`.
- Money is a plain number in US dollars at API list prices. It is API-equivalent, not a bill; readers format it.
- When `config.json` cannot be read, `write` replaces the feed with an empty one (`"periods": {}` and `"problem": "settings"`) and exits 2, so an older feed never stays up showing work a new rule was meant to hide.

## The schema (version 1)

All numbers below are samples.

```json
{
  "version": 1,
  "updated": "2026-11-23T11:41:00+02:00",
  "page_url": null,
  "periods": {
    "today": {
      "label": "Today, Nov 23",
      "cost_usd": 27.8,
      "prev_cost_usd": 145.3,
      "calls": 403,
      "sessions": 4,
      "tokens": 57300000,
      "series_unit": "hour",
      "series": [{"t": "2026-11-23T00:00:00+02:00", "tokens": 200000, "usd": 0.11}],
      "peak": {"t": "2026-11-23T10:00:00+02:00", "tokens": 14600000, "usd": 6.9},
      "first_try": {"rate": 0.82, "prev_rate": 0.76, "edit_turns": 61, "series": [0.8, null, 0.85]},
      "cache_hit": {"rate": 0.91, "prev_rate": 0.9, "series": [0.9, 0.92]},
      "corrections": {"count": 3, "prev_count": 2, "series": [1, 0, 2]},
      "activities": [{"name": "Coding", "usd": 13.09, "share": 0.47, "first_try": 0.86, "turns": 42}],
      "projects": [{"name": "widget-shop", "sessions": 2, "usd": 20.1, "share": 0.72}],
      "mcp_servers": [{"name": "github", "calls": 41, "tools": 6, "errors": 2}]
    },
    "week": {"label": "Last 7 days", "series_unit": "day", "...": "the same keys as today"},
    "month": {"label": "Last 30 days", "series_unit": "day", "...": "the same keys as today"},
    "all": {"label": "All time, since Nov 9", "series_unit": "day", "...": "the same keys as today",
            "since": "2026-11-09T00:00:00+02:00",
            "detail_since": "2026-11-20T00:00:00+02:00"}
  },
  "limits": {
    "as_of": "2026-11-23T11:40:10+02:00",
    "rows": [
      {"key": "five_hour", "label": "5-hour", "used_pct": 7, "resets_at": "2026-11-23T14:24:00+02:00",
       "pace_pct": -39, "runs_out_at": null, "level": "ok"}
    ]
  },
  "running": [
    {"project": "widget-shop", "branch": "main", "model": "Opus 5.5",
     "started": "2026-11-23T10:29:00+02:00", "context_pct": 64, "context_left_tokens": 359000, "level": "warn"}
  ],
  "tips": [
    {"title": "widget-shop has passed 600k of context.", "advice": "Compact or hand off before the next big step.",
     "pattern": "context_bloat"}
  ]
}
```

## Field rules

Top level:

- `version`: 1. A reader that meets a higher number should say the coach needs updating rather than guess.
- `updated`: when the feed was built.
- `page_url`: the link of your own published coach page, or null.
- `problem`: present only on the empty feed written when `config.json` cannot be read.

Each period (`today`, `week`, `month`, `all`):

- `label`: the words to show. All time names its first day, with the year when it is not this year ("All time, since Dec 3, 2025").
- `cost_usd`, `tokens`, `sessions`: for the whole period. `tokens` is input + output + cache read + cache write.
- `calls`: the number of assistant API messages, each counted once (a resumed session's repeated messages count for the transcript that had them first).
- `prev_*`: the same figure for the period just before of the same length (yesterday, the 7 days before, the 30 days before). Null when there is too little data, and always null for All time. Readers then show no change line.
- `series_unit` and `series`: the points of the spend graph, oldest first, each `{"t", "tokens", "usd"}`. Today has one point an hour from midnight to the hour that is running; 7 and 30 days one a day; All time one a day up to 60 days and one a week beyond that, each week dated by its Monday (`series_unit` "week").
- `peak`: the point with the most tokens, or null.
- `first_try`: the share of edit turns with no retry. A retry is the same file edited again with a shell command in between (Edit foo, Bash, Edit foo); a different file, or a shell command that only reads, is not a retry. `edit_turns` is how many edit turns there were. `rate` is null with no edit turns.
- `cache_hit`: cache read / (input + cache read + cache write). Cache writes count as misses.
- `corrections`: prompts that open by correcting the last answer, the same count the recap uses.
- The `series` inside `first_try`, `cache_hit` and `corrections` are small trend lines: the same measure per step of the period, put into at most 12 groups of neighbouring steps, oldest first, null where a group has nothing to measure.
- `activities`: every activity type with spend, highest cost first, with its `turns`, its `share` of the period's activity cost and its `first_try` rate (null with no edit turns). The 13 types are Coding, Debugging, Feature Dev, Refactoring, Testing, Exploration, Planning, Delegation, Git Ops, Build/Deploy, Brainstorming, Conversation and General. A subagent's turns are filed under what the agent did; Delegation is the cost of the main session's turns that started agents.
- `projects`: the top three by cost, the rest folded into one row named "Other".
- `mcp_servers`: one row per MCP server called (tool names of the form `mcp__<server>__<tool>`), most calls first, up to 8, an empty list when none. `tools` is how many different tools of that server were called and `errors` how many calls failed. Names are made readable: dashes and underscores become spaces, and a connector that shows up only as an id is named from its tool names where the coach knows them, else "Connector" and the first 6 characters of the id.

All time only:

- `since`: the first day the coach has anything for, at local midnight. Cost, sessions, tokens, projects and the graph cover every day from then on; older days come from the ledger, which keeps every day.
- `detail_since`: the first day whose transcript was read, or null. Calls, activities, first try, cache hit, corrections and MCP servers can only come from transcripts still on disk (at most the last 62 days, fewer once Claude Code has deleted older ones). Show a short "Detail from" line when it is later than `since`.

`limits`:

- Filled only from the 5-hour and weekly percentages Claude Code hands to status line scripts, which the coach status line keeps in `~/.claude/coach/state/limits.json`. With no coach status line, or on a day spent only in the Claude desktop app's Code tab (where the status line does not run), `rows` is empty and `as_of` is null; readers then hide the block.
- `as_of`: when the numbers were last seen.
- `rows[].key`: `five_hour` or `weekly`. A window whose reset time has passed is dropped.
- `pace_pct`: used percent less the share of the window that had passed, so positive is over pace and negative is in reserve. Null in the first 3 percent of a window.
- `runs_out_at`: when the limit would run out at the rate used so far; null unless over pace and before the reset.
- `level`: `ok` below 70 percent used, `warn` from 70, `crit` from 90.

`running`: sessions whose transcript changed in the last 10 minutes, most recent first.

- `started`: the first message of the session's current run. A session that was closed and resumed, or left untouched for 6 hours, starts a new run.
- `context_pct` and `context_left_tokens`: the last message's context against the model's window (1M for a model seen above 200k on this machine, else 200k).
- `level`: from the coach's own context thresholds, as in the status line.

`tips`: up to three from the latest findings, most severe first, in the coach's own wording.

Readers should cope with a missing file, a missing key or a null anywhere, and ignore keys they do not know.

## Credits

The activity types and the first-try rule follow [CodeBurn](https://github.com/getagentseal/codeburn) (MIT licence), written again in Python in `coach/activity.py`. No CodeBurn code is copied.
