import SwiftUI

/// SwiftUI popover content for the menu bar icon.
///
/// Minimal for now - status header, daemon controls, status info, quit.
/// Full dropdown (active task, recent tasks, team management) comes in Task 2.6.
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

            // Status info
            VStack(alignment: .leading, spacing: 4) {
                if let pid = daemonManager.pid {
                    infoRow("PID", "\(pid)")
                }
                if !daemonManager.config.projectPath.isEmpty {
                    infoRow("Project", daemonManager.config.projectPath)
                }
                infoRow("Port", "\(daemonManager.config.port)")
            }
            .font(.system(.caption, design: .monospaced))

            Divider()

            // Daemon controls
            HStack(spacing: 8) {
                Button("Start") {
                    daemonManager.start()
                }
                .disabled(daemonManager.state == .running
                          || daemonManager.state == .taskActive
                          || daemonManager.state == .disconnected)

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
        .frame(width: 300)
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

    private func infoRow(_ label: String, _ value: String) -> some View {
        HStack {
            Text(label)
                .foregroundColor(.secondary)
                .frame(width: 60, alignment: .leading)
            Text(value)
                .lineLimit(1)
                .truncationMode(.middle)
        }
    }
}
