import Foundation
import Testing
@testable import CoachCore

func fixturePath(_ name: String, file: String = #filePath) -> String {
    URL(fileURLWithPath: file)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .appendingPathComponent("Fixtures").appendingPathComponent(name).path
}

func readyFeed(_ name: String) throws -> Feed {
    guard case .ready(let feed) = FeedLoader.load(path: fixturePath(name)) else {
        throw FixtureError.notReady(name)
    }
    return feed
}

enum FixtureError: Error { case notReady(String) }

func decodeFeed(_ json: String) -> FeedState {
    FeedLoader.decode(Data(json.utf8))
}

@Suite struct FeedDecoding {
    @Test func fullFeed() throws {
        let feed = try readyFeed("live.json")
        #expect(feed.version == 1)
        #expect(feed.updated == FeedDates.parse("2026-11-23T11:41:00+02:00"))
        #expect(feed.pageURL?.absoluteString == "https://example.com/coach/")

        let today = try #require(feed.today)
        #expect(today.label == "Today, Nov 23")
        #expect(today.costUSD == 27.80)
        #expect(today.prevCostUSD == 145.30)
        #expect(today.calls == 403)
        #expect(today.sessions == 4)
        #expect(today.tokens == 57_300_000)
        #expect(today.seriesUnit == "hour")
        #expect(today.series.count == 12)
        #expect(today.series[10].tokens == 14_600_000)
        #expect(today.peak?.tokens == 14_600_000)
        #expect(today.firstTry?.rate == 0.82)
        #expect(today.firstTry?.prevRate == 0.76)
        #expect(today.cacheHit?.rate == 0.96)
        #expect(today.corrections?.count == 3)
        #expect(today.corrections?.prevCount == 2)

        // CHANGE 2: every activity with spend is listed, each with turns.
        #expect(today.activities.count == 7)
        #expect(today.activities[0].name == "Coding")
        #expect(today.activities[0].usd == 13.09)
        #expect(today.activities[0].turns == 74)
        #expect(today.activities[0].firstTry == 0.86)
        #expect(today.activities[1].firstTry == nil)

        #expect(feed.week?.series.count == 7)
        #expect(feed.month?.series.count == 30)
        #expect(feed.month?.firstTry?.prevRate == nil)

        #expect(feed.limits.rows.count == 3)
        #expect(feed.limits.rows[0].key == "five_hour")
        #expect(feed.limits.rows[0].pacePct == -39)
        #expect(feed.limits.rows[0].runsOutAt == nil)
        #expect(feed.limits.rows[1].level == .crit)
        #expect(feed.limits.rows[1].runsOutAt != nil)
        #expect(feed.limits.rows[2].level == .warn)

        #expect(feed.running.count == 1)
        #expect(feed.running[0].project == "agentic-coach")
        #expect(feed.running[0].contextLeftTokens == 359_000)
        #expect(feed.running[0].level == .warn)
        #expect(feed.tips.count == 3)
        #expect(feed.tips[0].pattern == "context_bloat")
    }

    @Test func allTimePeriod() throws {
        let feed = try readyFeed("live.json")
        let all = try #require(feed.all)
        #expect(feed.period(.all) == all)
        #expect(all.label == "All time, since Sep 9")
        #expect(all.costUSD == 10811.42)
        #expect(all.seriesUnit == "week")
        #expect(all.series.count == 12)
        #expect(all.series[0].t == FeedDates.parse("2026-09-07T00:00:00+02:00")) // a Monday
        #expect(all.since == FeedDates.parse("2026-09-09T00:00:00+02:00"))
        #expect(all.detailSince == FeedDates.parse("2026-10-25T00:00:00+02:00"))
        // no period before: every prev figure is null
        #expect(all.prevCostUSD == nil)
        #expect(all.firstTry?.prevRate == nil && all.cacheHit?.prevRate == nil && all.corrections?.prevCount == nil)
        // the other periods carry neither date
        #expect(feed.month?.since == nil && feed.month?.detailSince == nil)
        // an older feed has no such period
        #expect(try readyFeed("minimal.json").all == nil)
        guard case .ready(let odd) = decodeFeed(#"{"periods": {"all": {"since": 5, "detail_since": "soon"}}}"#) else {
            Issue.record("should decode")
            return
        }
        #expect(odd.all != nil && odd.all?.since == nil && odd.all?.detailSince == nil)
    }

    @Test func minimalFeed() throws {
        let feed = try readyFeed("minimal.json")
        #expect(feed.version == 1)
        #expect(feed.updated == nil)
        #expect(feed.pageURL == nil)
        #expect(feed.today == nil && feed.week == nil && feed.month == nil && feed.all == nil)
        #expect(feed.limits.rows.isEmpty)
        #expect(feed.running.isEmpty)
        #expect(feed.tips.isEmpty)
    }

    @Test func emptyObjectCountsAsVersionOne() {
        guard case .ready(let feed) = decodeFeed("{}") else {
            Issue.record("an empty object should decode")
            return
        }
        #expect(feed.version == 1)
    }

    @Test func nullAndWrongTypedFields() throws {
        let feed = try readyFeed("nulls.json")
        #expect(feed.updated == nil)
        #expect(feed.pageURL == nil)
        let today = try #require(feed.today)
        #expect(today.label == nil)
        #expect(today.costUSD == 4.5)
        #expect(today.calls == nil)
        #expect(today.peak == nil)
        #expect(today.cacheHit == nil)
        #expect(today.firstTry?.rate == nil)
        #expect(today.corrections?.count == nil) // "three" is not a number
        #expect(today.series.count == 2) // the string entry is dropped
        #expect(today.series[0].tokens == nil)
        #expect(today.activities.count == 1) // the null entry is dropped
        #expect(today.activities[0].turns == nil)
        #expect(feed.week == nil)
        #expect(feed.month == nil) // a string where a period should be
        #expect(feed.limits.rows.isEmpty)
        #expect(feed.running.isEmpty)
        #expect(feed.tips.isEmpty) // a tip with no words is dropped
    }

    @Test func futureVersion() {
        #expect(FeedLoader.load(path: fixturePath("future.json")) == .needsUpdate)
        #expect(decodeFeed(#"{"version": 1.5}"#) == .needsUpdate)
        #expect(decodeFeed(#"{"version": 99, "periods": 4}"#) == .needsUpdate)
    }

    @Test func missingAndUnreadableFiles() {
        #expect(FeedLoader.load(path: fixturePath("does-not-exist.json")) == .missing)
        #expect(decodeFeed("") == .unreadable)
        #expect(decodeFeed("not json") == .unreadable)
        #expect(decodeFeed("[1, 2]") == .unreadable)
        #expect(decodeFeed("null") == .unreadable)
    }

    @Test func absurdNumbersAreTreatedAsMissing() throws {
        // Each of these would stop the app if it reached a whole-number conversion.
        let json = #"""
        {"version": -1e300, "periods": {"today": {"cost_usd": 1e300, "calls": -1e40, "sessions": 1e19, "tokens": 1e30,
          "first_try": {"rate": 1e25, "prev_rate": 0.5}, "corrections": {"count": 1e99, "prev_count": 2},
          "activities": [{"name": "Coding", "usd": 1e18, "turns": 1e20, "first_try": 1e20}]}},
          "limits": {"rows": [{"label": "Weekly", "used_pct": 1e300, "pace_pct": -1e300}]},
          "running": [{"context_pct": 1e300, "context_left_tokens": 1e300}]}
        """#
        guard case .ready(let feed) = decodeFeed(json) else {
            Issue.record("should decode")
            return
        }
        let today = try #require(feed.today)
        #expect(feed.version == 1)
        #expect(today.costUSD == nil && today.calls == nil && today.sessions == nil && today.tokens == nil)
        #expect(today.firstTry?.rate == nil)
        #expect(today.corrections?.count == nil)
        #expect(today.activities[0].usd == nil && today.activities[0].turns == nil)
        #expect(feed.limits.rows[0].usedPct == nil && feed.limits.rows[0].pacePct == nil)
        let now = Date()
        #expect(MenuBarState.make(state: .ready(feed), compact: false, now: now) == MenuBarState(spend: nil, running: true, limit: nil))
        // The largest values that are let through still format without stopping.
        #expect(Fmt.money(1e15) == "$1,000,000,000,000,000.00")
        #expect(Fmt.tokens(1e15) == "1,000,000B")
        #expect(Fmt.count(-1e15) == "-1,000,000,000,000,000")
        #expect(Fmt.percent(rate: 1e15) != nil)
        #expect(Fmt.change(now: 1e15, previous: -1e15, scale: 100, period: .today) != nil)
    }

    @Test func onlyAPlainFileOfASaneSizeIsRead() throws {
        #expect(FeedLoader.load(path: fixturePath("")) == .unreadable) // a folder

        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("coach-tests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }

        // Valid JSON, but past the size cap: not read.
        let big = folder.appendingPathComponent("big.json")
        let padding = String(repeating: " ", count: FeedLoader.maxBytes)
        try Data(("{\"version\": 1}" + padding).utf8).write(to: big)
        #expect(FeedLoader.load(path: big.path) == .unreadable)

        // A feed reached through a symbolic link is still read.
        let link = folder.appendingPathComponent("live.json")
        try FileManager.default.createSymbolicLink(atPath: link.path, withDestinationPath: fixturePath("minimal.json"))
        guard case .ready = FeedLoader.load(path: link.path) else {
            Issue.record("a symlinked feed should load")
            return
        }
    }

    @Test func levelFallsBackToTheContractBands() {
        let state = decodeFeed(#"{"limits": {"rows": [{"used_pct": 95}, {"used_pct": 70}, {"used_pct": 69.9}, {"used_pct": 95, "level": "ok"}, {"level": "purple"}]}}"#)
        guard case .ready(let feed) = state else {
            Issue.record("should decode")
            return
        }
        #expect(feed.limits.rows.map(\.level) == [.crit, .warn, .ok, .ok, .ok])
    }

    @Test func pageURLMustBeAWebLink() {
        func url(_ value: String) -> URL? {
            guard case .ready(let feed) = decodeFeed("{\"page_url\": \(value)}") else { return nil }
            return feed.pageURL
        }
        #expect(url(#""https://example.com/coach/""#) != nil)
        #expect(url(#""http://localhost:8000/""#) != nil)
        #expect(url(#""file:///etc/passwd""#) == nil)
        #expect(url(#""javascript:alert(1)""#) == nil)
        #expect(url(#""""#) == nil)
        #expect(url("12") == nil)
    }

    @Test func datesWithOffsetsAndFractions() {
        let base = FeedDates.parse("2026-11-23T09:41:00Z")
        #expect(base != nil)
        #expect(FeedDates.parse("2026-11-23T11:41:00+02:00") == base)
        #expect(FeedDates.parse("2026-11-23T11:41:00.000+02:00") == base)
        #expect(FeedDates.parse("yesterday") == nil)
    }

    @Test func feedPathOrder() {
        let home = "/Users/someone"
        #expect(FeedLoader.resolvePath(arguments: ["app"], environment: [:], home: home) == "/Users/someone/.claude/coach/live.json")
        #expect(FeedLoader.resolvePath(arguments: ["app"], environment: ["COACH_HOME": "/tmp/c"], home: home) == "/tmp/c/live.json")
        #expect(FeedLoader.resolvePath(arguments: ["app"], environment: ["COACH_HOME": "/tmp/c", "COACH_FEED": "~/x.json"], home: home) == "/Users/someone/x.json")
        #expect(FeedLoader.resolvePath(arguments: ["app", "--feed", "/a/b.json"], environment: ["COACH_FEED": "/x.json"], home: home) == "/a/b.json")
        #expect(FeedLoader.resolvePath(arguments: ["app", "--feed"], environment: [:], home: home) == "/Users/someone/.claude/coach/live.json")
    }
}
