import AVFoundation
import UIKit

/// Native equivalent of app.html's getUserMedia({facingMode}) + canvas.toDataURL() —
/// same job (start a preview, grab JPEG frames, flip front/back), just via AVFoundation
/// instead of a browser. Defaults to the front camera, same reasoning as the web
/// overlay: a face scan is naturally a front-camera task, unlike the scene-analysis
/// camera panel which defaults to the back one.
final class CameraController: NSObject, ObservableObject {
    let session = AVCaptureSession()
    @Published var isFront = true
    @Published var errorMessage: String?

    private var currentInput: AVCaptureDeviceInput?
    private let photoOutput = AVCapturePhotoOutput()
    private var captureCompletion: ((Data?) -> Void)?

    func start() {
        session.beginConfiguration()
        session.sessionPreset = .high
        if session.canAddOutput(photoOutput) {
            session.addOutput(photoOutput)
        }
        session.commitConfiguration()
        configureInput(front: isFront)
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.session.startRunning()
        }
    }

    func stop() {
        session.stopRunning()
    }

    func flip() {
        isFront.toggle()
        configureInput(front: isFront)
    }

    private func configureInput(front: Bool) {
        session.beginConfiguration()
        if let current = currentInput {
            session.removeInput(current)
        }
        let position: AVCaptureDevice.Position = front ? .front : .back
        guard let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: position),
              let input = try? AVCaptureDeviceInput(device: device) else {
            errorMessage = "Could not access the \(front ? "front" : "back") camera."
            session.commitConfiguration()
            return
        }
        if session.canAddInput(input) {
            session.addInput(input)
            currentInput = input
        }
        session.commitConfiguration()
    }

    /// Captures one JPEG frame, base64-encoded — the exact shape /api/biometric/verify
    /// and /api/biometric/enroll expect in their "frames" array. Format is forced to
    /// JPEG explicitly: AVCapturePhotoOutput defaults to HEIC on most modern iPhones,
    /// which auth.py's cv2.imdecode (OpenCV, no HEIC codec) can't read at all — every
    /// frame would silently fail to decode server-side with no error surfaced here.
    func captureFrameBase64() async -> String? {
        await withCheckedContinuation { continuation in
            captureCompletion = { data in
                continuation.resume(returning: data?.base64EncodedString())
            }
            let settings = AVCapturePhotoSettings(format: [AVVideoCodecKey: AVVideoCodecType.jpeg])
            photoOutput.capturePhoto(with: settings, delegate: self)
        }
    }
}

extension CameraController: AVCapturePhotoCaptureDelegate {
    func photoOutput(_ output: AVCapturePhotoOutput, didFinishProcessingPhoto photo: AVCapturePhoto, error: Error?) {
        guard error == nil, let data = photo.fileDataRepresentation() else {
            captureCompletion?(nil)
            captureCompletion = nil
            return
        }
        captureCompletion?(data)
        captureCompletion = nil
    }
}
