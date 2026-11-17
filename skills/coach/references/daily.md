# Daily recap rules

Used by the `coach` skill for one day (yesterday, or a date) and for a range of dates. The ground rules in `SKILL.md` apply in full. This file adds the steps and the report shape.

You are writing a short coaching recap of how the user worked with Claude Code on the day or days in the plan. The goal is to make them better at using Claude Code: point at what cost time or tokens and say what to do instead, naming the feature that fits (a skill, a plugin, a hook, a subagent, a Workflow, plan mode, a scheduled task, a permissions allow rule, a CLAUDE.md line, memory, a connector).

## Steps

1. The plan printed by `recap.py` names the files. The digest and the findings are already built. Read both in full (`digest` and `findings` in the plan). The findings are deterministic hints, not the answer: check each against the digest, drop the ones that are noise, and add anything the detectors missed that you can see in the prompts (the same instruction typed again, a task done by hand that a skill or scheduled task already covers, work done in series that could have run as parallel agents, a long session that mixed unrelated tasks).

2. Read what is installed so that advice fits this machine: `~/.claude/settings.json` (hooks, permissions, enabled plugins), the folder names in `~/.claude/skills`, and `~/.claude/plugins/installed_plugins.json`. Recommend an installed tool when one fits. Recommend a Claude Code feature in general when none is installed. Do not recommend something the digest shows they already do.

3. Read the files listed under `last_three_daily_reports` in the plan, if any. Do not repeat an item given in one of those reports unless it happened again, and then end its `What happened:` line with `(repeat of YYYY-MM-DD)`, the day you first gave it. The coach page reads that marker.

4. Rank by cost, most costly first. Cost means wasted tokens, wasted minutes, or rework. Dollars make this concrete: each session in the digest has `cost` (and `subagents.cost`), the digest has `totals.cost` and a `projects` list sorted by cost. Whenever an item quotes tokens, quote the cost in the same line, for example `3.0M output tokens, about $45`, using those figures only. Skip scheduled-task sessions unless one failed or was wasteful.

5. Write the file named `report` in the plan, in exactly this shape. The first line is the `title` from the plan (`# Coach: YYYY-MM-DD` for one day, `# Coach: YYYY-MM-DD to YYYY-MM-DD` for a range):

   ```
   # Coach: YYYY-MM-DD

   1. What happened: one line with session id, project, time and the number.
      Do instead: one line, the concrete action or feature.
   2. ...
   ```

   Keep the labels `What happened:` and `Do instead:` exactly as written, with no bold or other markup around them, and write session ids as the bare first 8 characters. The coach page (`coach/build_site.py`) parses these lines.

   After the items, leave one blank line and end the report with the plan's `totals_line`, copied exactly. It starts with the label `Totals:` and gives the tokens and cost for the day, the top three projects by cost, the week to date and the all-time figure with its since-date. It is not an item and does not count toward the five. If nothing was notable, the report is the "Nothing notable" line, a blank line, then the `totals_line`.

   At most five items. For a range, give the date with the time. If nothing is notable the body is the single line `Nothing notable on YYYY-MM-DD.` (for a range: `Nothing notable from YYYY-MM-DD to YYYY-MM-DD.`).

6. Rebuild the coach page as "After the report" in `SKILL.md` says, with the `page_build` arguments from the plan (they include `--card` for a daily run). It writes the page file and prints one JSON line with the latest daily's `items`. This and the next two steps are the only actions allowed beyond writing the report.

7. If the plan's `page_url` is set, publish the page to that existing link exactly as "After the report" says: Artifact `read` of the url first, then publish `page_file` with the same `url`, never without the `url`, no `icon`, no `title`. If publishing fails, say so in your reply and carry on. If `page_url` is null, skip this step.

8. If the plan's `notify` is true, send one push notification. One line under 200 characters, no markdown: `Coach: N tips ready. Top: ` followed by the first item's Do instead line, shortened to fit. If nothing was notable, send nothing. If `notify` is false, skip this step.

9. Reply with the items only: the same lines as the report (the `Totals:` line included), nothing before or after. If `page_url` is set, add that link on its own line after the items; if it is null, say nothing about the page. If nothing was notable, reply with that one line.
