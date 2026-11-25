import AppKit
import CoachCore
import SwiftUI
import WebKit

/// The coach page in a window of its own. One window; opening it again brings it forward.
///
/// It shows the local page file (`<coach home>/site/index.html`), rebuilt by the coach's
/// own `page` command each time the window is opened. While that runs the last page stays
/// up with a small "Updating" mark; if it fails the last page stays and the reason is shown
/// with Try again. The web view may not reach the network: every http, https and web socket
/// request is blocked, and a link the person clicks to another site opens in their browser.
@MainActor
final class CoachWindow: NSObject, ObservableObject, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate {
    static let shared = CoachWindow()

    enum Phase: Equatable {
        case idle
        case updating
        case failed(String)
    }

    @Published private(set) var phase: Phase = .idle
    /// True once a page is on screen in the web view.
    @Published private(set) var hasPage = false

    private let pageURL: URL
    private var window: NSWindow?
    private var webView: WKWebView?

    /// The web view loads nothing until its no-network rules are in place.
    private var rulesReady = false
    /// Why the rules could not be put in place, when they could not.
    private var rulesFailure: String?
    /// A load asked for before the rules were ready; the value says whether it follows a build.
    private var loadWhenReady: Bool?
    /// True from the start of a load until the page is on screen.
    private var loading = false
    /// True while a reload is asking the page which tab and scroll offset it is on.
    private var capturing = false
    /// How many times in a row the page's own process has ended before showing a page.
    private var crashes = 0
    /// Tab to show once the next load finishes.
    private var fragmentToShow: String?
    /// Scroll offset to put back after a reload that kept the same tab.
    private var scrollToRestore: Double?

    private static let sizeKey = "CoachPageWindow"
    private static let maxPageBytes = 50_000_000
    /// The page file is a fragment with no doctype or charset of its own (the published
    /// copy gets them from its host). Give it both, in memory only, so it is drawn in
    /// standards mode and read as UTF-8. The file on disk is never changed.
    private static let head = Data("<!doctype html><meta charset=\"utf-8\">\n".utf8)
    private static let noNetworkRules = """
    [{"trigger":{"url-filter":"^https?://"},"action":{"type":"block"}},
     {"trigger":{"url-filter":"^wss?://"},"action":{"type":"block"}}]
    """

    override private init() {
        let path = CoachPaths.pageFile(environment: ProcessInfo.processInfo.environment, home: NSHomeDirectory())
        pageURL = URL(fileURLWithPath: path)
        super.init()
    }

    // MARK: Opening

    /// Shows the window in front, on the given tab, and rebuilds the page.
    func open(tab: PageTab) {
        let window = makeWindowIfNeeded()
        // Wide enough for the Overview's six tiles on one line, also when a narrower size was
        // remembered; never wider than the screen, and where it was left.
        if let visible = (window.screen ?? NSScreen.main)?.visibleFrame {
            let wide = PageWindowSize.onOpen(frame: window.frame, visible: visible)
            if wide != window.frame { window.setFrame(wide, display: true) }
        }
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)

        if let fragment = tab.fragment {
            scrollToRestore = nil // a new tab starts at its top
            // While a load or a reload is under way the tab is kept for when it lands.
            if hasPage, !loading, !capturing { show(fragment: fragment) } else { fragmentToShow = fragment }
        } else if !hasPage {
            fragmentToShow = nil // the latest request wins over one that never got shown
        }
        if !hasPage, !loading { loadPage(afterBuild: false) } // the last page built, when there is one
        rebuild()
    }

    /// Runs the coach's page command, then shows what it built. One build at a time.
    func rebuild() {
        guard phase != .updating else { return }
        if rulesFailure != nil { installRules() } // Try again also tries these again
        phase = .updating
        PageBuilder.run { [weak self] outcome in
            guard let self else { return }
            switch outcome {
            case .built:
                if let reason = self.rulesFailure {
                    self.phase = .failed(reason)
                } else {
                    self.phase = .idle
                    self.reload()
                }
            case .failed(let reason):
                self.phase = .failed(reason)
            }
        }
    }

    // MARK: Window

    private func makeWindowIfNeeded() -> NSWindow {
        if let window { return window }

        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent() // nothing is kept between runs
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        let webView = PageWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = false
        webView.allowsLinkPreview = false // a preview would load the other site inside the app
        webView.underPageBackgroundColor = Theme.pageNS
        self.webView = webView
        installRules()

        let host = NSHostingController(rootView: CoachPageView(model: self, webView: webView))
        host.sizingOptions = [] // the window, not the content, decides the size
        let window = CoachPageWindow(
            contentRect: NSRect(x: 0, y: 0, width: PageWindowSize.openWidth, height: 780),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false
        )
        window.title = "Claude Coach"
        window.isReleasedWhenClosed = false // closing hides it; the app keeps running
        window.contentViewController = host
        window.contentMinSize = NSSize(width: 900, height: 600)
        window.setContentSize(NSSize(width: PageWindowSize.openWidth, height: 780))
        window.center()
        // Size and position are remembered between openings and between launches.
        window.setFrameAutosaveName(Self.sizeKey)
        window.delegate = self
        window.onReload = { [weak self] in self?.rebuild() }
        self.window = window
        return window
    }

    /// Blocks every web request from the page. Until this is in place nothing is loaded.
    private func installRules() {
        rulesFailure = nil
        WKContentRuleListStore.default().compileContentRuleList(
            forIdentifier: "coach-page-no-network", encodedContentRuleList: Self.noNetworkRules
        ) { [weak self] list, error in
            Task { @MainActor in
                guard let self, let webView = self.webView else { return }
                guard let list else {
                    // Without the rules the page could reach the network, so it is not shown.
                    let reason = "The page view could not be made ready: \(error?.localizedDescription ?? "unknown reason")."
                    self.rulesFailure = reason
                    // A build in flight reports this when it ends, so it stays the only build.
                    if self.phase != .updating { self.phase = .failed(reason) }
                    return
                }
                webView.configuration.userContentController.removeAllContentRuleLists()
                webView.configuration.userContentController.add(list)
                self.rulesReady = true
                if let afterBuild = self.loadWhenReady {
                    self.loadWhenReady = nil
                    self.loadPage(afterBuild: afterBuild)
                }
            }
        }
    }

    // MARK: Loading

    /// Loads the page file into the web view. `afterBuild` is true when a build has just
    /// ended well: a page that then cannot be read is a failure to say, not a wait.
    private func loadPage(afterBuild: Bool) {
        guard let webView else { return }
        guard rulesReady else {
            loadWhenReady = (loadWhenReady ?? false) || afterBuild
            return
        }
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: pageURL.path),
              let size = attributes[.size] as? NSNumber, size.intValue <= Self.maxPageBytes,
              let page = FileManager.default.contents(atPath: pageURL.path)
        else {
            // No page yet: the plain loading state stays up while the first build runs.
            // During a build nothing is said here; the build's own end reports.
            fragmentToShow = nil
            scrollToRestore = nil
            if afterBuild, phase != .updating {
                phase = .failed("The page was built, but its file could not be read: \(pageURL.path)")
            }
            return
        }
        loading = true
        webView.load(Self.head + page, mimeType: "text/html", characterEncodingName: "utf-8", baseURL: pageURL)
    }

    /// Loads the newly built page, keeping the tab and scroll position the person was on.
    private func reload() {
        guard hasPage, !loading, let webView else {
            loadPage(afterBuild: true)
            return
        }
        capturing = true
        webView.evaluateJavaScript("[location.hash, window.scrollY]") { [weak self] value, _ in
            Task { @MainActor in
                guard let self else { return }
                self.capturing = false
                // A tab asked for in the meantime wins; otherwise keep the one showing.
                if self.fragmentToShow == nil, let pair = value as? [Any], pair.count == 2 {
                    if let hash = pair[0] as? String, hash.hasPrefix("#"), hash.count > 1 {
                        self.fragmentToShow = String(hash.dropFirst())
                    }
                    self.scrollToRestore = (pair[1] as? NSNumber)?.doubleValue
                }
                self.loadPage(afterBuild: true)
            }
        }
    }

    private func show(fragment: String) {
        guard let text = try? JSONEncoder().encode("#" + fragment), let literal = String(data: text, encoding: .utf8) else { return }
        webView?.evaluateJavaScript("location.hash = \(literal)", completionHandler: nil)
    }

    // MARK: WKNavigationDelegate

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        loading = false
        hasPage = true
        crashes = 0
        if let fragment = fragmentToShow {
            fragmentToShow = nil
            show(fragment: fragment)
        }
        if let y = scrollToRestore {
            scrollToRestore = nil
            if y > 0 { webView.evaluateJavaScript("window.scrollTo(0, \(y))", completionHandler: nil) }
        }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        loadFailed(error)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        loadFailed(error)
    }

    private func loadFailed(_ error: Error) {
        let code = (error as NSError).code
        // A navigation this class turned down itself is not a failure to report.
        if code == NSURLErrorCancelled || code == 102 { return }
        loading = false
        fragmentToShow = nil
        scrollToRestore = nil
        // With a page already up this shows as the bar over it: the page on screen is the last good one.
        if phase != .updating {
            phase = .failed("The page could not be shown: \(error.localizedDescription)")
        }
    }

    func webView(
        _ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        let url = action.request.url
        switch PageLinkRule.action(for: url, page: pageURL, clicked: action.navigationType == .linkActivated) {
        case .stay:
            // Allowed: this class's own load of the page, and moves within it (a hash).
            // A reload, or a link to the page file itself, would read the file straight
            // from disk without the doctype and charset added here: load it the usual way.
            let withinPage = action.navigationType == .other || url?.fragment != nil
            if action.navigationType == .reload || !withinPage {
                decisionHandler(.cancel)
                reload()
            } else {
                decisionHandler(.allow)
            }
        case .openInBrowser:
            if let url { NSWorkspace.shared.open(url) }
            decisionHandler(.cancel)
        case .block:
            decisionHandler(.cancel)
        }
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        hasPage = false
        loading = false
        capturing = false
        crashes += 1
        // Three times in a row without a page shown: stop trying and say so, with Try again.
        if crashes > 2 {
            crashes = 0
            if phase != .updating { phase = .failed("The page view stopped while showing the page.") }
            return
        }
        loadPage(afterBuild: phase != .updating)
    }

    // MARK: WKUIDelegate

    /// A link that asks for a new window (the page's outside links do) never gets one here.
    func webView(
        _ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
        for action: WKNavigationAction, windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        let url = action.request.url
        if PageLinkRule.action(for: url, page: pageURL, clicked: action.navigationType == .linkActivated) == .openInBrowser,
           let url {
            NSWorkspace.shared.open(url)
        }
        return nil
    }
}

/// The page's web view with a short right-click menu: Copy, Copy Link and Look Up only.
/// Download, Open in New Window, Reload and Back are taken out, because each would fetch
/// something or load the page file past this app's own loading.
final class PageWebView: WKWebView {
    private static let kept: Set<String> = [
        "WKMenuItemIdentifierCopy", "WKMenuItemIdentifierCopyLink", "WKMenuItemIdentifierLookUp",
    ]

    override func willOpenMenu(_ menu: NSMenu, with event: NSEvent) {
        for item in menu.items where !item.isSeparatorItem {
            if let id = item.identifier?.rawValue, Self.kept.contains(id) { continue }
            menu.removeItem(item)
        }
        // Tidy the separators left behind.
        while let first = menu.items.first, first.isSeparatorItem { menu.removeItem(first) }
        while let last = menu.items.last, last.isSeparatorItem { menu.removeItem(last) }
        super.willOpenMenu(menu, with: event)
    }
}

/// The window answers the usual keys itself: a menu bar app has no menus to carry them.
final class CoachPageWindow: NSWindow {
    var onReload: (() -> Void)?

    /// Key positions for W, M, R, C and A, for keyboards whose letters are not Latin.
    private static let keyCodes: [UInt16: String] = [13: "w", 46: "m", 15: "r", 8: "c", 0: "a"]

    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        if super.performKeyEquivalent(with: event) { return true }
        // Command alone; Caps Lock being on makes no difference.
        guard event.modifierFlags.intersection([.command, .shift, .option, .control]) == .command else { return false }
        let typed = event.charactersIgnoringModifiers?.lowercased() ?? ""
        // The key's position is used only when the keyboard gives no Latin letter at all,
        // so on AZERTY or Dvorak the letter typed is the one that counts.
        let isLatinLetter = typed.count == 1 && typed.unicodeScalars.allSatisfy { $0.isASCII && $0.properties.isAlphabetic }
        let key = isLatinLetter ? typed : (Self.keyCodes[event.keyCode] ?? "")
        switch key {
        case "w": performClose(nil)
        case "m": performMiniaturize(nil)
        case "r": onReload?()
        case "c": NSApp.sendAction(#selector(NSText.copy(_:)), to: nil, from: self)
        case "a": NSApp.sendAction(#selector(NSText.selectAll(_:)), to: nil, from: self)
        default: return false
        }
        return true
    }
}

private struct WebViewHost: NSViewRepresentable {
    let webView: WKWebView

    func makeNSView(context: Context) -> WKWebView { webView }
    func updateNSView(_ view: WKWebView, context: Context) {}
}

private struct CoachPageView: View {
    @ObservedObject var model: CoachWindow
    let webView: WKWebView

    var body: some View {
        VStack(spacing: 0) {
            if case .failed(let reason) = model.phase, model.hasPage {
                failureBar(reason)
            }
            ZStack {
                Theme.page
                WebViewHost(webView: webView).opacity(model.hasPage ? 1 : 0)
                if !model.hasPage { placeholder }
            }
        }
        .overlay(alignment: .bottomTrailing) {
            if model.phase == .updating, model.hasPage { updatingMark.padding(14) }
        }
        .foregroundStyle(Theme.ink)
        .font(Theme.sans(13))
    }

    /// The last page stays up; this says why it is not the newest.
    private func failureBar(_ reason: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text("The page could not be updated. This is the last one built.").font(Theme.sans(13, .medium))
                Text(reason).foregroundStyle(Theme.ink2).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            }
            Spacer()
            Button("Try again") { model.rebuild() }
                .help("Builds the coach page again.")
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(Theme.warnSoft)
        .overlay(alignment: .bottom) { Rectangle().fill(Theme.line).frame(height: 1) }
    }

    private var updatingMark: some View {
        HStack(spacing: 8) {
            ProgressView().controlSize(.small)
            Text("Updating").font(Theme.sans(12, .medium))
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
        .background(Theme.surface, in: RoundedRectangle(cornerRadius: 6))
        .overlay(RoundedRectangle(cornerRadius: 6).stroke(Theme.line, lineWidth: 1))
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Updating the coach page")
    }

    /// No page on screen yet: a plain loading state, or the reason with Try again.
    @ViewBuilder private var placeholder: some View {
        if case .failed(let reason) = model.phase {
            VStack(spacing: 10) {
                Text("The coach page could not be built").font(Theme.sans(15, .semibold))
                Text(reason)
                    .foregroundStyle(Theme.ink2)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 520)
                    .textSelection(.enabled)
                Button("Try again") { model.rebuild() }
                    .help("Builds the coach page again.")
            }
            .padding(24)
        } else {
            VStack(spacing: 10) {
                ProgressView()
                Text("Loading the coach page").foregroundStyle(Theme.ink2)
            }
        }
    }
}
