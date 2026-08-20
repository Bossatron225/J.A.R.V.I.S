import Foundation

/// A message from Jarvis's /ws stream — mirrors the `m.type` switch in app.html's
/// ws.onmessage handler. Only the fields this app actually uses are modeled.
struct JarvisWSMessage {
    let type: String
    let speaker: String?
    let text: String?
    let state: String?
}

enum JarvisWSCloseReason {
    case invalidToken      // close code 4001 — token itself is bad/expired, must re-login
    case biometricRequired // close code 4003 — token's fine, just needs a fresh face scan
    case other
}

/// Wraps the same /ws endpoint app.html connects to. Two use cases share this:
/// - JarvisChatSession keeps one open for the live chat screen.
/// - The App Intent (Intents/AskJarvisIntent.swift) opens one just long enough to see
///   the single reply to a command it just POSTed, then closes it — Siri needs *a*
///   spoken response, and /api/command alone never returns Jarvis's actual answer,
///   only "accepted".
final class JarvisWebSocket: NSObject, URLSessionWebSocketDelegate {
    private var task: URLSessionWebSocketTask?
    private var session: URLSession?
    var onMessage: ((JarvisWSMessage) -> Void)?
    var onAudioChunk: ((Data) -> Void)?
    var onClose: ((JarvisWSCloseReason) -> Void)?

    func connect() throws {
        guard let base = JarvisAPI.baseURL else { throw JarvisAPIError.noServerURL }
        guard let token = KeychainStore.get(.token) else { throw JarvisAPIError.notAuthenticated }

        var components = URLComponents(url: base, resolvingAgainstBaseURL: false)!
        components.scheme = components.scheme == "https" ? "wss" : "ws"
        components.path = "/ws"
        components.queryItems = [URLQueryItem(name: "token", value: token)]
        guard let url = components.url else { throw JarvisAPIError.server("Could not build websocket URL") }

        let session = URLSession(configuration: .default, delegate: self, delegateQueue: nil)
        self.session = session
        let task = session.webSocketTask(with: url)
        self.task = task
        task.resume()
        listen()
    }

    func disconnect() {
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        session = nil
    }

    private func listen() {
        task?.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure:
                return // delegate's didCloseWith handles the close reason
            case .success(let message):
                if case .string(let text) = message, let data = text.data(using: .utf8),
                   let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    let type = obj["type"] as? String ?? ""
                    if type == "ping" {
                        self.task?.send(.string("{\"type\":\"pong\"}")) { _ in }
                    } else {
                        self.onMessage?(JarvisWSMessage(
                            type: type,
                            speaker: obj["speaker"] as? String,
                            text: obj["text"] as? String,
                            state: obj["state"] as? String
                        ))
                    }
                }
                self.listen()
            }
        }
    }

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask, didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        switch closeCode.rawValue {
        case 4001: onClose?(.invalidToken)
        case 4003: onClose?(.biometricRequired)
        default: onClose?(.other)
        }
    }
}
