# For maintainers

```
coach/extract.py       transcripts to a digest (JSON) for a date range
coach/signals.py       digest to findings {pattern, severity, evidence, session, suggestion}
coach/recap.py         what the skill runs: picks the dates, writes digest and findings, prints the plan
coach/build_site.py    builds the coach page from the reports and digests; --card prints one JSON line
coach/config.py        show or change config.json
coach/prices.json      API list prices per model, with source and date
coach/pricing.py       prices token counts; unknown models are unpriced, never guessed
coach/projects.py      works out a session's project name; the exclude and pin matching
coach/scope.py         every "leave this out" rule in one place (exclude, account, dates)
coach/accounts.py      which account a session ran under (short forms only); the SessionStart hook
coach/ledger.py        the all-time ledger: backfill, show, and the totals for any period
coach/followthrough.py the advice log: new or repeat, the weekly follow-through block
coach/evidence.py      locators, session slices, candidates and verdicts
coach/compare.py       two periods side by side, snapshots
coach/config_audit.py  what is set up against what was used
coach/applyfix.py      preview, apply, undo and list for the three kinds of fix
coach/statusline.py    the status line, and its install and remove
coach/setup_check.py   read-only check of schedules, old hooks, old reports, page, status line, exclusions;
                       live-agent status, install and uninstall for the live feed runner
coach/live.py          the live feed: the Overview's figures and live.json (docs/LIVE-FEED.md)
coach/activity.py      files each turn under an activity type, and the first-try edit rule
coach/estimates.py     Claude's "Estimate" and "estimate X, actual Y" lines: pairing and totals by model
coach/nudge.py         Stop hook
coach/permlog.py       PermissionRequest hook: logs the prompt, decides nothing
coach/common.py        shared helpers and the config reader
skills/coach/          SKILL.md, plus references/: daily.md and weekly.md (the two flows), setup.md,
                       followthrough.md, evidence.md, compare.md, configaudit.md, applyfix.md, statusline.md
hooks/hooks.json       the three hooks, 5 second limit each
docs/GUIDE.md          how each part works; docs/SETTINGS.md: every setting and where things are written
docs/LIVE-FEED.md      the Overview's figures, the live.json schema and field rules
docs/mockups/          design mockups of the coach page (sample data only)
tests/                 unittest cases on synthetic data, one file per area
menubar/               the macOS menu bar app (Swift package): see menubar/README.md
```

Commands, from the repo root:

```
python3 -m unittest discover -s tests -v
swift test --package-path menubar               # the menu bar app; draws nothing on screen

python3 coach/recap.py                          # yesterday
python3 coach/recap.py week
python3 coach/extract.py --since 2026-11-01 --until 2026-11-08 --project demo
python3 coach/extract.py --session 1a2b3c4d --since 30d --pretty
python3 coach/signals.py digest.json --format text
python3 coach/signals.py --list
```

`--since` and `--until` take `YYYY-MM-DD`, `YYYY-MM-DDTHH:MM`, `today`, `yesterday`, `now` or `Nd` (N days ago at midnight), in local time; `--until` is exclusive. Only records inside the range are counted, so a session that runs for a week adds just that day's slice to a daily digest. If the digest is over `--max-bytes`, prompts are shortened first, then the smallest sessions collapse into `minor_sessions`.

Environment: `COACH_HOME` moves the data folder (tests use it); `CLAUDE_CONFIG_DIR` is honoured for the Claude folder.

The hooks: `nudge.py` prints either nothing or `{"systemMessage": "Coach: ..."}` and always exits 0. `systemMessage` is shown to the user and does not block the stop or go back to the model. Do not set `async` on it: an async hook's message goes to the model, not to the person. `permlog.py` prints nothing, so the normal permission prompt is unchanged. Hook schema: https://code.claude.com/docs/en/hooks.

Adding a detector: in `coach/signals.py`, write one function and register it.

```python
@detector("my_pattern", cheap=False)          # scope="digest" for cross-session checks
def my_pattern(s):
    if s.get("turns", 0) > 200:
        return [finding("my_pattern", "low", "what happened", _sid(s), "what to do instead")]
    return []
```

Set `cheap=True` only if it needs nothing beyond the counters `nudge.py` keeps (repeated failures, corrections, big results, compactions, interruptions, denials, peak context). Thresholds are in the `T` table at the top of the file.

Updating prices: edit `coach/prices.json` from the pricing page named in its `source` field and set `retrieved` to the day you did it. Never fill a price from memory. Nothing else needs rebuilding: the ledger stores tokens, not dollars.

Releasing: bump `version` in `.claude-plugin/plugin.json` and in `.claude-plugin/marketplace.json` together (a test checks they match).

The `SessionStart` hook (`accounts.py hook`, on `startup`, `resume` and `clear`) appends one line per session start to `state/session-accounts.jsonl`: the session id, the time, and the short forms of the account and organisation from `.claude.json`. It prints nothing and always exits 0.

Where the 1.2.0 features hook into a recap: `recap.py` adds to its plan the paths of `followthrough.py` and `evidence.py`, the reference files, `unattended` and `fixes`, and for a weekly run the comparison file and the saved audit. `skills/coach/references/daily.md` and `weekly.md` are the single flow; the other references are opened from the step that names them.
