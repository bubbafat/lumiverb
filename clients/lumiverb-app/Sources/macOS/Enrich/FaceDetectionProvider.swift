import Foundation
import Vision
import AppKit
import LumiverbKit

/// Detects faces in images using Apple Vision framework.
///
/// Uses `VNDetectFaceRectanglesRequest` for bounding boxes and confidence.
/// Fully local, no model downloads needed — built into macOS 14+.
///
/// Note: Apple Vision provides detection only (bounding boxes), not embeddings.
/// Face embeddings require a CoreML ArcFace model (Phase 4 TODO).
enum FaceDetectionProvider {

    /// Detected face with normalized bounding box.
    struct DetectedFace: Sendable {
        /// Normalized bounding box (0.0-1.0): x1, y1 (top-left), x2, y2 (bottom-right).
        let boundingBox: FacesSubmitRequest.BoundingBox
        let confidence: Float
    }

    static let detectionModel = "apple_vision"
    static let detectionModelVersion = "1"

    /// Detect faces in an image at the given URL.
    static func detectFaces(from imageURL: URL) throws -> [DetectedFace] {
        guard let cgImage = loadCGImage(from: imageURL) else {
            throw FaceDetectionError.unreadableImage(imageURL.lastPathComponent)
        }
        return try detectFaces(from: cgImage)
    }

    /// Detect faces from proxy cache data.
    static func detectFaces(from imageData: Data) throws -> [DetectedFace] {
        guard let nsImage = NSImage(data: imageData),
              let cgImage = nsImage.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
            throw FaceDetectionError.unreadableImage("proxy data")
        }
        return try detectFaces(from: cgImage)
    }

    /// Core face detection from a CGImage.
    static func detectFaces(from cgImage: CGImage) throws -> [DetectedFace] {
        let request = VNDetectFaceRectanglesRequest()

        let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
        try handler.perform([request])

        guard let observations = request.results else {
            return []
        }

        return observations.map { observation in
            // Vision returns bounding box in normalized coordinates with
            // origin at bottom-left. Convert to top-left origin (x1,y1,x2,y2).
            let box = observation.boundingBox
            let x1 = Float(box.origin.x)
            let y1 = Float(1.0 - box.origin.y - box.height) // Flip Y
            let x2 = Float(box.origin.x + box.width)
            let y2 = Float(1.0 - box.origin.y) // Flip Y

            return DetectedFace(
                boundingBox: FacesSubmitRequest.BoundingBox(x1: x1, y1: y1, x2: x2, y2: y2),
                confidence: Float(observation.confidence)
            )
        }
    }

    private static func loadCGImage(from url: URL) -> CGImage? {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
        return CGImageSourceCreateImageAtIndex(source, 0, nil)
    }
}

enum FaceDetectionError: Error, CustomStringConvertible {
    case unreadableImage(String)

    var description: String {
        switch self {
        case .unreadableImage(let file): return "Face detection: cannot read image: \(file)"
        }
    }
}
