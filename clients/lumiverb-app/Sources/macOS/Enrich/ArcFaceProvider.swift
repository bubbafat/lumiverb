import Foundation
import CoreML
import Vision
import AppKit

/// ArcFace face embedding provider using CoreML.
///
/// Requires a converted ArcFace .mlmodelc bundle at a known path.
/// Produces 512-dimensional face embeddings compatible with the Python
/// InsightFace ArcFace embeddings used by the server for face clustering.
///
/// Model conversion: see `docs/adr/014-native-clients.md` for the
/// ONNX → CoreML conversion pipeline using coremltools.
enum ArcFaceProvider {

    static let modelId = "arcface"
    static let modelVersion = "buffalo_l"

    private static var modelURL: URL? {
        if let bundled = Bundle.main.url(forResource: "ArcFace", withExtension: "mlmodelc") {
            return bundled
        }
        let appSupport = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".lumiverb/models/ArcFace.mlmodelc")
        if FileManager.default.fileExists(atPath: appSupport.path) {
            return appSupport
        }
        return nil
    }

    /// Whether the ArcFace model is available.
    static var isAvailable: Bool { modelURL != nil }

    private static var cachedModel: MLModel?

    private static func loadModel() throws -> MLModel {
        if let cached = cachedModel { return cached }
        guard let url = modelURL else { throw ArcFaceError.modelNotFound }
        let model = try MLModel(contentsOf: url)
        cachedModel = model
        return model
    }

    /// Compute a 512-dimensional face embedding from a cropped face image.
    ///
    /// The input should be a tightly cropped face image (from the bounding box
    /// returned by FaceDetectionProvider), resized to 112x112.
    static func embed(faceImage: CGImage) throws -> [Float] {
        let model = try loadModel()

        // ArcFace expects 112x112 input — resize the face crop
        guard let resized = resize(faceImage, to: CGSize(width: 112, height: 112)) else {
            throw ArcFaceError.resizeFailed
        }

        // Convert to CVPixelBuffer for MLModel input
        guard let pixelBuffer = cgImageToPixelBuffer(resized, width: 112, height: 112) else {
            throw ArcFaceError.conversionFailed
        }

        let input = try MLDictionaryFeatureProvider(dictionary: ["input": pixelBuffer])
        let output = try model.prediction(from: input)

        guard let multiArray = output.featureValue(for: "output")?.multiArrayValue else {
            throw ArcFaceError.inferenceFailure
        }

        let count = multiArray.count
        var vector = [Float](repeating: 0, count: count)
        let pointer = multiArray.dataPointer.bindMemory(to: Float.self, capacity: count)
        for i in 0..<count { vector[i] = pointer[i] }

        // L2 normalize
        let norm = sqrt(vector.reduce(0) { $0 + $1 * $1 })
        if norm > 0 { vector = vector.map { $0 / norm } }

        return vector
    }

    // MARK: - Image helpers

    private static func resize(_ image: CGImage, to size: CGSize) -> CGImage? {
        let context = CGContext(
            data: nil,
            width: Int(size.width),
            height: Int(size.height),
            bitsPerComponent: 8,
            bytesPerRow: 0,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        )
        context?.interpolationQuality = .high
        context?.draw(image, in: CGRect(origin: .zero, size: size))
        return context?.makeImage()
    }

    private static func cgImageToPixelBuffer(_ image: CGImage, width: Int, height: Int) -> CVPixelBuffer? {
        var pixelBuffer: CVPixelBuffer?
        let attrs: [String: Any] = [
            kCVPixelBufferCGImageCompatibilityKey as String: true,
            kCVPixelBufferCGBitmapContextCompatibilityKey as String: true,
        ]
        let status = CVPixelBufferCreate(
            kCFAllocatorDefault, width, height,
            kCVPixelFormatType_32BGRA, attrs as CFDictionary, &pixelBuffer
        )
        guard status == kCVReturnSuccess, let buffer = pixelBuffer else { return nil }

        CVPixelBufferLockBaseAddress(buffer, [])
        defer { CVPixelBufferUnlockBaseAddress(buffer, []) }

        guard let context = CGContext(
            data: CVPixelBufferGetBaseAddress(buffer),
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: CVPixelBufferGetBytesPerRow(buffer),
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue
        ) else { return nil }

        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        return buffer
    }
}

enum ArcFaceError: Error, CustomStringConvertible {
    case modelNotFound
    case resizeFailed
    case conversionFailed
    case inferenceFailure

    var description: String {
        switch self {
        case .modelNotFound:
            return "ArcFace model not found. Place ArcFace.mlmodelc in ~/.lumiverb/models/ or the app bundle."
        case .resizeFailed: return "ArcFace: face crop resize failed"
        case .conversionFailed: return "ArcFace: pixel buffer conversion failed"
        case .inferenceFailure: return "ArcFace: inference produced no output"
        }
    }
}
