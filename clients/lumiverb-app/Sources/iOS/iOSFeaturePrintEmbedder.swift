import Foundation
import Vision

/// Apple Vision feature print embedder for iOS — mirror of the macOS
/// `FeaturePrintProvider` so we can produce embeddings that match the
/// vectors stored in the user's library.
///
/// The macOS app indexes images using `VNGenerateImageFeaturePrintRequest`
/// (model_id `apple_vision`, model_version `feature_print_v1`). For
/// similar-by-image to work on iOS, the iOS-side embed has to land in
/// the **same vector space** — that means the same Vision request.
/// We pass raw image data to Vision and let it handle EXIF orientation
/// internally; this also dodges a simulator-only "Failed to create
/// espresso context" error that fires when CGImages are pre-loaded
/// outside Vision.
enum iOSFeaturePrintEmbedder {

    static let modelId = "apple_vision"
    static let modelVersion = "feature_print_v1"

    /// Compute a feature print embedding from JPEG/PNG bytes. Returns
    /// an L2-normalized vector ready to send to `/v1/similar/search-by-vector`.
    ///
    /// **Why we use the data init rather than loadOriented + cgImage**:
    /// the iOS simulator's Vision implementation throws "Failed to
    /// create espresso context" intermittently when handed CGImages.
    /// Passing raw data via `VNImageRequestHandler(data:options:)`
    /// lets Vision use its own internal loader (which is also better
    /// at handling EXIF orientation than our manual path).
    static func embed(imageData: Data) throws -> [Float] {
        let request = VNGenerateImageFeaturePrintRequest()
        let handler = VNImageRequestHandler(data: imageData, options: [:])
        try handler.perform([request])

        guard let observation = request.results?.first as? VNFeaturePrintObservation else {
            throw EmbedError.inferenceFailure
        }

        // Vision exposes the feature print as a raw byte buffer of floats.
        // 768 elements on the current macOS 14+ / iOS 17+ models.
        let elementCount = observation.elementCount
        var vector = [Float](repeating: 0, count: elementCount)
        observation.data.withUnsafeBytes { buffer in
            let floatBuffer = buffer.bindMemory(to: Float.self)
            for i in 0..<elementCount {
                vector[i] = floatBuffer[i]
            }
        }

        // L2 normalize so cosine-similarity comparisons match what the
        // server's pgvector index expects.
        let norm = sqrt(vector.reduce(0) { $0 + $1 * $1 })
        if norm > 0 {
            vector = vector.map { $0 / norm }
        }
        return vector
    }

    enum EmbedError: Error, LocalizedError {
        case unreadableImage
        case inferenceFailure

        var errorDescription: String? {
            switch self {
            case .unreadableImage: return "Couldn't read image data"
            case .inferenceFailure: return "Vision feature print failed"
            }
        }
    }
}
