import AppIntents

/// Declares the actual Siri phrases. App Shortcut phrases can only embed a parameter
/// placeholder for AppEntity/AppEnum types — Siri's on-device phrase grammar needs a
/// closed vocabulary to match against, which an arbitrary free-text query isn't. So
/// the phrase itself carries no parameter; instead AskJarvisIntent's `query` parameter
/// declares a requestValueDialog, and Siri asks a follow-up question for the free text
/// after the phrase triggers — the same two-step pattern used by AI-assistant apps
/// like Perplexity/ChatGPT for exactly this "arbitrary spoken text to a backend" case.
struct JarvisShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: AskJarvisIntent(),
            phrases: [
                "Ask \(.applicationName)",
                "Talk to \(.applicationName)",
            ],
            shortTitle: "Ask Jarvis",
            systemImageName: "sparkles"
        )
    }
}
