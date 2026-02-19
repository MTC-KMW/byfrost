import AppKit
import Combine
import SwiftUI

/// Owns the NSStatusItem and NSPopover for the menu bar icon.
///
/// Uses NSStatusItem + NSPopover (NOT MenuBarExtra) for macOS 13+ compat.
@MainActor
final class StatusBarController {

    private var statusItem: NSStatusItem
    private var popover: NSPopover
    private var eventMonitor: Any?
    private var cancellables = Set<AnyCancellable>()
    private var pulseTimer: Timer?

    init(daemonManager: DaemonManager) {
        statusItem = NSStatusBar.system.statusItem(
            withLength: NSStatusItem.squareLength
        )

        popover = NSPopover()
        popover.contentSize = NSSize(width: 320, height: 400)
        popover.behavior = .transient
        popover.contentViewController = NSHostingController(
            rootView: MenuBarView(daemonManager: daemonManager)
        )

        if let button = statusItem.button {
            button.action = #selector(togglePopover(_:))
            button.target = self
            updateIcon(for: .stopped)
        }

        // Observe daemon state changes to update the icon
        daemonManager.$state
            .receive(on: DispatchQueue.main)
            .sink { [weak self] newState in
                self?.updateIcon(for: newState)
            }
            .store(in: &cancellables)
    }

    // MARK: - Icon

    private func updateIcon(for state: DaemonState) {
        guard let button = statusItem.button else { return }

        let image = NSImage(
            systemSymbolName: state.iconName,
            accessibilityDescription: "Byfrost - \(state.displayName)"
        )

        // Apply template rendering + tint via a content tint color
        let config = NSImage.SymbolConfiguration(
            paletteColors: [colorForState(state)]
        )
        button.image = image?.withSymbolConfiguration(config)

        // Pulsing animation for taskActive state
        stopPulse()
        if state == .taskActive {
            startPulse(button: button)
        }
    }

    private func colorForState(_ state: DaemonState) -> NSColor {
        switch state {
        case .stopped: return .systemGray
        case .running: return .systemGreen
        case .taskActive: return .systemPurple
        case .disconnected: return .systemOrange
        case .error: return .systemRed
        }
    }

    private func startPulse(button: NSStatusBarButton) {
        // Simple opacity pulse for task-active state
        pulseTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { _ in
            NSAnimationContext.runAnimationGroup { context in
                context.duration = 0.5
                button.animator().alphaValue = 0.4
            } completionHandler: {
                NSAnimationContext.runAnimationGroup { context in
                    context.duration = 0.5
                    button.animator().alphaValue = 1.0
                }
            }
        }
    }

    private func stopPulse() {
        pulseTimer?.invalidate()
        pulseTimer = nil
        statusItem.button?.alphaValue = 1.0
    }

    // MARK: - Popover

    @objc private func togglePopover(_ sender: Any?) {
        if popover.isShown {
            popover.performClose(sender)
        } else if let button = statusItem.button {
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        }
    }
}
