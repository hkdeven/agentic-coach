import AppKit
import CoachCore
import SwiftUI

/// The menu bar panel, in the Mac Monitor look (menubar/mockups/PanelMM.dc.html): a header
/// with a status dot, the period control, a two-column grid of tiles, the activities as
/// plain rows with thin heat bars, and two buttons. 340 points wide with a 14 point inset.
struct PanelView: View {
    @ObservedObject var store: FeedStore
    @Binding var compact: Bool
    @State private var period: PeriodKey

    /// startPeriod: the period shown first (Today; the off-screen pictures pick each in turn).
    /// screenHeight: the visible height of the screen the panel opens on; nil takes the
    /// screen with the menu bar.
    /// onHeight: told the panel's height each time it changes, so the popover can follow.
    /// onClose: closes the panel, before a link or button opens the coach window.
    init(
        store: FeedStore, compact: Binding<Bool>, startPeriod: PeriodKey = .today, screenHeight: CGFloat? = nil,
        onHeight: @escaping (CGFloat) -> Void = { _ in }, onClose: @escaping () -> Void = {}
    ) {
        self.store = store
        _compact = compact
        _period = State(initialValue: startPeriod)
        self.screenHeight = screenHeight ?? NSScreen.screens.first?.visibleFrame.height ?? 900
        self.onHeight = onHeight
        self.onClose = onClose
    }

    /// Visible height of the screen the panel is on.
    private let screenHeight: CGFloat
    private let onHeight: (CGFloat) -> Void
    private let onClose: () -> Void

    static let width: CGFloat = 340
    static let inset: CGFloat = 14
    /// The header: 14 above, an 18pt row, 10 below. It stays put while the rest scrolls.
    private static let headerHeight: CGFloat = 42

    /// Room kept between the panel and the screen's visible height: the popover's arrow
    /// and a little air, so the popover never has to be squeezed to fit.
    static let screenMargin: CGFloat = 40

    /// The panel is never taller than the screen's visible height less that margin; past it, it scrolls.
    private var maxHeight: CGFloat { max(screenHeight - Self.screenMargin, 200) }

    var body: some View {
        VStack(spacing: 0) {
            header
                .frame(height: 18)
                .padding(.horizontal, Self.inset)
                .padding(.top, Self.inset)
                .padding(.bottom, 10)
            // As tall as its content, up to the cap; past the cap it scrolls. The height is
            // worked out in the one layout pass (no measuring and setting it a turn later),
            // so the popover can be sized right before it is first shown.
            ScrollView(.vertical) {
                content
                    .padding(.horizontal, Self.inset)
                    .padding(.bottom, Self.inset)
            }
            .frame(maxHeight: maxHeight - Self.headerHeight)
            .fixedSize(horizontal: false, vertical: true)
        }
        .frame(width: Self.width)
        .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { height in
            onHeight(height) // the popover takes this height: no spare room round the panel
            if ProcessInfo.processInfo.environment["COACH_LOG_HEIGHT"] == "1" {
                NSLog("Claude Coach panel: %.0f tall, limit %.0f", height, maxHeight)
            }
        }
        // The panel has one definite size, whatever room it is offered: the popover is sized
        // from it. The same colour is painted behind the whole popover (PanelBox), arrow
        // included, so nothing see-through shows round the panel.
        .background(Theme.surface)
        .foregroundStyle(Theme.ink)
        .font(Theme.sans(13))
        .onAppear { store.reload() }
    }

    // MARK: Header

    /// The name on the left; on the right a status dot, the words that go with it, and the settings.
    private var header: some View {
        let status = headerStatus
        return HStack(alignment: .center, spacing: 6) {
            Text("Claude Coach")
                .font(Theme.sans(14, .semibold))
                .lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .leading)
            Circle()
                .fill(status.fresh ? Theme.goodDot : Theme.warnDot)
                .frame(width: 7, height: 7)
                .accessibilityHidden(true) // the words beside it say the same
            Text(status.text)
                .font(Theme.sans(12))
                .foregroundStyle(Theme.ink2)
                .lineLimit(1)
                .fixedSize() // the time is never cut short; the name gives way first
                .layoutPriority(1)
            SettingsMenu(compact: $compact)
                .frame(width: 16)
        }
    }

    private var headerStatus: LiveLine {
        switch store.state {
        case .ready(let feed): return Fmt.liveLine(feed.updated, now: store.now)
        case .missing: return LiveLine(text: "No data yet", fresh: false)
        case .unreadable: return LiveLine(text: "Data not readable", fresh: false)
        case .needsUpdate: return LiveLine(text: "Update needed", fresh: false)
        }
    }

    // MARK: Body

    @ViewBuilder private var content: some View {
        VStack(alignment: .leading, spacing: 0) {
            switch store.state {
            case .missing:
                Message(
                    title: "No coach data yet",
                    detail: "Run /coach setup in Claude Code to turn on the menu bar feed. This panel fills in within a minute."
                )
            case .unreadable:
                Message(
                    title: "The coach data could not be read",
                    detail: "Run /coach setup in Claude Code to write it again."
                )
            case .needsUpdate:
                Message(
                    title: "Update the coach",
                    detail: "The coach data on this Mac is newer than this app. Update the Agentic Coach plugin, then build the menu bar app again."
                )
            case .ready(let feed):
                FeedBody(feed: feed, now: store.now, period: $period, close: onClose)
            }
            Rectangle().fill(Theme.line).frame(height: 1).padding(.top, 12)
            FootButtons(close: onClose).padding(.top, 10)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// A plain message where the tiles would be: no feed yet, an unreadable one, a newer one.
private struct Message: View {
    let title: String
    let detail: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(Theme.sans(13, .medium))
            Text(detail).font(Theme.sans(12)).foregroundStyle(Theme.ink2).fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// "Open coach window" takes the spare width; "Quit" hugs its word.
private struct FootButtons: View {
    /// Closes the panel.
    let close: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            Button {
                close() // the panel would otherwise stay up over the window
                CoachWindow.shared.open(tab: .current)
            } label: {
                Label("Open coach window", systemImage: "arrow.up.right.square")
                    .frame(maxWidth: .infinity)
            }
            .help("Opens the coach page in its own window.")
            Button("Quit") { NSApp.terminate(nil) }
                .help("Quits Claude Coach. The coach itself keeps recording.")
        }
        .font(.system(size: 13))
    }
}

/// One tile: a label, a figure and one line of detail on a soft rounded fill.
private struct Tile<Detail: View>: View {
    let label: String
    let value: String
    /// False when the value is words, not a figure ("No edits"): smaller and quieter.
    var isFigure = true
    var tip: String?
    @ViewBuilder var detail: Detail

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).font(Theme.sans(12)).foregroundStyle(Theme.ink2).lineLimit(1)
            Text(value)
                .font(isFigure ? Theme.sans(18, .medium) : Theme.sans(14, .semibold))
                .foregroundStyle(isFigure ? Theme.ink : Theme.ink2)
                .lineLimit(1)
                .minimumScaleFactor(0.6)
                .truncationMode(.tail)
                .frame(height: 22, alignment: .leading)
            detail
        }
        .padding(.vertical, 8)
        .padding(.horizontal, 10)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading) // both tiles of a row end level
        .background(Theme.tile, in: RoundedRectangle(cornerRadius: 8))
        .help(tip ?? "")
        .accessibilityElement(children: .combine)
    }
}

/// A tile's detail line: 12pt, tertiary, one line.
private struct DetailLine: View {
    let text: String

    var body: some View {
        Text(text.isEmpty ? " " : text)
            .font(Theme.sans(12))
            .foregroundStyle(Theme.ink3)
            .lineLimit(1)
            .minimumScaleFactor(0.8)
    }
}

private struct FeedBody: View {
    let feed: Feed
    let now: Date
    @Binding var period: PeriodKey
    /// Closes the panel, before a link opens the coach window.
    let close: () -> Void

    private static let gap: CGFloat = 8

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            periodPicker
            if let tip = Fmt.shownTip(feed) { tipTile(tip).padding(.top, 10) }
            if let data = feed.period(period) {
                tiles(data).padding(.top, 10)
                activities(data).padding(.top, 12)
            } else {
                Text("No data for this period yet.").font(Theme.sans(12)).foregroundStyle(Theme.ink2).padding(.top, 10)
                running.padding(.top, 10)
            }
        }
    }

    // MARK: Coach tip

    /// Wide tile: the first tip, its advice, and a link to the rest. Not drawn when there is no tip.
    private func tipTile(_ tip: Tip) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("Coach tip").font(Theme.sans(12)).foregroundStyle(Theme.ink2)
            if let title = tip.title {
                Text(title).font(Theme.sans(13, .medium)).lineLimit(3).fixedSize(horizontal: false, vertical: true)
            }
            if let advice = tip.advice {
                Text(advice).font(Theme.sans(12)).foregroundStyle(Theme.ink3).lineLimit(3).fixedSize(horizontal: false, vertical: true)
            }
            Button(Fmt.tipsLink(count: feed.tips.count)) {
                close()
                CoachWindow.shared.open(tab: .today)
            }
            .buttonStyle(.plain)
            .font(Theme.sans(12, .medium))
            .foregroundStyle(Theme.blue)
            .padding(.top, 2)
            .help("Opens the coach window on Today, where every tip is listed.")
        }
        .padding(.vertical, 8)
        .padding(.horizontal, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.tile, in: RoundedRectangle(cornerRadius: 8))
    }

    // MARK: Period control

    /// Four buttons on one row; the chosen one is an inverted pill.
    private var periodPicker: some View {
        HStack(spacing: 2) {
            ForEach(PeriodKey.allCases) { key in
                let on = key == period
                Button {
                    period = key // Lights at once; the data is already in memory.
                } label: {
                    Text(key.title)
                        .font(Theme.sans(12, on ? .semibold : .medium))
                        .foregroundStyle(on ? Theme.pillInk : Theme.ink2)
                        .lineLimit(1)
                        .frame(maxWidth: .infinity)
                        .frame(height: 24)
                        .background(on ? Theme.pill : Color.clear, in: RoundedRectangle(cornerRadius: 6))
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(on ? .isSelected : [])
                .help(key == .all ? "Show everything since the coach's first day." : "Show \(key.fallbackLabel.lowercased()).")
            }
        }
        .padding(2)
        .background(Theme.tile, in: RoundedRectangle(cornerRadius: 8))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Period")
    }

    // MARK: Tiles

    private func tiles(_ data: Period) -> some View {
        VStack(spacing: Self.gap) {
            row {
                Tile(label: Fmt.spendLabel(period), value: Fmt.money(data.costUSD), tip: "What these tokens would cost at API list prices.") {
                    DetailLine(text: "API-equivalent, not a bill")
                }
                Tile(label: "Calls", value: Fmt.count(data.calls), tip: "Messages Claude sent back in this period.") {
                    DetailLine(text: "in " + Fmt.counted(data.sessions, one: "session", many: "sessions"))
                    // All time: calls are only known for the days whose transcripts were read
                    if let from = Fmt.detailLine(data) { DetailLine(text: from) }
                }
            }
            tokens(data)
            row {
                rate("First-try edits", Fmt.percent(rate: data.firstTry?.rate), none: "No edits",
                     change: Fmt.change(now: data.firstTry?.rate, previous: data.firstTry?.prevRate, scale: 100, period: period),
                     upIsGood: true, data: data,
                     tip: "Share of turns that edited files and needed no retry or correction afterwards.")
                rate("Cache hit", Fmt.percent(rate: data.cacheHit?.rate), none: "No data",
                     change: Fmt.change(now: data.cacheHit?.rate, previous: data.cacheHit?.prevRate, scale: 100, period: period),
                     upIsGood: true, data: data,
                     tip: "Share of input tokens read from the prompt cache instead of being sent again at full price.")
            }
            row {
                rate("Corrections", data.corrections?.count.map { Fmt.count($0) }, none: "No data",
                     change: Fmt.change(now: data.corrections?.count, previous: data.corrections?.prevCount, scale: 1, period: period),
                     upIsGood: false, data: data,
                     tip: "Your prompts that open by correcting the answer before.")
                topProject(data)
            }
            running
        }
    }

    /// Two tiles side by side, equal in width and height.
    private func row<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        HStack(alignment: .top, spacing: Self.gap) { content() }
            .fixedSize(horizontal: false, vertical: true)
    }

    /// A tile whose detail says how the figure moved against the period before: the words
    /// that say so in a status colour, the rest after them. All time has nothing before it,
    /// so there the line says where its detail starts, when that is later than the period.
    private func rate(
        _ label: String, _ value: String?, none: String, change: Change?, upIsGood: Bool, data: Period, tip: String
    ) -> some View {
        Tile(label: label, value: value ?? none, isFigure: value != nil, tip: tip) {
            if let change {
                let good = change.delta > 0 ? upIsGood : !upIsGood
                (Text(change.lead).fontWeight(.medium).foregroundStyle(good ? Theme.good : Theme.warn)
                    + Text(change.lead.isEmpty ? change.rest : " " + change.rest).foregroundStyle(Theme.ink3))
                    .font(Theme.sans(12))
                    .lineLimit(2) // "on the 30 days before" needs a second line in a narrow tile
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                DetailLine(text: Fmt.detailLine(data) ?? "")
            }
        }
    }

    private func topProject(_ data: Period) -> some View {
        let top = Fmt.topProject(data)
        return Tile(label: "Top project", value: top?.name ?? "None", isFigure: top != nil, tip: "The project with the most spend in this period.") {
            DetailLine(text: top?.detail ?? "no projects in this period")
        }
    }

    /// Wide tile: the label and the totals on one line, the line graph, three axis labels.
    private func tokens(_ data: Period) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                Text("Tokens").font(Theme.sans(12)).foregroundStyle(Theme.ink2)
                Spacer(minLength: 8)
                Text(Fmt.tokensSummary(data)).font(Theme.sans(12)).foregroundStyle(Theme.ink3).lineLimit(1).minimumScaleFactor(0.8)
            }
            if data.series.isEmpty {
                Text("No tokens in this period yet.").font(Theme.sans(12)).foregroundStyle(Theme.ink3)
            } else {
                TokensChart(values: data.series.map { max($0.tokens ?? 0, 0) }, description: period.graphDescription)
                let labels = Fmt.axisLabels(data)
                if labels.count == 3 {
                    HStack {
                        Text(labels[0])
                        Spacer()
                        Text(labels[1])
                        Spacer()
                        Text(labels[2])
                    }
                    .font(Theme.sans(11))
                    .foregroundStyle(Theme.ink3)
                }
            }
        }
        .padding(.vertical, 8)
        .padding(.horizontal, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.tile, in: RoundedRectangle(cornerRadius: 8))
    }

    /// Running now: a wide tile for each session, the one touched last first, with how full
    /// its context is on the right in its status colour. Four at most, then a line for the rest.
    private var running: some View {
        let sessions = feed.liveRunning(now: now)
        let shown = Fmt.shownRunning(sessions)
        return VStack(alignment: .leading, spacing: Self.gap) {
            if shown.isEmpty {
                runningTile(nil, labelled: true)
            }
            ForEach(Array(shown.enumerated()), id: \.offset) { index, session in
                runningTile(session, labelled: index == 0)
            }
            if let more = Fmt.moreRunning(count: sessions.count) {
                Button(more) {
                    close()
                    CoachWindow.shared.open(tab: .overview)
                }
                .buttonStyle(.plain)
                .font(Theme.sans(12))
                .foregroundStyle(Theme.ink3)
                .help("Opens the coach window on its Overview, where every running session is listed.")
            }
        }
    }

    /// One session, or "Nothing running" when given none. The first tile carries the label.
    private func runningTile(_ session: RunningSession?, labelled: Bool) -> some View {
        HStack(alignment: .center, spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                if labelled {
                    Text("Running now").font(Theme.sans(12)).foregroundStyle(Theme.ink2)
                }
                Text(session.map(Fmt.sessionTitle) ?? "Nothing running")
                    .font(Theme.sans(13, .medium))
                    .lineLimit(1)
                    .truncationMode(.middle)
                if let session {
                    DetailLine(text: Fmt.runningDetail(session, now: now))
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            if let session, let pct = Fmt.percent(points: session.contextPct) {
                Text("\(pct) context")
                    .font(Theme.sans(14, .semibold))
                    .foregroundStyle(Theme.level(session.level))
                    .lineLimit(1)
                    .fixedSize()
            }
        }
        .padding(.vertical, 8)
        .padding(.horizontal, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.tile, in: RoundedRectangle(cornerRadius: 8))
        .accessibilityElement(children: .combine)
    }

    // MARK: By activity

    /// Plain rows: a thin heat bar, the name, then Cost, Turns and First try.
    private func activities(_ data: Period) -> some View {
        let rows = Fmt.shownActivities(data)
        let largest = rows.compactMap(\.usd).max()
        return VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 0) {
                Text("By activity").frame(maxWidth: .infinity, alignment: .leading)
                Text("Cost").frame(width: 64, alignment: .trailing)
                Text("Turns").frame(width: 40, alignment: .trailing)
                Text("First try").frame(width: 58, alignment: .trailing)
            }
            .foregroundStyle(Theme.ink2)
            if rows.isEmpty {
                Text("No activity in this period yet.").foregroundStyle(Theme.ink3)
            }
            ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                HStack(spacing: 0) {
                    HeatBar(share: Fmt.heatShare(row.usd, largest: largest)).padding(.trailing, 8)
                    Text(row.name ?? "Other").lineLimit(1).frame(maxWidth: .infinity, alignment: .leading)
                    Text(Fmt.money(row.usd)).lineLimit(1).minimumScaleFactor(0.8).frame(width: 64, alignment: .trailing)
                    Text(Fmt.count(row.turns)).lineLimit(1).minimumScaleFactor(0.8).foregroundStyle(Theme.ink2).frame(width: 40, alignment: .trailing)
                    Text(Fmt.percent(rate: row.firstTry) ?? "No edits").lineLimit(1).foregroundStyle(Theme.ink2).frame(width: 58, alignment: .trailing)
                }
                .accessibilityElement(children: .combine)
            }
            if let more = Fmt.moreActivities(data) {
                Button(more) {
                    close()
                    CoachWindow.shared.open(tab: .overview)
                }
                .buttonStyle(.plain)
                .foregroundStyle(Theme.ink3)
                .help("Opens the coach window on its Overview, where every activity is listed.")
            }
            if let line = Fmt.detailLine(data) {
                Text(line).foregroundStyle(Theme.ink3)
            }
        }
        .font(Theme.sans(12))
    }
}

/// Heat bar: length is the row's cost as a share of the largest row. One fixed blue to
/// amber to red gradient spans the whole 40pt track and the bar clips it, so a longer bar
/// reaches hotter colours. Decoration only: the figures beside it say the same.
struct HeatBar: View {
    let share: Double
    static let track: CGFloat = 40

    var body: some View {
        ZStack(alignment: .leading) {
            RoundedRectangle(cornerRadius: 2).fill(Theme.tile)
            LinearGradient(colors: Theme.heat, startPoint: .leading, endPoint: .trailing)
                .frame(width: Self.track)
                .mask(alignment: .leading) {
                    Rectangle().frame(width: max(Self.track * share, share > 0 ? 2 : 0))
                }
                .clipShape(RoundedRectangle(cornerRadius: 2))
        }
        .frame(width: Self.track, height: 6)
        .accessibilityHidden(true)
    }
}
