# Menu bar and Overview mockups

Design history for the menu bar app. They are static HTML sketches made in a design tool, not part of the app.

| File | What it shows |
|---|---|
| Prototype.dc.html | Clickable prototype on one Mac screen: the menu bar item opens the panel, the panel's links open the coach page, the period buttons drive both. |
| PanelMM.dc.html | The panel the app now draws, in the Mac Monitor style. |
| Extras.dc.html | An earlier panel, with limits and tips. |
| Main.dc.html | The first, leaner panel (kept for the period buttons and the line graph maths). |
| MenuBar.dc.html | The menu bar item states. |
| docs/mockups/Page.dc.html | The Overview tab of the coach page (stays in docs/mockups/, which ships). |
| canvas.json | Where each board sits on the canvas. |

All numbers are samples. The data each view needs is in menubar/CONTRACT.md.

Decisions (Nov 23, 2026):
- A local Mac app: it reads `~/.claude/coach/live.json` and sends nothing anywhere.
- Trends over time are line graphs. Rows that compare items (activities, projects, MCP servers) carry a heat bar.
- "By activity" lists every activity type with spend: top 5 in the panel, all on the Overview.
- The Overview gains an "MCP servers" block (calls, tools, errors per server).
- Limits come only from what Claude Code gives the status line; the app never reads the Claude Code login token.
- Activity types and the first-try edits rule follow CodeBurn (MIT licence, github.com/getagentseal/codeburn), rewritten in the coach's own Python.
