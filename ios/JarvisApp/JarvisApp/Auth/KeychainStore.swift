import Foundation
import Security

/// Persists the server URL, bearer token, and session key the same way app.html uses
/// sessionStorage — except Keychain survives app relaunches, which the App Intents
/// extension needs: a Siri-triggered command runs without the app UI ever opening, so
/// there's no in-memory session to fall back on. Token TTL is enforced server-side
/// (12h, dashboard/server.py's TOKEN_TTL_SECS) — this store doesn't track expiry itself,
/// it just holds whatever the server last issued.
enum KeychainStore {
    private static let service = "com.jameslumsden.JarvisApp"

    enum Key: String {
        case serverURL
        case token
        case sessionKey
    }

    static func set(_ value: String, for key: Key) {
        let data = Data(value.utf8)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key.rawValue,
        ]
        SecItemDelete(query as CFDictionary)
        var attributes = query
        attributes[kSecValueData as String] = data
        SecItemAdd(attributes as CFDictionary, nil)
    }

    static func get(_ key: Key) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key.rawValue,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess, let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func clear(_ key: Key) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key.rawValue,
        ]
        SecItemDelete(query as CFDictionary)
    }

    static func clearAll() {
        clear(.token)
        clear(.sessionKey)
        // Deliberately not clearing .serverURL — no reason to make the user re-type
        // their VPS URL just because their session/key expired.
    }
}
