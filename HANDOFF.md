# agentic-coach handoff

## 2026-11-27: 1.5.1, tip and totals format

Asked by Deven: drop cost figures and the session/turn/time tag from every daily and weekly tip; show totals as a list, one item per total.

Done on branch `coach-tip-format` (commit 6cd1b66, worktree `~/Code/Personal/.worktrees/coach-tip-format`):
- Tips no longer carry cost or the evidence tag. The tag moves to its own `Evidence:` line in the report file, so the page can still fold the excerpt under each item; the page and chat reply do not show it.
- Totals and Estimates are bullet lists in reports, replies and the page (`totals_block`, `estimates_block` in the plan; `totals_line` kept). Weekly "Week in numbers" is a list; the cost-by-project table is unchanged.
- Old reports still render; inline tags and cost phrases are stripped from displayed tip text.
- Version 1.5.1 in plugin.json and marketplace.json. 728 tests pass. Page checked on the Today tab from a copy of the real reports.

Known limits: older weekly reports that name a session in plain text keep it; old `Do instead` lines are left as written; menubar app version not bumped.

## Decisions made while away
- Kept the evidence tag in the report file on a separate `Evidence:` line instead of deleting it, so the page's evidence fold keeps working.
- Released as 1.5.1 on a branch only; nothing pushed.

## Released 2026-11-27
- PR hkdeven/agentic-coach#8 merged (1a8756f); installed plugin updated to 1.5.1. Next scheduled daily and weekly runs use the new format.

## Waiting for Deven
- Nothing.
