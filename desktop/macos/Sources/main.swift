import Cocoa
import ServiceManagement

let arguments = CommandLine.arguments
let environment = ProcessInfo.processInfo.environment

func loginItemStatusName() -> String {
    switch SMAppService.mainApp.status {
    case .enabled: return "enabled"
    case .requiresApproval: return "requires-approval"
    case .notRegistered: return "not-registered"
    case .notFound: return "not-found"
    @unknown default: return "unknown"
    }
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

func storeToken(fromFile path: String) -> Never {
    guard let contents = try? String(contentsOfFile: path, encoding: .utf8) else {
        fail("cannot read \(path)")
    }
    let token = contents.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !token.isEmpty else { fail("empty token") }
    do {
        try GiteaTokenKeychain.write(token)
    } catch {
        NSLog("Homelab Console: keychain write failed: %@", error.localizedDescription)
        fail(error.localizedDescription)
    }
    NSLog("Homelab Console: Gitea token stored in Keychain")
    print("stored")
    exit(0)
}

// MARK: - Headless helpers
//
// These run before NSApplication, so they work from a terminal. LaunchServices
// does not always forward argv to a GUI app, but it does inherit the session
// environment, hence the two env forms:
//   launchctl setenv HLC_SET_GITEA_TOKEN_FILE /path/to/token
//   launchctl setenv HLC_CLEAR_GITEA_TOKEN 1

if arguments.contains("--login-item-status") {
    print(loginItemStatusName())
    exit(0)
}
if arguments.contains("--login-item-register") {
    do {
        try SMAppService.mainApp.register()
    } catch {
        fail("register failed: \(error.localizedDescription)")
    }
    print(loginItemStatusName())
    exit(0)
}
if arguments.contains("--login-item-unregister") {
    do {
        try SMAppService.mainApp.unregister()
    } catch {
        fail("unregister failed: \(error.localizedDescription)")
    }
    print(loginItemStatusName())
    exit(0)
}
if arguments.contains("--gitea-token-status") {
    print(GiteaTokenKeychain.read() != nil ? "set" : "unset")
    exit(0)
}
if arguments.contains("--clear-gitea-token") {
    GiteaTokenKeychain.delete()
    print("cleared")
    exit(0)
}
if arguments.contains("--set-gitea-token-stdin") {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    let token = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    guard !token.isEmpty else { fail("empty token") }
    do {
        try GiteaTokenKeychain.write(token)
    } catch {
        fail(error.localizedDescription)
    }
    print("stored")
    exit(0)
}
if let index = arguments.firstIndex(of: "--set-gitea-token-file"), index + 1 < arguments.count {
    storeToken(fromFile: arguments[index + 1])
}
if let path = environment["HLC_SET_GITEA_TOKEN_FILE"], !path.isEmpty {
    storeToken(fromFile: path)
}
if environment["HLC_CLEAR_GITEA_TOKEN"] == "1" {
    GiteaTokenKeychain.delete()
    NSLog("Homelab Console: Gitea token cleared from Keychain")
    exit(0)
}

// Programmatic AppKit app: no storyboard, no Xcode project.
let application = NSApplication.shared
let appDelegate = AppDelegate()
application.delegate = appDelegate
application.setActivationPolicy(.regular)
application.run()
