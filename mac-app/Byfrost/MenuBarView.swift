import SwiftUI

/// SwiftUI popover content for the menu bar icon.
///
/// Shows daemon status, connection info, active task, and controls.
/// All data comes from DaemonManager's @Published properties (driven
/// by state.json polling every 5 seconds).
struct MenuBarView: View {
    @ObservedObject var daemonManager: DaemonManager

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            // Status header
            HStack {
                Text("Byfrost")
                    .font(.headline)
                Spacer()
                Circle()
                    .fill(statusColor)
                    .frame(width: 10, height: 10)
                Text(daemonManager.state.displayName)
                    .font(.subheadline)
                    .foregroundColor(.secondary)
            }

            Divider()

            // Connection info (when daemon is running)
            if daemonManager.state != .stopped {
                connectionSection
                Divider()
            }

            // Active task (when one is running)
            if let preview = daemonManager.activeTaskPreview {
                activeTaskSection(preview: preview)
                Divider()
            }

            // Status details
            statusSection

            Divider()

            // Daemon controls
            HStack(spacing: 8) {
                Button("Start") {
                    daemonManager.start()
                }
                .disabled(
                    daemonManager.state == .running
                    || daemonManager.state == .taskActive
                    || daemonManager.state == .disconnected
                )

                Button("Stop") {
                    daemonManager.stop()
                }
                .disabled(daemonManager.state == .stopped)

                Button("Restart") {
                    daemonManager.restart()
                }
                .disabled(daemonManager.state == .stopped)
            }

            Divider()

            // Quit button
            Button("Quit Byfrost") {
                NSApplication.shared.terminate(nil)
            }
        }
        .padding()
        .frame(width: 320)
    }

    // MARK: - Connection Section

    private var connectionSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            infoRow("Clients", "\(daemonManager.clientCount)")
            if let uptime = daemonManager.uptime {
                infoRow("Uptime", formatDuration(uptime))
            }
        }
        .font(.system(.caption, design: .monospaced))
    }

    // MARK: - Active Task Section

    private func activeTaskSection(preview: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Image(systemName: "bolt.fill")
                    .foregroundColor(.purple)
                Text("Active Task")
                    .font(.subheadline.bold())
            }
            Text(preview)
                .font(.caption)
                .lineLimit(2)
                .foregroundColor(.primary)
            if let runtime = daemonManager.activeTaskRuntime {
                Text(formatDuration(runtime))
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundColor(.secondary)
            }
        }
    }

    // MARK: - Status Section

    private var statusSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let pid = daemonManager.pid {
                infoRow("PID", "\(pid)")
            }
            if !daemonManager.config.projectPath.isEmpty {
                infoRow("Project", daemonManager.config.projectPath)
            }
            infoRow("Port", "\(daemonManager.config.port)")
            if daemonManager.queueSize > 0 {
                infoRow("Queue", "\(daemonManager.queueSize)")
            }
            if let version = daemonManager.daemonVersion {
                infoRow("Version", version)
            }
            if let error = daemonManager.lastError {
                HStack(alignment: .top) {
                    Text("Error")
                        .foregroundColor(.red)
                        .frame(width: 60, alignment: .leading)
                    Text(error)
                        .lineLimit(2)
                        .foregroundColor(.red)
                }
            }
        }
        .font(.system(.caption, design: .monospaced))
    }

    // MARK: - Helpers

    private var statusColor: Color {
        switch daemonManager.state {
        case .stopped: return .gray
        case .running: return .green
        case .taskActive: return .purple
        case .disconnected: return .orange
        case .error: return .red
        }
    }

    private func infoRow(
        _ label: String, _ value: String
    ) -> some View {
        HStack {
            Text(label)
                .foregroundColor(.secondary)
                .frame(width: 60, alignment: .leading)
            Text(value)
                .lineLimit(1)
                .truncationMode(.middle)
        }
    }

    private func formatDuration(_ seconds: TimeInterval) -> String {
        if seconds < 60 {
            return "\(Int(seconds))s"
        } else if seconds < 3600 {
            let m = Int(seconds / 60)
            let s = Int(seconds.truncatingRemainder(dividingBy: 60))
            return "\(m)m \(s)s"
        } else {
            let h = Int(seconds / 3600)
            let m = Int(seconds.truncatingRemainder(dividingBy: 3600) / 60)
            return "\(h)h \(m)m"
        }
    }
}
