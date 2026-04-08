import Foundation
import CoreML
import LumiverbKit

/// macOS-target shim around `LumiverbKit.FaceEmbedding`.
///
/// Owns the model file: bundle/`~/.lumiverb/models` resolution, on-demand
/// download via `ModelDownloader`, and the in-memory `MLModel` cache. The
/// inference itself lives in `LumiverbKit.FaceEmbedding` so that
/// the embedding pipeline can be tested end to end from the LumiverbKit
/// test target.
///
/// Model conversion: see `docs/adr/014-native-clients.md` and
/// `scripts/convert-models/convert_arcface.py`.
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

    /// Whether the ArcFace model is available locally (bundle or ~/.lumiverb/models/).
    static var isAvailable: Bool { modelURL != nil }

    /// Whether the model can be downloaded on demand.
    static var isDownloadable: Bool { true }

    // Not thread-safe. Safe today because enrichment pipelines are actors
    // that call providers sequentially. Guard with a lock if parallelizing.
    private static var cachedModel: MLModel?

    private static func loadModel() throws -> MLModel {
        if let cached = cachedModel { return cached }
        guard let url = modelURL else { throw ArcFaceError.modelNotFound }
        let model = try MLModel(contentsOf: url)
        cachedModel = model
        return model
    }

    /// Download the model if not already installed, then load it.
    static func ensureAvailable() async throws {
        if isAvailable { return }
        let url = try await ModelDownloader.ensureAvailable(ModelDownloader.arcFace)
        let model = try MLModel(contentsOf: url)
        cachedModel = model
    }

    /// Compute a 512-dimensional ArcFace embedding for a pre-aligned face crop.
    ///
    /// Caller is responsible for landmark-based alignment via
    /// `LumiverbKit.FaceAlignment.alignedCrop` (or
    /// `FaceDetectionProvider.extractAlignedFaceCrop` which wraps it). Passing
    /// a raw bbox crop instead of an aligned crop produces embeddings that
    /// look valid but contain no identity signal — ArcFace's filters were
    /// trained on inputs warped to its canonical 112×112 template.
    static func embed(faceImage: CGImage) throws -> [Float] {
        let model = try loadModel()
        return try FaceEmbedding.embed(faceImage: faceImage, model: model)
    }
}

enum ArcFaceError: Error, CustomStringConvertible {
    case modelNotFound

    var description: String {
        switch self {
        case .modelNotFound:
            return "ArcFace model not found. Place ArcFace.mlmodelc in ~/.lumiverb/models/ or the app bundle."
        }
    }
}
