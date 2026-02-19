import AppKit
import CoreGraphics

/// Byfrost Mac menu bar app entry point.
///
/// Headless detection: if no GUI session is available (SSH, CI, headless
/// Mac Mini), exit silently with code 0.
@main
struct ByfrostApp {
    static func main() {
        // Headless detection via CGSessionCopyCurrentDictionary().
        // Returns nil when no window server session is available.
        guard CGSessionCopyCurrentDictionary() != nil else {
            // No display session - exit silently (not an error)
            exit(0)
        }

        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.run()
    }
}
