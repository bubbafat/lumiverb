import Foundation
import CoreML
import CoreGraphics

/// ArcFace inference: turns an aligned 112×112 face crop into a 512-d
/// L2-normalized embedding via a CoreML model.
///
/// This module owns only the inference itself — it takes a `CGImage`
/// (expected to be already aligned via `FaceAlignment.alignedCrop`) and a
/// loaded `MLModel`. Model loading, file path resolution, on-demand
/// download, and the singleton cache stay in the macOS-target
/// `ArcFaceProvider` shim, because they involve filesystem and bundle
/// concerns that don't belong in the cross-platform LumiverbKit package.
///
/// Splitting it this way is what makes the embedding pipeline testable end
/// to end: tests can construct an `MLModel` directly from any path, hand it
/// to `FaceEmbedding.embed`, and assert real cos-sim relationships against
/// real face fixtures — without depending on the macOS app target.
public enum FaceEmbedding {

    /// Compute the 512-d L2-normalized ArcFace embedding for a face crop.
    ///
    /// - Parameters:
    ///   - faceImage: An aligned face crop. Will be resized to 112×112 if
    ///     not already. Best results come from passing a crop produced by
    ///     `FaceAlignment.alignedCrop`, which warps via the canonical
    ///     `arcface_dst` template.
    ///   - model: A loaded ArcFace `MLModel`. The caller is responsible for
    ///     loading and caching this — the inference path is hot enough that
    ///     reloading per-call would dominate runtime.
    /// - Returns: A 512-element `[Float]` L2-normalized to unit length, ready
    ///   to compare via dot product (== cosine similarity).
    public static func embed(faceImage: CGImage, model: MLModel) throws -> [Float] {
        guard let resized = resize(faceImage, to: CGSize(width: 112, height: 112)) else {
            throw FaceEmbeddingError.resizeFailed
        }

        guard let pixelBuffer = cgImageToPixelBuffer(resized, width: 112, height: 112) else {
            throw FaceEmbeddingError.conversionFailed
        }

        let input = try MLDictionaryFeatureProvider(dictionary: ["input": pixelBuffer])
        let output = try model.prediction(from: input)

        guard let multiArray = output.featureValue(for: "output")?.multiArrayValue else {
            throw FaceEmbeddingError.inferenceFailure
        }

        // Read the model output respecting its actual dtype.
        //
        // The original implementation hard-coded a `Float` (Float32) reinterpretation
        // of the data pointer regardless of the model's declared dtype. ArcFace
        // converted with `compute_precision=FLOAT16` (the smaller, faster default
        // in modern coremltools) produces a Float16 output buffer — half the size
        // of what a Float32 reader expects — so the hard-coded read returned
        // ~256 bit-shuffled values plus ~256 bytes of adjacent memory, which
        // survived L2-normalization as a vector that *looks* valid but contains
        // only the faintest residual identity signal. With small clean fixtures
        // a fixture-level cosine-sim test still passes weakly; on a real photo
        // library it collapses thousands of distinct people into one cluster.
        //
        // The 65e989a converter validation can't catch this because it runs in
        // Python where coremltools handles dtype conversion automatically — the
        // bug is purely on the Swift consumer side.
        let count = multiArray.count
        var vector = [Float](repeating: 0, count: count)
        switch multiArray.dataType {
        case .float32:
            let p = multiArray.dataPointer.bindMemory(to: Float.self, capacity: count)
            for i in 0..<count { vector[i] = p[i] }
        case .float16:
            let p = multiArray.dataPointer.bindMemory(to: Float16.self, capacity: count)
            for i in 0..<count { vector[i] = Float(p[i]) }
        case .double:
            let p = multiArray.dataPointer.bindMemory(to: Double.self, capacity: count)
            for i in 0..<count { vector[i] = Float(p[i]) }
        @unknown default:
            // Fall back to MLMultiArray's NSNumber accessor for any future dtypes.
            // Slower per-element but correct regardless of underlying representation.
            for i in 0..<count { vector[i] = multiArray[i].floatValue }
        }

        // L2 normalize so callers can use plain dot product as cosine similarity.
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

public enum FaceEmbeddingError: Error, CustomStringConvertible {
    case resizeFailed
    case conversionFailed
    case inferenceFailure

    public var description: String {
        switch self {
        case .resizeFailed: return "FaceEmbedding: face crop resize failed"
        case .conversionFailed: return "FaceEmbedding: pixel buffer conversion failed"
        case .inferenceFailure: return "FaceEmbedding: inference produced no output"
        }
    }
}
