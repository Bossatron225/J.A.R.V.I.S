import SwiftUI

private enum AppStage: Equatable {
    case loggedOut
    case needsBiometric
    case ready
}

struct ContentView: View {
    @State private var stage: AppStage = JarvisAPI.isLoggedIn ? .needsBiometric : .loggedOut

    var body: some View {
        Group {
            switch stage {
            case .loggedOut:
                LoginView(onLoggedIn: { stage = .needsBiometric })
            case .needsBiometric:
                BiometricScanView(onUnlocked: { stage = .ready })
            case .ready:
                ChatView(
                    onNeedsLogin: { stage = .loggedOut },
                    onNeedsBiometric: { stage = .needsBiometric }
                )
            }
        }
        .animation(.default, value: stage == .ready)
    }
}
