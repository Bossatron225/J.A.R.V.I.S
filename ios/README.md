# Jarvis iOS App

A native iOS app for Jarvis — talks to the same `vps_orchestrator.py` backend as the
web dashboard (`dashboard/static/app.html`), no server-side changes needed. Adds two
things the web page can't do:

- **Siri integration** ("Hey Siri, ask Jarvis...") via Apple's App Intents framework.
- A proper installable app icon / Home Screen presence instead of a Safari tab.

Apple does not allow any third-party app to fully replace Siri itself (no rebinding
"Hey Siri" or the side button) — this sits alongside Siri, not instead of it.

## One-time setup

1. **Install Xcode** from the App Store (already in progress as of this writing).
2. Generate the actual Xcode project (already done once, but re-run this any time
   `project.yml` changes):
   ```
   cd ios/JarvisApp
   xcodegen generate
   open JarvisApp.xcodeproj
   ```
3. In Xcode: **Signing & Capabilities** tab → sign in with your Apple ID → pick your
   personal team. This is the one step I genuinely cannot do for you — code signing
   requires your own Apple ID session inside Xcode.
4. Plug in your iPhone (or select it wirelessly if already paired), pick it as the run
   destination, and hit Run (▶). First launch will fail with a trust prompt — on the
   phone, go to **Settings → General → VPN & Device Management** and trust the
   developer certificate, then relaunch from the Home Screen.
5. Open the app, enter your Jarvis server URL (e.g. `https://jarvis.yourdomain.com`)
   and your login key — same key flow as `login.html`.
6. It'll walk you through the same face-scan lock as the web dashboard (enroll once if
   you haven't already, on either the app or the web page — they share one model).

## Siri

Once logged in and past the face scan at least once, try: **"Hey Siri, ask Jarvis..."**
followed by whatever you'd type into the chat box. It also shows up in the Shortcuts
app under Jarvis's icon, where you can rename the phrase or assign it to the Action
Button / Back Tap.

**Important limitation**: Siri-triggered commands run with no UI open, so they reuse
whatever token is already sitting in the Keychain from the last time you opened the
app and passed the face scan. That token is valid for 12 hours (server-enforced) — once
it expires, Siri commands will just report needing you to reopen the app. There's no
way around this without the phone doing a face scan itself in the background, which iOS
doesn't allow for a non-foregrounded app anyway.

## What this can't do

Third-party apps (this one included) cannot silently send SMS, toggle Wi-Fi/Bluetooth,
read other apps' notifications, or control other apps' UI, regardless of App Intents.
For that class of "system control," the actual capability lives in the **Shortcuts**
app's own privileged actions — a Jarvis-triggered Shortcut can chain into those, this
app's own intent can't reach them directly.

## Project layout

```
ios/JarvisApp/
  project.yml              XcodeGen spec — source of truth, not the .xcodeproj itself
  JarvisApp/
    JarvisAppApp.swift      App entry point
    ContentView.swift       Routes: login → biometric scan → chat
    Auth/KeychainStore.swift
    Networking/
      JarvisAPI.swift        REST calls (login, biometric status/verify/enroll, command)
      JarvisWebSocket.swift  Live /ws connection (chat feed)
      JarvisReplyWaiter.swift  Bridges POST /api/command → the WS reply, for Siri
    Views/
      LoginView.swift
      BiometricScanView.swift  Native version of app.html's #bio-lock overlay
      ChatView.swift
      CameraController.swift / CameraPreviewView.swift
    Intents/
      AskJarvisIntent.swift    The actual Siri hook
      JarvisShortcuts.swift    Declares the Siri phrases
```

`JarvisApp.xcodeproj/` is gitignored — it's generated, not hand-edited. Re-run
`xcodegen generate` after pulling changes to `project.yml` or adding new source files.
