import AppKit
import Combine
import Foundation

// MARK: - Daemon Rich State (from state.json)

/// Rich daemon state from ~/.byfrost/state.json.
///
/// Written atomically by the Python daemon on lifecycle events.
/// Schema matches daemon/byfrost_daemon.py `_write_state()`.
struct DaemonRichState: Codable {
    let pid: Int?
    let startedAt: Double?
    let state: String?
    let projectPath: String?
    let clients: Int?
    let activeTask: ActiveTaskInfo?
    let queueSize: Int?
    let lastError: String?
    let python: String?
    let version: String?
    let updatedAt: Double?

    struct ActiveTaskInfo: Codable {
        let id: String
        let promptPreview: String?
        let startedAt: Double?
        let status: String?

        enum CodingKeys: String, CodingKey {
            case id
            case promptPreview = "prompt_preview"
            case startedAt = "started_at"
            case status
        }
    }

    enum CodingKeys: String, CodingKey {
        case pid
        case startedAt = "started_at"
        case state
        case projectPath = "project_path"
        case clients
        case activeTask = "active_task"
        case queueSize = "queue_size"
        case lastError = "last_error"
        case python
        case version
        case updatedAt = "updated_at"
    }
}

// MARK: - Daemon Manager

/// Manages the Python byfrost daemon lifecycle.
///
/// Matches paths and formats from core/config.py and cli/daemon_mgr.py.
/// Coexists with `byfrost daemon` CLI - both use the same PID file,
/// daemon.json, and launchd plist.
@MainActor
final class DaemonManager: ObservableObject {

    // MARK: - Published State

    @Published var state: DaemonState = .stopped
    @Published var pid: pid_t?
    @Published var config: DaemonConfig

    // Rich state from state.json (updated every health check)
    @Published var richState: DaemonRichState?
    @Published var clientCount: Int = 0
    @Published var activeTaskPreview: String?
    @Published var activeTaskRuntime: TimeInterval?
    @Published var queueSize: Int = 0
    @Published var uptime: TimeInterval?
    @Published var lastError: String?
    @Published var daemonVersion: String?

    // MARK: - Constants (must match cli/daemon_mgr.py)

    /// Launchd label - matches LABEL in cli/daemon_mgr.py line 19
    static let launchdLabel = "com.byfrost.daemon"

    /// Python module to run - matches DAEMON_MODULE in cli/daemon_mgr.py line 21
    static let daemonModule = "daemon.byfrost_daemon"

    /// Max auto-restart attempts on crash
    private static let maxRestartAttempts = 3

    /// Delay between restart attempts (matches launchd ThrottleInterval)
    private static let restartDelay: TimeInterval = 10

    /// Health check interval
    private static let healthInterval: TimeInterval = 5

    // MARK: - Private

    private var healthTimer: Timer?
    private var restartCount = 0
    private var process: Process?
    private var plistPath: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/\(Self.launchdLabel).plist")
    }

    // MARK: - Init

    init() {
        self.config = DaemonConfig.load()
        recoverPID()
        startHealthCheck()
    }

    deinit {
        healthTimer?.invalidate()
    }

    // MARK: - PID Recovery

    /// On init, read ~/.byfrost/daemon.pid and adopt if process is alive.
    private func recoverPID() {
        guard FileManager.default.fileExists(atPath: DaemonConfig.pidFile.path) else {
            return
        }
        guard let contents = try? String(contentsOf: DaemonConfig.pidFile, encoding: .utf8),
              let savedPID = pid_t(contents.trimmingCharacters(in: .whitespacesAndNewlines)) else {
            return
        }
        if isProcessAlive(savedPID) {
            pid = savedPID
            state = .running
            restartCount = 0
        }
    }

    /// Check if a process is alive via kill(pid, 0).
    private func isProcessAlive(_ pid: pid_t) -> Bool {
        kill(pid, 0) == 0
    }

    // MARK: - Start

    func start() {
        guard state == .stopped || state == .error else { return }

        // Prefer launchctl if plist is installed
        if isPlistInstalled() {
            launchctlStart()
        } else {
            // Install plist first, then start via launchctl
            installPlist()
            launchctlStart()
        }
    }

    private func launchctlStart() {
        let result = shell("launchctl", "start", Self.launchdLabel)
        if result.exitCode == 0 {
            // Give the daemon a moment to write its PID file
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
                self?.recoverPID()
                if self?.pid != nil {
                    self?.state = .running
                    self?.restartCount = 0
                }
            }
        } else {
            // Fallback: direct process spawn
            spawnDirect()
        }
    }

    private func spawnDirect() {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        proc.arguments = ["-m", Self.daemonModule]
        proc.currentDirectoryURL = FileManager.default.homeDirectoryForCurrentUser
        proc.environment = [
            "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
            "BYFROST_HOME": DaemonConfig.bridgeDir.path,
        ]

        proc.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                self?.handleTermination(exitCode: proc.terminationStatus)
            }
        }

        do {
            try proc.run()
            process = proc
            pid = proc.processIdentifier
            state = .running
            restartCount = 0
        } catch {
            state = .error
        }
    }

    // MARK: - Stop

    func stop() {
        guard state != .stopped else { return }

        if isPlistInstalled() {
            let _ = shell("launchctl", "stop", Self.launchdLabel)
        }

        // Also send SIGTERM if we have a PID (covers direct-spawn case)
        if let currentPID = pid, isProcessAlive(currentPID) {
            kill(currentPID, SIGTERM)

            // Wait up to 5s for graceful shutdown on a background thread.
            // Capture PID value to avoid accessing @MainActor state off-main.
            let capturedPID = currentPID
            Task.detached {
                for _ in 0..<50 {
                    if kill(capturedPID, 0) != 0 { break }
                    try? await Task.sleep(nanoseconds: 100_000_000)
                }
                await MainActor.run { [weak self] in
                    // Force kill if still alive
                    if kill(capturedPID, 0) == 0 {
                        kill(capturedPID, SIGKILL)
                    }
                    self?.pid = nil
                    self?.state = .stopped
                    self?.process = nil
                }
            }
        } else {
            pid = nil
            state = .stopped
            process = nil
        }
    }

    // MARK: - Restart

    func restart() {
        stop()
        // Give stop a moment to finish
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
            self?.start()
        }
    }

    // MARK: - Auto-Restart on Crash

    private func handleTermination(exitCode: Int32) {
        process = nil

        if exitCode != 0 && restartCount < Self.maxRestartAttempts {
            state = .error
            restartCount += 1
            // Retry after delay (matches launchd ThrottleInterval = 10s)
            DispatchQueue.main.asyncAfter(deadline: .now() + Self.restartDelay) { [weak self] in
                guard let self, self.state == .error else { return }
                self.start()
            }
        } else if exitCode != 0 {
            state = .error
            pid = nil
        } else {
            state = .stopped
            pid = nil
        }
    }

    // MARK: - Health Check

    private func startHealthCheck() {
        healthTimer = Timer.scheduledTimer(
            withTimeInterval: Self.healthInterval,
            repeats: true
        ) { [weak self] _ in
            Task { @MainActor in
                self?.checkHealth()
            }
        }
    }

    private func checkHealth() {
        if let currentPID = pid {
            if isProcessAlive(currentPID) {
                // Still running - state.json reconciliation handles state
                if state == .error || state == .stopped {
                    state = .running
                }
            } else {
                // Process died
                handleTermination(exitCode: 1)
            }
        } else if state != .stopped && state != .error {
            // No PID but state says running - try to recover
            recoverPID()
            if pid == nil {
                state = .stopped
            }
        } else if state == .stopped {
            // Check if CLI started the daemon behind our back
            recoverPID()
        }

        // Poll state.json for rich state info
        loadStateFile()
    }

    /// Read ~/.byfrost/state.json and update published properties.
    private func loadStateFile() {
        let path = DaemonConfig.stateFile.path
        guard FileManager.default.fileExists(atPath: path) else {
            richState = nil
            return
        }
        do {
            let data = try Data(contentsOf: DaemonConfig.stateFile)
            let decoded = try JSONDecoder().decode(
                DaemonRichState.self, from: data
            )
            richState = decoded

            // Derive convenience properties
            clientCount = decoded.clients ?? 0
            queueSize = decoded.queueSize ?? 0
            lastError = decoded.lastError
            daemonVersion = decoded.version

            if let taskInfo = decoded.activeTask {
                activeTaskPreview = taskInfo.promptPreview
                if let taskStart = taskInfo.startedAt {
                    activeTaskRuntime = Date().timeIntervalSince1970 - taskStart
                } else {
                    activeTaskRuntime = nil
                }
            } else {
                activeTaskPreview = nil
                activeTaskRuntime = nil
            }

            if let startedAt = decoded.startedAt {
                uptime = Date().timeIntervalSince1970 - startedAt
            } else {
                uptime = nil
            }

            // Reconcile DaemonState with state.json when daemon is alive
            if pid != nil, let stateStr = decoded.state {
                switch stateStr {
                case "running":
                    if decoded.activeTask != nil {
                        state = .taskActive
                    } else if clientCount == 0 {
                        state = .disconnected
                    } else {
                        state = .running
                    }
                case "starting", "restarting":
                    state = .running
                case "stopped":
                    break // Let PID-based check handle this
                default:
                    break
                }
            }
        } catch {
            richState = nil
        }
    }

    // MARK: - Launchd Plist

    /// Check if plist file exists at ~/Library/LaunchAgents/
    func isPlistInstalled() -> Bool {
        FileManager.default.fileExists(atPath: plistPath.path)
    }

    /// Install the launchd plist. Content matches cli/daemon_mgr.py _generate_plist().
    func installPlist() {
        let agentsDir = plistPath.deletingLastPathComponent()
        try? FileManager.default.createDirectory(
            at: agentsDir, withIntermediateDirectories: true
        )

        // Ensure log directory exists (matches cli/daemon_mgr.py line 74)
        try? FileManager.default.createDirectory(
            at: DaemonConfig.logDir, withIntermediateDirectories: true
        )

        let python = findPython()
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let logDir = DaemonConfig.logDir.path
        let bridgeDir = DaemonConfig.bridgeDir.path

        // Must match cli/daemon_mgr.py lines 76-126 exactly
        let plist = """
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
        "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>\(Self.launchdLabel)</string>

            <key>ProgramArguments</key>
            <array>
                <string>\(python)</string>
                <string>-m</string>
                <string>\(Self.daemonModule)</string>
            </array>

            <key>RunAtLoad</key>
            <true/>

            <key>KeepAlive</key>
            <dict>
                <key>SuccessfulExit</key>
                <false/>
            </dict>

            <key>ThrottleInterval</key>
            <integer>10</integer>

            <key>WorkingDirectory</key>
            <string>\(home)</string>

            <key>EnvironmentVariables</key>
            <dict>
                <key>PATH</key>
                <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
                <key>BYFROST_HOME</key>
                <string>\(bridgeDir)</string>
            </dict>

            <key>StandardOutPath</key>
            <string>\(logDir)/launchd-stdout.log</string>
            <key>StandardErrorPath</key>
            <string>\(logDir)/launchd-stderr.log</string>

            <key>SoftResourceLimits</key>
            <dict>
                <key>NumberOfFiles</key>
                <integer>4096</integer>
            </dict>
        </dict>
        </plist>
        """

        try? plist.write(to: plistPath, atomically: true, encoding: .utf8)
        let _ = shell("launchctl", "load", plistPath.path)
    }

    /// Uninstall the launchd plist.
    func uninstallPlist() {
        if isPlistInstalled() {
            let _ = shell("launchctl", "unload", plistPath.path)
            try? FileManager.default.removeItem(at: plistPath)
        }
    }

    // MARK: - Helpers

    /// Find python3 executable path (matches what CLI would use).
    private func findPython() -> String {
        // Check common locations
        let candidates = [
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/usr/bin/python3",
        ]
        for path in candidates {
            if FileManager.default.isExecutableFile(atPath: path) {
                return path
            }
        }
        return "/usr/bin/python3"
    }

    /// Run a shell command and return result.
    @discardableResult
    private func shell(_ args: String...) -> (exitCode: Int32, output: String) {
        let proc = Process()
        let pipe = Pipe()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        proc.arguments = args
        proc.standardOutput = pipe
        proc.standardError = pipe
        do {
            try proc.run()
            proc.waitUntilExit()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            let output = String(data: data, encoding: .utf8) ?? ""
            return (proc.terminationStatus, output)
        } catch {
            return (-1, error.localizedDescription)
        }
    }
}
