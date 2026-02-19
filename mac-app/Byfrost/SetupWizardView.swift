import AppKit
import SwiftUI

// MARK: - Wizard Step

enum WizardStep: Int, CaseIterable {
    case welcome = 0
    case signIn = 1
    case prerequisites = 2
    case projectConfig = 3
    case startDaemon = 4
    case done = 5

    var title: String {
        switch self {
        case .welcome: return "Welcome"
        case .signIn: return "Sign In"
        case .prerequisites: return "Prerequisites"
        case .projectConfig: return "Project"
        case .startDaemon: return "Daemon"
        case .done: return "Done"
        }
    }
}

// MARK: - Wizard State

/// Shared state for the 6-step setup wizard.
@MainActor
final class WizardState: ObservableObject {
    @Published var currentStep: WizardStep = .welcome
    @Published var isLoading = false
    @Published var errorMessage: String?

    // Sign-in
    @Published var userCode: String?
    @Published var verificationURI: String?
    @Published var loginComplete = false
    @Published var username: String?

    // Prerequisites
    @Published var claudeFound = false
    @Published var tmuxFound = false
    @Published var prereqsChecked = false

    // Project config
    @Published var projectPath: String = ""
    @Published var port: Int = DaemonConfig.defaultPort

    // Daemon
    @Published var daemonStarted = false

    private var loginProcess: Process?
    private let cliRunner = CLIRunner()

    /// Whether the current step allows proceeding.
    var canProceed: Bool {
        switch currentStep {
        case .welcome:
            return true
        case .signIn:
            return loginComplete
        case .prerequisites:
            return claudeFound && tmuxFound
        case .projectConfig:
            return !projectPath.isEmpty
        case .startDaemon:
            return daemonStarted
        case .done:
            return true
        }
    }

    // MARK: - Sign In

    func startLogin() {
        guard !isLoading else { return }
        isLoading = true
        errorMessage = nil
        userCode = nil
        verificationURI = nil

        Task {
            do {
                let (process, deviceCode) = try await cliRunner.startLogin()
                loginProcess = process
                userCode = deviceCode.userCode
                verificationURI = deviceCode.verificationURI
                isLoading = false

                // Wait for login to complete on background thread
                await waitForLogin(process)
            } catch {
                isLoading = false
                errorMessage = error.localizedDescription
            }
        }
    }

    private func waitForLogin(_ process: Process) async {
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            DispatchQueue.global(qos: .userInitiated).async {
                process.waitUntilExit()
                continuation.resume()
            }
        }

        if process.terminationStatus == 0 {
            loginComplete = true
            // Try to read username from auth.json
            if let data = try? Data(contentsOf: DaemonConfig.authFile),
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let user = json["github_username"] as? String {
                username = user
            }
        } else {
            errorMessage = "Login failed. Please try again."
        }
    }

    func cancelLogin() {
        if let process = loginProcess, process.isRunning {
            process.terminate()
        }
        loginProcess = nil
        isLoading = false
    }

    func openVerificationURL() {
        guard let urlString = verificationURI,
              let url = URL(string: urlString) else { return }
        NSWorkspace.shared.open(url)
    }

    func copyCode() {
        guard let code = userCode else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(code, forType: .string)
    }

    // MARK: - Prerequisites

    func checkPrerequisites() {
        let result = cliRunner.checkPrerequisites()
        claudeFound = result.claude
        tmuxFound = result.tmux
        prereqsChecked = true
    }

    // MARK: - Project Config

    func selectProjectDirectory() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.message = "Select your project directory"
        panel.prompt = "Select"

        if panel.runModal() == .OK, let url = panel.url {
            projectPath = url.path
        }
    }

    // MARK: - Start Daemon

    func startDaemon(manager: DaemonManager) {
        guard !isLoading else { return }
        isLoading = true
        errorMessage = nil

        // Save config
        var config = DaemonConfig(projectPath: projectPath, port: port)
        do {
            try config.save()
            manager.config = config
        } catch {
            errorMessage = "Failed to save config: \(error.localizedDescription)"
            isLoading = false
            return
        }

        // Install plist and start
        manager.installPlist()
        manager.start()

        // Wait for PID to appear (up to 5s)
        Task {
            for _ in 0..<10 {
                try? await Task.sleep(nanoseconds: 500_000_000)
                if manager.pid != nil {
                    daemonStarted = true
                    isLoading = false
                    return
                }
            }
            isLoading = false
            errorMessage = "Daemon did not start. Check logs at ~/.byfrost/logs/"
        }
    }

    // MARK: - Navigation

    func advance() {
        guard canProceed else { return }
        if let next = WizardStep(rawValue: currentStep.rawValue + 1) {
            currentStep = next
        }
    }

    func goBack() {
        if let prev = WizardStep(rawValue: currentStep.rawValue - 1) {
            if currentStep == .signIn {
                cancelLogin()
            }
            currentStep = prev
        }
    }
}

// MARK: - Setup Wizard View

/// First-launch setup wizard. Shown when ~/.byfrost/auth.json is missing.
struct SetupWizardView: View {
    @StateObject private var wizard = WizardState()
    @ObservedObject var daemonManager: DaemonManager
    var onComplete: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            // Step indicator
            stepIndicator
                .padding(.vertical, 16)

            Divider()

            // Current step content
            Group {
                switch wizard.currentStep {
                case .welcome: welcomeStep
                case .signIn: signInStep
                case .prerequisites: prerequisitesStep
                case .projectConfig: projectConfigStep
                case .startDaemon: startDaemonStep
                case .done: doneStep
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .padding(24)

            Divider()

            // Error message
            if let error = wizard.errorMessage {
                Text(error)
                    .foregroundColor(.red)
                    .font(.caption)
                    .padding(.horizontal, 24)
                    .padding(.top, 8)
            }

            // Navigation buttons
            navigationButtons
                .padding(16)
        }
        .frame(width: 480, height: 520)
    }

    // MARK: - Step Indicator

    private var stepIndicator: some View {
        HStack(spacing: 8) {
            ForEach(WizardStep.allCases, id: \.rawValue) { step in
                Circle()
                    .fill(stepColor(step))
                    .frame(width: 8, height: 8)
            }
        }
    }

    private func stepColor(_ step: WizardStep) -> Color {
        if step.rawValue < wizard.currentStep.rawValue {
            return .green
        } else if step == wizard.currentStep {
            return .accentColor
        } else {
            return .gray.opacity(0.3)
        }
    }

    // MARK: - Welcome

    private var welcomeStep: some View {
        VStack(spacing: 20) {
            Spacer()

            Image(systemName: "bolt.shield.fill")
                .font(.system(size: 64))
                .foregroundColor(.accentColor)

            Text("Welcome to Byfrost")
                .font(.title.bold())

            Text("Secure bridge for remote Claude Code execution.\nLet's get your Mac set up as a worker.")
                .multilineTextAlignment(.center)
                .foregroundColor(.secondary)

            Spacer()
        }
    }

    // MARK: - Sign In

    private var signInStep: some View {
        VStack(spacing: 16) {
            Text("Sign in with GitHub")
                .font(.title2.bold())

            if wizard.loginComplete {
                // Success state
                Image(systemName: "checkmark.circle.fill")
                    .font(.system(size: 48))
                    .foregroundColor(.green)

                if let user = wizard.username {
                    Text("Signed in as \(user)")
                        .font(.headline)
                }
            } else if let code = wizard.userCode {
                // Device code displayed
                VStack(spacing: 12) {
                    Text("Open this URL and enter the code:")
                        .foregroundColor(.secondary)

                    Text(code)
                        .font(.system(size: 32, weight: .bold, design: .monospaced))
                        .padding(12)
                        .background(Color.gray.opacity(0.1))
                        .cornerRadius(8)

                    HStack(spacing: 12) {
                        Button("Copy Code") {
                            wizard.copyCode()
                        }
                        Button("Open Browser") {
                            wizard.openVerificationURL()
                        }
                        .buttonStyle(.borderedProminent)
                    }

                    HStack(spacing: 8) {
                        ProgressView()
                            .controlSize(.small)
                        Text("Waiting for authorization...")
                            .foregroundColor(.secondary)
                            .font(.caption)
                    }
                    .padding(.top, 8)
                }
            } else if wizard.isLoading {
                ProgressView("Connecting to server...")
            } else {
                // Initial state
                Text("Sign in with your GitHub account to register this Mac as a Byfrost worker.")
                    .multilineTextAlignment(.center)
                    .foregroundColor(.secondary)

                Button("Sign In") {
                    wizard.startLogin()
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
            }
        }
        .onAppear {
            // Auto-start login when step appears
            if !wizard.loginComplete && wizard.userCode == nil && !wizard.isLoading {
                wizard.startLogin()
            }
        }
    }

    // MARK: - Prerequisites

    private var prerequisitesStep: some View {
        VStack(spacing: 16) {
            Text("Prerequisites")
                .font(.title2.bold())

            Text("Byfrost needs these tools installed:")
                .foregroundColor(.secondary)

            VStack(alignment: .leading, spacing: 12) {
                prereqRow(
                    name: "Claude Code",
                    found: wizard.claudeFound,
                    installHint: "npm install -g @anthropic-ai/claude-code"
                )
                prereqRow(
                    name: "tmux",
                    found: wizard.tmuxFound,
                    installHint: "brew install tmux"
                )
            }
            .padding()
            .background(Color.gray.opacity(0.05))
            .cornerRadius(8)

            Button("Re-check") {
                wizard.checkPrerequisites()
            }
            .disabled(wizard.claudeFound && wizard.tmuxFound)
        }
        .onAppear {
            if !wizard.prereqsChecked {
                wizard.checkPrerequisites()
            }
        }
    }

    private func prereqRow(name: String, found: Bool, installHint: String) -> some View {
        HStack {
            Image(systemName: found ? "checkmark.circle.fill" : "xmark.circle.fill")
                .foregroundColor(found ? .green : .red)
            VStack(alignment: .leading) {
                Text(name)
                    .font(.headline)
                if !found {
                    Text(installHint)
                        .font(.system(.caption, design: .monospaced))
                        .foregroundColor(.secondary)
                        .textSelection(.enabled)
                }
            }
            Spacer()
        }
    }

    // MARK: - Project Config

    private var projectConfigStep: some View {
        VStack(spacing: 16) {
            Text("Project Directory")
                .font(.title2.bold())

            Text("Select the directory where your project will live on this Mac.")
                .multilineTextAlignment(.center)
                .foregroundColor(.secondary)

            HStack {
                TextField("Project path", text: $wizard.projectPath)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(.body, design: .monospaced))

                Button("Browse...") {
                    wizard.selectProjectDirectory()
                }
            }

            HStack {
                Text("Port:")
                    .foregroundColor(.secondary)
                TextField("Port", value: $wizard.port, format: .number)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 80)
                Text("(default: 9784)")
                    .foregroundColor(.secondary)
                    .font(.caption)
                Spacer()
            }
        }
    }

    // MARK: - Start Daemon

    private var startDaemonStep: some View {
        VStack(spacing: 16) {
            Text("Start Daemon")
                .font(.title2.bold())

            if wizard.daemonStarted {
                Image(systemName: "checkmark.circle.fill")
                    .font(.system(size: 48))
                    .foregroundColor(.green)
                Text("Daemon is running")
                    .font(.headline)
                if let pid = daemonManager.pid {
                    Text("PID: \(pid)")
                        .font(.system(.caption, design: .monospaced))
                        .foregroundColor(.secondary)
                }
            } else if wizard.isLoading {
                ProgressView("Starting daemon...")
            } else {
                Text("Install the daemon service and start it.\nIt will run automatically on login.")
                    .multilineTextAlignment(.center)
                    .foregroundColor(.secondary)

                Button("Install & Start") {
                    wizard.startDaemon(manager: daemonManager)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
            }
        }
    }

    // MARK: - Done

    private var doneStep: some View {
        VStack(spacing: 20) {
            Spacer()

            Image(systemName: "checkmark.seal.fill")
                .font(.system(size: 64))
                .foregroundColor(.green)

            Text("Byfrost is ready!")
                .font(.title.bold())

            Text("Your Mac is set up as a worker.\nWaiting for a controller to connect.")
                .multilineTextAlignment(.center)
                .foregroundColor(.secondary)

            Text("On your controller machine, run:")
                .font(.caption)
                .foregroundColor(.secondary)

            Text("byfrost connect")
                .font(.system(.body, design: .monospaced))
                .padding(8)
                .background(Color.gray.opacity(0.1))
                .cornerRadius(4)

            Spacer()
        }
    }

    // MARK: - Navigation

    private var navigationButtons: some View {
        HStack {
            if wizard.currentStep != .welcome && wizard.currentStep != .done {
                Button("Back") {
                    wizard.goBack()
                }
            }

            Spacer()

            if wizard.currentStep == .done {
                Button("Done") {
                    onComplete()
                }
                .buttonStyle(.borderedProminent)
            } else if wizard.currentStep != .welcome {
                Button("Next") {
                    wizard.advance()
                }
                .buttonStyle(.borderedProminent)
                .disabled(!wizard.canProceed)
            } else {
                Button("Get Started") {
                    wizard.advance()
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
            }
        }
    }
}
