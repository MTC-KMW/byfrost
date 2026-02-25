import Foundation

/// Runs `byfrost` CLI commands as subprocesses.
///
/// The Mac app delegates auth and setup to the CLI rather than
/// reimplementing API calls in Swift. This keeps the app as a thin
/// management layer over the existing Python CLI.
@MainActor
final class CLIRunner: ObservableObject {

    /// Result of a CLI command execution.
    struct Result {
        let exitCode: Int32
        let output: String
        let error: String
    }

    /// Parsed device code from `byfrost login` output.
    struct DeviceCode {
        let userCode: String
        let verificationURI: String
    }

    // MARK: - Find CLI

    /// Find the byfrost CLI executable.
    /// Checks common macOS install locations, then falls back to PATH.
    static func findByfrostCLI() -> String? {
        let candidates = [
            // Prefer venv install (development) over system installs
            NSHomeDirectory() + "/byfrost/.venv/bin/byfrost",
            NSHomeDirectory() + "/.local/bin/byfrost",
            "/opt/homebrew/bin/byfrost",
            "/usr/local/bin/byfrost",
        ]
        for path in candidates {
            if FileManager.default.isExecutableFile(atPath: path) {
                return path
            }
        }
        // Fall back to which
        let result = shell("which", "byfrost")
        if result.exitCode == 0 {
            let path = result.output.trimmingCharacters(in: .whitespacesAndNewlines)
            if !path.isEmpty {
                return path
            }
        }
        return nil
    }

    // MARK: - Run Commands

    /// Run a byfrost CLI command asynchronously. Returns result with
    /// exit code, stdout, and stderr.
    func run(_ arguments: String...) async -> Result {
        guard let cli = Self.findByfrostCLI() else {
            return Result(
                exitCode: 127,
                output: "",
                error: "byfrost CLI not found"
            )
        }
        return await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                let result = Self.shell(cli, arguments)
                continuation.resume(returning: result)
            }
        }
    }

    // MARK: - Login

    /// Start `byfrost login` and parse the device code from output.
    ///
    /// Returns the running Process and parsed DeviceCode. The process
    /// continues polling the server until the user authorizes in their
    /// browser. Monitor `process.isRunning` and `process.terminationStatus`
    /// for completion.
    func startLogin(
        serverURL: String? = nil
    ) async throws -> (process: Process, deviceCode: DeviceCode) {
        guard let cli = Self.findByfrostCLI() else {
            throw CLIError.notFound
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: cli)
        var args = ["login"]
        if let url = serverURL {
            args += ["--server", url]
        }
        process.arguments = args

        // Disable Python stdout buffering so output arrives line-by-line
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        process.environment = env

        let stdout = Pipe()
        process.standardOutput = stdout
        // Discard stderr to prevent pipe deadlock (we only parse stdout)
        process.standardError = FileHandle.nullDevice

        try process.run()

        // Read stdout lines on a background thread looking for the
        // device code and verification URI
        let deviceCode: DeviceCode = try await withCheckedThrowingContinuation {
            continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                var userCode: String?
                var verificationURI: String?
                var resumed = false

                let handle = stdout.fileHandleForReading
                var buffer = Data()

                // Read line by line until we find both values,
                // then keep draining to prevent pipe deadlock
                while process.isRunning {
                    let chunk = handle.availableData
                    if chunk.isEmpty {
                        Thread.sleep(forTimeInterval: 0.1)
                        continue
                    }
                    // After finding device code, just drain without parsing
                    if resumed { continue }

                    buffer.append(chunk)

                    // Process complete lines
                    while let range = buffer.range(
                        of: Data("\n".utf8)
                    ) {
                        let lineData = buffer.subdata(in: buffer.startIndex..<range.lowerBound)
                        buffer.removeSubrange(buffer.startIndex...range.lowerBound)

                        guard let line = String(data: lineData, encoding: .utf8) else {
                            continue
                        }
                        let trimmed = line.trimmingCharacters(
                            in: .whitespaces
                        )

                        if trimmed.contains("http") && trimmed.contains("://") {
                            verificationURI = trimmed
                        } else if trimmed.count >= 8,
                                  trimmed.count <= 12,
                                  trimmed.contains("-"),
                                  trimmed.allSatisfy({
                                      $0.isUppercase || $0.isNumber || $0 == "-"
                                  }) {
                            userCode = trimmed
                        }

                        if !resumed, let code = userCode, let uri = verificationURI {
                            continuation.resume(
                                returning: DeviceCode(
                                    userCode: code,
                                    verificationURI: uri
                                )
                            )
                            resumed = true
                        }
                    }
                }
                // Drain any remaining data after process exits
                _ = handle.readDataToEndOfFile()

                if !resumed {
                    continuation.resume(throwing: CLIError.loginFailed(
                        "Login process exited before displaying device code"
                    ))
                }
            }
        }

        return (process, deviceCode)
    }

    // MARK: - Prerequisites

    /// Check for required tools. Returns availability of each.
    func checkPrerequisites() -> (claude: Bool, tmux: Bool) {
        let claude = Self.commandExists("claude")
            || Self.commandExists("claude-code")
        let tmux = Self.commandExists("tmux")
        return (claude: claude, tmux: tmux)
    }

    /// Check if a command exists at common macOS paths or in PATH.
    static func commandExists(_ name: String) -> Bool {
        let paths = [
            "/usr/local/bin/\(name)",
            "/opt/homebrew/bin/\(name)",
        ]
        for path in paths {
            if FileManager.default.isExecutableFile(atPath: path) {
                return true
            }
        }
        // Check PATH via which
        let result = shell("which", name)
        return result.exitCode == 0
    }

    // MARK: - Shell Helpers

    /// Run a shell command synchronously. Returns exit code + output.
    private static func shell(
        _ command: String, _ arguments: [String] = []
    ) -> Result {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: command)
        process.arguments = arguments

        // Disable Python stdout buffering
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        process.environment = env

        let stdout = Pipe()
        let stderr = Pipe()
        process.standardOutput = stdout
        process.standardError = stderr

        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            return Result(
                exitCode: 127,
                output: "",
                error: error.localizedDescription
            )
        }

        let outData = stdout.fileHandleForReading.readDataToEndOfFile()
        let errData = stderr.fileHandleForReading.readDataToEndOfFile()

        return Result(
            exitCode: process.terminationStatus,
            output: String(data: outData, encoding: .utf8) ?? "",
            error: String(data: errData, encoding: .utf8) ?? ""
        )
    }

    /// Convenience: run shell with variadic args.
    private static func shell(
        _ command: String, _ arguments: String...
    ) -> Result {
        shell(command, Array(arguments))
    }
}

// MARK: - Errors

enum CLIError: LocalizedError {
    case notFound
    case loginFailed(String)

    var errorDescription: String? {
        switch self {
        case .notFound:
            return "byfrost CLI not found. Install with: pip install byfrost"
        case .loginFailed(let reason):
            return "Login failed: \(reason)"
        }
    }
}
