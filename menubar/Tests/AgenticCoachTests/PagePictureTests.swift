import AppKit
import Testing
import WebKit

/// Draws a built coach page into picture files without putting anything on screen, so the
/// page's look can be checked with no window shown. Runs only when COACH_PAGE_FILE names a
/// built page and COACH_SNAPSHOT_DIR a folder:
/// `COACH_PAGE_FILE=/tmp/page.html COACH_SNAPSHOT_DIR=/tmp/shots swift test --filter pagePictures`.
/// COACH_PAGE_HASHES lists the views to draw, for example "overview/month,cost".
@MainActor
@Suite(.serialized) struct PagePictures {
    private static var environment: [String: String] { ProcessInfo.processInfo.environment }

    @Test(.enabled(if: ProcessInfo.processInfo.environment["COACH_PAGE_FILE"] != nil
            && ProcessInfo.processInfo.environment["COACH_SNAPSHOT_DIR"] != nil))
    func pagePictures() async throws {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.prohibited) // a test process: no Dock icon, nothing on screen
        let file = URL(fileURLWithPath: Self.environment["COACH_PAGE_FILE"] ?? "")
        let folder = URL(fileURLWithPath: Self.environment["COACH_SNAPSHOT_DIR"] ?? "")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let hashes = (Self.environment["COACH_PAGE_HASHES"] ?? "overview/today").split(separator: ",").map(String.init)
        let width = Double(Self.environment["COACH_PAGE_WIDTH"] ?? "") ?? 1280
        // As the app loads it: a doctype and charset in front, the file's own URL as the base.
        let page = Data("<!doctype html><meta charset=\"utf-8\">\n".utf8) + (try Data(contentsOf: file))

        for (look, name) in [(NSAppearance.Name.darkAqua, "dark"), (.aqua, "light")] {
            for hash in hashes {
                let configuration = WKWebViewConfiguration()
                configuration.websiteDataStore = .nonPersistent()
                let web = WKWebView(frame: NSRect(x: 0, y: 0, width: width, height: 1100), configuration: configuration)
                let window = NSWindow(contentRect: web.frame, styleMask: [.borderless], backing: .buffered, defer: true)
                window.isReleasedWhenClosed = false
                window.appearance = NSAppearance(named: look)
                window.contentView = web
                web.load(page, mimeType: "text/html", characterEncodingName: "utf-8", baseURL: URL(string: file.absoluteString + "#" + hash)!)
                var tries = 0
                while web.isLoading, tries < 200 {
                    try await Task.sleep(nanoseconds: 50_000_000)
                    tries += 1
                }
                _ = try? await web.evaluateJavaScript("location.hash = '#\(hash)'; 0")
                try await Task.sleep(nanoseconds: 300_000_000)
                let height = (try? await web.evaluateJavaScript("document.documentElement.scrollHeight") as? Double) ?? 1100
                web.setFrameSize(NSSize(width: width, height: min(max(height, 400), 6000)))
                window.setContentSize(web.frame.size)
                try await Task.sleep(nanoseconds: 300_000_000)
                let shot = WKSnapshotConfiguration()
                shot.rect = web.bounds
                let image = try await web.takeSnapshot(configuration: shot)
                let tiff = try #require(image.tiffRepresentation)
                let png = try #require(NSBitmapImageRep(data: tiff)?.representation(using: .png, properties: [:]))
                try png.write(to: folder.appendingPathComponent("page-\(name)-\(hash.replacingOccurrences(of: "/", with: "-")).png"))
            }
        }
    }
}
