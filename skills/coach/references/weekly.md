# Weekly review rules

Used by the `coach` skill for `week`. The ground rules in `SKILL.md` apply in full. This file adds the steps and the report shape.

You are reviewing how the user worked with Claude Code over the last 7 days and proposing concrete changes that would make next week cheaper and faster. You propose. You do not apply. Applying waits for the user's yes.

Read only, apart from the report file named in the plan, the coach's own scripts named below (they write only under `~/.claude/coach/`) and the page steps (rebuild, and publish or notify only when the plan says so). Do not edit `~/.claude/settings.json`, any CLAUDE.md, any skill, hook, plugin or repo yourself: a fix lands only through `coach/applyfix.py`, after a yes for that fix (step 13). Do not create scheduled tasks. Five follow-through lines, five observations, five "set up but unused" lines and five proposals are ceilings, not targets. If the week was clean, the report is one line that says so; still write it and still rebuild the page.

## Steps

The plan printed by `recap.py` names every file and script used below. `FT` stands for `python3` plus the plan's `followthrough` path, `EV` for `python3` plus the path at the start of the plan's `evidence` list, `applyfix` for `python3` plus the plan's `fixes.cli`. Open the other references (their paths are in the plan under `references`) at the step that names them.

1. **Read the plan's inputs.** The digest and the findings for the last 7 days are already built. Read both in full (`digest` and `findings` in the plan). Treat the findings as hints. Confirm each against the digest, drop noise, merge duplicates across sessions into one pattern, and look for what the detectors cannot see: the same instruction retyped in different words, manual routines that should be a skill or a scheduled task, serial work that should have been parallel agents or a Workflow, sessions that mixed unrelated tasks, features never used (see `totals.plan_mode_used`, `totals.skills_used`, `totals.permission_modes`, `inventory`).

2. **Read what is installed and what the coach already changed**, so proposals fit this machine and do not duplicate what exists: `~/.claude/settings.json` (hooks, permissions, enabledPlugins), `~/.claude/CLAUDE.md`, the folders in `~/.claude/skills`, `~/.claude/plugins/installed_plugins.json`, the names in `~/.claude/scheduled-tasks`, and `applyfix list --json`. Do not propose again a fix whose status is `applied`; one with status `undone` was tried and taken back, so propose it again only if the person asks. Prefer an installed tool when one fits. Otherwise draw on Claude Code features in general: skills, plugins, hooks, subagents, Workflows, plan mode, scheduled tasks, permission allow rules, CLAUDE.md rules, memory, connectors.

3. **Follow-through, first.** Run `FT annotate --findings <the plan's findings>` (with `--text` for items of your own) and keep each row's `key`, `status` and `trend`. Then run `FT weekly --label <the plan's label>`, adding `--also KEY` for each `RECURRING` key you are about to give again, as `followthrough.md` steps 5 to 7 say. Keep its `block` and `lines` exactly as given: the statuses and numbers are not yours to change.

4. **Comparison with the previous week.** If the plan's `compare` is a file, read it and apply the rules of `compare.md` (sections "Steps", "Honesty rules" and "In the weekly review"). It gives last week's figure beside this week's for "Week in numbers", the lines of the "Before and after" block, and the only source for the words better and worse in a `Trend:` line that has no follow-through row. Also read the files under `earlier_weekly_reports`, `baseline_reports` and `daily_reports_this_week`: do not repeat an item from the last three weekly reports unless it happened again.

5. **Set up but unused.** If the plan's `config_audit` is a file, read its `always_on`, `findings` and `weekly_block` and apply `configaudit.md` ("What the figures mean", "Do not flag these", "The block"). Keep at most five lines. If it is null, leave the block out.

6. **Rank by cost**, most costly first. Skip scheduled-task sessions unless one failed or was wasteful.

7. **Verify the top candidates (evidence).** Follow `evidence.md` steps 1 to 5 (the plan's `evidence` command, `EV find` for your own items, at most eight verdicts, merge). From here on the items of "What cost the most" come only from the merge's `items`.

8. **Write the file named `report` in the plan**, in this shape. The first line is the `title` from the plan. A section with nothing to say is left out whole, heading included:

   ```
   # Coach weekly: YYYY-MM-DD

   ## Follow-through
   (the `block` lines from step 3, as given, at most five; left out when the block is empty)

   ## Week in numbers
   One or two lines: sessions, prompts, active hours, output tokens and cost, errors, corrections,
   compactions, each with last week's figure beside it when known.
   Estimates: (the plan's estimates_line, only when it is not null)
   Checked: N items against the transcripts.

   ## Cost by project
   (the plan's `cost_block`, copied exactly: a table of project, week tokens, week cost
   and all-time cost, a Total row, and one note line)

   ## Before and after
   (at most six lines from step 4: the two periods, then the real movements, one per line)

   ## What cost the most
   1. What happened: one line with dates and the number (session 1a2b3c4d, turn 41, 13:39)
      Do instead: one line.
      Trend: new, repeat, better or worse, with numbers.
   (at most five, most costly first)

   ## Set up but unused
   - skill NAME (scope): about N tokens per turn (estimate), never in D days. Action: one action.
   (at most five lines from step 5, then the one sentence that starts `Always on:`)

   ## Proposed changes (not applied)
   ### P1: short name
   Why: the evidence in one line (sessions, dates, counts).
   Change: the full text, ready to paste. See the forms below.
   Where: the exact file or place it goes.
   (at most five, highest value first)
   ```

   - **Follow-through**: the heading and the lines exactly as `FT weekly` gave them. Each line starts with one status: `worse`, `recurred`, `same`, `better`, `not seen again`, `cannot measure` or `no data`.
   - **Week in numbers**: give the week's cost beside the tokens (from `totals.cost` in the digest). When the plan's `estimates_line` is not null, copy it exactly as the line before `Checked:` (it starts with `Estimates:`: per model, the pieces of work with a stated actual, the minutes estimated and taken, and the overrun); when it is null, leave it out. Its last line is `Checked: N items against the transcripts.`, with `, M candidates dropped on verification` added only when the merge dropped something.
   - **Cost by project**: the plan's `cost_block` already starts with its own heading. Paste it once, as it is.
   - **Before and after**: written as `compare.md` "The written comparison" says (first line the two periods with their dates and session counts, then one line per real movement with both numbers, the basis, the percent and the verdict word from `state`). If the file says `too_little_data`, the block is that first line plus the reason, with no percent and no verdict. If the plan has no `compare`, leave the block out.
   - **What cost the most**: each `What happened:` line ends with its evidence tag in round brackets, as in the daily. Every item that quotes tokens quotes the cost in the same line. Start every `Trend:` line with one of the four words new, repeat, better, worse: for a `RECURRING` row use its `trend` from step 3 (`Trend: repeat since 2026-11-10, given 3 times; ...`); when the item has a follow-through row, the two numbers are that row's; say better or worse only where a follow-through row or a compare row (`improvement` or `regression`) says so. If a fix for the pattern was applied and it still happened, say the change did not hold. Do not write a per-item `Evidence:` line.
   - **Set up but unused**: bullet lines in the shape `configaudit.md` gives, then its one total sentence. Every figure says "about" or "estimate".
   - When a proposal carries out the advice of an item in "What cost the most", end that item's `Do instead:` line with the proposal's id in brackets, for example `(P1)`. That is what ties an applied proposal back to the advice in next week's follow-through.
   - **Proposed changes**: keep the `### P1: name` form and the labels `Why:`, `Change:`, `Where:` exactly as written. Put the text to paste for `Change:` in a fenced code block on the lines right after it (one sentence on what it does may follow `Change:` on the same line), so the page can offer it for copying.
   - The coach page (`coach/build_site.py`) parses this file: keep the `##` headings and every label exactly as written, each at the start of its line with no bold around it.

   Write every proposal out in full so that a yes is all it needs:
   - CLAUDE.md rule: the exact line, and which file (`~/.claude/CLAUDE.md` or a project's).
   - Hook: the exact JSON block for `settings.json`, with event, matcher and command, and one line on what it does. Check the schema against https://code.claude.com/docs/en/hooks before you write it.
   - Skill: the name, the full `description` line that decides when it triggers, and an outline of its steps.
   - Permission allow rule: the exact entry, for example `"Bash(gh pr view *)"`, and the file it goes in. Only for read-only commands, and only in a shape `applyfix.md` accepts.
   - Plugin, connector or scheduled task: the exact name, and for a scheduled task its schedule and prompt. A line from "Set up but unused" may become a proposal, written out as `configaudit.md` "Acting on it" says.
   - Workflow or habit: the exact sentence to type next time.

   Do not propose anything that needs a push, a deploy, or a change to another team's code.

9. **Record the advice.** Run `FT record --report <the plan's report> --key 1=<key> ...` with the keys from step 3, as `followthrough.md` step 4 says.

10. **Rebuild the coach page** as "After the report" in `SKILL.md` says, with the `page_build` arguments from the plan. Then, only if the plan's `page_url` is set, publish it to that existing link exactly as that section says: Artifact `read` of the url first, then publish `page_file` with the same `url`, never without the `url`, no `icon`, no `title`. If publishing fails, say so in your reply and carry on.

11. **Notify, only if the plan's `notify` is true.** One push notification, one line under 200 characters, no markdown: `Coach weekly review ready: N proposals waiting for your yes.` If the week was clean, send nothing.

12. **Reply**, in this order and with nothing else: the page link on its own line (only if `page_url` is set); the follow-through lines; one line with the week's cost and the top three projects by cost; the "What cost the most" lines; the list of proposals as `P1: name` with one line each; then the single question `Apply which? (for example: P1 P3, all, none)`. If the week was clean, reply with one line that says so. In a run marked `unattended` (the plan's `unattended` is true or `fixes.apply_allowed` is false), leave the question out and stop here: nothing is previewed, applied or undone.

13. **Stop there.** If the user later answers with a yes for specific proposals, follow `applyfix.md`: only the proposals they picked; a CLAUDE.md line, a permission rule or a hook goes through `applyfix preview`, a yes for that one fix after its diff, then `applyfix apply <key> --person-said-yes --plan <the plan's plan_file>`, one at a time; anything else (a skill, a plugin, a connector, a scheduled task, removing or moving a file) is not applied by the coach: give the text to paste or the command to run. After each fix that landed, or each change the person confirms they made by hand, run `FT applied --report <the weekly report> --proposal Pn`, so next week's follow-through can say whether the number moved. Leave the rest untouched.
