# Agentic Coach menu bar and Overview: shared contract

The design brief the menu bar app, the live feed and the Overview tab were first built against, kept as design history. The current description of live.json is [docs/LIVE-FEED.md](../docs/LIVE-FEED.md), which has fields added since (for example `models`); where the two differ, that file is right. The menu bar mockups named below live in menubar/mockups/; Page.dc.html is in docs/mockups/.

## Mockups (the design to match)

Read these sources for exact layout, labels, colours and sizes:
- menubar/mockups/Extras.dc.html (the panel to build, with all four extras)
- menubar/mockups/Main.dc.html (period buttons behaviour and the line graph maths)
- menubar/mockups/MenuBar.dc.html (menu bar item states)
- docs/mockups/Page.dc.html (the Overview tab)
- menubar/mockups/Prototype.dc.html (the clickable prototype: menu bar item, panel and coach page on one Mac screen)
The "Extra 1" to "Extra 4" pills in Extras are mockup markers only; do not build them.

## Rules that apply to all three

- Trends over time are line graphs. No calendar heatmap.
- CHANGE 1 (owner, Nov 23): rows that compare items (activities, projects) carry a heat bar, as in the CodeBurn terminal view. A heat bar is a horizontal bar whose length is the row's cost as a share of the largest row in the list, and whose fill is one fixed cool-to-hot gradient laid across the full track width (accent, then warn, then crit tokens) and clipped by the bar, so a longer bar reaches hotter colours. It sits left of the row name, track about 64px wide in the panel and 96px on the page, 8px tall, 2px radius. It is decoration beside the numbers (hidden from screen readers); the numbers stay.
- CHANGE 2 (owner, Nov 23): activities are no longer cut to 3. The feed lists every activity type with any spend in the period, highest cost first, and each row adds `"turns": <int>`. The block is titled "By activity" with columns Cost, Turns, First try. The panel shows the top 5 and a line "N more on the coach page" when there are more; the Overview shows all of them. Projects get the heat bar too.
- Words users read: plain English, no em or en dashes, no emoji, dates as Nov 23, money with a dollar sign, exact labels from the mockups. Cost always carries "API-equivalent, not a bill".
- Colours and fonts come from the coach page tokens in `coach/build_site.py` (light and dark). Both themes must work; the menu bar app follows the Mac's appearance.
- Never read or show work excluded in `~/.claude/coach/config.json`. Use the ledger and digests, which already leave it out; any direct transcript read must apply the same filter (`coach/scope.py`, `coach/projects.py`).
- Python: standard library only, tests in `tests/` run with the repo's existing runner.
- Nothing leaves the Mac: no network calls. Never read the Claude Code login token or the Keychain.
- Every click shows at once that it was received (chosen item lit; a small turning mark while data loads).

## The feed: ~/.claude/coach/live.json

Written atomically (write temp file, rename) by `python3 coach/live.py write`, at most once a minute. `$COACH_HOME` overrides the folder. All times are local ISO 8601 with offset. Money is a number in US dollars; the readers format it.

```json
{
  "version": 1,
  "updated": "2026-11-23T11:41:00+02:00",
  "page_url": "https://... or null",
  "periods": {
    "today": {
      "label": "Today, Nov 23",
      "cost_usd": 27.80,
      "prev_cost_usd": 145.30,
      "calls": 403,
      "sessions": 4,
      "tokens": 57300000,
      "series_unit": "hour",
      "series": [{"t": "2026-11-23T00:00:00+02:00", "tokens": 200000, "usd": 0.11}],
      "peak": {"t": "2026-11-23T10:00:00+02:00", "tokens": 14600000, "usd": 6.9},
      "first_try": {"rate": 0.82, "prev_rate": 0.76, "edit_turns": 61},
      "cache_hit": {"rate": 0.96, "prev_rate": 0.96},
      "corrections": {"count": 3, "prev_count": 2},
      "activities": [{"name": "Coding", "usd": 13.09, "share": 0.47, "first_try": 0.86}],
      "projects": [{"name": "agentic-coach", "sessions": 2, "usd": 20.1, "share": 0.72}]
    },
    "week": {"...": "same keys; label 'Last 7 days'; series_unit 'day'; 7 points"},
    "month": {"...": "same keys; label 'Last 30 days'; series_unit 'day'; 30 points"},
    "all": {"...": "same keys, plus the two below; label 'All time, since Nov 9'; see CHANGE 5",
            "since": "2026-11-09T00:00:00+02:00",
            "detail_since": "2026-11-20T00:00:00+02:00 or null"}
  },
  "limits": {
    "as_of": "2026-11-23T11:40:10+02:00 or null",
    "rows": [
      {"key": "five_hour", "label": "5-hour", "used_pct": 7, "resets_at": "2026-11-23T14:24:00+02:00",
       "pace_pct": -39, "runs_out_at": null, "level": "ok"}
    ]
  },
  "running": [
    {"project": "agentic-coach", "branch": "main", "model": "Fable 5.2",
     "started": "2026-11-23T10:29:00+02:00", "context_pct": 64, "context_left_tokens": 359000, "level": "warn"}
  ],
  "tips": [
    {"title": "agentic-coach, main has passed 600k of context.", "advice": "Compact or hand off before the next big step.", "pattern": "context_bloat"}
  ]
}
```

Field rules:
- `calls` is the count of assistant API messages. `tokens` is in + out + cache read + cache write.
- `prev_*` is the same-length period just before (yesterday, the 7 days before, the 30 days before). Any `prev_*`, `rate` or `first_try` may be null when there is too little data; readers then show no change line, or "No edits" for an activity with no edit turns.
- `activities`: every type with spend, highest cost first, each with `turns` (see CHANGE 2 above). `projects`: up to 3 with the rest folded into "Other".
- CHANGE 3 (owner, Nov 23): each period adds `"mcp_servers": [{"name": "zoho-crm", "calls": 41, "tools": 6, "errors": 2}]`, one row per MCP server called in the period (tool names of the form `mcp__<server>__<tool>`; `tools` is how many different tools of that server were called; `errors` is failed calls), most calls first, up to 8, empty list when none. The Overview shows it as a block "MCP servers" with columns Calls, Tools, Errors and a heat bar by calls; with no rows it reads "No MCP calls in this period". The menu bar panel does not show it.
- CHANGE 5 (owner, Nov 23): a fourth period, `periods.all`, "All time", beside today, week and month.
  - Label: "All time, since Nov 9", naming the first day the coach has anything for: the ledger's first day when the ledger is in use, else the first day still in the live cache, else today. A first day in an earlier year carries its year ("All time, since Dec 3, 2025"). A day more than ten years back is taken for a broken clock and ignored. `since` is that day as a local ISO time (midnight).
  - `cost_usd`, `sessions`, `tokens`, `projects` and the graph cover every day from `since`; older days come from the ledger, which keeps every day.
  - `calls`, `activities`, `first_try`, `cache_hit`, `corrections` and `mcp_servers` can only come from transcripts the coach has read (the last 62 days at most, fewer once Claude Code has deleted older transcripts). `detail_since` is the first such day, or null when no transcript was read. `calls` therefore counts only the days from `detail_since` on. Readers show a small line "Detail from Nov 9" under those blocks when `detail_since` is a later day than `since`, and nothing when it is the same day, earlier, or null.
  - There is no period before: every `prev_*` is null and readers show no change line for this period.
  - `series`: one point a day while the period is 60 days or shorter (`series_unit` "day"); beyond that one point a week (`series_unit` "week"), each dated by its Monday (`t` is that Monday at local midnight, also for a first week that starts later). Axis labels for weekly points read like daily ones (the Monday as "Nov 9", the last one "Today"). The small series of `first_try`, `cache_hit` and `corrections` follow the same steps, put into at most 12 groups, null where a group has no transcript to measure.
  - Panel: the period control has four buttons, Today, 7 days, 30 days, All time. Page Overview: the same four; its address is `#overview/all`.
  - Readers that do not know the period ignore it; a reader given a feed without it shows "No data for this period".
- `running[].started` is the first message of the session's current run, not of the session: a session that was closed and resumed (Claude Code writes a `cost-state` record when it closes one), or left untouched for 6 hours, starts a new run with its next message.
- `limits.rows` keys: `five_hour`, `weekly`, then one `weekly_<model>` per model row. `pace_pct` is used percent minus elapsed percent of the window (positive means over pace, negative means in reserve). `runs_out_at` is null unless over pace and computable. `level`: ok below 70, warn 70 to 89, crit 90 and up. `rows` is empty and `as_of` null when no limit data has been seen; readers then hide the limits block.
- `running`: sessions whose transcript changed in the last 10 minutes. `level` follows the coach's context thresholds in `signals.T`.
- `tips`: up to 3 from the latest findings, most severe first, in the coach's own wording.
- Readers must tolerate a missing file, a missing key and `version` above 1 (show "Update the coach" instead of crashing).
