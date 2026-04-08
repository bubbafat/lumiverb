import Foundation
import CoreML
import Vision
import AppKit

/// CLIP image embedding provider using CoreML.
///
/// Requires a converted ViT-B/32 .mlmodelc bundle at a known path.
/// The model produces 512-dimensional vectors compatible with the Python
/// CLIP embeddings used by the server for similarity search.
///
/// Model conversion: see `docs/adr/014-native-clients.md` for the
/// PyTorch → ONNX → CoreML conversion pipeline using coremltools.
enum CLIPProvider {

    static let modelId = "clip"
    static let modelVersion = "ViT-B-32-openai"

    /// Path where the converted CoreML model should be placed.
    /// Users/developers run the conversion script once; the app checks this path.
    private static var modelURL: URL? {
        // Check in app bundle first, then in a well-known location
        if let bundled = Bundle.main.url(forResource: "CLIPImageEncoder", withExtension: "mlmodelc") {
            return bundled
        }
        let appSupport = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".lumiverb/models/CLIPImageEncoder.mlmodelc")
        if FileManager.default.fileExists(atPath: appSupport.path) {
            return appSupport
        }
        return nil
    }

    /// Whether the CLIP model is available.
    static var isAvailable: Bool { modelURL != nil }

    // Not thread-safe. Safe today because enrichment pipelines are actors
    // that call providers sequentially. Guard with a lock if parallelizing.
    private static var cachedModel: VNCoreMLModel?

    /// Load the model (cached after first load).
    private static func loadModel() throws -> VNCoreMLModel {
        if let cached = cachedModel { return cached }

        guard let url = modelURL else {
            throw CLIPError.modelNotFound
        }

        let mlModel = try MLModel(contentsOf: url)
        let vnModel = try VNCoreMLModel(for: mlModel)
        cachedModel = vnModel
        return vnModel
    }

    /// Compute a 512-dimensional CLIP embedding for an image.
    static func embed(imageData: Data) throws -> [Float] {
        guard let nsImage = NSImage(data: imageData),
              let cgImage = nsImage.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
            throw CLIPError.unreadableImage
        }
        return try embed(cgImage: cgImage)
    }

    /// Compute a CLIP embedding from a CGImage.
    static func embed(cgImage: CGImage) throws -> [Float] {
        let model = try loadModel()

        let request = VNCoreMLRequest(model: model)
        request.imageCropAndScaleOption = .centerCrop

        let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
        try handler.perform([request])

        guard let result = request.results?.first as? VNCoreMLFeatureValueObservation,
              let multiArray = result.featureValue.multiArrayValue else {
            throw CLIPError.inferenceFailure
        }

        // Extract 512-dim float vector
        let count = multiArray.count
        var vector = [Float](repeating: 0, count: count)
        let pointer = multiArray.dataPointer.bindMemory(to: Float.self, capacity: count)
        for i in 0..<count {
            vector[i] = pointer[i]
        }

        // L2 normalize
        let norm = sqrt(vector.reduce(0) { $0 + $1 * $1 })
        if norm > 0 {
            vector = vector.map { $0 / norm }
        }

        return vector
    }
}

enum CLIPError: Error, CustomStringConvertible {
    case modelNotFound
    case unreadableImage
    case inferenceFailure

    var description: String {
        switch self {
        case .modelNotFound:
            return "CLIP model not found. Place CLIPImageEncoder.mlmodelc in ~/.lumiverb/models/ or the app bundle."
        case .unreadableImage:
            return "CLIP: cannot read image"
        case .inferenceFailure:
            return "CLIP: inference produced no output"
        }
    }
}
