import Foundation

/// What the menu bar item shows. The rule, from the mockup:
/// - the mark, then today's spend;
/// - a green dot beside the mark while any session is running (not once the feed is over 15 minutes old);
/// - the highest limit, in red with its name, when any limit is at 90 percent or more;
/// - compact: the mark alone.
public struct MenuBarState: Equatable {
    public static let nearLimitAt = 90.0

    public let spend: String?
    public let running: Bool
    public let limit: String?

    public init(spend: String?, running: Bool, limit: String?) {
        self.spend = spend
        self.running = running
        self.limit = limit
    }

    public static func make(state: FeedState, compact: Bool, now: Date) -> MenuBarState {
        guard case .ready(let feed) = state, !compact else {
            return MenuBarState(spend: nil, running: false, limit: nil)
        }
        let spend = feed.today?.costUSD.map { Fmt.money($0) }
        var top: LimitRow?
        // A limit whose window has already reset is no longer near its limit.
        for row in feed.limits.liveRows(now: now) {
            guard let used = row.usedPct, used >= nearLimitAt else { continue }
            if top == nil || used > (top?.usedPct ?? 0) { top = row }
        }
        let limit = top.flatMap { row in
            Fmt.percent(points: row.usedPct).map { "\(row.label ?? "Limit") \($0)" }
        }
        return MenuBarState(spend: spend, running: !feed.liveRunning(now: now).isEmpty, limit: limit)
    }

    /// Read out by VoiceOver in place of the drawn item.
    public var spokenText: String {
        var parts = ["Claude Coach"]
        if let spend { parts.append("\(spend) today") }
        if running { parts.append("a session is running") }
        if let limit { parts.append("\(limit), near limit") }
        return parts.joined(separator: ", ")
    }
}
