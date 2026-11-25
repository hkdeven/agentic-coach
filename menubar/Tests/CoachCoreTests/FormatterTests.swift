import Foundation
import Testing
@testable import CoachCore

private let plus2 = TimeZone(secondsFromGMT: 7200)!
private let newYork = TimeZone(identifier: "America/New_York")!
private func at(_ text: String) -> Date { FeedDates.parse(text)! }
private let now = at("2026-11-23T11:41:00+02:00")

@Suite struct Formatters {
    @Test func money() {
        #expect(Fmt.money(27.8) == "$27.80")
        #expect(Fmt.money(7509.94) == "$7,509.94")
        #expect(Fmt.money(1_234_567.5) == "$1,234,567.50")
        #expect(Fmt.money(0.92) == "$0.92")
        #expect(Fmt.money(0.005) == "$0.01")
        #expect(Fmt.money(0) == "$0.00")
        #expect(Fmt.money(999.999) == "$1,000.00")
        #expect(Fmt.money(-3.5) == "-$3.50")
        #expect(Fmt.money(nil) == "$0.00")
        #expect(Fmt.money(.nan) == "$0.00")
    }

    @Test func counts() {
        #expect(Fmt.count(5120) == "5,120")
        #expect(Fmt.count(61300) == "61,300")
        #expect(Fmt.count(403) == "403")
        #expect(Fmt.count(nil) == "0")
        #expect(Fmt.counted(403, one: "call", many: "calls") == "403 calls")
        #expect(Fmt.counted(1, one: "session", many: "sessions") == "1 session")
        #expect(Fmt.counted(nil, one: "call", many: "calls") == "0 calls")
        #expect(Fmt.counted(5120, one: "call", many: "calls") == "5,120 calls")
    }

    @Test func tokens() {
        #expect(Fmt.tokens(57_300_000) == "57.3M")
        #expect(Fmt.tokens(14_600_000) == "14.6M")
        #expect(Fmt.tokens(9_100_000_000) == "9.1B")
        #expect(Fmt.tokens(742_000_000) == "742M")
        #expect(Fmt.tokens(359_000) == "359k")
        #expect(Fmt.tokens(1_500) == "1.5k")
        #expect(Fmt.tokens(950) == "950")
        #expect(Fmt.tokens(999_700_000) == "1.0B")
        #expect(Fmt.tokens(999_600) == "1.0M")
        #expect(Fmt.tokens(99_970_000) == "100M")
        #expect(Fmt.tokens(2_500_000_000_000) == "2,500B")
        #expect(Fmt.tokens(0) == "0")
        #expect(Fmt.tokens(nil) == "0")
        #expect(Fmt.tokens(-5) == "0")
    }

    @Test func percents() {
        #expect(Fmt.percent(rate: 0.82) == "82%")
        #expect(Fmt.percent(rate: 0.955) == "96%")
        #expect(Fmt.percent(rate: 1) == "100%")
        #expect(Fmt.percent(rate: nil) == nil)
        #expect(Fmt.percent(points: 94) == "94%")
        #expect(Fmt.percent(points: 7.4) == "7%")
        #expect(Fmt.percent(points: nil) == nil)
    }

    @Test func datesAndTimesUseTheGivenZone() {
        let t = at("2026-11-23T10:00:00+02:00")
        #expect(Fmt.shortDate(t, zone: plus2) == "Nov 23")
        #expect(Fmt.clock(t, zone: plus2) == "10:00")
        #expect(Fmt.clock(t, zone: newYork) == "03:00")
        #expect(Fmt.shortDate(at("2026-11-08T00:00:00+02:00"), zone: plus2) == "Nov 8")
        #expect(Fmt.shortDate(at("2026-11-08T00:00:00+02:00"), zone: newYork) == "Nov 7")
    }

    @Test func durations() {
        #expect(Fmt.duration(2 * 3600 + 43 * 60) == "2h 43m")
        #expect(Fmt.duration(2 * 86400 + 18 * 3600 + 59) == "2d 18h")
        #expect(Fmt.duration(72 * 60) == "1h 12m")
        #expect(Fmt.duration(43 * 60) == "43m")
        #expect(Fmt.duration(20) == "under 1m")
        #expect(Fmt.duration(-100) == "under 1m")
        #expect(Fmt.roughDuration(6 * 3600) == "6h")
        #expect(Fmt.roughDuration(5.6 * 3600) == "6h")
        #expect(Fmt.roughDuration(20 * 60) == "20m")
        #expect(Fmt.roughDuration(3 * 86400) == "3d")
        #expect(Fmt.roughDuration(0) == "1m")
    }

    @Test func liveLineWhileTheFeedIsFresh() {
        // now is 11:41:00; the time is the Mac's own zone, to the second
        #expect(Fmt.liveLine(at("2026-11-23T11:39:07+02:00"), now: now, zone: plus2) == LiveLine(text: "Live, updated 11:39:07", fresh: true))
        #expect(Fmt.liveLine(at("2026-11-23T11:39:07+02:00"), now: now, zone: newYork).text == "Live, updated 04:39:07")
        #expect(Fmt.liveLine(at("2026-11-23T11:26:00+02:00"), now: now, zone: plus2).fresh) // 15 minutes exactly
        #expect(Fmt.liveLine(at("2026-11-23T11:50:00+02:00"), now: now, zone: plus2).fresh) // a clock a little ahead
        #expect(Fmt.clockSeconds(at("2026-11-23T14:16:05+02:00"), zone: plus2) == "14:16:05")
    }

    @Test func anOldFeedIsNotLive() throws {
        // Over 15 minutes old: an orange dot, and the words say it too.
        #expect(Fmt.liveLine(at("2026-11-23T11:25:59+02:00"), now: now, zone: plus2) == LiveLine(text: "Not live, updated 11:25", fresh: false))
        #expect(Fmt.liveLine(at("2026-11-23T08:30:00+02:00"), now: now, zone: plus2).text == "Not live, updated 08:30")
        #expect(Fmt.liveLine(at("2026-11-22T23:50:00+02:00"), now: now, zone: plus2).text == "Not live, updated Nov 22")
        #expect(Fmt.liveLine(at("2026-11-21T08:30:00+02:00"), now: now, zone: plus2).text == "Not live, updated Nov 21")
        #expect(Fmt.liveLine(at("2025-11-23T11:00:00+02:00"), now: now, zone: plus2).text == "Not live, updated Nov 23") // a year old, same date
        #expect(Fmt.liveLine(nil, now: now, zone: plus2) == LiveLine(text: "Not updated yet", fresh: false))
        // The words and the stale flag (which also hides running sessions) switch together.
        let feed = try readyFeed("live.json") // updated 11:41
        for time in ["2026-11-23T11:41:00+02:00", "2026-11-23T11:56:00+02:00", "2026-11-23T11:56:01+02:00", "2026-11-24T09:00:00+02:00"] {
            let t = at(time)
            #expect(feed.isStale(now: t) == !Fmt.liveLine(feed.updated, now: t, zone: plus2).fresh)
        }
        #expect(!feed.isStale(now: at("2026-11-23T11:56:00+02:00")))
        #expect(feed.isStale(now: at("2026-11-23T11:56:01+02:00")))
    }

    @Test func limitsPastTheirResetAreDropped() throws {
        let limits = try readyFeed("live.json").limits // 5-hour resets Nov 23 14:24; both weekly rows Nov 26 05:41
        #expect(limits.liveRows(now: now).count == 3)
        #expect(limits.liveRows(now: at("2026-11-23T14:23:59+02:00")).count == 3)
        #expect(limits.liveRows(now: at("2026-11-23T14:24:00+02:00")).map(\.key) == ["weekly", "weekly_fable"])
        #expect(limits.liveRows(now: at("2026-11-26T05:41:00+02:00")).isEmpty) // the panel then hides the block
        // A row with no reset time is kept.
        guard case .ready(let bare) = decodeFeed(#"{"limits": {"rows": [{"used_pct": 50}]}}"#) else {
            Issue.record("should decode")
            return
        }
        #expect(bare.limits.liveRows(now: now).count == 1)
    }

    @Test func changeLines() {
        #expect(Fmt.change(now: 0.82, previous: 0.76, scale: 100, period: .today) == Change(lead: "Up 6", rest: "on yesterday", delta: 6))
        #expect(Fmt.change(now: 0.96, previous: 0.96, scale: 100, period: .week) == Change(lead: "", rest: "same as the week before", delta: 0))
        #expect(Fmt.change(now: 0.79, previous: 0.81, scale: 100, period: .week) == Change(lead: "Down 2", rest: "on the week before", delta: -2))
        #expect(Fmt.change(now: 3, previous: 2, scale: 1, period: .today) == Change(lead: "Up 1", rest: "on yesterday", delta: 1))
        // the whole line, as a screen reader hears it
        #expect(Fmt.change(now: 3, previous: 2, scale: 1, period: .today)?.text == "Up 1 on yesterday")
        #expect(Fmt.change(now: 0.96, previous: 0.96, scale: 100, period: .today)?.text == "same as yesterday")
        #expect(Fmt.change(now: 118, previous: 131, scale: 1, period: .month) == Change(lead: "Down 13", rest: "on the 30 days before", delta: -13))
        #expect(Fmt.change(now: 0.8, previous: nil, scale: 100, period: .month) == nil)
        #expect(Fmt.change(now: nil, previous: 0.8, scale: 100, period: .month) == nil)
    }

    @Test func tokensSummaryAndAxisLabels() throws {
        let feed = try readyFeed("live.json")
        let today = try #require(feed.today), week = try #require(feed.week), month = try #require(feed.month)
        #expect(Fmt.tokensSummary(today, zone: plus2) == "57.3M, peak 14.6M at 10:00")
        #expect(Fmt.tokensSummary(week, zone: plus2) == "742M, peak 142M on Nov 20")
        #expect(Fmt.tokensSummary(month, zone: plus2) == "9.1B, peak 512M on Nov 12")
        #expect(Fmt.axisLabels(today, zone: plus2) == ["00:00", "06:00", "Now"])
        #expect(Fmt.axisLabels(week, zone: plus2) == ["Nov 17", "Nov 20", "Today"])
        #expect(Fmt.axisLabels(month, zone: plus2) == ["Oct 25", "Nov 9", "Today"])

        let sparse = try #require(try readyFeed("nulls.json").today)
        #expect(Fmt.tokensSummary(sparse, zone: plus2) == "0")
        #expect(Fmt.axisLabels(sparse, zone: plus2) == ["", "", "Today"])
    }

    @Test func limitLines() throws {
        let rows = try readyFeed("live.json").limits.rows
        #expect(Fmt.limitTitle(rows[0], now: now) == "5-hour, resets in 2h 43m")
        #expect(Fmt.paceLine(rows[0], now: now) == "39% in reserve")
        #expect(Fmt.limitTitle(rows[1], now: now) == "Weekly, near limit, resets in 2d 18h")
        #expect(Fmt.paceLine(rows[1], now: now) == "33% over pace, runs out in about 6h")
        #expect(Fmt.limitTitle(rows[2], now: now) == "Weekly, Fable, resets in 2d 18h")
        #expect(Fmt.paceLine(rows[2], now: now) == "10% over pace")

        // A reset or run-out time already in the past is left out.
        let later = at("2026-11-27T00:00:00+02:00")
        #expect(Fmt.limitTitle(rows[0], now: later) == "5-hour")
        #expect(Fmt.paceLine(rows[1], now: later) == "33% over pace")

        guard case .ready(let bare) = decodeFeed(#"{"limits": {"rows": [{"used_pct": 12}, {"pace_pct": 0.2}]}}"#) else {
            Issue.record("should decode")
            return
        }
        #expect(Fmt.limitTitle(bare.limits.rows[0], now: now) == "Limit")
        #expect(Fmt.paceLine(bare.limits.rows[0], now: now) == nil)
        #expect(Fmt.paceLine(bare.limits.rows[1], now: now) == "On pace")
    }

    @Test func runningSessionLines() throws {
        let session = try readyFeed("live.json").running[0]
        #expect(Fmt.sessionTitle(session) == "agentic-coach, main")
        #expect(Fmt.sessionDetail(session, now: now) == "Fable 5.2, running 1h 12m")
        guard case .ready(let bare) = decodeFeed(#"{"running": [{}]}"#) else {
            Issue.record("should decode")
            return
        }
        #expect(Fmt.sessionTitle(bare.running[0]) == "Session")
        #expect(Fmt.sessionDetail(bare.running[0], now: now) == "")
    }

    @Test func activityRows() throws {
        let feed = try readyFeed("live.json")
        let today = try #require(feed.today), week = try #require(feed.week), month = try #require(feed.month)
        #expect(Fmt.shownActivities(today).count == 5)
        #expect(Fmt.shownActivities(today).last?.name == "Testing")
        #expect(Fmt.moreActivities(today) == "2 more in the coach window")
        #expect(Fmt.moreActivities(week) == nil)
        #expect(Fmt.moreActivities(month) == "1 more in the coach window")
    }

    @Test func heatBarShare() {
        #expect(Fmt.heatShare(13.09, largest: 13.09) == 1)
        #expect(Fmt.heatShare(6.545, largest: 13.09) == 0.5)
        #expect(Fmt.heatShare(0, largest: 13.09) == 0)
        #expect(Fmt.heatShare(nil, largest: 13.09) == 0)
        #expect(Fmt.heatShare(5, largest: nil) == 0)
        #expect(Fmt.heatShare(5, largest: 0) == 0)
        #expect(Fmt.heatShare(20, largest: 10) == 1)
    }

    @Test func coachTipIsShownOnlyWhenThereIsOne() throws {
        let feed = try readyFeed("live.json") // three tips, the most severe first
        #expect(Fmt.shownTip(feed)?.title == "agentic-coach, main has passed 600k of context.")
        #expect(Fmt.shownTip(feed)?.advice == "Compact or hand off before the next big step.")
        #expect(Fmt.tipsLink(count: feed.tips.count) == "See all 3 tips in the coach window")
        #expect(Fmt.tipsLink(count: 1) == "See this tip in the coach window")
        // no tips: no tile
        #expect(Fmt.shownTip(try readyFeed("minimal.json")) == nil)
        // a tip with no words at all is not a tip
        #expect(Fmt.shownTip(try readyFeed("nulls.json")) == nil)
        guard case .ready(let one) = decodeFeed(#"{"tips": [{"title": null, "advice": "Split the request."}]}"#) else {
            Issue.record("should decode")
            return
        }
        #expect(Fmt.shownTip(one)?.advice == "Split the request.")
        #expect(Fmt.shownTip(one)?.title == nil)
    }

    @Test func everyRunningSessionGetsATileUpToFour() throws {
        func feed(_ count: Int) -> Feed? {
            let rows = (1...max(count, 1)).map { "{\"project\": \"p\($0)\", \"started\": \"2026-11-23T10:0\($0 % 10):00+02:00\"}" }
            guard case .ready(let f) = decodeFeed("{\"updated\": \"2026-11-23T11:41:00+02:00\", \"running\": [\(count == 0 ? "" : rows.joined(separator: ","))]}") else { return nil }
            return f
        }
        for count in 0...4 {
            let sessions = try #require(feed(count)).liveRunning(now: now)
            #expect(Fmt.shownRunning(sessions).count == count)
            #expect(Fmt.moreRunning(count: sessions.count) == nil)
        }
        let five = try #require(feed(5)).liveRunning(now: now)
        #expect(Fmt.shownRunning(five).map(\.project) == ["p1", "p2", "p3", "p4"]) // the feed's order: touched last first
        #expect(Fmt.moreRunning(count: five.count) == "1 more in the coach window")
        let nine = try #require(feed(9)).liveRunning(now: now)
        #expect(Fmt.shownRunning(nine).count == 4)
        #expect(Fmt.moreRunning(count: nine.count) == "5 more in the coach window")
        #expect(Fmt.runningTileLimit == 4)
        // an old feed says nothing about what is running: no tiles, no line
        let old = try #require(feed(9)).liveRunning(now: at("2026-11-23T12:30:00+02:00"))
        #expect(Fmt.shownRunning(old).isEmpty && Fmt.moreRunning(count: old.count) == nil)
    }

    @Test func tileWords() throws {
        #expect(PeriodKey.allCases.map(Fmt.spendLabel) == ["Spend today", "Spend, 7 days", "Spend, 30 days", "Spend, all time"])
        let feed = try readyFeed("live.json")
        let today = try #require(feed.today)
        // the first project of the period and its cost against the period's
        let top = try #require(Fmt.topProject(today))
        #expect(top.name == "agentic-coach")
        #expect(top.detail == "$20.10 of $27.80")
        #expect(today.projects.map(\.name) == ["agentic-coach", "Other"])
        #expect(today.projects[0].sessions == 2 && today.projects[0].share == 0.72)
        #expect(Fmt.topProject(try #require(feed.week)) == nil) // no project in the period
        // running now
        #expect(Fmt.runningDetail(feed.running[0], now: now) == "Fable 5.2, running 1h 12m, 359k left")
        guard case .ready(let bare) = decodeFeed(#"{"running": [{}, {"model": "Opus 5.5"}], "periods": {"today": {"projects": [{"usd": 3}, 7, {"name": "x"}]}}}"#) else {
            Issue.record("should decode")
            return
        }
        #expect(Fmt.runningDetail(bare.running[0], now: now) == "")
        #expect(Fmt.runningDetail(bare.running[1], now: now) == "Opus 5.5")
        #expect(bare.today?.projects.count == 2) // the entry that is not an object is dropped
        #expect(Fmt.topProject(try #require(bare.today)) == nil) // the first project has no name
    }

    @Test func periodWords() {
        #expect(PeriodKey.allCases == [.today, .week, .month, .all])
        #expect(PeriodKey.allCases.map(\.title) == ["Today", "7 days", "30 days", "All time"])
        #expect(PeriodKey.allCases.map(\.fallbackLabel) == ["Today", "Last 7 days", "Last 30 days", "All time"])
        #expect(PeriodKey.allCases.map(\.hasPrevious) == [true, true, true, false])
    }

    @Test func allTimeHasNoChangeLines() throws {
        let all = try #require(try readyFeed("live.json").all)
        // null prev figures: no line
        #expect(Fmt.change(now: all.firstTry?.rate, previous: all.firstTry?.prevRate, scale: 100, period: .all) == nil)
        #expect(Fmt.change(now: all.corrections?.count, previous: all.corrections?.prevCount, scale: 1, period: .all) == nil)
        // and none even if a feed sent some by mistake
        #expect(Fmt.change(now: 0.8, previous: 0.7, scale: 100, period: .all) == nil)
        #expect(Fmt.change(now: 0.8, previous: 0.7, scale: 100, period: .month) != nil)
    }

    @Test func weeklyPointsReadLikeDailyOnes() throws {
        let all = try #require(try readyFeed("live.json").all)
        #expect(Fmt.axisLabels(all, zone: plus2) == ["Sep 7", "Oct 19", "Today"])
        #expect(Fmt.tokensSummary(all, zone: plus2) == "5.7B, peak 742M in the week of Nov 16")
    }

    @Test func detailLineOnlyWhenDetailStartsLater() throws {
        let all = try #require(try readyFeed("live.json").all)
        #expect(Fmt.detailLine(all, zone: plus2) == "Detail from Oct 25")
        func line(_ since: String, _ detail: String) -> String? {
            guard case .ready(let feed) = decodeFeed("{\"periods\": {\"all\": {\"since\": \(since), \"detail_since\": \(detail)}}}"),
                  let period = feed.all else { return "did not decode" }
            return Fmt.detailLine(period, zone: plus2)
        }
        #expect(line(#""2026-11-09T00:00:00+02:00""#, #""2026-11-10T00:00:00+02:00""#) == "Detail from Nov 10")
        #expect(line(#""2026-11-09T00:00:00+02:00""#, #""2026-11-09T00:00:00+02:00""#) == nil) // the whole period has detail
        #expect(line(#""2026-11-09T00:00:00+02:00""#, #""2026-11-09T18:00:00+02:00""#) == nil) // later the same day
        #expect(line(#""2026-11-09T00:00:00+02:00""#, #""2026-11-01T00:00:00+02:00""#) == nil) // earlier: nothing to say
        #expect(line(#""2026-11-09T00:00:00+02:00""#, "null") == nil)
        #expect(line("null", #""2026-11-10T00:00:00+02:00""#) == nil)
        // the other periods never carry one
        #expect(Fmt.detailLine(try #require(try readyFeed("live.json").month), zone: plus2) == nil)
    }
}

@Suite struct MenuBarRule {
    private func state(_ json: String, compact: Bool = false) -> MenuBarState {
        MenuBarState.make(state: decodeFeed(json), compact: compact, now: now)
    }

    @Test func spendAlone() {
        let s = state(#"{"periods": {"today": {"cost_usd": 27.8}}}"#)
        #expect(s == MenuBarState(spend: "$27.80", running: false, limit: nil))
        #expect(s.spokenText == "Claude Coach, $27.80 today")
    }

    @Test func dotWhileASessionRuns() {
        let s = state(#"{"periods": {"today": {"cost_usd": 1234.5}}, "running": [{"project": "a"}]}"#)
        #expect(s == MenuBarState(spend: "$1,234.50", running: true, limit: nil))
    }

    @Test func highestLimitAtNinetyOrMore() {
        let s = state(#"{"periods": {"today": {"cost_usd": 27.8}}, "limits": {"rows": [{"label": "5-hour", "used_pct": 91}, {"label": "Weekly", "used_pct": 94}, {"label": "Weekly, Fable", "used_pct": 71}]}}"#)
        #expect(s.limit == "Weekly 94%")
        #expect(s.spokenText == "Claude Coach, $27.80 today, Weekly 94%, near limit")
    }

    @Test func ninetyCountsEightyNineDoesNot() {
        #expect(state(#"{"limits": {"rows": [{"label": "Weekly", "used_pct": 90}]}}"#).limit == "Weekly 90%")
        #expect(state(#"{"limits": {"rows": [{"label": "Weekly", "used_pct": 89.9, "level": "crit"}]}}"#).limit == nil)
        #expect(state(#"{"limits": {"rows": [{"used_pct": 97}]}}"#).limit == "Limit 97%")
    }

    @Test func compactShowsTheMarkAlone() {
        let s = state(#"{"periods": {"today": {"cost_usd": 27.8}}, "running": [{}], "limits": {"rows": [{"label": "Weekly", "used_pct": 94}]}}"#, compact: true)
        #expect(s == MenuBarState(spend: nil, running: false, limit: nil))
        #expect(s.spokenText == "Claude Coach")
    }

    @Test func noFeedShowsTheMarkAlone() {
        let empty = MenuBarState(spend: nil, running: false, limit: nil)
        #expect(MenuBarState.make(state: .missing, compact: false, now: now) == empty)
        #expect(MenuBarState.make(state: .unreadable, compact: false, now: now) == empty)
        #expect(MenuBarState.make(state: .needsUpdate, compact: false, now: now) == empty)
        #expect(state("{}") == empty)
    }

    @Test func anOldFeedShowsNoRunningDot() throws {
        let feed = try readyFeed("live.json") // updated 11:41
        func running(_ time: String) -> Bool {
            MenuBarState.make(state: .ready(feed), compact: false, now: at(time)).running
        }
        #expect(running("2026-11-23T11:56:00+02:00"))
        #expect(!running("2026-11-23T11:57:00+02:00"))
        #expect(feed.liveRunning(now: at("2026-11-23T11:56:00+02:00")).count == 1)
        #expect(feed.liveRunning(now: at("2026-11-24T09:00:00+02:00")).isEmpty)
    }

    @Test func aLimitThatHasResetIsNotShown() throws {
        let feed = try readyFeed("live.json") // Weekly 94%, resets Nov 26 05:41
        #expect(MenuBarState.make(state: .ready(feed), compact: false, now: at("2026-11-26T05:40:00+02:00")).limit == "Weekly 94%")
        #expect(MenuBarState.make(state: .ready(feed), compact: false, now: at("2026-11-26T05:41:00+02:00")).limit == nil)
    }

    @Test func theFixtureShowsEverything() throws {
        let s = MenuBarState.make(state: .ready(try readyFeed("live.json")), compact: false, now: now)
        #expect(s == MenuBarState(spend: "$27.80", running: true, limit: "Weekly 94%"))
    }
}
