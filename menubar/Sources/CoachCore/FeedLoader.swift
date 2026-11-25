import Foundation

public enum FeedState: Equatable {
    /// No live.json yet: the person has not run /coach setup.
    case missing
    /// The file is there but is not the JSON this app expects.
    case unreadable
    /// The feed's version is above the one this app knows.
    case needsUpdate
    case ready(Feed)
}

public enum FeedLoader {
    public static let supportedVersion = 1
    static let maxBytes = 5_000_000

    private struct VersionProbe: Decodable {
        let version: Double?
        enum Keys: String, CodingKey { case version }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: Keys.self)
            version = c.number(.version)
        }
    }

    public static func decode(_ data: Data) -> FeedState {
        let decoder = JSONDecoder()
        guard let probe = try? decoder.decode(VersionProbe.self, from: data) else {
            return .unreadable
        }
        if let v = probe.version, v > Double(supportedVersion) {
            return .needsUpdate
        }
        guard let feed = try? decoder.decode(Feed.self, from: data) else {
            return .unreadable
        }
        return .ready(feed)
    }

    public static func load(path: String) -> FeedState {
        guard FileManager.default.fileExists(atPath: path) else { return .missing }
        // Only a plain file of a sane size is read: the read happens on the main thread.
        let real = (path as NSString).resolvingSymlinksInPath
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: real),
              attrs[.type] as? FileAttributeType == .typeRegular,
              let size = attrs[.size] as? NSNumber, size.intValue <= maxBytes,
              let data = FileManager.default.contents(atPath: real)
        else { return .unreadable }
        return decode(data)
    }

    /// Where the feed is read from, first match wins:
    /// `--feed <path>`, `COACH_FEED`, `$COACH_HOME/live.json`, `~/.claude/coach/live.json`.
    public static func resolvePath(arguments: [String], environment: [String: String], home: String) -> String {
        if let i = arguments.firstIndex(of: "--feed"), i + 1 < arguments.count, !arguments[i + 1].isEmpty {
            return expand(arguments[i + 1], home: home)
        }
        if let p = environment["COACH_FEED"], !p.isEmpty {
            return expand(p, home: home)
        }
        if let dir = environment["COACH_HOME"], !dir.isEmpty {
            return (expand(dir, home: home) as NSString).appendingPathComponent("live.json")
        }
        return (home as NSString).appendingPathComponent(".claude/coach/live.json")
    }

    private static func expand(_ path: String, home: String) -> String {
        if path == "~" { return home }
        if path.hasPrefix("~/") { return (home as NSString).appendingPathComponent(String(path.dropFirst(2))) }
        return path
    }
}
