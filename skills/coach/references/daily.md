# Daily recap rules

Used by the `coach` skill for one day (yesterday, or a date) and for a range of dates. The ground rules in `SKILL.md` apply in full. This file adds the steps and the report shape.

You are writing a short coaching recap of how the user worked with Claude Code on the day or days in the plan. The goal is to make them better at using Claude Code: point at what cost time or tokens and say what to do instead, naming the feature that fits (a skill, a plugin, a hook, a subagent, a Workflow, plan mode, a scheduled task, a permissions allow rule, a CLAUDE.md line, memory, a connector).

## Steps

The plan printed by `recap.py` names every file and script used below. `FT` stands for `python3` plus the plan's `followthrough` path, `EV` for `python3` plus the path at the start of the plan's `evidence` list. Open the other references (their paths are in the plan under `references`) at the step that names them.

1. **Read the plan's inputs.** The digest and the findings are already built. Read both in full (`digest` and `findings` in the plan). The findings are deterministic hints, not the answer: check each against the digest, drop the ones that are noise, and add anything the detectors missed that you can see in the prompts (the same instruction typed again, a task done by hand that a skill or scheduled task already covers, work done in series that could have run as parallel agents, a long session that mixed unrelated tasks).

2. **Read what is installed** so that advice fits this machine: `~/.claude/settings.json` (hooks, permissions, enabled plugins), the folder names in `~/.claude/skills`, and `~/.claude/plugins/installed_plugins.json`. Recommend an installed tool when one fits. Recommend a Claude Code feature in general when none is installed. Do not recommend something the digest shows they already do.

3. **New or repeat (follow-through).** Run `FT annotate --findings <the plan's findings>`, adding `--text "<your planned Do instead line>"` for each item of your own, as `followthrough.md` steps 1 to 3 say. Keep each row's `key`, `status` and `marker`. Read the files under `last_three_daily_reports` too: a `RECURRING` candidate goes in only if it happened again in this period.

4. **Rank by cost**, most costly first. Cost means wasted tokens, wasted minutes, or rework. Dollars make this concrete: each session in the digest has `cost` (and `subagents.cost`), the digest has `totals.cost` and a `projects` list sorted by cost. Whenever an item quotes tokens, quote the cost in the same line, for example `3.0M output tokens, about $45`, using those figures only. Skip scheduled-task sessions unless one failed or was wasteful.

5. **Verify the top candidates (evidence).** Follow `evidence.md` steps 1 to 5: run the plan's `evidence` command (`python3` plus that list) to build the candidates, add your own items with `EV find`, verify at most eight, most costly first, and merge the verdicts. From here on work only from the merge's `items`: a dropped item never appears anywhere, a corrected item uses the corrected wording.

6. **Write the file named `report` in the plan**, in exactly this shape. The first line is the `title` from the plan (`# Coach: YYYY-MM-DD` for one day, `# Coach: YYYY-MM-DD to YYYY-MM-DD` for a range):

   ```
   # Coach: YYYY-MM-DD

   1. What happened: one line with the project and the number (session 1a2b3c4d, turn 41, 13:39) (repeat of YYYY-MM-DD, given N times)
      Do instead: one line, the concrete action or feature.
   2. ...

   Totals: (the plan's totals_line)
   Checked: N items against the transcripts.
   ```

   - Two lines per item, never more. At most five items. For a range, give the date with the time.
   - `What happened:` ends with the item's evidence tag in round brackets, exactly as `evidence.py` gave it (`session 1a2b3c4d, turn 41, 13:39`, or `turns 41 to 47, 13:39 to 14:02`, or `session 1a2b3c4d, evidence no longer on disk`). The tag carries the session id and the time, so do not write them a second time. After the tag, only for a `RECURRING` row, comes its `marker` from step 3, for example `(repeat of 2026-11-10, given 3 times)`. A `NEW` item has no marker.
   - Do not write a per-item `Evidence:` line: the page works out the command to open the evidence from the tag.
   - After the items, one blank line, then the plan's `totals_line`, copied exactly. It starts with the label `Totals:`.
   - The last line is `Checked: N items against the transcripts.`, with the merge's `totals_line` added only when something was dropped: `Checked: 4 items against the transcripts, 2 candidates dropped on verification.` Neither line is an item or counts toward the five.
   - Keep the labels `What happened:`, `Do instead:`, `Totals:` and `Checked:` exactly as written, at the start of their line, with no bold or other markup. The coach page (`coach/build_site.py`) parses these lines.
   - If nothing is notable after verification, the body is the single line `Nothing notable on YYYY-MM-DD.` (for a range: `Nothing notable from YYYY-MM-DD to YYYY-MM-DD.`), a blank line, the `totals_line`, and the `Checked:` line only if candidates were dropped.

7. **Record the advice.** Run `FT record --report <the plan's report> --key 1=<key> --key 2=<key>` with the keys kept in step 3, as `followthrough.md` step 4 says. Run it for a "Nothing notable" report too.

8. **Rebuild the coach page** as "After the report" in `SKILL.md` says, with the `page_build` arguments from the plan (they include `--card` for a daily run). It writes the page file and prints one JSON line with the latest daily's `items`.

9. **Publish, only if the plan's `page_url` is set**, to that existing link exactly as "After the report" says: Artifact `read` of the url first, then publish `page_file` with the same `url`, never without the `url`, no `icon`, no `title`. If publishing fails, say so in your reply and carry on. If `page_url` is null, skip this step.

10. **Notify, only if the plan's `notify` is true.** One push notification, one line under 200 characters, no markdown: `Coach: N tips ready. Top: ` followed by the first item's Do instead line, shortened to fit. If nothing was notable, send nothing.

11. **Reply with the items only**: the same lines as the report (the `Totals:` and `Checked:` lines included), nothing before or after. Then, when at least one item has a tag on disk, one line: `Evidence: python3 <the real plugin folder>/coach/evidence.py show <session> --turn <turn>` (the numbers are in each item's tag). If `page_url` is set, add that link on its own line last; if it is null, say nothing about the page. If nothing was notable, reply with that one line.

Steps 3, 5, 7, 8, 9 and 10 are the only actions allowed beyond reading and writing the report. If the person then asks to apply an item ("add that rule for me"), follow `applyfix.md`; never in a run marked `unattended`.
