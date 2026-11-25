import AppKit
import CoachCore
import SwiftUI

/// Draws the menu bar item: the mark, the running dot, today's spend and the limit.
/// With no colour to show it is a template image, so macOS tints it for any menu bar.
enum MenuBarIcon {
    private static let height: CGFloat = 18
    private static let gap: CGFloat = 6
    private static let font = NSFont.monospacedSystemFont(ofSize: 12, weight: .medium)

    private static func width(_ text: String) -> CGFloat {
        ceil((text as NSString).size(withAttributes: [.font: font]).width)
    }

    static func image(for state: MenuBarState) -> NSImage {
        var total: CGFloat = 16
        if state.running { total += gap + 6 }
        if let spend = state.spend { total += gap + width(spend) }
        if let limit = state.limit { total += gap + width(limit) }
        let coloured = state.running || state.limit != nil

        let image = NSImage(size: NSSize(width: total, height: height), flipped: false) { _ in
            let dark = Theme.isDark(NSAppearance.currentDrawing())
            let base: NSColor = coloured ? (dark ? .white : .black) : .black
            let crit = Theme.nsColor(dark ? Theme.critDark : Theme.critLight)
            let good = Theme.nsColor(dark ? Theme.goodDark : Theme.goodLight)

            // The mark: a rising line, 16 by 14, as in the mockup.
            let y0 = (height - 14) / 2
            let mark = NSBezierPath()
            let points: [(CGFloat, CGFloat)] = [(1, 11), (5, 7), (8, 9), (15, 2)]
            for (i, p) in points.enumerated() {
                let point = NSPoint(x: p.0, y: y0 + 14 - p.1)
                if i == 0 { mark.move(to: point) } else { mark.line(to: point) }
            }
            mark.lineWidth = 1.6
            mark.lineCapStyle = .round
            mark.lineJoinStyle = .round
            (state.limit != nil ? crit : base).setStroke()
            mark.stroke()

            var x: CGFloat = 16
            if state.running {
                x += gap
                good.setFill()
                NSBezierPath(ovalIn: NSRect(x: x, y: (height - 6) / 2, width: 6, height: 6)).fill()
                x += 6
            }
            func draw(_ text: String, _ colour: NSColor) {
                x += gap
                let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: colour]
                let size = (text as NSString).size(withAttributes: attrs)
                (text as NSString).draw(at: NSPoint(x: x, y: (height - size.height) / 2), withAttributes: attrs)
                x += ceil(size.width)
            }
            if let spend = state.spend { draw(spend, base) }
            if let limit = state.limit { draw(limit, crit) }
            return true
        }
        image.isTemplate = !coloured
        return image
    }
}
