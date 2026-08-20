import SwiftUI

/// Native equivalent of app.html's #bio-lock overlay — same guided 6-pose enrollment
/// and burst-capture verify flow, against the exact same /api/biometric/* endpoints,
/// so a face enrolled here or on the web dashboard works for both (same vps_web model).
struct BiometricScanView: View {
    let onUnlocked: () -> Void

    private enum Stage: Equatable {
        case checking
        case error(String)
        case needEnroll
        case enrolling(step: Int)
        case needVerify
        case verifying
        case verified
    }

    private static let poses = [
        "Look straight at the camera",
        "Turn your head slightly left",
        "Turn your head slightly right",
        "Tilt your head up a little",
        "Move a little closer",
        "Move a little farther back",
    ]

    @StateObject private var camera = CameraController()
    @State private var stage: Stage = .checking
    @State private var enrollFrames: [String] = []
    @State private var statusText = "Checking biometric status…"

    var body: some View {
        VStack(spacing: 20) {
            Text("BIOMETRIC LOCK")
                .font(.caption.bold())
                .tracking(2)
                .foregroundColor(.accentColor)

            ZStack(alignment: .topTrailing) {
                CameraPreviewView(session: camera.session)
                    .aspectRatio(3.0 / 4.0, contentMode: .fit)
                    .clipShape(RoundedRectangle(cornerRadius: 16))
                    .scaleEffect(x: camera.isFront ? -1 : 1, y: 1) // mirror, like a selfie preview

                Button(action: camera.flip) {
                    Image(systemName: "arrow.triangle.2.circlepath.camera")
                        .padding(10)
                        .background(.black.opacity(0.5))
                        .foregroundColor(.white)
                        .clipShape(Circle())
                }
                .padding(10)
            }

            if case .enrolling = stage {
                HStack(spacing: 6) {
                    ForEach(0..<Self.poses.count, id: \.self) { i in
                        Circle()
                            .fill(dotColor(for: i))
                            .frame(width: 8, height: 8)
                    }
                }
            }

            Text(statusText)
                .font(.footnote)
                .foregroundColor(.secondary)
                .multilineTextAlignment(.center)
                .frame(minHeight: 36)

            actionButtons
        }
        .padding(24)
        .onAppear {
            camera.start()
            Task { await checkStatus() }
        }
        .onDisappear { camera.stop() }
    }

    @ViewBuilder
    private var actionButtons: some View {
        switch stage {
        case .checking, .verifying:
            ProgressView()
        case .error:
            Button("Retry") { Task { await checkStatus() } }
                .buttonStyle(.borderedProminent)
        case .needEnroll:
            Button("Start Enrollment") { beginEnroll() }
                .buttonStyle(.borderedProminent)
        case .enrolling:
            Button("Capture") { Task { await captureEnrollStep() } }
                .buttonStyle(.borderedProminent)
        case .needVerify:
            VStack(spacing: 10) {
                Button("Scan Face") { Task { await runVerify() } }
                    .buttonStyle(.borderedProminent)
                Button("Re-enroll") { beginEnroll() }
                    .buttonStyle(.bordered)
            }
        case .verified:
            EmptyView()
        }
    }

    private func dotColor(for index: Int) -> Color {
        guard case .enrolling(let step) = stage else { return .gray.opacity(0.3) }
        if index < step { return .green }
        if index == step { return .accentColor }
        return .gray.opacity(0.3)
    }

    private func checkStatus() async {
        stage = .checking
        statusText = "Checking biometric status…"
        do {
            let status = try await JarvisAPI.biometricStatus()
            if !status.enrolled {
                stage = .needEnroll
                statusText = "No face enrolled yet for this app. Tap Start to begin a guided capture."
            } else if status.verified {
                stage = .verified
                onUnlocked()
            } else {
                stage = .needVerify
                statusText = "Center your face in frame and tap Scan."
            }
        } catch {
            stage = .error(error.localizedDescription)
            statusText = error.localizedDescription
        }
    }

    private func beginEnroll() {
        enrollFrames = []
        stage = .enrolling(step: 0)
        statusText = Self.poses[0]
    }

    private func captureEnrollStep() async {
        guard case .enrolling(let step) = stage else { return }
        guard let frame = await camera.captureFrameBase64() else {
            statusText = "Camera frame not ready — hold still and try again."
            return
        }
        enrollFrames.append(frame)
        let next = step + 1
        if next >= Self.poses.count {
            await finishEnroll()
        } else {
            stage = .enrolling(step: next)
            statusText = Self.poses[next]
        }
    }

    private func finishEnroll() async {
        statusText = "Training face model…"
        do {
            try await JarvisAPI.biometricEnroll(frames: enrollFrames)
            statusText = "Face enrolled. Now scan to unlock."
            stage = .needVerify
        } catch {
            statusText = "Enrollment failed: \(error.localizedDescription)"
            stage = .needEnroll
        }
    }

    private func runVerify() async {
        stage = .verifying
        statusText = "Scanning… hold still."
        var frames: [String] = []
        for _ in 0..<6 {
            if let frame = await camera.captureFrameBase64() {
                frames.append(frame)
            }
            try? await Task.sleep(nanoseconds: 150_000_000)
        }
        guard !frames.isEmpty else {
            statusText = "No camera frame captured — try again."
            stage = .needVerify
            return
        }
        do {
            try await JarvisAPI.biometricVerify(frames: frames)
            statusText = "Face matched."
            stage = .verified
            onUnlocked()
        } catch {
            statusText = error.localizedDescription
            stage = .needVerify
        }
    }
}
