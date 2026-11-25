import CoreGraphics
import Foundation
import Testing
@testable import CoachCore

@Suite struct CoachPageRules {
    private let home = "/Users/someone"
    private let page = URL(fileURLWithPath: "/Users/someone/.claude/coach/site/index.html")

    @Test func pathsFollowTheCoachHome() {
        #expect(CoachPaths.coachHome(environment: [:], home: home) == "/Users/someone/.claude/coach")
        #expect(CoachPaths.pageFile(environment: [:], home: home) == "/Users/someone/.claude/coach/site/index.html")
        #expect(CoachPaths.launcher(environment: [:], home: home) == "/Users/someone/.claude/coach/coach_live.py")

        let moved = ["COACH_HOME": "/tmp/coach"]
        #expect(CoachPaths.pageFile(environment: moved, home: home) == "/tmp/coach/site/index.html")
        #expect(CoachPaths.launcher(environment: moved, home: home) == "/tmp/coach/coach_live.py")
        #expect(CoachPaths.coachHome(environment: ["COACH_HOME": "~/c"], home: home) == "/Users/someone/c")
        #expect(CoachPaths.coachHome(environment: ["COACH_HOME": ""], home: home) == "/Users/someone/.claude/coach")

        // COACH_FEED moves only the feed; the page and launcher stay in the coach home.
        #expect(CoachPaths.pageFile(environment: ["COACH_FEED": "/x/live.json"], home: home) == "/Users/someone/.claude/coach/site/index.html")
        // COACH_LAUNCHER names another script; the page stays where it is.
        let other = ["COACH_LAUNCHER": "/work/coach/live.py", "COACH_HOME": "/tmp/coach"]
        #expect(CoachPaths.launcher(environment: other, home: home) == "/work/coach/live.py")
        #expect(CoachPaths.pageFile(environment: other, home: home) == "/tmp/coach/site/index.html")
    }

    @Test func eachPanelLinkOpensItsTab() {
        #expect(PageTab.current.fragment == nil) // Open coach page
        #expect(PageTab.today.fragment == "today") // See all N tips on the coach page
        #expect(PageTab.overview.fragment == "overview") // N more on the coach page
    }

    @Test func thePagesOwnTabsAndHashLinksStayInTheWindow() {
        func action(_ text: String, clicked: Bool = true) -> PageLinkAction {
            PageLinkRule.action(for: URL(string: text), page: page, clicked: clicked)
        }
        #expect(action("file:///Users/someone/.claude/coach/site/index.html") == .stay)
        #expect(action("file:///Users/someone/.claude/coach/site/index.html#today") == .stay)
        #expect(action("file:///Users/someone/.claude/coach/site/index.html#cost/2026-11-23", clicked: false) == .stay)
        #expect(action("file:///Users/someone/.claude/coach/site/../site/index.html#week") == .stay)
        #expect(action("about:blank", clicked: false) == .stay)
    }

    @Test func clickedWebLinksGoToTheBrowser() {
        func action(_ text: String, clicked: Bool) -> PageLinkAction {
            PageLinkRule.action(for: URL(string: text), page: page, clicked: clicked)
        }
        #expect(action("https://docs.claude.com/en/docs/claude-code", clicked: true) == .openInBrowser)
        #expect(action("http://example.com/a?b=c#d", clicked: true) == .openInBrowser)
        #expect(action("HTTPS://EXAMPLE.COM/", clicked: true) == .openInBrowser)
        // A script or a redirect never opens the browser by itself.
        #expect(action("https://example.com/", clicked: false) == .block)
        #expect(action("https:///no-host", clicked: true) == .block)
    }

    @Test func nothingElseIsFollowed() {
        func action(_ text: String) -> PageLinkAction {
            PageLinkRule.action(for: URL(string: text), page: page, clicked: true)
        }
        #expect(action("file:///etc/passwd") == .block)
        // The page's own path, but with a query or on another machine.
        #expect(action("file:///Users/someone/.claude/coach/site/index.html?x=1") == .block)
        #expect(action("file:///Users/someone/.claude/coach/site/index.html?x=1#today") == .block)
        #expect(action("file://otherhost/Users/someone/.claude/coach/site/index.html") == .block)
        #expect(action("file:///Users/someone/.claude/coach/config.json") == .block)
        #expect(action("file:///Users/someone/.claude/coach/site/other.html") == .block)
        #expect(action("javascript:alert(1)") == .block)
        #expect(action("data:text/html,hello") == .block)
        #expect(action("about:srcdoc") == .block)
        #expect(action("mailto:someone@example.com") == .block)
        #expect(action("x-apple.systempreferences:") == .block)
        #expect(PageLinkRule.action(for: nil, page: page, clicked: true) == .block)
    }

    @Test func coachWindowOpensWideEnoughForSixTiles() {
        let screen = CGRect(x: 0, y: 0, width: 2560, height: 1320)
        #expect(PageWindowSize.openWidth >= 1240)
        // a remembered narrower size is widened, where it was left
        #expect(PageWindowSize.onOpen(frame: CGRect(x: 300, y: 200, width: 1000, height: 700), visible: screen)
            == CGRect(x: 300, y: 200, width: 1240, height: 700))
        // the first-open size and anything wider are left alone
        #expect(PageWindowSize.onOpen(frame: CGRect(x: 300, y: 200, width: 1240, height: 780), visible: screen)
            == CGRect(x: 300, y: 200, width: 1240, height: 780))
        #expect(PageWindowSize.onOpen(frame: CGRect(x: 100, y: 100, width: 1800, height: 900), visible: screen)
            == CGRect(x: 100, y: 100, width: 1800, height: 900))
        // near the right edge: pulled left only as far as needed to stay on the screen
        #expect(PageWindowSize.onOpen(frame: CGRect(x: 1500, y: 200, width: 1000, height: 700), visible: screen)
            == CGRect(x: 1320, y: 200, width: 1240, height: 700))
        // a smaller screen: its whole visible width, no more
        let laptop = CGRect(x: 0, y: 40, width: 1147, height: 700)
        #expect(PageWindowSize.onOpen(frame: CGRect(x: 60, y: 80, width: 1000, height: 620), visible: laptop)
            == CGRect(x: 0, y: 80, width: 1147, height: 620))
        #expect(PageWindowSize.onOpen(frame: CGRect(x: 0, y: 80, width: 1147, height: 620), visible: laptop)
            == CGRect(x: 0, y: 80, width: 1147, height: 620))
        // a screen to the left of the main one (negative coordinates): position kept there too
        let left = CGRect(x: -1920, y: 238, width: 1920, height: 1050)
        #expect(PageWindowSize.onOpen(frame: CGRect(x: -1900, y: 300, width: 900, height: 700), visible: left)
            == CGRect(x: -1900, y: 300, width: 1240, height: 700))
        #expect(PageWindowSize.onOpen(frame: CGRect(x: -1000, y: 300, width: 900, height: 700), visible: left)
            == CGRect(x: -1240, y: 300, width: 1240, height: 700))
        // nothing to measure against: left alone
        #expect(PageWindowSize.onOpen(frame: CGRect(x: 0, y: 0, width: 900, height: 600), visible: .zero)
            == CGRect(x: 0, y: 0, width: 900, height: 600))
    }

    @Test func aFailedBuildGivesItsReason() {
        #expect(PageBuild.outcome(exitCode: 0, errors: "") == .built)
        #expect(PageBuild.outcome(exitCode: 0, errors: "wrote /x/site/index.html: 245935 bytes\n") == .built)
        // The coach's own line, without its "coach: " mark.
        #expect(PageBuild.outcome(exitCode: 2, errors: "coach: /x/config.json cannot be read as the coach's settings.\n")
            == .failed("/x/config.json cannot be read as the coach's settings."))
        #expect(PageBuild.outcome(exitCode: 1, errors: "coach: the agentic-coach plugin was not found\n")
            == .failed("The agentic-coach plugin was not found"))
        #expect(PageBuild.outcome(exitCode: 2, errors: "coach: config.json cannot be read.\n") == .failed("config.json cannot be read."))
        // A traceback: the last line is the one that names the error.
        let trace = "Traceback (most recent call last):\n  File \"live.py\", line 3, in page\n    boom()\nValueError: no reports folder\n\n"
        #expect(PageBuild.outcome(exitCode: 1, errors: trace) == .failed("ValueError: no reports folder"))
        #expect(PageBuild.outcome(exitCode: 2, errors: "") == .failed(PageBuild.oldCoach))
        #expect(PageBuild.outcome(exitCode: 9, errors: " \n") == .failed("The page build stopped without saying why (code 9)."))
        // The coach's longest message (settings that cannot be read, about 320 characters) is shown whole.
        let settings = "/Users/someone/.claude/coach/config.json cannot be read as the coach's settings (not valid JSON, or one of exclude, "
            + "only_account, unknown_account, exclude_dates has a value that cannot be used). "
            + "Nothing was built, so that nothing you chose to leave out is shown by mistake. "
            + "Fix or delete the file, then run again."
        #expect(settings.count > 300)
        #expect(PageBuild.outcome(exitCode: 2, errors: "coach: " + settings + "\n") == .failed(settings))
        // A very long line is cut.
        guard case .failed(let long) = PageBuild.outcome(exitCode: 1, errors: String(repeating: "x", count: 900)) else {
            Issue.record("should fail")
            return
        }
        #expect(long.count == PageBuild.reasonLimit + 3 && long.hasSuffix("..."))
    }

    @Test func pythonIsLookedForWhereTheCoachsSetupLooks() {
        #expect(PageBuild.pythonPlaces == ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"])
        #expect(PageBuild.python(canRun: { _ in true }) == "/opt/homebrew/bin/python3")
        #expect(PageBuild.python(canRun: { $0 == "/usr/bin/python3" }) == "/usr/bin/python3")
        #expect(PageBuild.python(canRun: { $0.hasPrefix("/usr/") }) == "/usr/local/bin/python3")
        #expect(PageBuild.python(canRun: { _ in false }) == nil)
    }
}
