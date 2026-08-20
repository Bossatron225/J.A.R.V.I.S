import AppIntents

/// This is the actual "Hey Siri, ask Jarvis..." hook. App Intents (iOS 16+) is Apple's
/// sanctioned way for a third-party app to be invoked by Siri — it can't replace Siri
/// itself, but Siri can hand a spoken phrase straight to this and speak back whatever
/// it returns. Runs headless (no UI), so it depends entirely on KeychainStore already
/// holding a valid, already-biometric-verified token from the last time the app was
/// opened — there's no way to run a face scan from here.
struct AskJarvisIntent: AppIntent {
    static var title: LocalizedStringResource = "Ask Jarvis"
    static var description = IntentDescription("Send a command or question to Jarvis and hear the reply.")

    @Parameter(title: "What do you want to ask Jarvis?", requestValueDialog: "What would you like to ask Jarvis?")
    var query: String

    static var parameterSummary: some ParameterSummary {
        Summary("Ask Jarvis \(\.$query)")
    }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        guard JarvisAPI.isLoggedIn else {
            return .result(dialog: "Open the Jarvis app and log in first — Siri can't do that part for you.")
        }
        do {
            let reply = try await JarvisReplyWaiter.sendAndAwaitReply(query)
            return .result(dialog: IntentDialog(stringLiteral: reply))
        } catch {
            return .result(dialog: IntentDialog(stringLiteral: "Jarvis error: \(error.localizedDescription)"))
        }
    }
}
