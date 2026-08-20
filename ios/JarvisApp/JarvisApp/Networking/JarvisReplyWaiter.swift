import Foundation

/// Bridges the fire-and-forget /api/command POST to something Siri can actually speak.
/// /api/command only ever returns {"accepted": true} — Jarvis's real answer streams
/// back later over /ws as a `{"type":"log", "speaker":"JARVIS", ...}` message (see
/// dashboard/static/app.html's ws.onmessage). For a Siri App Intent there's no chat
/// screen sitting there to display that message, so this opens a short-lived socket,
/// grabs the next JARVIS-authored log line, and hands it back synchronously.
enum JarvisReplyWaiter {
    static func sendAndAwaitReply(_ text: String, timeout: TimeInterval = 12) async throws -> String {
        try await JarvisAPI.sendCommand(text)

        let socket = JarvisWebSocket()
        defer { socket.disconnect() }

        return try await withCheckedThrowingContinuation { continuation in
            var didResume = false
            let resumeOnce: (Result<String, Error>) -> Void = { result in
                guard !didResume else { return }
                didResume = true
                switch result {
                case .success(let reply): continuation.resume(returning: reply)
                case .failure(let error): continuation.resume(throwing: error)
                }
            }

            socket.onMessage = { message in
                guard message.type == "log", let replyText = message.text, !replyText.isEmpty else { return }
                let speaker = (message.speaker ?? "").lowercased()
                // The user's own echoed text can also arrive as a "log" message —
                // only resolve on a line that isn't attributed to the user themself.
                if speaker.contains("user") { return }
                resumeOnce(.success(replyText))
            }
            socket.onClose = { reason in
                switch reason {
                case .invalidToken:
                    resumeOnce(.failure(JarvisAPIError.notAuthenticated))
                case .biometricRequired:
                    resumeOnce(.failure(JarvisAPIError.server("Jarvis needs a face scan in the app before Siri commands will work again.")))
                case .other:
                    break // let the timeout below handle a plain disconnect
                }
            }

            do {
                try socket.connect()
            } catch {
                resumeOnce(.failure(error))
                return
            }

            DispatchQueue.main.asyncAfter(deadline: .now() + timeout) {
                resumeOnce(.failure(JarvisAPIError.server("Jarvis didn't reply in time.")))
            }
        }
    }
}
