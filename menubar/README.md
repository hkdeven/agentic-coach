# Claude Coach menu bar app

A small macOS menu bar app for the coach (the agentic-coach plugin). It shows today's spend in the menu bar and a panel, in the look of the Mac Monitor panel, with a tile for each figure of the chosen period, the Tokens line graph, what is running now and cost by activity.

It reads one file, `~/.claude/coach/live.json`, which the coach writes, and shows the coach page in a window of its own. It has no parsing logic of its own and makes no network calls. Cost is API-equivalent, not a bill.

Needs macOS 14 or later, and the plugin installed and set up with its live feed runner turned on (`/agentic-coach:coach setup`, see [docs/LIVE-FEED.md](../docs/LIVE-FEED.md#turning-it-on)). The runner writes `live.json` once a minute and installs the launcher the coach window uses to rebuild the page.

## Build

```sh
sh menubar/build-app.sh
```

This makes `menubar/build/AgenticCoach.app`, unsigned, for use on this Mac.

To build and test by hand:

```sh
swift build --package-path menubar
swift test --package-path menubar
```

The tests use Swift Testing.

### What the build needs

Xcode, selected (`xcode-select -p` points inside Xcode.app). That is the normal path and needs nothing else.

### Without Xcode (Command Line Tools only)

The macOS 27 SDK cannot build SwiftUI with the Command Line Tools alone: it declares `@State` as a macro, and the compiler plugin for it (`SwiftUIMacros`) ships only with Xcode, so the build stops with "plugin for module 'SwiftUIMacros' not found". Build against the macOS 26 SDK that comes with the same tools instead. `build-app.sh` does this by itself when no Xcode is selected. By hand:

```sh
SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX26.sdk swift build --package-path menubar
SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX26.sdk swift test --package-path menubar
```

Keep to one SDK path: right after a build with a different SDK path, `swift test` has been seen to stop once with "plugin for module 'TestingMacros' not found"; running it again passes.

## Run

```sh
open menubar/build/AgenticCoach.app
```

The mark and today's spend appear in the menu bar; there is no Dock icon. Click the item to open the panel. If the panel says "No coach data yet", run `/coach setup` in Claude Code.

The small gear at the end of the panel's header holds the two settings: Compact (the mark alone in the menu bar) and Launch at login. Quit is a button at the foot of the panel.

## The panel

The design is `menubar/mockups/PanelMM.dc.html`. It is 340 points wide with a 14 point inset, on macOS's solid control background (no translucency). The panel always has one definite size, because the popover is sized from it.

- Header: "Claude Coach", a status dot and "Live, updated 14:16:05". The dot is green while the feed is under 15 minutes old and orange after that, when the words read "Not live, updated 14:16" (or the date, once it is another day).
- Period control: Today, 7 days, 30 days, All time. The chosen one is an inverted pill.
- Coach tip, when there is one: a wide tile with the first tip, its advice, and "See all N tips in the coach window", which opens the window on Today. With no tip the tile is not drawn.
- Tiles, two to a row: Spend, Calls, Tokens with its line graph (wide), First-try edits, Cache hit, Corrections, Top project. A figure that moved against the period before says so in words with a colour ("Up 6 on yesterday"). All time has no period before; there the line says where its detail starts ("Detail from Oct 25") when that is later than the period.
- Running now: a wide tile for each running session, the one touched last first, with how full its context is on the right in its status colour. Four at most; with more, a line "N more in the coach window" opens the window on its Overview.
- By activity: the top five as plain rows with a thin heat bar, then "N more in the coach window", which opens the window on its Overview.
- "Open coach window" and "Quit".

Limits used is not in the panel. A limit at 90 percent or more still shows in the menu bar item, and all of them are on the page's Overview.

If the panel is taller than the screen it scrolls under its header.

### The popover

The panel is shown in a system popover under the menu bar item (an `NSStatusItem` with an `NSPopover`), the way the Mac Monitor app shows its panel. The popover gives it the system's round corners and arrow. It closes when you click elsewhere, and the panel's links and buttons close it before they open the coach window.

- No band, no see-through frame: the panel's solid colour is painted behind the whole popover, arrow included (`PanelBox` in `PanelPopover.swift`), resolved against plain light or dark.
- No spare room: the popover's size is set from the panel's own size and set again whenever the panel's height changes (another period, a session starting or stopping).
- The panel follows the Mac's light or dark setting, also while it is open.

## The coach page window

"Open coach window" and "N more in the coach window" open the coach page in a window titled "Claude Coach", inside the app, not in the browser. The activities link opens the page on its Overview.

- It opens 1240 points wide, so the Overview's six stat tiles fit on one line. A narrower size remembered from last time is widened to that on opening, where the window was left; on a screen narrower than 1240 points it takes the screen's whole visible width.
- There is one window. Opening it again brings it to the front. Closing it (the red button or Command W) leaves the app running in the menu bar.
- It shows the local page file, `<coach home>/site/index.html` (`$COACH_HOME` moves the coach home). The published web copy and `page_url` are not used and not changed.
- Each time it is opened the page is rebuilt by the coach itself: the app runs `python3 <coach home>/coach_live.py page`, the launcher that `/coach setup` installs, so the app holds no path to the coach code. Python 3 is looked for in three places, in this order: `/opt/homebrew/bin/python3`, `/usr/local/bin/python3`, then `/usr/bin/python3`. This is the full build with the trend scan: about half a second once the coach's caches exist, about 16 seconds the very first time on a Mac with many sessions. The build is stopped after three minutes.
- While it builds, the last page stays up with a small turning mark and "Updating". If the build fails, the last page stays up and a bar gives the reason with Try again. With no page yet, the window shows "Loading the coach page". Command R builds it again.
- The page may not reach the network from this window: every http, https and web socket request is blocked. The page asks for no web font; it is drawn in the system fonts. A link in the page that you click and that leads to another site opens in your default browser. The page's own tabs and hash links stay in the window. No other file on the Mac can be opened from it. The right-click menu holds Copy, Copy Link and Look Up only, and link previews are off.
- The page file has no doctype or charset of its own, so the app adds both in memory when it loads the page. The file on disk is not changed.
- The window follows the Mac's light or dark setting and remembers its size and position. It is never smaller than 900 by 600.

## Read a different feed file

First match wins:

1. `--feed <path>` launch argument
2. `COACH_FEED=<path>`
3. `$COACH_HOME/live.json`
4. `~/.claude/coach/live.json`

To try the app with the sample feed in this folder:

```sh
COACH_FEED="$PWD/menubar/Fixtures/live.json" menubar/build/AgenticCoach.app/Contents/MacOS/AgenticCoach
```

## Switches for screenshots and checks

- `COACH_NOW=2026-11-23T11:41:00+02:00` pins the clock, so a sample feed reads the same every time.
- `COACH_APPEARANCE=light` or `dark` overrides the Mac's appearance for this app only.
- `COACH_OPEN_PANEL=1` opens the panel at launch and keeps it open.
- `COACH_LOG_HEIGHT=1` logs the panel's height.
- `COACH_SCREEN_HEIGHT=900` stands in for a smaller screen, to check that the panel scrolls.
- `COACH_SNAPSHOT_DIR=<folder> swift test --filter panelPictures` draws the panel to picture files, dark and light and for each period, without putting anything on screen. `COACH_SNAPSHOT_FEED` and `COACH_SNAPSHOT_NOW` pick the feed file and the moment.
- `COACH_PAGE_FILE=<built page> COACH_SNAPSHOT_DIR=<folder> swift test --filter pagePictures` does the same for a built coach page. `COACH_PAGE_HASHES` lists the views, for example `overview/month,cost`.
- `COACH_LAUNCHER=<script>` runs that script with `page` in place of `<coach home>/coach_live.py`, to check the window from a worktree (for example `COACH_LAUNCHER="$PWD/coach/live.py"`).

The panel is as tall as its content, up to the screen's visible height minus 16pt; past that the content scrolls under the title row.

## What the menu bar item shows

- The mark and today's spend.
- A green dot beside the mark while a session is running.
- The highest limit, in red with its name (for example "Weekly 94%"), when any limit is at 90 percent or more.
- Compact: the mark alone.

## Fonts

SF Pro (the system font) with tabular digits throughout, in the panel and on the page. No SF Mono for figures, no web fonts. The page keeps a monospace only for code.

## Layout

- `Sources/CoachCore`: the feed model, the formatters and the menu bar rule (tested).
- `Sources/AgenticCoach`: the app. `App.swift` holds the menu bar item and the popover, `PanelPopover.swift` the popover's content and fill, `PanelView.swift` the panel itself (SwiftUI), `CoachWindow.swift` the coach page window.
- `Tests/AgenticCoachTests`: checks that lay the panel out off screen (its size, and that its top edge stays put).
- `Fixtures`: sample feeds used by the tests.
