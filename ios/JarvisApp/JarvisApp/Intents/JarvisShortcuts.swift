import AppIntents

/// Declares the actual Siri phrases. Once installed, "Hey Siri, ask Jarvis to turn on
/// the lights" (or whatever query) routes straight to AskJarvisIntent without opening
/// the app. Also shows up automatically in the Shortcuts app and can be assigned to
/// the Action Button / Back Tap on supported iPhones from there.
struct JarvisShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: AskJarvisIntent(),
            phrases: [
                "Ask \(.applicationName) \(\.$query)",
                "Tell \(.applicationName) \(\.$query)",
                "\(.applicationName) \(\.$query)",
            ],
            shortTitle: "Ask Jarvis",
            systemImageName: "sparkles"
        )
    }
}
