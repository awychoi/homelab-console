import Cocoa
import ServiceManagement
import UniformTypeIdentifiers
import WebKit

/// A thin native shell: it owns a WKWebView and the Python backend's lifetime.
/// Everything the console does still lives in `homelab_console`.
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    private var window: NSWindow?
    private var webView: WKWebView?
    private var overlay: NSStackView?
    private var statusLabel: NSTextField?
    private var spinner: NSProgressIndicator?
    private var backend: Process?
    private var logHandle: FileHandle?
    private var logPath = "(no log)"
    private var consoleURL: URL?
    private var shuttingDown = false
    private lazy var preferences = PreferencesController()

    // MARK: - Lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildWindow()
        preferences.onTokenSaved = { [weak self] token in
            self?.restartBackend(token: token)
        }
        // No login item: remove one registered by an older build.
        // Unconditional: a disabled/stale BTM record can report notRegistered
        // while still appearing in System Settings.
        try? SMAppService.mainApp.unregister()
        startBackend { GiteaTokenKeychain.read() }
        // First launch has no workspace pointer. Ask once so a new user never
        // has to hand-write workspace.txt from a terminal.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in
            self?.promptForWorkspaceIfNeeded()
        }
    }

    /// The console window is the whole app, but closing it should not end the
    /// backend: hide to the Dock instead so a click resumes instantly.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        shuttingDown = true
        return .terminateNow
    }

    /// The red close button hides the app and keeps the window, webview and
    /// backend alive. The Dock icon stays dotted, and clicking it restores the
    /// same page. Quit (⌘Q) is still the way to stop everything.
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        guard sender === window, !shuttingDown else { return true }
        NSApp.hide(nil)
        return false
    }

    /// Fallback for a programmatic close: keep the process in the Dock rather
    /// than terminating it with no window.
    func windowWillClose(_ notification: Notification) {
        guard !shuttingDown,
              let closing = notification.object as? NSWindow,
              closing === window else { return }
        NSApp.hide(nil)
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showConsoleWindow()
        return true
    }

    /// Bring the hidden console back without recreating the window or backend.
    private func showConsoleWindow() {
        NSApp.unhide(nil)
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    func applicationSupportsSecureRestorableState(_ app: NSApplication) -> Bool {
        true
    }

    /// Deep link from the web UI: homelab-console://settings opens this window.
    func application(_ application: NSApplication, open urls: [URL]) {
        for url in urls where url.scheme == "homelab-console" {
            NSLog("Homelab Console: opened %@", url.absoluteString)
            if url.host == "settings" {
                showPreferences()
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        shuttingDown = true
        if let backend, backend.isRunning {
            backend.terminate()
            var waited = 0
            while backend.isRunning && waited < 40 {
                usleep(50_000)
                waited += 1
            }
            if backend.isRunning {
                kill(backend.processIdentifier, SIGKILL)
            }
        }
        try? logHandle?.close()
    }

    // MARK: - Window and web view

    private func buildWindow() {
        let frame = NSRect(x: 0, y: 0, width: 1240, height: 840)
        let window = NSWindow(
            contentRect: frame,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Homelab Console"
        window.minSize = NSSize(width: 760, height: 520)
        window.center()
        window.setFrameAutosaveName("HomelabConsoleMain")
        window.isReleasedWhenClosed = false

        let container = NSView(frame: frame)
        window.contentView = container

        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        let contentController = WKUserContentController()
        contentController.add(self, name: "hlcPick")
        contentController.add(self, name: "hlcTerminal")
        configuration.userContentController = contentController
        let webView = WKWebView(frame: frame, configuration: configuration)
        webView.translatesAutoresizingMaskIntoConstraints = false
        webView.navigationDelegate = self
        webView.uiDelegate = self
        container.addSubview(webView)

        let spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.isIndeterminate = true
        spinner.controlSize = .regular
        spinner.startAnimation(nil)
        spinner.translatesAutoresizingMaskIntoConstraints = false

        let label = NSTextField(labelWithString: "Starting the console…")
        label.font = NSFont.systemFont(ofSize: 13)
        label.textColor = .secondaryLabelColor
        label.alignment = .center
        label.maximumNumberOfLines = 0
        label.lineBreakMode = .byWordWrapping
        label.translatesAutoresizingMaskIntoConstraints = false

        let stack = NSStackView(views: [spinner, label])
        stack.orientation = .vertical
        stack.spacing = 12
        stack.alignment = .centerX
        stack.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(stack)

        NSLayoutConstraint.activate([
            webView.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            webView.topAnchor.constraint(equalTo: container.topAnchor),
            webView.bottomAnchor.constraint(equalTo: container.bottomAnchor),
            stack.centerXAnchor.constraint(equalTo: container.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: container.centerYAnchor),
            label.widthAnchor.constraint(lessThanOrEqualToConstant: 520),
        ])

        window.delegate = self
        self.window = window
        self.webView = webView
        self.overlay = stack
        self.statusLabel = label
        self.spinner = spinner

        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: - Backend lifecycle

    /// `tokenProvider` runs off the main thread: a Keychain read can block on a
    /// system permission prompt, and the window must be visible before that.
    private func startBackend(tokenProvider: @escaping () -> String?) {
        guard let executable = backendExecutable() else {
            fail("The console backend is missing from this app bundle.")
            return
        }
        guard let port = Self.pickFreePort() else {
            fail("Could not reserve a local port for the console.")
            return
        }

        let log = openLog()
        logHandle = log.handle
        logPath = log.path

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let token = tokenProvider()
            DispatchQueue.main.async {
                guard let self else { return }
                self.launchBackend(executable: executable, port: port, token: token)
            }
        }
    }

    private func launchBackend(executable: URL, port: Int, token: String?) {
        let process = Process()
        process.executableURL = executable
        process.arguments = ["serve", "--host", "127.0.0.1", "--port", String(port), "--no-browser"]
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUNBUFFERED"] = "1"
        // Marks this run as app-managed so the web UI defers token storage to
        // the shell instead of offering a field that would be overridden.
        environment["HLC_APP"] = "1"
        if let token, !token.isEmpty {
            environment["HLC_GITEA_TOKEN"] = token
        }
        // If this app dies without applicationWillTerminate (crash, Force
        // Quit), the backend notices it was reparented and exits by itself.
        environment["HLC_PARENT_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        process.environment = environment
        if let handle = logHandle {
            process.standardOutput = handle
            process.standardError = handle
        }
        process.terminationHandler = { [weak self] finished in
            DispatchQueue.main.async {
                guard let self, !self.shuttingDown else { return }
                self.fail("The console backend stopped (exit \(finished.terminationStatus)). See \(self.logPath).")
            }
        }

        do {
            try process.run()
        } catch {
            fail("Could not start the console backend: \(error.localizedDescription)")
            return
        }

        backend = process
        consoleURL = URL(string: "http://127.0.0.1:\(port)/")
        waitForHealth(port: port, attemptsLeft: 120)
    }

    private func backendExecutable() -> URL? {
        if let override = ProcessInfo.processInfo.environment["HLC_BACKEND"], !override.isEmpty {
            return URL(fileURLWithPath: override)
        }
        guard let resources = Bundle.main.resourceURL else { return nil }
        let candidate = resources.appendingPathComponent("backend/HomelabConsole")
        return FileManager.default.isExecutableFile(atPath: candidate.path) ? candidate : nil
    }

    private func waitForHealth(port: Int, attemptsLeft: Int) {
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/health") else { return }
        var request = URLRequest(url: url)
        request.timeoutInterval = 2
        URLSession.shared.dataTask(with: request) { [weak self] _, response, _ in
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            DispatchQueue.main.async {
                guard let self else { return }
                if status == 200 {
                    self.loadConsole()
                } else if attemptsLeft > 0 {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
                        self.waitForHealth(port: port, attemptsLeft: attemptsLeft - 1)
                    }
                } else {
                    self.fail("The console did not answer on port \(port). See \(self.logPath).")
                }
            }
        }.resume()
    }

    private func loadConsole() {
        guard let webView, let consoleURL else { return }
        webView.load(URLRequest(url: consoleURL))
    }

    private func fail(_ message: String) {
        NSLog("Homelab Console: %@", message)
        spinner?.stopAnimation(nil)
        spinner?.isHidden = true
        statusLabel?.stringValue = message
        statusLabel?.textColor = .systemRed
        window?.title = "Homelab Console — backend error"
    }

    private func openLog() -> (handle: FileHandle?, path: String) {
        let manager = FileManager.default
        guard let library = manager.urls(for: .libraryDirectory, in: .userDomainMask).first else {
            return (nil, "(no log)")
        }
        let directory = library.appendingPathComponent("Logs/Homelab Console", isDirectory: true)
        try? manager.createDirectory(at: directory, withIntermediateDirectories: true)
        let file = directory.appendingPathComponent("sidecar.log")
        if !manager.fileExists(atPath: file.path) {
            manager.createFile(atPath: file.path, contents: nil, attributes: [.posixPermissions: 0o600])
        }
        try? manager.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        let handle = try? FileHandle(forWritingTo: file)
        _ = handle?.seekToEndOfFile()
        return (handle, file.path)
    }

    /// Ask the kernel for a free loopback port, then hand it to the backend.
    /// The close-then-bind window is microseconds and only affects this user.
    private static func pickFreePort() -> Int? {
        let descriptor = socket(AF_INET, SOCK_STREAM, 0)
        guard descriptor >= 0 else { return nil }
        defer { close(descriptor) }

        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = 0
        address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))

        let bound = withUnsafePointer(to: &address) { pointer -> Int32 in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { socketAddress in
                Darwin.bind(descriptor, socketAddress, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0 else { return nil }

        var assigned = sockaddr_in()
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let named = withUnsafeMutablePointer(to: &assigned) { pointer -> Int32 in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { socketAddress in
                Darwin.getsockname(descriptor, socketAddress, &length)
            }
        }
        guard named == 0 else { return nil }
        return Int(UInt16(bigEndian: assigned.sin_port))
    }

    // MARK: - Menu

    private func buildMenu() {
        let mainMenu = NSMenu()

        let appItem = NSMenuItem()
        mainMenu.addItem(appItem)
        let appMenu = NSMenu()
        appItem.submenu = appMenu
        appMenu.addItem(
            withTitle: "About Homelab Console",
            action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
            keyEquivalent: ""
        )
        appMenu.addItem(.separator())
        let settingsItem = NSMenuItem(
            title: "Settings…",
            action: #selector(showPreferences),
            keyEquivalent: ","
        )
        settingsItem.target = self
        appMenu.addItem(settingsItem)
        let workspaceItem = NSMenuItem(
            title: "Choose Workspace…",
            action: #selector(chooseWorkspace),
            keyEquivalent: ""
        )
        workspaceItem.target = self
        appMenu.addItem(workspaceItem)
        appMenu.addItem(.separator())
        appMenu.addItem(
            withTitle: "Hide Homelab Console",
            action: #selector(NSApplication.hide(_:)),
            keyEquivalent: "h"
        )
        let hideOthers = appMenu.addItem(
            withTitle: "Hide Others",
            action: #selector(NSApplication.hideOtherApplications(_:)),
            keyEquivalent: "h"
        )
        hideOthers.keyEquivalentModifierMask = [.command, .option]
        appMenu.addItem(.separator())
        appMenu.addItem(
            withTitle: "Quit Homelab Console",
            action: #selector(NSApplication.terminate(_:)),
            keyEquivalent: "q"
        )

        let editItem = NSMenuItem()
        mainMenu.addItem(editItem)
        let editMenu = NSMenu(title: "Edit")
        editItem.submenu = editMenu
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")

        let viewItem = NSMenuItem()
        mainMenu.addItem(viewItem)
        let viewMenu = NSMenu(title: "View")
        viewItem.submenu = viewMenu
        let reload = NSMenuItem(title: "Reload", action: #selector(reloadPage), keyEquivalent: "r")
        reload.target = self
        viewMenu.addItem(reload)
        let open = NSMenuItem(title: "Open in Browser", action: #selector(openInBrowser), keyEquivalent: "o")
        open.keyEquivalentModifierMask = [.command, .shift]
        open.target = self
        viewMenu.addItem(open)

        NSApplication.shared.mainMenu = mainMenu
    }

    @objc private func reloadPage() {
        webView?.reload()
    }

    @objc private func showPreferences() {
        NSApp.unhide(nil)
        preferences.showWindow(nil)
        preferences.window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: - Native pickers

    /// Folder/file dialogs for the web UI. `NSOpenPanel` is the only way to get
    /// a real path from a sandboxed WKWebView; the JS side wraps this in a
    /// promise and falls back to a typed path in a plain browser.
    private func isConsoleURL(_ url: URL?) -> Bool {
        guard let url, let consoleURL else { return false }
        return url.scheme == consoleURL.scheme
            && url.host == consoleURL.host
            && url.port == consoleURL.port
    }

    private func openExternal(_ url: URL) {
        let scheme = (url.scheme ?? "").lowercased()
        guard ["http", "https", "mailto", "homelab-console"].contains(scheme) else { return }
        NSWorkspace.shared.open(url)
    }

    private func isTrustedMessage(_ message: WKScriptMessage) -> Bool {
        guard message.frameInfo.isMainFrame else { return false }
        let origin = message.frameInfo.securityOrigin
        guard origin.protocol == "http" || origin.protocol == "https" else { return false }
        return isConsoleURL(message.webView?.url)
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard isTrustedMessage(message) else { return }
        if message.name == "hlcTerminal" {
            guard let body = message.body as? [String: Any],
                  let command = body["command"] as? String,
                  command.hasPrefix("ssh "),
                  command.count <= 8_000,
                  !command.contains("\n"),
                  !command.contains("\r"),
                  !command.contains("\t") else { return }
            openTerminalWindow(command)
            return
        }
        guard message.name == "hlcPick",
              let body = message.body as? [String: Any],
              let requestID = body["id"] as? String else { return }
        let mode = (body["mode"] as? String) ?? "open"
        let directories = (body["directories"] as? Bool) ?? false
        let multiple = (body["multiple"] as? Bool) ?? false
        let extensions = (body["extensions"] as? [String]) ?? []
        let suggestedName = (body["filename"] as? String) ?? ""

        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            if mode == "save" {
                let panel = NSSavePanel()
                panel.canCreateDirectories = true
                if !suggestedName.isEmpty {
                    panel.nameFieldStringValue = suggestedName
                }
                if !extensions.isEmpty {
                    panel.allowedContentTypes = extensions.compactMap { UTType(filenameExtension: $0) }
                }
                let response = panel.runModal()
                let path = response == .OK ? panel.url?.path : nil
                self.replyPick(id: requestID, paths: path.map { [$0] } ?? [])
                return
            }

            let panel = NSOpenPanel()
            panel.canCreateDirectories = true
            panel.canChooseDirectories = directories
            panel.canChooseFiles = !directories
            panel.allowsMultipleSelection = multiple
            if !extensions.isEmpty {
                panel.allowedContentTypes = extensions.compactMap { UTType(filenameExtension: $0) }
            }
            let response = panel.runModal()
            self.replyPick(id: requestID, paths: response == .OK ? panel.urls.map(\.path) : [])
        }
    }

    private func openTerminalWindow(_ command: String) {
        // Terminal runs a temporary .command file, so no Automation
        // permission is involved. The script deletes itself before running.
        let script = "#!/bin/sh\nrm -f -- \"$0\"\n\(command)\n"
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("hlc-terminal-\(UUID().uuidString).command")
        do {
            try script.write(to: url, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: url.path)
        } catch {
            NSLog("Homelab Console: could not write Terminal command: %@", error.localizedDescription)
            webView?.evaluateJavaScript("window.hlcTerminalFailed?.({reason: \"could not write Terminal command\"});")
            return
        }
        let terminal = URL(fileURLWithPath: "/System/Applications/Utilities/Terminal.app")
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.activates = true
        NSWorkspace.shared.open([url], withApplicationAt: terminal, configuration: configuration) { [weak self] _, error in
            guard error != nil else { return }
            DispatchQueue.main.async {
                self?.webView?.evaluateJavaScript("window.hlcTerminalFailed?.({reason: \"Terminal could not open\"});")
            }
        }
    }

    private func replyPick(id: String, paths: [String]) {
        guard let webView else { return }
        let payload: [String: Any] = ["id": id, "paths": paths]
        guard let data = try? JSONSerialization.data(withJSONObject: payload),
              let json = String(data: data, encoding: .utf8) else { return }
        webView.evaluateJavaScript("window.hlcPickResult(\(json));")
    }

    // MARK: - Workspace

    /// The backend reads this pointer when no `HLC_REPO`/`HLC_WORKSPACE` is
    /// set, so the shell only has to write it and restart the child process.
    private func workspacePointerURL() -> URL? {
        guard let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else {
            return nil
        }
        let directory = support.appendingPathComponent("Homelab Console", isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory.appendingPathComponent("workspace.txt")
    }

    private func writeWorkspacePointer(_ url: URL) {
        guard let pointer = workspacePointerURL() else { return }
        try? (url.path + "\n").write(to: pointer, atomically: true, encoding: .utf8)
    }

    @objc private func chooseWorkspace() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = true
        panel.prompt = "Use Workspace"
        panel.message = "Choose a folder for stacks, hosts.json and .hlc state."
        guard panel.runModal() == .OK, let url = panel.url else { return }

        // An empty folder is fine (the backend seeds stacks/, scripts/ and a
        // .gitignore). A populated folder must already look like a workspace,
        // so picking the app checkout by mistake cannot litter it.
        let contents = (try? FileManager.default.contentsOfDirectory(atPath: url.path)) ?? []
        let looksLikeWorkspace = contents.isEmpty || contents.contains("stacks") || contents.contains("scripts")
        guard looksLikeWorkspace else {
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = "That folder is not an empty workspace"
            alert.informativeText =
                "Choose an empty folder, or one that already contains stacks/ and scripts/. "
                + "Keep the app checkout and your fleet data in separate folders."
            alert.addButton(withTitle: "OK")
            alert.runModal()
            return
        }

        writeWorkspacePointer(url)
        restartBackend(token: GiteaTokenKeychain.read())
    }

    private func promptForWorkspaceIfNeeded() {
        let environment = ProcessInfo.processInfo.environment
        if environment["HLC_REPO"] != nil || environment["HLC_WORKSPACE"] != nil {
            return
        }
        guard let pointer = workspacePointerURL(), !FileManager.default.fileExists(atPath: pointer.path) else {
            return
        }
        guard !UserDefaults.standard.bool(forKey: "HLCWorkspacePromptShown") else { return }
        UserDefaults.standard.set(true, forKey: "HLCWorkspacePromptShown")

        let alert = NSAlert()
        alert.messageText = "Choose a workspace"
        alert.informativeText =
            "Hosts, stacks and local state live in a workspace folder. Pick one now, "
            + "or start with the default empty workspace and change it later from the app menu."
        alert.addButton(withTitle: "Choose Folder…")
        alert.addButton(withTitle: "Use Default")
        if alert.runModal() == .alertFirstButtonReturn {
            chooseWorkspace()
        }
    }

    /// Applied after the Keychain changes: the token reaches the backend
    /// through its environment, and a child's environment cannot change.
    private func restartBackend(token: String?) {
        showConsoleWindow()
        if let backend, backend.isRunning {
            shuttingDown = true
            backend.terminate()
            var waited = 0
            while backend.isRunning && waited < 40 {
                usleep(50_000)
                waited += 1
            }
            shuttingDown = false
        }
        backend = nil
        consoleURL = nil
        overlay?.isHidden = false
        spinner?.isHidden = false
        spinner?.startAnimation(nil)
        statusLabel?.stringValue = "Restarting the console…"
        statusLabel?.textColor = .secondaryLabelColor
        window?.title = "Homelab Console"
        webView?.loadHTMLString("", baseURL: nil)
        startBackend { token }
    }

    @objc private func openInBrowser() {
        guard let consoleURL else { return }
        NSWorkspace.shared.open(consoleURL)
    }

    // MARK: - WKNavigationDelegate

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        spinner?.stopAnimation(nil)
        overlay?.isHidden = true
        window?.title = "Homelab Console"
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        fail("The console page failed to load: \(error.localizedDescription)")
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation!,
        withError error: Error
    ) {
        fail("The console page failed to load: \(error.localizedDescription)")
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.allow)
            return
        }
        if url.scheme == "about" || isConsoleURL(url) {
            decisionHandler(.allow)
            return
        }
        // The webview only ever hosts the console.  External links and
        // programmatic navigations (including a meta refresh injected by host
        // data) must not inherit the native message handlers.
        openExternal(url)
        decisionHandler(.cancel)
    }

    // MARK: - WKUIDelegate

    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        if let url = navigationAction.request.url {
            openExternal(url)
        }
        return nil
    }
}
