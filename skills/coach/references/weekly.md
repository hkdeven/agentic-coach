# Weekly review rules

Used by the `coach` skill for `week`. The ground rules in `SKILL.md` apply in full. This file adds the steps and the report shape.

You are reviewing how the user worked with Claude Code over the last 7 days and proposing concrete changes that would make next week cheaper and faster. You propose. You do not apply. Applying waits for the user's yes.

Read only, apart from the report file named in the plan and the page steps below (rebuild, and publish or notify only when the plan says so). Do not edit `~/.claude/settings.json`, any CLAUDE.md, any skill, hook, plugin or repo. Do not create scheduled tasks. Five observations and five proposals are ceilings, not targets. If the week was clean, the report is one line that says so; still write it and still rebuild the page.

## Steps

1. The plan printed by `recap.py` names the files. The digest and the findings for the last 7 days are already built. Read both in full (`digest` and `findings` in the plan). Treat the findings as hints. Confirm each against the digest, drop noise, merge duplicates across sessions into one pattern, and look for what the detectors cannot see: the same instruction retyped in different words, manual routines that should be a skill or a scheduled task, serial work that should have been parallel agents or a Workflow, sessions that mixed unrelated tasks, features never used (see `totals.plan_mode_used`, `totals.skills_used`, `totals.permission_modes`, `inventory`).

2. Read what is installed, so proposals fit this machine and do not duplicate what exists: `~/.claude/settings.json` (hooks, permissions, enabledPlugins), `~/.claude/CLAUDE.md`, the folders in `~/.claude/skills`, `~/.claude/plugins/installed_plugins.json`, and the names in `~/.claude/scheduled-tasks`. Prefer an installed tool when one fits. Otherwise draw on Claude Code features in general: skills, plugins, hooks, subagents, Workflows, plan mode, scheduled tasks, permission allow rules, CLAUDE.md rules, memory, connectors.

3. Trend. Read the files listed in the plan under `earlier_weekly_reports`, `baseline_reports` (the starting point, if any) and `daily_reports_this_week`. For each pattern say whether it is new, repeating, better or worse, with the numbers from both weeks. Do not repeat an item from the last three weekly reports unless it happened again, and then say that it is a repeat. If an earlier proposal was adopted (you can see it in settings, CLAUDE.md or skills), say whether the pattern it targeted went away. Where `earlier_weekly_digests` lists a file, compare `totals` directly with the newest one.

4. Rank by cost, most costly first. Skip scheduled-task sessions unless one failed or was wasteful.

5. Write the file named `report` in the plan, in this shape. The first line is the `title` from the plan:

   ```
   # Coach weekly: YYYY-MM-DD

   ## Week in numbers
   One or two lines: sessions, prompts, active hours, output tokens, errors, corrections,
   compactions, each with last week's figure beside it when known.

   ## Cost by project
   (the plan's `cost_block`, copied exactly: a table of project, week tokens, week cost
   and all-time cost, a Total row, and one note line)

   ## What cost the most
   1. What happened: one line with sessions, dates and the number.
      Do instead: one line.
      Trend: new, repeat, better or worse, with numbers.
   (at most five, most costly first)

   ## Proposed changes (not applied)
   ### P1: short name
   Why: the evidence in one line (sessions, dates, counts).
   Change: the full text, ready to paste. See the forms below.
   Where: the exact file or place it goes.
   (at most five, highest value first)
   ```

   The plan's `cost_block` already starts with its own `## Cost by project` heading: paste it once, as it is, between "Week in numbers" and "What cost the most". In "Week in numbers" give the week's cost beside the tokens (from `totals.cost` in the digest), and in every item that quotes tokens quote the cost in the same line, for example `3.0M output tokens, about $45`.

   The coach page (`coach/build_site.py`) parses this file, so keep the four `##` headings, the `### P1: name` form and the labels `What happened:`, `Do instead:`, `Trend:`, `Why:`, `Change:`, `Where:` exactly as written, each at the start of its line with no bold around it. Start every `Trend:` line with one of the four words new, repeat, better, worse. Put the text to paste for `Change:` in a fenced code block on the lines right after it (one sentence on what it does may follow `Change:` on the same line), so the page can offer it for copying.

   Write every proposal out in full so that a yes is all it needs:
   - CLAUDE.md rule: the exact line, and which file (`~/.claude/CLAUDE.md` or a project's).
   - Hook: the exact JSON block for `settings.json`, with event, matcher and command, and one line on what it does. Check the schema against https://code.claude.com/docs/en/hooks before you write it.
   - Skill: the name, the full `description` line that decides when it triggers, and an outline of its steps.
   - Permission allow rule: the exact entry, for example `"Bash(gh pr view:*)"`, and the file it goes in. Only for read-only commands.
   - Plugin, connector or scheduled task: the exact name, and for a scheduled task its schedule and prompt.
   - Workflow or habit: the exact sentence to type next time.

   Do not propose anything that needs a push, a deploy, or a change to another team's code.

6. Rebuild the coach page as "After the report" in `SKILL.md` says, with the `page_build` arguments from the plan. Then, only if the plan's `page_url` is set, publish it to that existing link exactly as that section says: Artifact `read` of the url first, then publish `page_file` with the same `url`, never without the `url`, no `icon`, no `title`. If publishing fails, say so in your reply and carry on. These actions and the notification in step 7 are allowed in addition to writing the report.

7. If the plan's `notify` is true, send one push notification. One line under 200 characters, no markdown: `Coach weekly review ready: N proposals waiting for your yes.` If the week was clean, send nothing. If `notify` is false, skip this step.

8. Reply with the page link on its own line (only if `page_url` is set), then one line with the week's cost and the top three projects by cost, then the "What cost the most" lines, then the list of proposals as `P1: name` with one line each, then the single question: `Apply which? (for example: P1 P3, all, none)`. Nothing else. If the week was clean, reply with one line that says so.

9. Stop there. If the user later answers with a yes for specific proposals, apply only those, show each change as it lands, and leave the rest untouched.
