import AppKit
import CoachCore
import Combine
import SwiftUI

// The panel is shown in a system popover under the menu bar item, the way the Mac Monitor
// app shows its panel (its Panel.swift and App.swift are the model for this file). The
// popover brings the round corners and the arrow; this file paints the panel's solid
// colour behind the whole of it and keeps it exactly as tall as the panel.

/// What the panel's settings menu changes.
@MainActor
final class PanelSettings: ObservableObject {
    static let compactKey = "compact"

    /// The menu bar item shows the mark alone.
    @Published var compact: Bool {
        didSet { defaults.set(compact, forKey: Self.compactKey) }
    }

    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        compact = defaults.bool(forKey: Self.compactKey)
    }
}

/// A view filled with one colour, resolved against plain light or dark and redrawn when
/// the Mac changes between them.
class FillView: NSView {
    var fill: NSColor = .clear { didSet { needsDisplay = true } }

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override var wantsUpdateLayer: Bool { true }

    override func updateLayer() {
        // Inside a popover the appearance is a see-through one, where system colours are
        // greys meant for blending. Resolve against plain light or dark.
        let plain = effectiveAppearance.bestMatch(from: [.aqua, .darkAqua]) ?? .aqua
        NSAppearance(named: plain)?.performAsCurrentDrawingAppearance {
            layer?.backgroundColor = fill.cgColor
        }
    }
}

/// The panel's root view. It puts one solid colour behind the whole popover, arrow
/// included, so nothing behind the panel shows through and no band of the popover's own
/// material shows round it.
final class PanelBox: FillView {
    /// The panel's surface: white on a light Mac, near black on a dark one.
    static let surface = NSColor.controlBackgroundColor

    let backing = FillView(frame: .zero)

    override init(frame: NSRect) {
        super.init(frame: frame)
        fill = Self.surface
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    /// Gives the panel and the colour behind it the same plain light or dark look, so the
    /// arrow can never be a different colour from the body and the panel's own colours are
    /// the ordinary ones.
    func setLook(_ name: NSAppearance.Name) {
        guard appearance?.name != name else { return }
        let look = NSAppearance(named: name)
        appearance = look
        backing.appearance = look
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        // The popover's frame view holds its see-through material (arrow included) and,
        // above that, this view. A plain view slipped between the two covers the material
        // and is cut to the popover's shape.
        guard let frame = window?.contentView?.superview else { return }
        backing.fill = Self.surface
        backing.frame = frame.bounds
        backing.autoresizingMask = [.width, .height]
        // The frame view's child that holds this view: this view itself today.
        var holder: NSView = self
        while let up = holder.superview, up !== frame { holder = up }
        guard holder.superview === frame else { return }
        if backing.superview !== frame {
            backing.removeFromSuperview()
            frame.addSubview(backing, positioned: .below, relativeTo: holder)
        }
    }

    override func layout() {
        super.layout()
        // Keep covering the whole popover if it is resized without autoresizing.
        if let frame = backing.superview, backing.frame != frame.bounds { backing.frame = frame.bounds }
    }
}

/// Holds the SwiftUI panel inside the popover and keeps the popover as tall as the panel.
@MainActor
final class PanelController: NSViewController {
    /// Closes the popover (set by whoever owns it).
    var onClose: (() -> Void)?

    private let store: FeedStore
    private let settings: PanelSettings
    private var screenHeight: CGFloat?
    private(set) var host: NSHostingView<PanelView>!
    private var sizing = false
    private var settingsWatch: AnyCancellable?

    init(store: FeedStore, settings: PanelSettings, screenHeight: CGFloat? = nil) {
        self.store = store
        self.settings = settings
        self.screenHeight = screenHeight
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    private func panel() -> PanelView {
        PanelView(
            store: store,
            compact: Binding(get: { [settings] in settings.compact }, set: { [settings] in settings.compact = $0 }),
            screenHeight: screenHeight,
            onHeight: { [weak self] _ in self?.resizeSoon() },
            onClose: { [weak self] in self?.onClose?() }
        )
    }

    override func loadView() {
        let box = PanelBox(frame: NSRect(x: 0, y: 0, width: PanelView.width, height: 400))
        host = NSHostingView(rootView: panel())
        host.translatesAutoresizingMaskIntoConstraints = false
        // The panel keeps its own height and sits at the top; if the popover is ever a
        // moment too tall, what shows under the panel is the box's solid colour.
        host.setContentHuggingPriority(.required, for: .vertical)
        host.setContentCompressionResistancePriority(.required, for: .vertical)
        box.addSubview(host)
        let bottom = host.bottomAnchor.constraint(equalTo: box.bottomAnchor)
        bottom.priority = NSLayoutConstraint.Priority(749) // the popover grows a moment after its content does
        NSLayoutConstraint.activate([
            host.topAnchor.constraint(equalTo: box.topAnchor),
            bottom,
            host.leadingAnchor.constraint(equalTo: box.leadingAnchor),
            host.trailingAnchor.constraint(equalTo: box.trailingAnchor),
            host.widthAnchor.constraint(equalToConstant: PanelView.width),
        ])
        view = box
        setLook()
        resize()
        // Compact was switched: draw the panel again so its settings menu shows the new tick.
        settingsWatch = settings.$compact.dropFirst().receive(on: RunLoop.main).sink { [weak self] _ in
            guard let self else { return }
            self.host.rootView = self.panel()
        }
    }

    /// Before the popover is shown: the screen it opens on (the panel is never taller than
    /// that screen and scrolls instead), the Mac's light or dark look, and the right size.
    func prepare(screenHeight: CGFloat?) {
        loadViewIfNeeded()
        if screenHeight != self.screenHeight {
            self.screenHeight = screenHeight
            host.rootView = panel() // the same panel, measured against the new screen
        }
        setLook()
        resize()
    }

    /// A plain light or dark look, following the Mac. The popover's own look is a
    /// see-through one whose text colours are made for blending.
    func setLook() {
        guard isViewLoaded else { return }
        let plain = NSApp.effectiveAppearance.bestMatch(from: [.aqua, .darkAqua]) ?? .aqua
        (view as? PanelBox)?.setLook(plain)
    }

    /// Makes the popover exactly as tall as the panel: no spare room under or over it.
    func resize() {
        guard isViewLoaded, !sizing else { return }
        sizing = true
        defer { sizing = false }
        host.layoutSubtreeIfNeeded()
        let height = host.fittingSize.height
        guard height.isFinite, height >= 40, height <= 10000 else { return } // never a size of nothing
        let size = NSSize(width: PanelView.width, height: height)
        if preferredContentSize != size { preferredContentSize = size }
    }

    /// The panel's height changed (another period, a session started or stopped): follow it
    /// on the run loop's next turn, once SwiftUI has finished the update it is in.
    private func resizeSoon() {
        RunLoop.main.perform(inModes: [.common]) { [weak self] in
            MainActor.assumeIsolated { self?.resize() } // the main run loop runs on the main thread
        }
        CFRunLoopWakeUp(CFRunLoopGetMain())
    }
}

/// The popover the panel is shown in.
@MainActor
enum PanelPopover {
    /// A popover set up as the Mac Monitor one is: it closes when the person clicks
    /// elsewhere, does not animate, and holds the panel's controller.
    static func make(controller: PanelController, keepOpen: Bool = false) -> NSPopover {
        let popover = NSPopover()
        popover.contentViewController = controller
        popover.behavior = keepOpen ? .applicationDefined : .transient
        popover.animates = false
        controller.onClose = { [weak popover] in popover?.performClose(nil) }
        return popover
    }
}
