import AppKit
import SwiftUI
import Testing
@testable import AgenticCoach
@testable import CoachCore

/// The popover the panel is shown in, set up as the app sets it up but never shown:
/// its size follows the panel through shorter and taller content, and the panel's solid
/// colour is painted behind the content. No menu bar item is made and nothing is put on screen.
@MainActor
@Suite(.serialized) struct PanelPopoverSetup {
    private static func fixture(_ name: String, file: String = #filePath) -> String {
        URL(fileURLWithPath: file)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Fixtures").appendingPathComponent(name).path
    }

    private static func settle(_ view: NSView) {
        view.layoutSubtreeIfNeeded()
        for _ in 0..<12 { RunLoop.main.run(until: Date().addingTimeInterval(0.02)) }
        view.layoutSubtreeIfNeeded()
    }

    private static func writeFeed(to url: URL, edit: (inout [String: Any]) -> Void) throws {
        let data = try Data(contentsOf: URL(fileURLWithPath: fixture("live.json")))
        var feed = try #require(try JSONSerialization.jsonObject(with: data) as? [String: Any])
        edit(&feed)
        try JSONSerialization.data(withJSONObject: feed).write(to: url, options: .atomic)
    }

    private static func today(_ feed: inout [String: Any], _ change: (inout [String: Any]) -> Void) {
        var periods = feed["periods"] as? [String: Any] ?? [:]
        var today = periods["today"] as? [String: Any] ?? [:]
        change(&today)
        periods["today"] = today
        feed["periods"] = periods
    }

    private struct Built {
        let controller: PanelController
        let popover: NSPopover
        let store: FeedStore
        let settings: PanelSettings
        let file: URL
        let folder: URL
    }

    private static func build(screenHeight: CGFloat = 3000) throws -> Built {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.prohibited) // a test process: no Dock icon, nothing on screen
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("coach-popover-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let file = folder.appendingPathComponent("live.json")
        try writeFeed(to: file) { _ in }
        let store = FeedStore(path: file.path, now: FeedDates.parse("2026-11-23T11:43:00+02:00")!)
        let defaults = try #require(UserDefaults(suiteName: "coach-test-\(UUID().uuidString)"))
        let settings = PanelSettings(defaults: defaults)
        let controller = PanelController(store: store, settings: settings, screenHeight: screenHeight)
        let popover = PanelPopover.make(controller: controller)
        controller.loadViewIfNeeded()
        settle(controller.view)
        return Built(controller: controller, popover: popover, store: store, settings: settings, file: file, folder: folder)
    }

    /// The panel's own height: what its hosting view asks for.
    private static func panelHeight(_ b: Built) -> CGFloat { b.controller.host.fittingSize.height }

    /// The same feed laid out on its own, in a window that sizes itself from the panel.
    private static func heightOnItsOwn(feedAt file: URL) -> CGFloat {
        let store = FeedStore(path: file.path, now: FeedDates.parse("2026-11-23T11:43:00+02:00")!)
        let host = NSHostingView(rootView: PanelView(store: store, compact: .constant(false), screenHeight: 3000))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 300, height: 300), styleMask: [.borderless], backing: .buffered, defer: true)
        window.isReleasedWhenClosed = false
        window.contentView = host
        settle(host)
        return window.frame.height
    }

    @Test func popoverIsSetUpAsMacMonitorsIs() throws {
        let b = try Self.build()
        defer { try? FileManager.default.removeItem(at: b.folder) }
        #expect(b.popover.behavior == .transient) // closes when the person clicks elsewhere
        #expect(!b.popover.animates)
        #expect(b.popover.contentViewController === b.controller)
        #expect(!b.popover.isShown) // nothing is put on screen by a test
        #expect(b.controller.onClose != nil) // the panel's links and buttons close this popover
        let other = PanelController(store: b.store, settings: b.settings, screenHeight: 3000)
        #expect(PanelPopover.make(controller: other, keepOpen: true).behavior == .applicationDefined)
    }

    /// The size is right the moment the popover is about to be shown: straight after the
    /// controller loads and straight after `prepare`, with no turn of the run loop between.
    @Test func sizeIsRightBeforeTheFirstShow() throws {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.prohibited)
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("coach-popover-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let file = folder.appendingPathComponent("live.json")
        try Self.writeFeed(to: file) { _ in }
        let want = Self.heightOnItsOwn(feedAt: file)
        #expect(want > 600)

        let store = FeedStore(path: file.path, now: FeedDates.parse("2026-11-23T11:43:00+02:00")!)
        let name = "coach-test-\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: name))
        defer { defaults.removePersistentDomain(forName: name) }
        let controller = PanelController(store: store, settings: PanelSettings(defaults: defaults))
        _ = PanelPopover.make(controller: controller)
        controller.prepare(screenHeight: 3000) // what a click does, then the popover is shown at once
        #expect(controller.preferredContentSize == NSSize(width: 340, height: want))

        // the feed changed while the panel was closed: right again at the next click, at once
        try Self.writeFeed(to: file) { feed in feed["tips"] = [Any]() }
        store.reload()
        controller.prepare(screenHeight: 3000)
        #expect(controller.preferredContentSize.height == Self.heightOnItsOwn(feedAt: file))
        #expect(controller.preferredContentSize.height < want - 60)
    }

    /// The popover is exactly as tall as the panel, when the panel first appears and each
    /// time its content gets shorter or taller: no spare room, so no band round the panel.
    @Test func popoverSizeFollowsThePanelShorterAndTaller() throws {
        let b = try Self.build()
        defer { try? FileManager.default.removeItem(at: b.folder) }
        let full = b.controller.preferredContentSize
        #expect(full.width == 340)
        #expect(full.height > 600 && full.height < 1000, "the panel is \(full.height)pt tall")
        #expect(full.height == Self.panelHeight(b))
        #expect(full.height == Self.heightOnItsOwn(feedAt: b.file))

        // shorter: no activity rows, no tip (as another period, or a quieter feed)
        try Self.writeFeed(to: b.file) { feed in
            Self.today(&feed) { $0["activities"] = [Any]() }
            feed["tips"] = [Any]()
        }
        b.store.reload()
        Self.settle(b.controller.view)
        let short = b.controller.preferredContentSize
        #expect(short.width == 340)
        #expect(short.height < full.height - 120, "shorter content: \(short.height) against \(full.height)")
        #expect(short.height == Self.panelHeight(b))
        #expect(short.height == Self.heightOnItsOwn(feedAt: b.file))

        // taller: three more sessions start running
        try Self.writeFeed(to: b.file) { feed in
            let one = (feed["running"] as? [[String: Any]])?.first ?? [:]
            feed["running"] = [one, one, one, one]
        }
        b.store.reload()
        Self.settle(b.controller.view)
        let tall = b.controller.preferredContentSize
        #expect(tall.height > full.height + 120, "taller content: \(tall.height) against \(full.height)")
        #expect(tall.height == Self.panelHeight(b))

        // and back: the very same height as at first
        try Self.writeFeed(to: b.file) { _ in }
        b.store.reload()
        Self.settle(b.controller.view)
        #expect(b.controller.preferredContentSize == full)

        // back and forth again, as someone flicking between two feeds would see
        for _ in 0..<3 {
            try Self.writeFeed(to: b.file) { feed in feed["tips"] = [Any]() }
            b.store.reload()
            Self.settle(b.controller.view)
            #expect(b.controller.preferredContentSize.height == Self.panelHeight(b))
            #expect(b.controller.preferredContentSize.height < full.height)
            try Self.writeFeed(to: b.file) { _ in }
            b.store.reload()
            Self.settle(b.controller.view)
            #expect(b.controller.preferredContentSize == full)
        }
    }

    /// The size is never one that would leave no popover: a positive, finite height at all times,
    /// also with no feed at all.
    @Test func popoverAlwaysHasARealSize() throws {
        let b = try Self.build()
        defer { try? FileManager.default.removeItem(at: b.folder) }
        try FileManager.default.removeItem(at: b.file) // no feed: the short "No coach data yet" panel
        b.store.reload()
        Self.settle(b.controller.view)
        let size = b.controller.preferredContentSize
        #expect(size.width == 340)
        #expect(size.height.isFinite && size.height >= 100 && size.height < 300, "no-feed panel is \(size.height)pt")
        #expect(size.height == Self.panelHeight(b))
    }

    /// On a screen too short for the panel the popover is that screen's visible height
    /// less 40 points (room for the arrow) and the panel scrolls; `prepare` passes the screen the item is on.
    @Test func popoverIsCappedToTheScreenItOpensOn() throws {
        let b = try Self.build()
        defer { try? FileManager.default.removeItem(at: b.folder) }
        let full = b.controller.preferredContentSize.height
        b.controller.prepare(screenHeight: 600)
        Self.settle(b.controller.view)
        #expect(b.controller.preferredContentSize.height == 560)
        #expect(PanelView.screenMargin == 40)
        b.controller.prepare(screenHeight: 3000)
        Self.settle(b.controller.view)
        #expect(b.controller.preferredContentSize.height == full)
    }

    /// The panel's root is a solid fill in the panel's colour, with the SwiftUI panel pinned to its top.
    @Test func solidFillIsBehindThePanel() throws {
        let b = try Self.build()
        defer { try? FileManager.default.removeItem(at: b.folder) }
        let box = try #require(b.controller.view as? PanelBox)
        #expect(box.fill == NSColor.controlBackgroundColor)
        #expect(b.controller.host.superview === box)
        // in a window a moment too tall for the panel (80pt), as a popover can be while it resizes
        let tall = NSSize(width: 340, height: b.controller.preferredContentSize.height + 80)
        let window = NSPanel(contentRect: NSRect(origin: .zero, size: tall), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: true)
        window.isReleasedWhenClosed = false
        let content = NSView(frame: NSRect(origin: .zero, size: tall))
        window.contentView = content
        box.frame = content.bounds
        box.autoresizingMask = [.width, .height]
        content.addSubview(box)
        Self.settle(box)
        // the panel's own height wins: the spare 80pt is given up, not left as a band
        #expect(box.bounds.height == b.controller.preferredContentSize.height)
        #expect(window.frame.height == b.controller.preferredContentSize.height)
        // the panel fills the box from its top; anything else showing is the box's own solid colour
        let hostFrame = b.controller.host.frame
        #expect(hostFrame.width == 340)
        #expect(box.isFlipped ? hostFrame.minY == 0 : hostFrame.maxY == box.bounds.height)
        box.updateLayer()
        #expect((box.layer?.backgroundColor?.alpha ?? 0) == 1)
        // a plain light or dark look, never the popover's see-through one
        #expect(box.appearance?.name == .aqua || box.appearance?.name == .darkAqua)
        box.setLook(.darkAqua)
        #expect(box.appearance?.name == .darkAqua && box.backing.appearance?.name == .darkAqua)
        box.setLook(.aqua)
        #expect(box.appearance?.name == .aqua && box.backing.appearance?.name == .aqua)
    }

    /// In a window built as a popover's is (a frame view holding the material and, above it,
    /// the content), the backing fill is slipped between the two and covers the whole frame,
    /// arrow included, and keeps covering it when the popover changes size.
    @Test func backingFillCoversTheWholePopoverFrame() throws {
        let b = try Self.build()
        defer { try? FileManager.default.removeItem(at: b.folder) }
        let box = try #require(b.controller.view as? PanelBox)
        let window = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 340, height: 700),
            styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: true
        )
        window.isReleasedWhenClosed = false
        let content = NSView(frame: NSRect(x: 0, y: 0, width: 340, height: 700))
        window.contentView = content
        let frame = try #require(content.superview)
        let material = NSVisualEffectView(frame: frame.bounds) // the popover's see-through material
        material.autoresizingMask = [.width, .height]
        frame.addSubview(material, positioned: .below, relativeTo: content)
        box.frame = content.bounds.insetBy(dx: 0, dy: 13) // the content sits inside the frame, clear of the arrow
        box.autoresizingMask = [.width, .height]
        content.addSubview(box)
        Self.settle(box)

        let backing = box.backing
        #expect(backing.superview === frame)
        #expect(backing.frame == frame.bounds) // the whole popover: the band above and below the content too
        let order = frame.subviews
        let at = try #require(order.firstIndex(of: backing))
        #expect(try #require(order.firstIndex(of: material)) < at) // over the material
        #expect(at < (try #require(order.firstIndex(of: content)))) // under the content
        backing.updateLayer()
        #expect((backing.layer?.backgroundColor?.alpha ?? 0) == 1)
        #expect(!material.isHidden && window.alphaValue == 1) // nothing hidden, nothing made see-through
        #expect(frame.subviews.filter { $0 === backing }.count == 1)

        window.setContentSize(NSSize(width: 340, height: 900))
        Self.settle(box)
        #expect(backing.frame == frame.bounds)
        #expect(backing.superview === frame)
    }

    /// The links and buttons close the popover first; Compact is kept between launches.
    @Test func closeActionAndSettings() throws {
        let b = try Self.build()
        defer { try? FileManager.default.removeItem(at: b.folder) }
        // Compact is switched from the panel's own menu: the setting changes and is kept
        #expect(!b.settings.compact)
        b.settings.compact = true
        Self.settle(b.controller.view)
        #expect(b.settings.compact)
        b.settings.compact = false

        let name = "coach-test-\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: name))
        defer { defaults.removePersistentDomain(forName: name) }
        let settings = PanelSettings(defaults: defaults)
        #expect(!settings.compact)
        settings.compact = true
        #expect(defaults.bool(forKey: PanelSettings.compactKey))
        #expect(PanelSettings(defaults: defaults).compact)
    }
}
