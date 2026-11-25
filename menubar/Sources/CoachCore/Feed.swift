import Foundation

// The shape of ~/.claude/coach/live.json (version 1).
// Every field is optional on purpose: a missing key, a null or a value of the
// wrong type becomes nil (or an empty list) and never stops the decode.

public enum Level: String, Equatable {
    case ok, warn, crit
}

public struct Feed: Decodable, Equatable {
    public let version: Int
    public let updated: Date?
    public let pageURL: URL?
    public let today: Period?
    public let week: Period?
    public let month: Period?
    /// Every day from the first one the coach has (contract CHANGE 5).
    public let all: Period?
    public let limits: Limits
    public let running: [RunningSession]
    public let tips: [Tip]

    enum Keys: String, CodingKey {
        case version, updated, page_url, periods, limits, running, tips
    }

    enum PeriodKeys: String, CodingKey {
        case today, week, month, all
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        version = c.number(.version).map { Int($0) } ?? 1
        updated = c.date(.updated)
        pageURL = c.webURL(.page_url)
        let p = try? c.nestedContainer(keyedBy: PeriodKeys.self, forKey: .periods)
        today = p?.opt(.today)
        week = p?.opt(.week)
        month = p?.opt(.month)
        all = p?.opt(.all)
        limits = c.opt(.limits) ?? Limits()
        running = c.list(.running)
        let allTips: [Tip] = c.list(.tips)
        tips = allTips.filter { $0.title != nil || $0.advice != nil }
    }

    /// The feed is written once a minute and a session counts as running for 10 minutes,
    /// so a feed older than this can no longer say what is running.
    public static let staleAfter: TimeInterval = 15 * 60

    public func isStale(now: Date) -> Bool {
        guard let updated else { return false }
        return now.timeIntervalSince(updated) > Feed.staleAfter
    }

    /// Running sessions, or none once the feed is too old to know.
    public func liveRunning(now: Date) -> [RunningSession] {
        isStale(now: now) ? [] : running
    }

    public func period(_ key: PeriodKey) -> Period? {
        switch key {
        case .today: return today
        case .week: return week
        case .month: return month
        case .all: return all
        }
    }
}

public struct Period: Decodable, Equatable {
    public let label: String?
    public let costUSD: Double?
    public let prevCostUSD: Double?
    public let calls: Double?
    public let sessions: Double?
    public let tokens: Double?
    public let seriesUnit: String?
    public let series: [SeriesPoint]
    public let peak: SeriesPoint?
    public let firstTry: Rate?
    public let cacheHit: Rate?
    public let corrections: Count?
    public let activities: [Activity]
    /// Up to three projects by cost, the rest folded into one row named "Other".
    public let projects: [Project]
    /// All time only: the period's first day.
    public let since: Date?
    /// All time only: the first day whose transcripts were read. Activities, first-try,
    /// cache hit and corrections reach back no further than this.
    public let detailSince: Date?

    enum Keys: String, CodingKey {
        case label, cost_usd, prev_cost_usd, calls, sessions, tokens, series_unit, series, peak
        case first_try, cache_hit, corrections, activities, projects, since, detail_since
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        label = c.text(.label)
        costUSD = c.number(.cost_usd)
        prevCostUSD = c.number(.prev_cost_usd)
        calls = c.number(.calls)
        sessions = c.number(.sessions)
        tokens = c.number(.tokens)
        seriesUnit = c.text(.series_unit)
        series = c.list(.series)
        peak = c.opt(.peak)
        firstTry = c.opt(.first_try)
        cacheHit = c.opt(.cache_hit)
        corrections = c.opt(.corrections)
        activities = c.list(.activities)
        projects = c.list(.projects)
        since = c.date(.since)
        detailSince = c.date(.detail_since)
    }
}

public struct SeriesPoint: Decodable, Equatable {
    public let t: Date?
    public let tokens: Double?
    public let usd: Double?

    enum Keys: String, CodingKey { case t, tokens, usd }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        t = c.date(.t)
        tokens = c.number(.tokens)
        usd = c.number(.usd)
    }
}

public struct Rate: Decodable, Equatable {
    public let rate: Double?
    public let prevRate: Double?

    enum Keys: String, CodingKey { case rate, prev_rate }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        rate = c.number(.rate)
        prevRate = c.number(.prev_rate)
    }
}

public struct Count: Decodable, Equatable {
    public let count: Double?
    public let prevCount: Double?

    enum Keys: String, CodingKey { case count, prev_count }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        count = c.number(.count)
        prevCount = c.number(.prev_count)
    }
}

public struct Activity: Decodable, Equatable {
    public let name: String?
    public let usd: Double?
    public let share: Double?
    public let firstTry: Double?
    public let turns: Double?

    enum Keys: String, CodingKey { case name, usd, share, first_try, turns }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        name = c.text(.name)
        usd = c.number(.usd)
        share = c.number(.share)
        turns = c.number(.turns)
        firstTry = c.number(.first_try)
    }
}

public struct Project: Decodable, Equatable {
    public let name: String?
    public let sessions: Double?
    public let usd: Double?
    public let share: Double?

    enum Keys: String, CodingKey { case name, sessions, usd, share }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        name = c.text(.name)
        sessions = c.number(.sessions)
        usd = c.number(.usd)
        share = c.number(.share)
    }
}

public struct Limits: Decodable, Equatable {
    public let asOf: Date?
    public let rows: [LimitRow]

    enum Keys: String, CodingKey { case as_of, rows }

    public init() {
        asOf = nil
        rows = []
    }

    /// Rows still in their window. A limit whose reset time has passed is dropped:
    /// its old percent no longer says anything. A row with no reset time is kept.
    public func liveRows(now: Date) -> [LimitRow] {
        rows.filter { row in
            guard let resets = row.resetsAt else { return true }
            return resets > now
        }
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        asOf = c.date(.as_of)
        rows = c.list(.rows)
    }
}

public struct LimitRow: Decodable, Equatable {
    public let key: String?
    public let label: String?
    public let usedPct: Double?
    public let resetsAt: Date?
    public let pacePct: Double?
    public let runsOutAt: Date?
    public let level: Level

    enum Keys: String, CodingKey { case key, label, used_pct, resets_at, pace_pct, runs_out_at, level }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        key = c.text(.key)
        label = c.text(.label)
        usedPct = c.number(.used_pct)
        resetsAt = c.date(.resets_at)
        pacePct = c.number(.pace_pct)
        runsOutAt = c.date(.runs_out_at)
        // The feed's own level wins; without one, use the contract's bands.
        if let given = c.text(.level).flatMap(Level.init(rawValue:)) {
            level = given
        } else if let used = usedPct, used >= 90 {
            level = .crit
        } else if let used = usedPct, used >= 70 {
            level = .warn
        } else {
            level = .ok
        }
    }
}

public struct RunningSession: Decodable, Equatable {
    public let project: String?
    public let branch: String?
    public let model: String?
    public let started: Date?
    public let contextPct: Double?
    public let contextLeftTokens: Double?
    public let level: Level

    enum Keys: String, CodingKey {
        case project, branch, model, started, context_pct, context_left_tokens, level
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        project = c.text(.project)
        branch = c.text(.branch)
        model = c.text(.model)
        started = c.date(.started)
        contextPct = c.number(.context_pct)
        contextLeftTokens = c.number(.context_left_tokens)
        level = c.text(.level).flatMap(Level.init(rawValue:)) ?? .ok
    }
}

public struct Tip: Decodable, Equatable {
    public let title: String?
    public let advice: String?
    public let pattern: String?

    enum Keys: String, CodingKey { case title, advice, pattern }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        title = c.text(.title)
        advice = c.text(.advice)
        pattern = c.text(.pattern)
    }
}

// MARK: - Forgiving decoding helpers

/// One list element that may fail on its own without failing the list.
struct Failable<T: Decodable>: Decodable {
    let value: T?
    init(from decoder: Decoder) throws {
        value = try? T(from: decoder)
    }
}

public enum FeedDates {
    /// Local ISO 8601 with offset, with or without fractions of a second.
    public static func parse(_ text: String) -> Date? {
        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        if let d = plain.date(from: text) { return d }
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return fractional.date(from: text)
    }
}

extension KeyedDecodingContainer {
    func opt<T: Decodable>(_ key: Key) -> T? {
        (try? decodeIfPresent(T.self, forKey: key)) ?? nil
    }

    func text(_ key: Key) -> String? {
        let s: String? = opt(key)
        guard let s, !s.isEmpty else { return nil }
        return s
    }

    func number(_ key: Key) -> Double? {
        let d: Double? = opt(key)
        // Anything past a million billion is not a real figure, and turning a value
        // that large into a whole number would stop the app; treat it as missing.
        guard let d, d.isFinite, abs(d) <= 1e15 else { return nil }
        return d
    }

    func date(_ key: Key) -> Date? {
        text(key).flatMap(FeedDates.parse)
    }

    /// Only http and https links are kept; anything else is treated as no link.
    func webURL(_ key: Key) -> URL? {
        guard let s = text(key), let url = URL(string: s),
              let scheme = url.scheme?.lowercased(), scheme == "https" || scheme == "http",
              url.host != nil
        else { return nil }
        return url
    }

    func list<T: Decodable>(_ key: Key) -> [T] {
        let raw: [Failable<T>]? = opt(key)
        return raw?.compactMap(\.value) ?? []
    }
}
