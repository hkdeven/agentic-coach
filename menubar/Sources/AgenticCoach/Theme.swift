import AppKit
import CoachCore
import SwiftUI

/// The panel's look, matched to the Mac Monitor menu bar panel (menubar/mockups/PanelMM.dc.html):
/// macOS's own surface, label and fill colours, SF Pro with tabular digits, and the system
/// green, orange and red for status. Each colour follows light or dark by itself.
enum Theme {
    static func nsColor(_ hex: UInt32, alpha: CGFloat = 1) -> NSColor {
        NSColor(
            srgbRed: CGFloat((hex >> 16) & 0xff) / 255,
            green: CGFloat((hex >> 8) & 0xff) / 255,
            blue: CGFloat(hex & 0xff) / 255,
            alpha: alpha
        )
    }

    static func isDark(_ appearance: NSAppearance) -> Bool {
        appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
    }

    private static func dyn(_ light: NSColor, _ dark: NSColor) -> Color {
        Color(nsColor: NSColor(name: nil) { isDark($0) ? dark : light })
    }

    private static func token(_ light: UInt32, _ dark: UInt32) -> Color {
        dyn(nsColor(light), nsColor(dark))
    }

    // MARK: Surfaces and text (macOS's own)

    /// The panel's solid surface: white on a light Mac, near black on a dark one. No translucency.
    static let surface = Color(nsColor: .controlBackgroundColor)
    static let ink = Color(nsColor: .labelColor)
    static let ink2 = Color(nsColor: .secondaryLabelColor)
    static let ink3 = Color(nsColor: .tertiaryLabelColor)
    /// The soft fill of a tile, of the period control and of a heat bar's track.
    static let tile = Color(nsColor: .quaternaryLabelColor)
    static let line = Color(nsColor: .separatorColor)

    // MARK: The values the mockup fixes

    /// The line graph and the cool end of a heat bar.
    static let blue = token(0x2a78d6, 0x3987e5)
    /// A tile's colour as one solid colour, for the ring round the graph's last point.
    static let tileSolid = token(0xf0f0f0, 0x353535)
    /// Guide lines of the graph.
    static let grid = dyn(NSColor(white: 0, alpha: 0.08), NSColor(white: 1, alpha: 0.1))
    /// The chosen period: an inverted pill.
    static let pill = token(0x0b0b0b, 0xf0efec)
    static let pillInk = token(0xfcfcfb, 0x1a1a19)
    /// Heat bar: blue, amber, red across the whole track.
    static let heat: [Color] = [blue, token(0xeda100, 0xc98500), token(0xd03b3b, 0xff453a)]

    // MARK: Status

    /// Dots use the system colours as they are.
    static let goodDot = Color(nsColor: .systemGreen)
    static let warnDot = Color(nsColor: .systemOrange)

    /// A status word written as text. On a light panel the system green, orange and red are
    /// too pale to read as text, so there each is mixed half and half with black; on a dark
    /// panel they are used as they are (the Mac Monitor rule).
    private static func statusText(_ base: NSColor) -> Color {
        Color(nsColor: NSColor(name: nil) { look in
            if isDark(look) { return base }
            return base.usingColorSpace(.sRGB)?.blended(withFraction: 0.5, of: .black) ?? base
        })
    }

    static let good = statusText(.systemGreen)
    static let warn = statusText(.systemOrange)
    static let crit = statusText(.systemRed)

    /// The colour of a context or limit level, always beside its words.
    static func level(_ level: Level) -> Color {
        switch level {
        case .ok: return good
        case .warn: return warn
        case .crit: return crit
        }
    }

    // MARK: The coach window

    /// The coach page's own background (its --bg), behind the page in its window.
    static let page = token(0xf3f2ee, 0x111110)
    static let pageNS = NSColor(name: nil) { isDark($0) ? nsColor(0x111110) : nsColor(0xf3f2ee) }
    /// Behind the bar that says a page build failed.
    static let warnSoft = dyn(nsColor(0xfab219, alpha: 0.2), nsColor(0xfab219, alpha: 0.16))

    // Raw values for the menu bar item, which is drawn with AppKit on the menu bar's own colours.
    static let goodLight: UInt32 = 0x1f7045, goodDark: UInt32 = 0x6cc693
    static let critLight: UInt32 = 0xae2f27, critDark: UInt32 = 0xf08b81

    // MARK: Type

    /// SF Pro with tabular digits, as `NSFont.monospacedDigitSystemFont` gives. No SF Mono.
    static func sans(_ size: CGFloat, _ weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight).monospacedDigit()
    }
}
