import Foundation

/// Read/write ~/.byfrost/daemon.json with snake_case keys to match Python.
///
/// Python format (from core/config.py):
/// ```json
/// {"project_path": "/path/to/project", "port": 9784}
/// ```
struct DaemonConfig: Codable, Equatable {
    var projectPath: String
    var port: Int

    enum CodingKeys: String, CodingKey {
        case projectPath = "project_path"
        case port
    }

    // Must match core/config.py DEFAULT_PORT
    static let defaultPort = 9784

    static let bridgeDir: URL = {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".byfrost")
    }()

    static let configFile: URL = {
        bridgeDir.appendingPathComponent("daemon.json")
    }()

    static let pidFile: URL = {
        bridgeDir.appendingPathComponent("daemon.pid")
    }()

    static let logDir: URL = {
        bridgeDir.appendingPathComponent("logs")
    }()

    static let authFile: URL = {
        bridgeDir.appendingPathComponent("auth.json")
    }()

    static let stateFile: URL = {
        bridgeDir.appendingPathComponent("state.json")
    }()

    init(projectPath: String = "", port: Int = DaemonConfig.defaultPort) {
        self.projectPath = projectPath
        self.port = port
    }

    /// Load config from ~/.byfrost/daemon.json. Returns defaults if missing.
    static func load() -> DaemonConfig {
        guard FileManager.default.fileExists(atPath: configFile.path) else {
            return DaemonConfig()
        }
        do {
            let data = try Data(contentsOf: configFile)
            return try JSONDecoder().decode(DaemonConfig.self, from: data)
        } catch {
            // File exists but can't parse - return defaults
            return DaemonConfig()
        }
    }

    /// Save config to ~/.byfrost/daemon.json.
    func save() throws {
        try FileManager.default.createDirectory(
            at: DaemonConfig.bridgeDir,
            withIntermediateDirectories: true
        )
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let data = try encoder.encode(self)
        try data.write(to: DaemonConfig.configFile)
    }
}
