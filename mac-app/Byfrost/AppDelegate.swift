import AppKit
import SwiftUI

/// Application delegate - manages first-launch wizard and menu bar.
///
/// On first launch (no auth.json), shows the setup wizard.
/// After setup, transitions to menu bar mode. The daemon keeps running
/// when the app quits (via launchd). On relaunch, DaemonManager
/// recovers the running PID.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {

    private var statusBarController: StatusBarController?
    private var daemonManager: DaemonManager?
    private var wizardWindow: NSWindow?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let manager = DaemonManager()
        daemonManager = manager

        if needsSetup() {
            showWizard(manager: manager)
        } else {
            showMenuBar(manager: manager)
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(
        _ sender: NSApplication
    ) -> Bool {
        // Quit when wizard window closes (no menu bar yet).
        // Once in menu bar mode, wizardWindow is nil so this returns false.
        wizardWindow != nil
    }

    // MARK: - First Launch Detection

    /// Returns true when ~/.byfrost/auth.json doesn't exist.
    private func needsSetup() -> Bool {
        !FileManager.default.fileExists(
            atPath: DaemonConfig.authFile.path
        )
    }

    // MARK: - Wizard

    private func showWizard(manager: DaemonManager) {
        let wizardView = SetupWizardView(
            daemonManager: manager,
            onComplete: { [weak self] in
                self?.wizardWindow?.close()
                self?.wizardWindow = nil
                self?.showMenuBar(manager: manager)
            }
        )

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 480, height: 520),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = "Byfrost Setup"
        window.contentView = NSHostingView(rootView: wizardView)
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        wizardWindow = window
    }

    // MARK: - Menu Bar

    private func showMenuBar(manager: DaemonManager) {
        statusBarController = StatusBarController(daemonManager: manager)
    }
}
