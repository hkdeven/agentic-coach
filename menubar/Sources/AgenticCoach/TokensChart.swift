import Charts
import SwiftUI

/// The Tokens line graph in its tile: a line with a soft area under it, three thin
/// guide lines, and a dot on the last point with a ring of the tile's colour round it.
struct TokensChart: View {
    let values: [Double]
    let description: String

    var body: some View {
        let top = max(values.max() ?? 0, 1)
        Chart {
            ForEach(Array(values.enumerated()), id: \.offset) { index, value in
                AreaMark(x: .value("Step", index), y: .value("Tokens", value))
                    .foregroundStyle(Theme.blue.opacity(0.18))
                LineMark(x: .value("Step", index), y: .value("Tokens", value))
                    .foregroundStyle(Theme.blue)
                    .lineStyle(StrokeStyle(lineWidth: 2, lineCap: .round, lineJoin: .round))
            }
            if let last = values.last {
                PointMark(x: .value("Step", values.count - 1), y: .value("Tokens", last))
                    .symbol {
                        Circle()
                            .fill(Theme.blue)
                            .frame(width: 6, height: 6)
                            .padding(1)
                            .background(Theme.tileSolid, in: Circle())
                    }
            }
        }
        .chartXAxis(.hidden)
        .chartYAxis {
            AxisMarks(values: [0, top / 2, top]) { _ in
                AxisGridLine(stroke: StrokeStyle(lineWidth: 1)).foregroundStyle(Theme.grid)
            }
        }
        .chartYScale(domain: 0...top, range: .plotDimension(startPadding: 4, endPadding: 4))
        .chartXScale(domain: 0...max(values.count - 1, 1), range: .plotDimension(padding: 2))
        .chartLegend(.hidden)
        .frame(height: 70)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(description)
    }
}
