import AppKit
import CoachCore
import Combine

// Claude Coach: a menu bar item for the agentic-coach plugin, with its panel in a system
// popover and the coach page in a window (the Mac Monitor app is the model).
// For checks only: COACH_APPEARANCE=light or dark sets this app's look (not the Mac's),
// and COACH_OPEN_PANEL=1 opens the panel at launch and keeps it open.

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private let store = FeedStore.shared
    private let settings = PanelSettings()
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private lazy var panel = PanelController(store: store, settings: settings)
    private lazy var popover = PanelPopover.make(controller: panel, keepOpen: keepOpen)
    private var watching: Set<AnyCancellable> = []
    private var lookWatch: NSKeyValueObservation?
    private var shown: MenuBarState?
    private var keepOpen = false

    func applicationDidFinishLaunching(_ note: Notification) {
        let env = ProcessInfo.processInfo.environment
        switch env["COACH_APPEARANCE"] {
        case "dark": NSApp.appearance = NSAppearance(named: .darkAqua)
        case "light": NSApp.appearance = NSAppearance(named: .aqua)
        default: break
        }
        keepOpen = env["COACH_OPEN_PANEL"] == "1"

        if let button = statusItem.button {
            button.target = self
            button.action = #selector(togglePanel)
            button.imagePosition = .imageOnly
        }

        // The menu bar item is always kept current: the feed, the clock and the Compact setting.
        store.$state.combineLatest(store.$now, settings.$compact)
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in self?.refreshItem() }
            .store(in: &watching)
        // An open panel changes with the Mac's light or dark setting at once.
        lookWatch = NSApp.observe(\.effectiveAppearance) { [weak self] _, _ in
            Task { @MainActor in self?.panel.setLook() }
        }
        refreshItem()

        if keepOpen {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in self?.togglePanel() }
        }
    }

    /// A click on the menu bar item opens the panel, and closes it when it is open.
    @objc private func togglePanel() {
        guard let button = statusItem.button else { return }
        if popover.isShown {
            popover.performClose(nil)
            return
        }
        store.reload()
        // COACH_SCREEN_HEIGHT stands in for a smaller screen when checking that the panel scrolls.
        let pretend = ProcessInfo.processInfo.environment["COACH_SCREEN_HEIGHT"].flatMap(Double.init).map { CGFloat($0) }
        panel.prepare(screenHeight: pretend ?? button.window?.screen?.visibleFrame.height)
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        // No button starts out ringed as the keyboard focus; Tab still reaches them.
        panel.view.window?.makeFirstResponder(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Draws the item: the mark, the running dot, today's spend, a limit at 90 percent or
    /// more in red with its name; the mark alone in Compact.
    private func refreshItem() {
        let state = MenuBarState.make(state: store.state, compact: settings.compact, now: store.now)
        guard state != shown, let button = statusItem.button else { return }
        shown = state
        button.image = MenuBarIcon.image(for: state)
        button.toolTip = state.spokenText
        button.setAccessibilityLabel(state.spokenText)
    }
}

@main
enum ClaudeCoachApp {
    @MainActor static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.accessory) // no Dock icon, also when run without the app bundle
        app.run()
    }
}
