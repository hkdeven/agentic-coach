# Daily recap rules

Used by the `coach` skill for one day (yesterday, or a date) and for a range of dates. The ground rules in `SKILL.md` apply in full. This file adds the steps and the report shape.

You are writing a short coaching recap of how the user worked with Claude Code on the day or days in the plan. The goal is to make them better at using Claude Code: point at what cost time or tokens and say what to do instead, naming the feature that fits (a skill, a plugin, a hook, a subagent, a Workflow, plan mode, a scheduled task, a permissions allow rule, a CLAUDE.md line, memory, a connector).

## Steps

1. The plan printed by `recap.py` names the files. The digest and the findings are already built. Read both in full (`digest` and `findings` in the plan). The findings are deterministic hints, not the answer: check each against the digest, drop the ones that are noise, and add anything the detectors missed that you can see in the prompts (the same instruction typed again, a task done by hand that a skill or scheduled task already covers, work done in series that could have run as parallel agents, a long session that mixed unrelated tasks).

2. Read what is installed so that advice fits this machine: `~/.claude/settings.json` (hooks, permissions, enabled plugins), the folder names in `~/.claude/skills`, and `~/.claude/plugins/installed_plugins.json`. Recommend an installed tool when one fits. Recommend a Claude Code feature in general when none is installed. Do not recommend something the digest shows they already do.

3. Read the files listed under `last_three_daily_reports` in the plan, if any. Do not repeat an item given in one of those reports unless it happened again, and then say that it is a repeat.

4. Rank by cost, most costly first. Cost means wasted tokens, wasted minutes, or rework. Skip scheduled-task sessions unless one failed or was wasteful.

5. Write the file named `report` in the plan, in exactly this shape. The first line is the `title` from the plan (`# Coach: YYYY-MM-DD` for one day, `# Coach: YYYY-MM-DD to YYYY-MM-DD` for a range):

   ```
   # Coach: YYYY-MM-DD

   1. What happened: one line with session id, project, time and the number.
      Do instead: one line, the concrete action or feature.
   2. ...
   ```

   At most five items. For a range, give the date with the time. If nothing is notable the body is the single line `Nothing notable on YYYY-MM-DD.` (for a range: `Nothing notable from YYYY-MM-DD to YYYY-MM-DD.`).

6. Reply with the items only: the same lines as the report, nothing before or after. If nothing was notable, reply with that one line.
