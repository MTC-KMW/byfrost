import AppKit

/// Application delegate - owns the StatusBarController and DaemonManager.
///
/// The daemon keeps running when the app quits (via launchd).
/// On relaunch, DaemonManager recovers the running PID.
final class AppDelegate: NSObject, NSApplicationDelegate {

    private var statusBarController: StatusBarController?
    private var daemonManager: DaemonManager?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let manager = DaemonManager()
        daemonManager = manager
        statusBarController = StatusBarController(daemonManager: manager)
    }
}
