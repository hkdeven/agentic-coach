import AppKit
import SwiftUI
import Testing
@testable import AgenticCoach
@testable import CoachCore

/// Lays the panel out in a window that is never put on screen and measures it.
/// Nothing is launched, shown or clicked.
@MainActor
@Suite(.serialized) struct PanelLayout {
    private static func fixture(_ name: String, file: String = #filePath) -> String {
        URL(fileURLWithPath: file)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Fixtures").appendingPathComponent(name).path
    }

    private static func settle(_ host: NSView) {
        host.layoutSubtreeIfNeeded()
        for _ in 0..<12 { RunLoop.main.run(until: Date().addingTimeInterval(0.02)) }
        host.layoutSubtreeIfNeeded()
    }

    /// A copy of the sample feed, changed by `edit`, so a test can make the panel's
    /// content taller or shorter while its window exists.
    private static func writeFeed(to url: URL, edit: (inout [String: Any]) -> Void) throws {
        let data = try Data(contentsOf: URL(fileURLWithPath: fixture("live.json")))
        var feed = try #require(try JSONSerialization.jsonObject(with: data) as? [String: Any])
        edit(&feed)
        try JSONSerialization.data(withJSONObject: feed).write(to: url, options: .atomic)
    }

    /// The panel is 340 points wide. With the sample feed (a coach tip and one running
    /// session) it is about 830 points tall; on a screen shorter than that it scrolls.
    @Test func panelSizeWithTheSampleFeed() throws {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.prohibited)
        let store = FeedStore(path: Self.fixture("live.json"), now: FeedDates.parse("2026-11-23T11:43:00+02:00")!)
        let host = NSHostingView(rootView: PanelView(store: store, compact: .constant(false), screenHeight: 3000))
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 300, height: 300),
            styleMask: [.borderless], backing: .buffered, defer: true
        )
        window.isReleasedWhenClosed = false
        window.contentView = host
        Self.settle(host)
        #expect(window.frame.width == 340)
        #expect(window.frame.height > 450)
        #expect(window.frame.height < 880, "the panel is \(window.frame.height)pt tall")
        print("PANELSIZE \(window.frame.width) x \(window.frame.height)")
    }

    /// Draws the panel into picture files without putting anything on screen, in dark and
    /// in light and for each period. Runs only when COACH_SNAPSHOT_DIR names a folder:
    /// `COACH_SNAPSHOT_DIR=/tmp/shots swift test --filter panelPictures`.
    @Test(.enabled(if: ProcessInfo.processInfo.environment["COACH_SNAPSHOT_DIR"] != nil))
    func panelPictures() throws {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.prohibited)
        let folder = URL(fileURLWithPath: ProcessInfo.processInfo.environment["COACH_SNAPSHOT_DIR"] ?? "")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let feedName = ProcessInfo.processInfo.environment["COACH_SNAPSHOT_FEED"] ?? Self.fixture("live.json")
        let nowText = ProcessInfo.processInfo.environment["COACH_SNAPSHOT_NOW"] ?? "2026-11-23T11:43:00+02:00"
        for (look, name) in [(NSAppearance.Name.darkAqua, "dark"), (.aqua, "light")] {
            for period in PeriodKey.allCases {
                let store = FeedStore(path: feedName, now: FeedDates.parse(nowText)!)
                let host = NSHostingView(rootView: PanelView(store: store, compact: .constant(false), startPeriod: period))
                let window = NSWindow(
                    contentRect: NSRect(x: 0, y: 0, width: 300, height: 300),
                    styleMask: [.borderless], backing: .buffered, defer: true
                )
                window.isReleasedWhenClosed = false
                window.appearance = NSAppearance(named: look)
                window.contentView = host
                Self.settle(host)
                let rep = try #require(host.bitmapImageRepForCachingDisplay(in: host.bounds))
                host.cacheDisplay(in: host.bounds, to: rep)
                let png = try #require(rep.representation(using: .png, properties: [:]))
                try png.write(to: folder.appendingPathComponent("panel-\(name)-\(period.rawValue).png"))
            }
        }
    }

    /// The panel's height for a feed changed by the given edit, laid out off screen.
    private static func panelHeight(_ edit: (inout [String: Any]) -> Void) throws -> CGFloat {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.prohibited)
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("coach-layout-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let file = folder.appendingPathComponent("live.json")
        try writeFeed(to: file, edit: edit)
        let store = FeedStore(path: file.path, now: FeedDates.parse("2026-11-23T11:43:00+02:00")!)
        let host = NSHostingView(rootView: PanelView(store: store, compact: .constant(false), screenHeight: 3000))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 300, height: 300), styleMask: [.borderless], backing: .buffered, defer: true)
        window.isReleasedWhenClosed = false
        window.contentView = host
        settle(host)
        return window.frame.height
    }

    private static func sessions(_ count: Int) -> [[String: Any]] {
        (0..<count).map { i in
            ["project": "project-\(i)", "branch": "main", "model": "Opus 5.5", "started": "2026-11-23T10:0\(i):00+02:00",
             "context_pct": 20 + i, "context_left_tokens": 700_000, "level": "ok"]
        }
    }

    /// The Coach tip tile is drawn when the feed has a tip and not otherwise.
    @Test func coachTipTileComesAndGoes() throws {
        let with = try Self.panelHeight { _ in }
        let without = try Self.panelHeight { feed in feed["tips"] = [Any]() }
        #expect(with - without > 60, "the tile should take room: \(with) against \(without)")
        #expect(with - without < 140)
    }

    /// One wide tile per running session, four at most, then a line for the rest.
    @Test func eachRunningSessionHasATileUpToFour() throws {
        let none = try Self.panelHeight { feed in feed["running"] = [Any]() }
        let one = try Self.panelHeight { feed in feed["running"] = Self.sessions(1) }
        let two = try Self.panelHeight { feed in feed["running"] = Self.sessions(2) }
        let four = try Self.panelHeight { feed in feed["running"] = Self.sessions(4) }
        let five = try Self.panelHeight { feed in feed["running"] = Self.sessions(5) }
        let nine = try Self.panelHeight { feed in feed["running"] = Self.sessions(9) }
        let tile = two - one // a tile without the label, and the gap above it
        #expect(tile > 40 && tile < 70, "a session tile is \(tile)pt")
        #expect(four - one == 3 * tile)
        #expect(one - none > 0 && one - none < 30) // "Nothing running" is one line shorter
        // past four: no more tiles, only the line that says how many more there are
        #expect(five - four > 10 && five - four < 30, "the line is \(five - four)pt")
        #expect(nine == five)
    }

    /// On a screen too short for it the panel is the screen's visible height less 16 points, and scrolls.
    @Test func panelIsCappedToTheScreen() throws {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.prohibited)
        let store = FeedStore(path: Self.fixture("live.json"), now: FeedDates.parse("2026-11-23T11:43:00+02:00")!)
        let host = NSHostingView(rootView: PanelView(store: store, compact: .constant(false), screenHeight: 600))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 300, height: 300), styleMask: [.borderless], backing: .buffered, defer: true)
        window.isReleasedWhenClosed = false
        window.contentView = host
        Self.settle(host)
        #expect(window.frame.height == 560) // 600 less the 40pt kept for the popover's arrow
    }
}
