import Foundation

public enum PeriodKey: String, CaseIterable, Identifiable {
    case today, week, month, all

    public var id: String { rawValue }

    /// The words on the period button.
    public var title: String {
        switch self {
        case .today: return "Today"
        case .week: return "7 days"
        case .month: return "30 days"
        case .all: return "All time"
        }
    }

    /// The fallback line above the cost when the feed gives no label.
    public var fallbackLabel: String {
        switch self {
        case .today: return "Today"
        case .week: return "Last 7 days"
        case .month: return "Last 30 days"
        case .all: return "All time"
        }
    }

    /// What `prev_*` is measured against (the same-length period just before).
    public var previousPhrase: String {
        switch self {
        case .today: return "yesterday"
        case .week: return "the week before"
        case .month: return "the 30 days before"
        case .all: return "the time before" // never shown: All time has no period before it
        }
    }

    /// False for All time: there is nothing before it, so no change line is written.
    public var hasPrevious: Bool { self != .all }

    public var graphDescription: String {
        switch self {
        case .today: return "Line graph of tokens by hour today"
        case .week: return "Line graph of tokens by day over the last 7 days"
        case .month: return "Line graph of tokens by day over the last 30 days"
        case .all: return "Line graph of tokens over all time"
        }
    }
}

public struct Change: Equatable {
    /// The words that say which way it moved, "Up 6" or "Down 2": drawn in a status colour.
    /// Empty when nothing moved.
    public let lead: String
    /// The rest of the line: "on yesterday", or "same as yesterday" when nothing moved.
    public let rest: String
    /// Above zero is up, below zero is down, zero is the same.
    public let delta: Int

    /// The whole line: "Up 6 on yesterday", "same as yesterday".
    public var text: String { lead.isEmpty ? rest : "\(lead) \(rest)" }
}

/// The line beside the panel's name: whether the figures are live, and when they were written.
public struct LiveLine: Equatable {
    public let text: String
    /// True while the feed is under 15 minutes old: a green dot. False: an orange one.
    public let fresh: Bool

    public init(text: String, fresh: Bool) {
        self.text = text
        self.fresh = fresh
    }
}

/// Every number and date the app shows is formatted here.
public enum Fmt {
    private static let posix = Locale(identifier: "en_US_POSIX")

    // MARK: Numbers

    static func grouped(_ n: Int) -> String {
        let digits = String(abs(n))
        var out = ""
        for (i, ch) in digits.enumerated() {
            if i > 0 && (digits.count - i) % 3 == 0 { out.append(",") }
            out.append(ch)
        }
        return n < 0 ? "-" + out : out
    }

    /// $27.80, $7,509.94
    public static func money(_ value: Double?) -> String {
        guard let value, value.isFinite else { return "$0.00" }
        let cents = Int((abs(value) * 100).rounded())
        let sign = value < 0 && cents > 0 ? "-" : ""
        let fraction = cents % 100
        return "\(sign)$\(grouped(cents / 100)).\(fraction < 10 ? "0" : "")\(fraction)"
    }

    /// 5,120
    public static func count(_ value: Double?) -> String {
        guard let value, value.isFinite else { return "0" }
        return grouped(Int(value.rounded()))
    }

    /// 403 calls, 1 call
    public static func counted(_ value: Double?, one: String, many: String) -> String {
        let n = Int((value ?? 0).rounded())
        return "\(grouped(n)) \(n == 1 ? one : many)"
    }

    /// 950, 359k, 57.3M, 742M, 9.1B
    public static func tokens(_ value: Double?) -> String {
        guard let value, value.isFinite, value > 0 else { return "0" }
        if value.rounded() < 1000 { return String(Int(value.rounded())) }
        let units: [(Double, String)] = [(1e3, "k"), (1e6, "M"), (1e9, "B")]
        for (i, unit) in units.enumerated() {
            let scaled = value / unit.0
            // 999.7M reads better as 1.0B, so move up a unit when rounding reaches 1000.
            if scaled.rounded() < 1000 || i == units.count - 1 {
                if scaled >= 99.95 { return "\(grouped(Int(scaled.rounded())))\(unit.1)" }
                return String(format: "%.1f", locale: posix, scaled) + unit.1
            }
        }
        return String(Int(value.rounded()))
    }

    /// 0.82 becomes 82%
    public static func percent(rate: Double?) -> String? {
        guard let rate, rate.isFinite else { return nil }
        return "\(Int((rate * 100).rounded()))%"
    }

    /// 94 becomes 94%
    public static func percent(points: Double?) -> String? {
        guard let points, points.isFinite else { return nil }
        return "\(Int(points.rounded()))%"
    }

    // MARK: Dates and times

    private static func formatter(_ format: String, _ zone: TimeZone) -> DateFormatter {
        let f = DateFormatter()
        f.locale = posix
        f.timeZone = zone
        f.dateFormat = format
        return f
    }

    /// Nov 23
    public static func shortDate(_ date: Date, zone: TimeZone = .current) -> String {
        formatter("MMM d", zone).string(from: date)
    }

    /// 10:00, in the Mac's local zone
    public static func clock(_ date: Date, zone: TimeZone = .current) -> String {
        formatter("HH:mm", zone).string(from: date)
    }

    /// 2h 43m, 2d 18h, 43m
    public static func duration(_ seconds: TimeInterval) -> String {
        let minutes = Int(max(seconds, 0) / 60)
        let d = minutes / 1440, h = (minutes % 1440) / 60, m = minutes % 60
        if d > 0 { return "\(d)d \(h)h" }
        if h > 0 { return "\(h)h \(m)m" }
        if m > 0 { return "\(m)m" }
        return "under 1m"
    }

    /// A rounded length for "runs out in about 6h".
    public static func roughDuration(_ seconds: TimeInterval) -> String {
        let s = max(seconds, 0)
        let hours = Int((s / 3600).rounded())
        if hours >= 48 { return "\(Int((s / 86400).rounded()))d" }
        if hours >= 1 { return "\(hours)h" }
        return "\(max(Int((s / 60).rounded()), 1))m"
    }

    /// 14:16:05, in the Mac's local zone
    public static func clockSeconds(_ date: Date, zone: TimeZone = .current) -> String {
        formatter("HH:mm:ss", zone).string(from: date)
    }

    /// "Live, updated 14:16:05" while the feed is fresh. Once it is over 15 minutes old
    /// (`Feed.staleAfter`) the dot turns orange and the words say so too: "Not live,
    /// updated 14:16" the same day, "Not live, updated Nov 22" after that.
    public static func liveLine(_ updated: Date?, now: Date, zone: TimeZone = .current) -> LiveLine {
        guard let updated else { return LiveLine(text: "Not updated yet", fresh: false) }
        if now.timeIntervalSince(updated) <= Feed.staleAfter {
            return LiveLine(text: "Live, updated \(clockSeconds(updated, zone: zone))", fresh: true)
        }
        let sameDay = shortDate(updated, zone: zone) == shortDate(now, zone: zone) && now.timeIntervalSince(updated) < 86400
        return LiveLine(text: "Not live, updated \(sameDay ? clock(updated, zone: zone) : shortDate(updated, zone: zone))", fresh: false)
    }

    // MARK: Panel lines

    /// "Up 6 on yesterday", "Down 1 on the week before", "Same as yesterday".
    /// `scale` is 100 for rates (shown in percentage points) and 1 for counts.
    public static func change(now: Double?, previous: Double?, scale: Double, period: PeriodKey) -> Change? {
        guard period.hasPrevious, let now, let previous, now.isFinite, previous.isFinite else { return nil }
        let delta = Int((now * scale).rounded()) - Int((previous * scale).rounded())
        if delta == 0 { return Change(lead: "", rest: "same as \(period.previousPhrase)", delta: 0) }
        return Change(lead: "\(delta > 0 ? "Up" : "Down") \(abs(delta))", rest: "on \(period.previousPhrase)", delta: delta)
    }

    /// "57.3M, peak 14.6M at 10:00" or "742M, peak 142M on Nov 20"
    public static func tokensSummary(_ period: Period, zone: TimeZone = .current) -> String {
        var text = tokens(period.tokens)
        if let peak = period.peak, let t = peak.t, let v = peak.tokens, v > 0 {
            let when: String
            switch period.seriesUnit {
            case "hour": when = "at \(clock(t, zone: zone))"
            case "week": when = "in the week of \(shortDate(t, zone: zone))" // a week's point is dated by its Monday
            default: when = "on \(shortDate(t, zone: zone))"
            }
            text += ", peak \(tokens(v)) \(when)"
        }
        return text
    }

    /// "Detail from Nov 9": shown under the blocks that come only from transcripts
    /// (how it went, by activity) when those reach back less far than the period does.
    /// Days are compared in the given zone; the same day, or an earlier one, says nothing.
    public static func detailLine(_ period: Period, zone: TimeZone = .current) -> String? {
        guard let since = period.since, let detail = period.detailSince else { return nil }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        guard calendar.startOfDay(for: detail) > calendar.startOfDay(for: since) else { return nil }
        return "Detail from \(shortDate(detail, zone: zone))"
    }

    /// The three labels under the graph: first point, middle point, and Now or Today.
    /// Weekly points (All time beyond 60 days) read like daily ones: the week's Monday, then Today.
    public static func axisLabels(_ period: Period, zone: TimeZone = .current) -> [String] {
        let points = period.series
        guard !points.isEmpty else { return [] }
        let hourly = period.seriesUnit == "hour"
        func label(_ p: SeriesPoint) -> String {
            guard let t = p.t else { return "" }
            return hourly ? clock(t, zone: zone) : shortDate(t, zone: zone)
        }
        let last = hourly ? "Now" : "Today"
        if points.count < 3 { return [label(points[0]), "", last] }
        return [label(points[0]), label(points[points.count / 2]), last]
    }

    /// "5-hour, resets in 2h 43m"; a limit at 90 or more also says "near limit".
    public static func limitTitle(_ row: LimitRow, now: Date) -> String {
        var parts = [row.label ?? "Limit"]
        if row.level == .crit { parts.append("near limit") }
        if let resets = row.resetsAt, resets > now {
            parts.append("resets in \(duration(resets.timeIntervalSince(now)))")
        }
        return parts.joined(separator: ", ")
    }

    /// "39% in reserve", "10% over pace", "33% over pace, runs out in about 6h"
    public static func paceLine(_ row: LimitRow, now: Date) -> String? {
        guard let pace = row.pacePct else { return nil }
        let p = Int(pace.rounded())
        if p == 0 { return "On pace" }
        if p < 0 { return "\(-p)% in reserve" }
        var text = "\(p)% over pace"
        if let out = row.runsOutAt, out > now {
            text += ", runs out in about \(roughDuration(out.timeIntervalSince(now)))"
        }
        return text
    }

    /// "agentic-coach, main"
    public static func sessionTitle(_ s: RunningSession) -> String {
        let parts = [s.project, s.branch].compactMap { $0 }
        return parts.isEmpty ? "Session" : parts.joined(separator: ", ")
    }

    /// "Fable 5.2, running 1h 12m"
    public static func sessionDetail(_ s: RunningSession, now: Date) -> String {
        var parts: [String] = []
        if let model = s.model { parts.append(model) }
        if let started = s.started {
            parts.append("running \(duration(now.timeIntervalSince(started)))")
        }
        return parts.joined(separator: ", ")
    }

    // MARK: By activity

    /// The panel shows this many activity rows; the rest are on the coach page.
    public static let activityRowLimit = 5

    public static func shownActivities(_ period: Period) -> [Activity] {
        Array(period.activities.prefix(activityRowLimit))
    }

    /// "3 more on the coach page", or nil when every row is shown.
    public static func moreActivities(_ period: Period) -> String? {
        let extra = period.activities.count - activityRowLimit
        return extra > 0 ? "\(extra) more in the coach window" : nil
    }

    /// Heat bar length: the row's cost as a share of the largest row, 0 to 1.
    public static func heatShare(_ usd: Double?, largest: Double?) -> Double {
        guard let usd, let largest, usd.isFinite, largest.isFinite, largest > 0, usd > 0 else { return 0 }
        return min(usd / largest, 1)
    }

    // MARK: Tiles

    /// The Spend tile's label: "Spend today", "Spend, 7 days", "Spend, 30 days", "Spend, all time".
    public static func spendLabel(_ key: PeriodKey) -> String {
        key == .today ? "Spend today" : "Spend, \(key.title.prefix(1).lowercased() + key.title.dropFirst())"
    }

    /// The Top project tile: the first project of the period (they come highest cost first)
    /// and "$112.39 of $204.49". Nil when the period has no project.
    public static func topProject(_ period: Period) -> (name: String, detail: String)? {
        guard let top = period.projects.first, let name = top.name else { return nil }
        return (name, "\(money(top.usd)) of \(money(period.costUSD))")
    }

    /// The panel gives each running session a tile, up to this many; the rest are in the coach window.
    public static let runningTileLimit = 4

    /// The sessions that get a tile: the first four, in the feed's order (touched last first).
    public static func shownRunning(_ sessions: [RunningSession]) -> [RunningSession] {
        Array(sessions.prefix(runningTileLimit))
    }

    /// "2 more in the coach window", or nil when every running session has its tile.
    public static func moreRunning(count: Int) -> String? {
        count > runningTileLimit ? "\(count - runningTileLimit) more in the coach window" : nil
    }

    /// The tip the panel shows: the first one (the feed lists the most severe first), or nil
    /// when there is none, and then the Coach tip tile is not drawn.
    public static func shownTip(_ feed: Feed) -> Tip? {
        feed.tips.first
    }

    /// "See all 3 tips in the coach window"
    public static func tipsLink(count: Int) -> String {
        count == 1 ? "See this tip in the coach window" : "See all \(count) tips in the coach window"
    }

    /// The Running now tile's detail: "Opus 5.5, running 4h 11m, 734k left"
    public static func runningDetail(_ s: RunningSession, now: Date) -> String {
        var parts = [sessionDetail(s, now: now)].filter { !$0.isEmpty }
        if let left = s.contextLeftTokens { parts.append("\(tokens(left)) left") }
        return parts.joined(separator: ", ")
    }
}
