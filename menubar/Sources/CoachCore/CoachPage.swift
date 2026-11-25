import CoreGraphics
import Foundation

// The rules behind the coach page window, kept here so they can be tested
// without a window: where the page and its builder are, which tab a link
// opens, what a link inside the page may do, and how a failed build is worded.

public enum CoachPaths {
    /// The coach's folder: `$COACH_HOME`, else `~/.claude/coach`.
    public static func coachHome(environment: [String: String], home: String) -> String {
        if let dir = environment["COACH_HOME"], !dir.isEmpty {
            if dir == "~" { return home }
            if dir.hasPrefix("~/") { return (home as NSString).appendingPathComponent(String(dir.dropFirst(2))) }
            return dir
        }
        return (home as NSString).appendingPathComponent(".claude/coach")
    }

    /// The local coach page: `<coach home>/site/index.html`.
    public static func pageFile(environment: [String: String], home: String) -> String {
        (coachHome(environment: environment, home: home) as NSString).appendingPathComponent("site/index.html")
    }

    /// The launcher that finds the current coach code: `<coach home>/coach_live.py`.
    /// `COACH_LAUNCHER` names another script (used when checking the app from a worktree).
    public static func launcher(environment: [String: String], home: String) -> String {
        if let path = environment["COACH_LAUNCHER"], !path.isEmpty { return path }
        return (coachHome(environment: environment, home: home) as NSString).appendingPathComponent("coach_live.py")
    }
}

/// The tab of the coach page a panel link opens.
public enum PageTab: Equatable {
    /// "Open coach page": whichever tab the page shows by itself, or the one already showing.
    case current
    /// "See all N tips on the coach page"
    case today
    /// "N more on the coach page" under By activity
    case overview

    /// The page's own hash for the tab, without the `#`.
    public var fragment: String? {
        switch self {
        case .current: return nil
        case .today: return "today"
        case .overview: return "overview"
        }
    }
}

public enum PageLinkAction: Equatable {
    /// The page's own tabs and hash links: stay in the window.
    case stay
    /// An http or https link the person clicked: hand it to the default browser.
    case openInBrowser
    /// Anything else is not followed.
    case block
}

public enum PageLinkRule {
    /// - Parameters:
    ///   - url: where the web view is asked to go.
    ///   - page: the page's own file URL.
    ///   - clicked: true when the person clicked a link (not a script or a redirect).
    public static func action(for url: URL?, page: URL, clicked: Bool) -> PageLinkAction {
        guard let url, let scheme = url.scheme?.lowercased() else { return .block }
        switch scheme {
        case "file":
            // Only the page itself, with any hash: no other file on the Mac, no file on
            // another machine (a host), and no query.
            guard url.host == nil || url.host == "", url.query == nil else { return .block }
            return url.standardizedFileURL.path == page.standardizedFileURL.path ? .stay : .block
        case "about":
            return url.absoluteString == "about:blank" ? .stay : .block
        case "http", "https":
            guard clicked, url.host?.isEmpty == false else { return .block }
            return .openInBrowser
        default:
            return .block
        }
    }
}

/// How wide the coach window opens.
public enum PageWindowSize {
    /// The width at which the Overview's top row of six stat tiles fits on one line.
    public static let openWidth: CGFloat = 1240

    /// The frame to open the window with, from the one it has (the default, or the size and
    /// position remembered from last time) and the visible part of its screen.
    ///
    /// A window narrower than `openWidth` is widened to it; on a screen that is narrower
    /// still it takes the screen's whole visible width. A window already wide enough is
    /// left as it is. The position is kept: the window is only pulled left as far as it
    /// must be to stay on the screen. Height and vertical position never change.
    public static func onOpen(frame: CGRect, visible: CGRect) -> CGRect {
        let want = min(openWidth, visible.width)
        guard want > 0, frame.width < want - 0.5 else { return frame }
        var out = frame
        out.size.width = want
        if out.maxX > visible.maxX { out.origin.x = visible.maxX - want }
        if out.minX < visible.minX { out.origin.x = visible.minX }
        return out
    }
}

public enum PageBuildOutcome: Equatable {
    case built
    case failed(String)
}

public enum PageBuild {
    /// A first build on a Mac with a long history reads every transcript and its trend
    /// cache is saved only at the end, so the limit is generous: stopping it early would
    /// make every later try start from nothing again.
    public static let timeoutSeconds: TimeInterval = 180
    /// A failure line longer than this is cut. The coach's longest message is about 320.
    public static let reasonLimit = 600

    public static let notSetUp = "The coach is not set up on this Mac yet. Run /coach setup in Claude Code."
    public static let tookTooLong = "Building the page took longer than three minutes and was stopped."
    public static let oldCoach = "This version of the coach cannot build the page for the window yet. Update the Agentic Coach plugin, then try again."
    public static let noPython = "Python 3 was not found on this Mac."

    /// Where Python 3 is looked for, in the order the coach's own setup falls back through
    /// (setup_check.py): Homebrew on Apple silicon, Homebrew on Intel, then the system one.
    /// The system one comes last because without the developer tools it is only a stand-in
    /// that asks to install them.
    public static let pythonPlaces = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]

    /// The first place that holds something that can be run, or nil.
    public static func python(canRun: (String) -> Bool) -> String? {
        pythonPlaces.first(where: canRun)
    }

    /// What to show when the page command ends. `errors` is what it wrote to standard error.
    public static func outcome(exitCode: Int32, errors: String) -> PageBuildOutcome {
        if exitCode == 0 { return .built }
        // The command's own last line says why, for example "coach: config.json cannot be read ...".
        let lines = errors.split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        guard var last = lines.last else {
            // An older coach answers a word it does not know with its help text and code 2.
            if exitCode == 2 { return .failed(PageBuild.oldCoach) }
            return .failed("The page build stopped without saying why (code \(exitCode)).")
        }
        if last.hasPrefix("coach: ") { last.removeFirst("coach: ".count) }
        if last.count > reasonLimit { last = String(last.prefix(reasonLimit)) + "..." }
        // Start with a capital, unless the line opens with a file name or a path.
        let first = last.split(separator: " ").first.map(String.init) ?? ""
        if first.contains(".") || first.contains("/") { return .failed(last) }
        return .failed(last.prefix(1).uppercased() + last.dropFirst())
    }
}
