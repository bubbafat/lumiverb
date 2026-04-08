import Foundation
import Vision
import AppKit
import LumiverbKit

/// Detects faces in images using Apple Vision framework.
///
/// Uses `VNDetectFaceLandmarksRequest` for bounding boxes, confidence, and
/// the facial landmarks needed for ArcFace alignment. Fully local, no model
/// downloads needed — built into macOS 14+.
///
/// Quality gates match the Python InsightFace provider to ensure consistent
/// results across clients — confidence ≥ 0.5, ≥ 40 px face width, ≥ 0.3%
/// image-area fraction, ≥ 15% of the largest-face area, and a Laplacian-
/// variance sharpness floor of 15.0. See
/// `src/client/workers/faces/insightface_provider.py` for the reference.
///
/// Vision supplies bounding boxes and landmarks; embeddings come from
/// `ArcFaceProvider` (CoreML). The 5-point landmarks Vision produces are
/// converted to ArcFace's canonical template via
/// `LumiverbKit.FaceLandmarks.extractAlignmentLandmarks`, and the sharpness
/// gate is computed by `LumiverbKit.ImageQuality.laplacianVariance` — both
/// live in the shared package so they can be unit-tested.
enum FaceDetectionProvider {

    // MARK: - Quality gate thresholds (match Python insightface_provider.py)

    /// Minimum Vision detection confidence (Python: MIN_DETECTION_CONFIDENCE = 0.5).
    static let minDetectionConfidence: Float = 0.5

    /// Minimum bounding box area as fraction of image (Python: MIN_BBOX_AREA_FRACTION = 0.003).
    static let minBboxAreaFraction: Float = 0.003

    /// Minimum face width in pixels (Python: MIN_FACE_PIXELS = 40).
    static let minFacePixels: Float = 40

    /// Must be >= 15% area of the largest detected face (Python: MIN_RELATIVE_SIZE = 0.15).
    static let minRelativeSize: Float = 0.15

    /// Minimum Laplacian variance — drops blurry / out-of-focus faces that would
    /// embed unreliably and pollute clusters. Matches Python's
    /// `MIN_LAPLACIAN_VARIANCE = 15.0` computed via `cv2.Laplacian(gray, CV_64F)`
    /// with the default 3x3 4-neighbor kernel `[0,1,0; 1,-4,1; 0,1,0]`.
    static let minLaplacianVariance: Double = 15.0

    /// Detected face with normalized bounding box and optional landmarks.
    struct DetectedFace: Sendable {
        /// Normalized bounding box (0.0-1.0): x1, y1 (top-left), x2, y2 (bottom-right).
        let boundingBox: FacesSubmitRequest.BoundingBox
        let confidence: Float
        /// 5 facial landmarks in pixel coords (left eye, right eye, nose, left mouth, right mouth).
        /// Used for ArcFace alignment. Nil if landmarks unavailable.
        let landmarks: [CGPoint]?
    }

    static let detectionModel = "apple_vision"
    static let detectionModelVersion = "1"

    /// Detect faces in an image at the given URL.
    static func detectFaces(from imageURL: URL) throws -> [DetectedFace] {
        guard let cgImage = ImageLoading.loadOriented(from: imageURL) else {
            throw FaceDetectionError.unreadableImage(imageURL.lastPathComponent)
        }
        return try detectFaces(from: cgImage)
    }

    /// Detect faces from proxy cache data.
    static func detectFaces(from imageData: Data) throws -> [DetectedFace] {
        guard let cgImage = ImageLoading.loadOriented(from: imageData) else {
            throw FaceDetectionError.unreadableImage("proxy data")
        }
        return try detectFaces(from: cgImage)
    }

    /// Core face detection from a CGImage with quality gates.
    /// Uses VNDetectFaceLandmarksRequest to get both bounding boxes and facial landmarks.
    static func detectFaces(from cgImage: CGImage) throws -> [DetectedFace] {
        let request = VNDetectFaceLandmarksRequest()

        let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
        try handler.perform([request])

        guard let observations = request.results, !observations.isEmpty else {
            return []
        }

        let imageWidth = Float(cgImage.width)
        let imageHeight = Float(cgImage.height)

        // First pass: apply per-face quality gates
        struct Candidate {
            let detection: DetectedFace
            let bboxArea: Float
        }

        var candidates: [Candidate] = []

        for observation in observations {
            let confidence = Float(observation.confidence)
            if confidence < minDetectionConfidence {
                continue
            }

            // Vision returns bounding box in normalized coordinates with
            // origin at bottom-left. Convert to top-left origin (x1,y1,x2,y2).
            let box = observation.boundingBox
            let x1 = Float(box.origin.x)
            let y1 = Float(1.0 - box.origin.y - box.height) // Flip Y
            let x2 = Float(box.origin.x + box.width)
            let y2 = Float(1.0 - box.origin.y) // Flip Y

            let bboxW = x2 - x1
            let bboxH = y2 - y1
            let bboxArea = bboxW * bboxH

            // Minimum area fraction of the whole image
            if bboxArea < minBboxAreaFraction {
                continue
            }

            // Minimum face width in pixels
            let facePixelWidth = bboxW * imageWidth
            if facePixelWidth < minFacePixels {
                continue
            }

            // Sharpness gate: drop blurry faces that would embed unreliably.
            // Computed on the face crop (not the whole image) matching Python.
            let sharpness = ImageQuality.laplacianVariance(
                of: cgImage, bboxTopLeft: (x1, y1, x2, y2)
            )
            if sharpness < minLaplacianVariance {
                continue
            }

            // Extract 5 landmarks for ArcFace alignment (pixel coords, top-left origin)
            let landmarks = FaceLandmarks.extractAlignmentLandmarks(
                from: observation, imageWidth: imageWidth, imageHeight: imageHeight
            )

            candidates.append(Candidate(
                detection: DetectedFace(
                    boundingBox: FacesSubmitRequest.BoundingBox(x1: x1, y1: y1, x2: x2, y2: y2),
                    confidence: confidence,
                    landmarks: landmarks
                ),
                bboxArea: bboxArea
            ))
        }

        guard !candidates.isEmpty else { return [] }

        // Second pass: relative size gate — drop faces much smaller than the largest
        let maxArea = candidates.map(\.bboxArea).max()!
        return candidates
            .filter { $0.bboxArea >= maxArea * minRelativeSize }
            .map(\.detection)
    }

    // MARK: - Helpers live in LumiverbKit
    //
    // Pure math helpers — landmark extraction, image quality, similarity-
    // transform alignment, and ArcFace inference — all live in LumiverbKit
    // so they can be unit-tested from the Swift package test target.

    /// Extract an aligned 112x112 face crop for ArcFace embedding.
    ///
    /// If landmarks are available, computes a similarity transform matching InsightFace's
    /// `norm_crop` alignment (canonical `arcface_dst` template) via
    /// `LumiverbKit.FaceAlignment.alignedCrop`. This produces embeddings
    /// compatible with existing Python InsightFace embeddings.
    ///
    /// Falls back to a simple padded bbox crop if landmarks are unavailable.
    static func extractAlignedFaceCrop(from image: CGImage, face: DetectedFace) -> CGImage? {
        if let landmarks = face.landmarks {
            return FaceAlignment.alignedCrop(from: image, landmarks: landmarks)
        }
        return FaceAlignment.bboxCrop(
            from: image,
            x1: face.boundingBox.x1, y1: face.boundingBox.y1,
            x2: face.boundingBox.x2, y2: face.boundingBox.y2
        )
    }

    /// Decode proxy image data to a `CGImage` with EXIF orientation applied.
    ///
    /// Used by the enrichment pipeline to load a face's source image once and
    /// share it across detection + alignment. Routes through
    /// `LumiverbKit.ImageLoading.loadOriented` so callers always see
    /// right-side-up pixels regardless of how the JPG was stored — the naive
    /// `NSImage(data:)` path this helper used to wrap silently dropped EXIF
    /// orientation, which made face detection operate in the wrong
    /// coordinate system for almost every modern phone photo and was the
    /// dominant cause of cluster collapse on real libraries.
    static func cgImage(from data: Data) -> CGImage? {
        return ImageLoading.loadOriented(from: data)
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
