import Foundation
import Security

enum KeychainError: LocalizedError {
    case status(OSStatus)

    var errorDescription: String? {
        switch self {
        case .status(let code):
            let message = SecCopyErrorMessageString(code, nil) as String? ?? "OSStatus \(code)"
            return "Keychain error: \(message)"
        }
    }
}

/// The Gitea token lives here when the console runs inside the app.
/// The backend never reads the Keychain itself; the shell passes the value as
/// `HLC_GITEA_TOKEN`, which already takes precedence over the config file.
enum GiteaTokenKeychain {
    /// Scoped to the bundle, so a redistributed build never reads the
    /// original author's Keychain item. Set HLC_BUNDLE_ID when building.
    static var service: String {
        Bundle.main.bundleIdentifier ?? "dev.homelab-console.app"
    }
    static let account = "gitea-token"

    private static var baseQuery: [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }

    static func read() -> String? {
        var query = baseQuery
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        guard status == errSecSuccess, let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func write(_ token: String) throws {
        let data = Data(token.utf8)
        let update: [String: Any] = [kSecValueData as String: data]
        let status = SecItemUpdate(baseQuery as CFDictionary, update as CFDictionary)
        if status == errSecItemNotFound {
            var add = baseQuery
            add[kSecValueData as String] = data
            add[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
            let added = SecItemAdd(add as CFDictionary, nil)
            if added != errSecSuccess {
                throw KeychainError.status(added)
            }
        } else if status != errSecSuccess {
            throw KeychainError.status(status)
        }
    }

    static func delete() {
        SecItemDelete(baseQuery as CFDictionary)
    }
}
