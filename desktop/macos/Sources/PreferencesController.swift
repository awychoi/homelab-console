import Cocoa

/// A single-field settings window for the Gitea token.
/// Saving writes to the Keychain and asks the shell to restart the backend so
/// the new value reaches it.
final class PreferencesController: NSWindowController {
    var onTokenSaved: ((String?) -> Void)?

    private let tokenField = NSSecureTextField()
    private let statusLabel = NSTextField(labelWithString: "")

    convenience init() {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 500, height: 320),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = "Homelab Console Settings"
        window.isReleasedWhenClosed = false
        window.center()
        self.init(window: window)
        buildUI()
        refreshStatus()
    }

    private func buildUI() {
        guard let content = window?.contentView else { return }

        let title = NSTextField(labelWithString: "Gitea token")
        title.font = NSFont.systemFont(ofSize: 13, weight: .semibold)

        let explanation = NSTextField(
            wrappingLabelWithString:
                "Used for the startup and on-demand ref checks. It is stored in the macOS Keychain "
                + "(service \(GiteaTokenKeychain.service)) and handed to the console at launch, so it "
                + "is never written to .hlc/config.json."
        )
        explanation.font = NSFont.systemFont(ofSize: 11)
        explanation.textColor = .secondaryLabelColor

        tokenField.placeholderString = "token with repo write access"
        tokenField.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)

        statusLabel.font = NSFont.systemFont(ofSize: 11)
        statusLabel.textColor = .secondaryLabelColor
        statusLabel.maximumNumberOfLines = 0
        statusLabel.lineBreakMode = .byWordWrapping

        let saveButton = NSButton(title: "Save Token", target: self, action: #selector(saveToken))
        saveButton.keyEquivalent = "\r"
        let clearButton = NSButton(title: "Clear Token", target: self, action: #selector(clearToken))
        let closeButton = NSButton(title: "Close", target: self, action: #selector(closeWindow))
        closeButton.keyEquivalent = "\u{1b}"

        let buttons = NSStackView(views: [saveButton, clearButton, closeButton])
        buttons.orientation = .horizontal
        buttons.spacing = 8

        let footer = NSTextField(
            wrappingLabelWithString:
                "Create one in Gitea → Settings → Applications → Generate Token with repository: Read and Write. "
                + "Saving restarts the console backend so the new token takes effect immediately."
        )
        footer.font = NSFont.systemFont(ofSize: 11)
        footer.textColor = .tertiaryLabelColor

        let stack = NSStackView(views: [title, explanation, tokenField, statusLabel, buttons, footer])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)

        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 20),
            tokenField.widthAnchor.constraint(equalTo: stack.widthAnchor),
            explanation.widthAnchor.constraint(equalTo: stack.widthAnchor),
            statusLabel.widthAnchor.constraint(equalTo: stack.widthAnchor),
            footer.widthAnchor.constraint(equalTo: stack.widthAnchor),
        ])
    }

    override func showWindow(_ sender: Any?) {
        refreshStatus()
        super.showWindow(sender)
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func refreshStatus() {
        if GiteaTokenKeychain.read() != nil {
            statusLabel.stringValue = "A token is stored in the Keychain."
        } else {
            statusLabel.stringValue =
                "No token stored. Ref checks fall back to the git credential helper, which is how many setups authenticate."
        }
        window?.makeFirstResponder(tokenField)
    }

    @objc private func saveToken() {
        let token = tokenField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !token.isEmpty else {
            NSSound.beep()
            statusLabel.stringValue = "Enter a token before saving."
            return
        }
        do {
            try GiteaTokenKeychain.write(token)
            tokenField.stringValue = ""
            statusLabel.stringValue = "Token saved. Restarting the console…"
            onTokenSaved?(token)
            window?.close()
        } catch {
            present(error)
        }
    }

    @objc private func clearToken() {
        GiteaTokenKeychain.delete()
        statusLabel.stringValue = "Token cleared. Restarting the console…"
        onTokenSaved?(nil)
        window?.close()
    }

    @objc private func closeWindow() {
        window?.close()
    }

    private func present(_ error: Error) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Could not update the Keychain"
        alert.informativeText = error.localizedDescription
        alert.addButton(withTitle: "OK")
        if let window {
            alert.beginSheetModal(for: window)
        } else {
            alert.runModal()
        }
    }
}
