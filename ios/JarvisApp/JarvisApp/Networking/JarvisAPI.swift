import Foundation

/// REST calls against vps_orchestrator.py — mirrors dashboard/static/app.html's and
/// login.html's fetch() calls exactly, so this talks to the same backend with zero
/// server-side changes needed.
enum JarvisAPIError: Error, LocalizedError {
    case noServerURL
    case notAuthenticated
    case server(String)
    case http(Int)

    var errorDescription: String? {
        switch self {
        case .noServerURL: return "No Jarvis server URL configured yet."
        case .notAuthenticated: return "Not logged in."
        case .server(let message): return message
        case .http(let code): return "Server returned HTTP \(code)."
        }
    }
}

struct JarvisAPI {
    static var baseURL: URL? {
        guard let raw = KeychainStore.get(.serverURL), !raw.isEmpty else { return nil }
        return URL(string: raw)
    }

    private static func request(_ path: String, method: String = "GET", body: [String: Any]? = nil, authorized: Bool = true) throws -> URLRequest {
        guard let base = baseURL else { throw JarvisAPIError.noServerURL }
        var req = URLRequest(url: base.appendingPathComponent(path))
        req.httpMethod = method
        if authorized {
            guard let token = KeychainStore.get(.token) else { throw JarvisAPIError.notAuthenticated }
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        return req
    }

    private static func send(_ req: URLRequest) async throws -> [String: Any] {
        let (data, response) = try await URLSession.shared.data(for: req)
        let httpResponse = response as? HTTPURLResponse
        let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        if let code = httpResponse?.statusCode, code >= 400 {
            let message = (json["error"] as? String) ?? "HTTP \(code)"
            throw JarvisAPIError.server(message)
        }
        return json
    }

    // MARK: - Login (mirrors login.html's doLogin())

    static func login(key: String, remotePin: String = "") async throws -> String {
        let req = try request("/login", method: "POST", body: ["key": key, "remote_pin": remotePin], authorized: false)
        let json = try await send(req)
        guard json["ok"] as? Bool == true, let token = json["token"] as? String else {
            throw JarvisAPIError.server((json["error"] as? String) ?? "Invalid or expired key")
        }
        KeychainStore.set(key, for: .sessionKey)
        KeychainStore.set(token, for: .token)
        return token
    }

    // MARK: - Biometric lock (mirrors app.html's _bioInit/_bioShowVerify/_bioFinishEnrollment)

    struct BiometricStatus {
        let enrolled: Bool
        let verified: Bool
    }

    static func biometricStatus() async throws -> BiometricStatus {
        let req = try request("/api/biometric/status")
        let json = try await send(req)
        return BiometricStatus(
            enrolled: json["enrolled"] as? Bool ?? false,
            verified: json["verified"] as? Bool ?? false
        )
    }

    static func biometricVerify(frames: [String]) async throws {
        let req = try request("/api/biometric/verify", method: "POST", body: ["frames": frames])
        let json = try await send(req)
        guard json["ok"] as? Bool == true else {
            throw JarvisAPIError.server((json["error"] as? String) ?? "Face did not match.")
        }
    }

    static func biometricEnroll(frames: [String]) async throws {
        let req = try request("/api/biometric/enroll", method: "POST", body: ["frames": frames])
        let json = try await send(req)
        guard json["ok"] as? Bool == true else {
            throw JarvisAPIError.server((json["error"] as? String) ?? "Enrollment failed.")
        }
    }

    // MARK: - Commands (mirrors app.html's doSend()/_sendCommandText())

    /// Fire-and-forget, same as the web dashboard: the actual reply comes back over
    /// the websocket, not in this HTTP response — see JarvisWebSocket.awaitNextReply().
    static func sendCommand(_ text: String) async throws {
        let req = try request("/api/command", method: "POST", body: ["text": text])
        let json = try await send(req)
        guard json["ok"] as? Bool == true else {
            throw JarvisAPIError.server((json["error"] as? String) ?? "Command was not accepted.")
        }
    }

    static var isLoggedIn: Bool {
        KeychainStore.get(.token) != nil
    }

    static func logout() {
        KeychainStore.clearAll()
    }
}
