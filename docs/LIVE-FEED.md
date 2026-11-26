# The Overview and the live feed: live.json

The coach can keep one small JSON file with its latest numbers: `~/.claude/coach/live.json` (the folder moves with `$COACH_HOME`). The Overview tab of the coach page is drawn from the same figures, the menu bar app ([menubar/README.md](../menubar/README.md)) reads the file, and other tools on your machine can read it too. This page describes the Overview and what is in the file.

The feed is built on your machine from your own transcripts, the coach's ledger and the plan limits the coach status line last saw. It makes no network call and never reads your login token or the Keychain. Work you left out in `config.json` (`exclude`, `only_account`, `exclude_dates`; see [SETTINGS.md](SETTINGS.md#leaving-work-out)) is never in it.

## What the Overview shows

The Overview is the first tab of the coach page once there is data for it. It has four periods, Today, 7 days, 30 days and All time, and its own address for each (for example `#overview/all`). Its figures are worked out on your machine each time the page is built: after every recap, or when you run `python3 coach/live.py page`. A green "Live" mark beside the name shows while they are under 15 minutes old.

| Figure | How it is worked out |
|---|---|
| Cost | Tokens at API list prices. API-equivalent, not a bill. |
| Calls | Assistant API messages, each counted once. |
| Tokens | Input + output + cache read + cache write, as a line graph: one point per hour today, one per day for 7 and 30 days, one per day or week for All time, and the peak. |
| Cache hit | Cache read / (input + cache read + cache write). Cache writes count as misses. |
| Corrections | Prompts that open by correcting the last answer, the same count the recap uses. |
| First try | The share of edit turns with no retry. A retry is the same file edited again with a shell command in between (Edit foo, Bash, Edit foo). A different file after the shell command is not a retry, and neither is a shell command that only reads, such as grep or git log. |
| By activity | Every turn is filed under one of 13 types (listed under `activities` below). Every type with spend is listed, highest cost first, with its turns and its first-try rate. A subagent's work is filed under what the agent did. |
| By model | The four models that cost the most, each with its cost and share of spend, its first-try rate, its failed tool calls and their share of its tool calls, and its rework (edits it had to redo, plus times you corrected it). Errors and rework are counted on the model that made the call. The error figure turns amber from 2 percent of tool calls and red from 4 percent. A row opens "Where the errors were": the tools and MCP servers behind that model's errors, redone edits and corrections. Smaller models fold into one line, and one line names the model with the most rework. |
| Estimates by model | Claude's time estimates against the actual time it reported, one row per model, sorted by model: pieces of work, minutes estimated, minutes taken and how far over or under. A piece counts only when Claude stated its actual ("estimate 15 min, actual 11"); an estimate with no stated actual is counted apart, under the table, and left out of the totals. Hidden when the period has none. |
| Projects | The top three by cost, the rest as Other. |
| MCP servers | Calls, different tools used and failed calls per server, most calls first, up to 8, with readable names. |
| Before | The same figures for the period just before: yesterday, the 7 days before, the 30 days before. All time has none. |

It also lists the sessions running now (transcript changed in the last 10 minutes) with how much context each has left, up to three tips from the latest findings, and your plan limits with pace. All time covers cost, tokens, sessions and projects back to the first day on record; the other figures reach back only as far as the transcripts still on disk, and the page says from when.

No model is asked to work any of this out: the tools a turn used decide its activity type first, then keywords in the prompt.

## Plan limits

The limits come from one place only: the 5-hour and weekly percentages that Claude Code hands to status line scripts (`rate_limits` in the [status line input](https://code.claude.com/docs/en/statusline)). The coach status line keeps them in `~/.claude/coach/state/limits.json` and the Overview reads that file. The coach never calls a usage endpoint and never reads your login token or the Keychain.

- Pace is used percent less the share of the window that has passed. 7% used with 46% of the window gone is 39% in reserve; 80% used at the halfway point is 30% over pace.
- A time the limit runs out is given only when you are over pace. Nothing is said about pace in the first 3 percent of a window.
- No status line, no limits: turn it on in `/agentic-coach:coach setup` (see [Status line](GUIDE.md#status-line)). The status line belongs to the terminal. Sessions in the Claude desktop app's Code tab do not run it, so on a day spent only there the limits block stays hidden. Claude Code reports a weekly figure for all models together; it does not report one per model.

## Turning it on

Write it by hand, from the plugin folder:

```
python3 coach/live.py write            # write the feed (skipped when it is under a minute old)
python3 coach/live.py write --force    # write it now
python3 coach/live.py show             # print the feed without writing live.json
python3 coach/live.py page             # rebuild the coach page with fresh figures and print its path
python3 coach/live.py time             # cold and warm run times on this machine
```

To keep it fresh on its own (macOS only), `/agentic-coach:coach setup` offers a per-user LaunchAgent, `com.agentic-coach.live`, that runs `live.py write` once a minute, so `live.json` is never more than a minute old and the cache behind the Overview stays warm, which keeps each page build quick. It does not rebuild the page by itself. Everything else in the coach works without it; the menu bar app is the one thing that needs it. It asks first, and you can take it out again at any time:

```
python3 coach/setup_check.py live-agent status      # read-only
python3 coach/setup_check.py live-agent install     # writes the LaunchAgent and loads it
python3 coach/setup_check.py live-agent uninstall   # unloads it and removes what install wrote
```

What it costs: one short background run a minute, about a tenth of a second of CPU once the cache is warm (the first run reads every transcript once and can take half a minute). The files it keeps are all under the coach folder: `live.json`, `live-cache.json` and `live-mids.json` (what was already counted), `coach_live.py` (a small launcher that finds the current plugin version), `live.path`, `live.lock` (one run at a time) and `live.err.log`. The only file outside it is the LaunchAgent itself, `~/Library/LaunchAgents/com.agentic-coach.live.plist`. A transcript that did not change since the last run is not opened, and one that grew is read from where the last run stopped.

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
      "mcp_servers": [{"name": "github", "calls": 41, "tools": 6, "errors": 2}],
      "models": [
        {"name": "Claude Fable 5.2", "usd": 21.4, "share": 0.77,
         "main_usd": 6.1, "sub_usd": 15.3,
         "first_try": 0.79, "edit_turns": 48,
         "tool_calls": 512, "tool_errors": 21, "error_rate": 0.041,
         "redone_edits": 10, "corrections": 2,
         "where_most": "Testing, Debugging, Coding",
         "where": [{"name": "Bash", "kind": "tool", "errors": 14, "redone_edits": 0, "corrections": 1},
                   {"name": "Edit", "kind": "tool", "errors": 2, "redone_edits": 10, "corrections": 0},
                   {"name": "Claude browser", "kind": "mcp", "errors": 5, "redone_edits": 0, "corrections": 0},
                   {"name": "No tool", "kind": "none", "errors": 0, "redone_edits": 0, "corrections": 1}]}
      ],
      "estimates": {
        "rows": [{"model": "claude-fable-5-2", "name": "Claude Fable 5.2", "pieces": 4,
                  "est_min": 50, "act_min": 65, "overrun_pct": 30, "no_actual": 2}],
        "pieces": 4, "est_min": 50, "act_min": 65, "overrun_pct": 30, "no_actual": 2
      }
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
- `models`: one row per model with spend in the period, highest cost first, an empty list when none. The rules:
  - Every assistant message in a transcript, main session or subagent, carries its model. A tool call, its error and any edit it makes belong to the model of the message that made the call.
  - `name`: the model's name as the coach shows it elsewhere ("Claude Fable 5.2"). A dated model id and its plain id are one row. Long-prompt requests are priced apart and keep their own row ("Claude Haiku 5.5 (long prompt)"), as on the coach page. A model with no list price has no dollars, so no row; fast mode has no list price, so its work is in no row.
  - `usd`: the model's cost in the period; `share`: that over the cost of all model rows. `main_usd` and `sub_usd` split it between main sessions and subagents.
  - `tool_calls`: tool calls in the model's messages. `tool_errors`: tool results marked as errors, counted as the coach counts errors elsewhere, so refusals (the person said no, or a permission rule blocked the call) are not errors (`mcp_servers` errors still count them, so an MCP server can show fewer errors here). An error belongs to the day of its call. `error_rate`: errors over calls, null with no calls.
  - `edit_turns`, `first_try` and `redone_edits` use the first-try rule above. An edit turn counts once for each model that edited in it. It is redone for a model when that model made the retrying edit (the same file edited again after a shell command that checks work), and clean for it otherwise, so `redone_edits` is the model's edit turns that count as retries and `first_try` is its clean edit turns over its edit turns (null with no edit turns). A turn with one model gives the same answer as the period's own first-try rate.
  - `corrections`: the period's correction count, each given to the model of the last assistant message before the correcting prompt. A correction with no answer before it in its session belongs to no model.
  - `where_most`: the model's top three activity types by cost, joined with ", ", or null when there are none.
  - `where`: up to 6 rows, the tools where this model's errors, redone edits and corrections happened, most errors first (then most redone edits, then most corrections, then by name). `kind` is `tool` for a built-in tool (named by its tool name), `mcp` for an MCP server (named as in `mcp_servers`), and `none` for the row "No tool". An error sits on the tool that failed, a redone edit on the edit tool that made the retry, and a correction on the last tool call of the answer it corrects, or "No tool" when that answer made none. A tool with nothing to report has no row.
  - Cost per model covers every day, ledger days included (the ledger keeps the main and subagent split per model). Every other figure exists only where the transcript was read, as for activities.
  - Exclusions and the account filter apply as everywhere else, and no prompt text is in the rows.
- `estimates`: Claude's stated time estimates against the actual time it reported (`coach/estimates.py`), or null when the period has none. The rules:
  - Claude states an estimate before a piece of work ("Estimate: 15 minutes", "Estimate 25 min.") and reports it when the work is done ("estimate 15 min, actual 11", "Estimate 20, actual 6."). Only main-session transcripts are read; hours become minutes; a range ("20 to 30 minutes") is not read.
  - A report is paired with the latest open estimate of the same minutes earlier in the same transcript, and the piece belongs to the model that made the estimate. A report with no such estimate stands alone and belongs to the model that wrote it. The same report written again within an hour in the same transcript counts once.
  - A piece counts only when Claude stated its actual, and it is filed on the day of the report. An estimate never followed by a report is counted in `no_actual` (on the day of the estimate) and is in no other figure. The time from the estimate to a later message is not used instead: the transcript does not mark where a piece of work ends, and that gap takes in waiting for checks or for the person, other tasks and nights the session sat open.
  - `rows`: one per model, sorted by model id (a dated id and its plain id are one row). `model` is the id, `name` the display name, `pieces` the pieces with a stated actual, `est_min` and `act_min` their minutes, `overrun_pct` (actual - estimated) / estimated as a whole percent (positive is over, negative under, null with no pieces) and `no_actual` that model's estimates with no stated actual. A model with only open estimates has a row with 0 pieces.
  - The top-level `pieces`, `est_min`, `act_min`, `overrun_pct` and `no_actual` are the same figures for all models together.
  - Like the other detail figures, it exists only where the transcript was read.

All time only:

- `since`: the first day the coach has anything for, at local midnight. Cost, sessions, tokens, projects and the graph cover every day from then on; older days come from the ledger, which keeps every day.
- `detail_since`: the first day whose transcript was read, or null. Calls, activities, first try, cache hit, corrections, MCP servers and every model figure but cost can only come from transcripts still on disk (at most the last 62 days, fewer once Claude Code has deleted older ones). Show a short "Detail from" line when it is later than `since`.

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
