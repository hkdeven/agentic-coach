# Configured versus used rules

Used by the `coach` skill during the weekly review. The ground rules in `SKILL.md` apply in full. This file adds one short block to the weekly report: what is set up in Claude Code, loaded into every session, and was not used.

Everything that is loaded into every session is re-read on every turn. A skill nobody calls, a connector nobody uses and a long CLAUDE.md all cost a little on each request. This audit lists them and puts an estimated price on them. It proposes. It never disables, removes or edits anything.

## Run it

In a weekly review `recap.py` has already run the audit: the plan's `config_audit` is the saved JSON (null when it could not run: then leave the block out). For `audit` on its own, or to run it again:

```
python3 "${CLAUDE_PLUGIN_ROOT}/coach/config_audit.py" --save --format weekly
```

It prints the draft block and a line `saved <path>`: the full JSON, kept under `~/.claude/coach/reports/config-audit/`. Dollar figures use the same price table as every other cost the coach shows (`coach/prices.json`). The window is the last 30 days, cut down to the days that transcripts exist for; the JSON says the real window in `window.real_days`. It reads config files and transcripts only and writes nothing outside the coach home. If the command fails, leave the block out of the report and carry on.

Read `always_on`, `findings` and `weekly_block` in the saved JSON. Use `items` only to check a line.

## What the figures mean

- Every token and dollar figure is an estimate. Tokens are characters divided by four. Say "about" and "estimate" each time you quote one.
- `tokens_per_turn` is what one item adds to each request. `turns` is the number of model requests in main sessions that carried it. `window_tokens` is the two multiplied.
- `usd` is `window_tokens` at the cache read rate, weighted by the models used. It is null when no price table is installed: then quote tokens only. The first request of each session pays the higher cache write rate; that is in `always_on.usd_first_turn_write`, shown apart, never added to an item.
- Subagent requests are not counted, so every total is a floor.
- An item installed part way through the window is charged only for the turns since then (`turns_basis` says so). An MCP server, and anything known only from what sessions listed, is charged for the sessions that listed it.
- `cost_basis` says where a size came from. It starts with "measured" (the line a session was shown), "estimate" (read from the file on disk; for an MCP server it may add "assumed" when a default size per tool was used) or "unknown" (no session recorded the server's tools: quote no figure). Any other text means the item loads nothing right now: typed-only, turned off, not approved, not selected, beaten by another copy, or no longer listed.
- `window.transcripts_cut_short` counts transcript files that held a record in an unexpected shape and were read only up to it. When it is not 0, say the counts are a floor.

## Do not flag these

- Hooks. A guard hook that never ran is doing its job, and only runs that leave a record are counted. `findings.hooks_no_run_recorded` is for your information. Never propose removing a hook from it.
- Anything in `findings.new_no_history`: installed in the last 14 days, so there is no history yet.
- Items with scope `builtin` (part of Claude Code) or `managed` (set by the organisation). The person cannot change them.
- MCP servers that are deferred and small. With tool search on (the default) an unused server costs only its tool names and instructions. Flag one only when it is in `findings.mcp_large_unused` or its per-turn figure is among the largest, and say "deferred" when it is.
- Anything when `window.real_days` is under 7 (never-used findings then carry a `confidence` that starts with "low"), unless the person asked for the audit directly. Then say how many days it rests on.
- A finding with `may_be_off_already`: the item was in earlier sessions of the window but not in the newest ones (`last_listed` gives the date), so it was probably switched off since. The draft block already leaves these out.
- A project item marked `used_in_another_checkout`: the same file was used from another copy of the repository.
- A plugin whose action reads "review before disabling": it also runs hooks. Say so.
- Items with scope `session` are loaded into sessions but are not in the local config files (a connector, or a skill switched on in the account or the desktop app). They can be listed, with the action as given. Prefer items the person can change in a local file when there are more than five candidates of similar cost.

## The block

Add this after "What cost the most" and before "Proposed changes" in the weekly report, only when at least one line survives the rules above:

```
## Set up but unused
- skill NAME (scope): about N tokens per turn (estimate), never in D days. Action: disable, remove, or move to project level.
```

Rules for the block:

- At most five lines, most costly first. It is a ceiling. Fewer is fine. None means no block and no heading.
- Start from `weekly_block` in the output: it is already in this shape. Drop a line that breaks a rule above, and check each remaining line against `findings`.
- Each line names four things and nothing else: the item (kind, name, scope), its estimated per-turn load, when it was last used (`last used YYYY-MM-DD`, or `never in D days` with the real window; a rarely used item reads `used N in D days`), and one action.
- The action is one of: disable (a plugin, a connector, an MCP server), remove (a skill, command or subagent file), move to project level (it was only ever used in one project: name the project), trim (a memory file). Take it from the `action` field.
- Lines from `findings.single_project`, `findings.duplicates` and `findings.large_memory` may take a place in the five when they cost more than a never-used line. For a duplicate, name both places and say which copy wins (the `wins` field: the user copy for skills, the project copy for subagents, the nearest level for MCP servers). Two plugins that each bring a skill of the same name are not duplicates. For a large memory file, give its lines and its per-turn figure and say "trim, or move parts into path-scoped rules or a skill".
- After the lines, one sentence with the total: "Always on: about N tokens on every turn, about $X over D days at the cache read rate (estimate)." Leave out the dollars when `usd_cache_read` is null.
- Do not repeat a line from the last three weekly reports unless the item is still there and still unused; then end the line with `(repeat)`.

## Acting on it

Nothing here is applied by the review. A line in this block may also be written up as a proposal under "Proposed changes (not applied)" when it is among the five most valuable changes of the week. Write it out in full like any other proposal:

- Disable a plugin: `claude plugin disable NAME`, or the exact `enabledPlugins` entry set to `false` and the settings file it goes in.
- Remove an MCP server: `claude mcp remove NAME`, with the scope it is set at. For a connector, name the place it was added.
- Remove a skill, command or subagent: the exact folder or file path. Say "move it out, do not delete it" so it can be put back.
- Move to project level: the path to move from and the project folder to move to (`.claude/skills/NAME/` in that project).
- Trim a memory file: say which parts to cut or move. Do not rewrite the file.

None of these is a kind of fix `coach/applyfix.py` applies (it handles a CLAUDE.md line, a permission rule and a hook only), so the coach does not make them: after the person's yes, give the exact command or path for them to run, one change at a time. Never remove or disable anything on your own.
