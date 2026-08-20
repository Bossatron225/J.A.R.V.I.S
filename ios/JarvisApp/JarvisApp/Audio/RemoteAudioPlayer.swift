import AVFoundation

/// Native equivalent of app.html's _scheduleRemoteAudioChunk/_playRemoteAudioChunk —
/// same wire format (raw PCM16, mono, 24kHz, no header, sent as binary /ws frames),
/// just played through AVAudioEngine instead of the Web Audio API. Where the JS side
/// hand-tracks _remoteAudioNextTime to queue chunks back-to-back without gaps,
/// AVAudioPlayerNode does that natively — scheduled buffers already play in order.
final class RemoteAudioPlayer {
    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()

    /// Matches the backend's actual output format exactly (see auth of truth:
    /// dashboard/static/app.html's `new Ctx({ sampleRate: 24000 })` +
    /// `createBuffer(1, bytes.length, 24000)`).
    private let sourceFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 24000, channels: 1, interleaved: true)!

    private var isRunning = false

    func start() {
        guard !isRunning else { return }
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playback, mode: .voiceChat, options: [.duckOthers])
            try session.setActive(true)
        } catch {
            print("[RemoteAudioPlayer] Failed to activate audio session: \(error)")
        }

        engine.attach(player)
        // Explicit format on connect is what makes AVAudioEngine insert a sample-rate
        // converter automatically — the hardware output is usually 44.1/48kHz, the
        // source audio is 24kHz; without this the engine would assume matching rates.
        engine.connect(player, to: engine.mainMixerNode, format: sourceFormat)

        do {
            try engine.start()
            player.play()
            isRunning = true
        } catch {
            print("[RemoteAudioPlayer] Failed to start engine: \(error)")
        }
    }

    func stop() {
        guard isRunning else { return }
        player.stop()
        engine.stop()
        isRunning = false
    }

    /// `data` is raw little-endian Int16 PCM samples straight off the wire — same
    /// payload the browser hands to `new Int16Array(arrayBuffer)`.
    func enqueue(pcm16Data data: Data) {
        guard isRunning else { return }
        let frameCount = data.count / MemoryLayout<Int16>.size
        guard frameCount > 0,
              let buffer = AVAudioPCMBuffer(pcmFormat: sourceFormat, frameCapacity: AVAudioFrameCount(frameCount)) else {
            return
        }
        buffer.frameLength = AVAudioFrameCount(frameCount)

        data.withUnsafeBytes { (rawBuffer: UnsafeRawBufferPointer) in
            guard let source = rawBuffer.bindMemory(to: Int16.self).baseAddress,
                  let destination = buffer.int16ChannelData?[0] else { return }
            destination.update(from: source, count: frameCount)
        }

        player.scheduleBuffer(buffer, completionHandler: nil)
    }
}
