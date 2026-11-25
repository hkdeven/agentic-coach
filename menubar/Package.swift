// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "AgenticCoach",
    platforms: [.macOS(.v14)],
    targets: [
        // Everything that can be tested without a screen: the feed model,
        // the formatters and the menu bar state rule.
        .target(name: "CoachCore", path: "Sources/CoachCore"),
        .executableTarget(
            name: "AgenticCoach",
            dependencies: ["CoachCore"],
            path: "Sources/AgenticCoach"
        ),
        .testTarget(
            name: "CoachCoreTests",
            dependencies: ["CoachCore"],
            path: "Tests/CoachCoreTests"
        ),
        // Layout checks that draw the panel off screen (no window is ever shown).
        .testTarget(
            name: "AgenticCoachTests",
            dependencies: ["AgenticCoach", "CoachCore"],
            path: "Tests/AgenticCoachTests"
        ),
    ]
)
