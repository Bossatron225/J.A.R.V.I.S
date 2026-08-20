import SwiftUI

private struct ChatMessage: Identifiable {
    let id = UUID()
    let speaker: String
    let text: String
}

/// Native equivalent of app.html's main feed + footer input — same /ws connection,
/// same fire-the-POST-then-wait-for-the-log-message pattern for replies.
struct ChatView: View {
    let onNeedsLogin: () -> Void
    let onNeedsBiometric: () -> Void

    @State private var messages: [ChatMessage] = []
    @State private var draft = ""
    @State private var connected = false
    private let socket = JarvisWebSocket()

    var body: some View {
        VStack(spacing: 0) {
            statusBar

            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 10) {
                        ForEach(messages) { message in
                            messageRow(message)
                        }
                    }
                    .padding()
                }
                .onChange(of: messages.count) { _, _ in
                    if let last = messages.last {
                        withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                    }
                }
            }

            HStack {
                TextField("Send a command to JARVIS…", text: $draft)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { send() }
                Button("Send") { send() }
                    .buttonStyle(.borderedProminent)
                    .disabled(draft.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            .padding()
        }
        .onAppear(perform: connect)
        .onDisappear { socket.disconnect() }
    }

    private var statusBar: some View {
        HStack {
            Circle()
                .fill(connected ? .green : .gray)
                .frame(width: 8, height: 8)
            Text(connected ? "Connected" : "Connecting…")
                .font(.caption)
                .foregroundColor(.secondary)
            Spacer()
        }
        .padding(.horizontal)
        .padding(.top, 8)
    }

    private func messageRow(_ message: ChatMessage) -> some View {
        VStack(alignment: message.speaker == "user" ? .trailing : .leading, spacing: 2) {
            Text(message.speaker.uppercased())
                .font(.caption2.bold())
                .foregroundColor(.secondary)
            Text(message.text)
                .padding(10)
                .background(message.speaker == "user" ? Color.gray.opacity(0.15) : Color.accentColor.opacity(0.15))
                .clipShape(RoundedRectangle(cornerRadius: 12))
        }
        .frame(maxWidth: .infinity, alignment: message.speaker == "user" ? .trailing : .leading)
        .id(message.id)
    }

    private func connect() {
        socket.onMessage = { message in
            DispatchQueue.main.async {
                switch message.type {
                case "log":
                    if let text = message.text {
                        messages.append(ChatMessage(speaker: message.speaker ?? "jarvis", text: text))
                    }
                case "sys":
                    if let text = message.text {
                        messages.append(ChatMessage(speaker: "system", text: text))
                    }
                case "status":
                    connected = true
                default:
                    break
                }
            }
        }
        socket.onClose = { reason in
            DispatchQueue.main.async {
                connected = false
                switch reason {
                case .invalidToken:
                    JarvisAPI.logout()
                    onNeedsLogin()
                case .biometricRequired:
                    onNeedsBiometric()
                case .other:
                    break
                }
            }
        }
        do {
            try socket.connect()
            connected = true
        } catch {
            messages.append(ChatMessage(speaker: "system", text: error.localizedDescription))
        }
    }

    private func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        draft = ""
        messages.append(ChatMessage(speaker: "user", text: text))
        Task {
            do {
                try await JarvisAPI.sendCommand(text)
            } catch {
                await MainActor.run {
                    messages.append(ChatMessage(speaker: "system", text: error.localizedDescription))
                }
            }
        }
    }
}
