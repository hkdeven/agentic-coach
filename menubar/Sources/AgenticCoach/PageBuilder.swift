import CoachCore
import Foundation

/// Rebuilds the local coach page by running the coach's own launcher:
/// `python3 <coach home>/coach_live.py page`. The launcher finds the current
/// coach code, so this app holds no path to it. Nothing here touches the network.
enum PageBuilder {
    static func run(completion: @escaping @MainActor (PageBuildOutcome) -> Void) {
        let environment = ProcessInfo.processInfo.environment
        let launcher = CoachPaths.launcher(environment: environment, home: NSHomeDirectory())

        DispatchQueue.global(qos: .userInitiated).async {
            let outcome: PageBuildOutcome
            if !FileManager.default.fileExists(atPath: launcher) {
                outcome = .failed(PageBuild.notSetUp)
            } else if let python = PageBuild.python(canRun: FileManager.default.isExecutableFile(atPath:)) {
                outcome = build(python: python, launcher: launcher, environment: environment)
            } else {
                outcome = .failed(PageBuild.noPython)
            }
            Task { @MainActor in completion(outcome) }
        }
    }

    private static func build(python: String, launcher: String, environment: [String: String]) -> PageBuildOutcome {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = [launcher, "page"]
        process.environment = environment // COACH_HOME, when set, reaches the launcher this way
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        let errors = Pipe()
        process.standardError = errors

        do {
            try process.run()
        } catch {
            return .failed("The page build could not start: \(error.localizedDescription)")
        }

        let lock = NSLock()
        var timedOut = false
        let stop = DispatchWorkItem {
            guard process.isRunning else { return }
            lock.lock()
            timedOut = true
            lock.unlock()
            process.terminate()
            // If it does not stop when asked, end it, so the window is never left on "Updating".
            let pid = process.processIdentifier
            DispatchQueue.global().asyncAfter(deadline: .now() + 5) {
                if process.isRunning { kill(pid, SIGKILL) }
            }
        }
        DispatchQueue.global().asyncAfter(deadline: .now() + PageBuild.timeoutSeconds, execute: stop)

        // Read to the end before waiting, so a long message cannot fill the pipe and stall the build.
        let data = errors.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        stop.cancel()

        lock.lock()
        let late = timedOut
        lock.unlock()
        if late { return .failed(PageBuild.tookTooLong) }
        return PageBuild.outcome(exitCode: process.terminationStatus, errors: String(decoding: data, as: UTF8.self))
    }
}
