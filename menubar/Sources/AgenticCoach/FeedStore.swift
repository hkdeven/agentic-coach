import CoachCore
import Foundation

/// Reads live.json and keeps it fresh: once when the file's folder changes
/// (the feed is replaced by rename, so the folder is what is watched) and
/// on a timer twice a minute. No network, no parsing of anything else.
@MainActor
final class FeedStore: ObservableObject {
    static let shared = FeedStore()

    @Published private(set) var state: FeedState = .missing
    @Published private(set) var now = Date()

    let path: String
    private let fixedNow: Date?
    private var watcher: DispatchSourceFileSystemObject?
    private var timer: Timer?
    private var pending: DispatchWorkItem?

    /// A store on one feed file at one fixed moment, with no watcher and no timer.
    /// For layout checks that draw the panel off screen.
    init(path: String, now: Date) {
        self.path = path
        fixedNow = now
        reload()
    }

    init() {
        let env = ProcessInfo.processInfo.environment
        path = FeedLoader.resolvePath(
            arguments: CommandLine.arguments,
            environment: env,
            home: NSHomeDirectory()
        )
        // COACH_NOW pins the clock so screenshots of a fixture are repeatable.
        fixedNow = env["COACH_NOW"].flatMap(FeedDates.parse)
        reload()
        watch()
        let timer = Timer(timeInterval: 30, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.reload()
                self?.watch()
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    func reload() {
        now = fixedNow ?? Date()
        let fresh = FeedLoader.load(path: path)
        if fresh != state { state = fresh }
    }

    private func watch() {
        guard watcher == nil else { return }
        let folder = (path as NSString).deletingLastPathComponent
        let fd = open(folder, O_EVTONLY)
        guard fd >= 0 else { return } // No folder yet; the timer tries again.
        let source = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd,
            eventMask: [.write, .rename, .delete],
            queue: .main
        )
        source.setEventHandler { [weak self] in
            MainActor.assumeIsolated {
                guard let self else { return }
                if source.data.contains(.delete) || source.data.contains(.rename) {
                    // The folder itself went away; start again on the next tick.
                    source.cancel()
                    self.watcher = nil
                }
                self.pending?.cancel()
                let work = DispatchWorkItem { [weak self] in
                    MainActor.assumeIsolated { self?.reload() }
                }
                self.pending = work
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.2, execute: work)
            }
        }
        source.setCancelHandler { close(fd) }
        source.resume()
        watcher = source
    }
}
