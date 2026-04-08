import Foundation
import Vision
import AppKit

/// Apple Vision feature print provider for image embeddings.
///
/// Uses VNGenerateImageFeaturePrintRequest (macOS 14+) to produce
/// on-device image embeddings with zero external dependencies.
/// No model files to download or convert.
///
/// These embeddings are NOT compatible with CLIP embeddings — they live
/// in a different vector space. All assets in a library must use the same
/// model for similarity search to work. The model_id is tracked per-embedding
/// so the server filters correctly.
enum FeaturePrintProvider {

    static let modelId = "apple_vision"
    static let modelVersion = "feature_print_v1"

    /// Always available on macOS 14+.
    static var isAvailable: Bool { true }

    /// Compute a feature print embedding from image data.
    static func embed(imageData: Data) throws -> [Float] {
        guard let nsImage = NSImage(data: imageData),
              let cgImage = nsImage.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
            throw FeaturePrintError.unreadableImage
        }
        return try embed(cgImage: cgImage)
    }

    /// Compute a feature print embedding from a CGImage.
    static func embed(cgImage: CGImage) throws -> [Float] {
        let request = VNGenerateImageFeaturePrintRequest()

        let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
        try handler.perform([request])

        guard let observation = request.results?.first as? VNFeaturePrintObservation else {
            throw FeaturePrintError.inferenceFailure
        }

        // Extract the feature print data as Float array.
        // VNFeaturePrintObservation.elementType == .float (1), data is raw Float bytes.
        // Element count is 768 for the current macOS 14+ model.
        let elementCount = observation.elementCount
        var vector = [Float](repeating: 0, count: elementCount)
        observation.data.withUnsafeBytes { buffer in
            let floatBuffer = buffer.bindMemory(to: Float.self)
            for i in 0..<elementCount {
                vector[i] = floatBuffer[i]
            }
        }

        // L2 normalize for cosine similarity compatibility
        let norm = sqrt(vector.reduce(0) { $0 + $1 * $1 })
        if norm > 0 {
            vector = vector.map { $0 / norm }
        }

        return vector
    }
}

enum FeaturePrintError: Error, CustomStringConvertible {
    case unreadableImage
    case inferenceFailure

    var description: String {
        switch self {
        case .unreadableImage: return "FeaturePrint: cannot read image"
        case .inferenceFailure: return "FeaturePrint: feature print generation failed"
        }
    }
}
