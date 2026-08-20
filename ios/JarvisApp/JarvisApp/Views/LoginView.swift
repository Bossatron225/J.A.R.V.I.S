import SwiftUI

/// Native equivalent of login.html — same 6-character key + optional PIN flow, same
/// /login endpoint.
struct LoginView: View {
    let onLoggedIn: () -> Void

    @State private var serverURL: String = KeychainStore.get(.serverURL) ?? ""
    @State private var key: String = ""
    @State private var pin: String = ""
    @State private var errorText: String?
    @State private var isLoading = false

    var body: some View {
        VStack(spacing: 18) {
            Text("JARVIS")
                .font(.largeTitle.bold())
                .tracking(6)

            Text("Remote Access")
                .font(.caption)
                .foregroundColor(.secondary)
                .textCase(.uppercase)

            TextField("https://your-jarvis-domain.com", text: $serverURL)
                .textFieldStyle(.roundedBorder)
                .autocapitalization(.none)
                .autocorrectionDisabled()
                .keyboardType(.URL)

            TextField("Key", text: $key)
                .textFieldStyle(.roundedBorder)
                .multilineTextAlignment(.center)
                .font(.system(.title2, design: .monospaced))
                .autocapitalization(.allCharacters)
                .autocorrectionDisabled()
                .onChange(of: key) { _, newValue in
                    key = String(newValue.uppercased().filter { $0.isLetter || $0.isNumber }.prefix(6))
                }

            SecureField("Optional private PIN", text: $pin)
                .textFieldStyle(.roundedBorder)

            if let errorText {
                Text(errorText)
                    .font(.footnote)
                    .foregroundColor(.red)
            }

            Button {
                Task { await doLogin() }
            } label: {
                if isLoading {
                    ProgressView()
                } else {
                    Text("CONNECT").bold()
                }
            }
            .buttonStyle(.borderedProminent)
            .disabled(serverURL.isEmpty || key.count < 6 || isLoading)
        }
        .padding(28)
    }

    private func doLogin() async {
        errorText = nil
        guard let normalized = normalizedURL(serverURL) else {
            errorText = "Enter a valid server URL."
            return
        }
        KeychainStore.set(normalized, for: .serverURL)
        isLoading = true
        defer { isLoading = false }
        do {
            _ = try await JarvisAPI.login(key: key, remotePin: pin)
            onLoggedIn()
        } catch {
            errorText = error.localizedDescription
        }
    }

    private func normalizedURL(_ raw: String) -> String? {
        var value = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else { return nil }
        if !value.hasPrefix("http://") && !value.hasPrefix("https://") {
            value = "https://" + value
        }
        while value.hasSuffix("/") { value.removeLast() }
        return URL(string: value) != nil ? value : nil
    }
}
