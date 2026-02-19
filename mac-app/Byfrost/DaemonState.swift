import Foundation

/// Daemon lifecycle states - drives menu bar icon color.
enum DaemonState: String {
    case stopped        // gray circle outline - not running
    case running        // green circle.fill - running, idle
    case taskActive     // purple circle.fill (pulsing) - task running
    case disconnected   // orange circle.fill - running but no controller
    case error          // red exclamationmark.circle.fill - crashed

    var displayName: String {
        switch self {
        case .stopped: return "Stopped"
        case .running: return "Running"
        case .taskActive: return "Task Active"
        case .disconnected: return "Disconnected"
        case .error: return "Error"
        }
    }

    /// SF Symbol name for the menu bar icon.
    var iconName: String {
        switch self {
        case .stopped: return "circle"
        case .running: return "circle.fill"
        case .taskActive: return "circle.fill"
        case .disconnected: return "circle.fill"
        case .error: return "exclamationmark.circle.fill"
        }
    }

    /// Icon tint color name (NSColor).
    var colorName: String {
        switch self {
        case .stopped: return "gray"
        case .running: return "green"
        case .taskActive: return "purple"
        case .disconnected: return "orange"
        case .error: return "red"
        }
    }
}
