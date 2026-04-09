import Foundation
import Vision
import CoreGraphics

/// Detects faces in images using Apple Vision framework.
///
/// Uses `VNDetectFaceLandmarksRequest` for bounding boxes, confidence, and
/// the facial landmarks needed for ArcFace alignment. Fully local, no model
/// downloads needed — built into macOS 14+ / iOS 17+.
///
/// **Sharpness is judged by `VNDetectFaceCaptureQualityRequest` only.**
/// Vision's capture-quality scorer is purpose-built to answer "is this
/// face usable for recognition" — it evaluates blur, lighting, occlusion,
/// *and* pose together, which is exactly the signal we want. The gate
/// runs on the landmark observations from the first pass, so no
/// re-detection is needed.
///
/// Historical note: until 2026-04 this code also ran a Laplacian-variance
/// gate on the face crop as a cheap first filter, matching the Python
/// InsightFace provider. It was removed after it was found dropping a
/// perfectly sharp, large, frontal face (`DSC05984.ARW` — smooth-skinned
/// subject, Vision confidence 1.000, `faceCaptureQuality` 0.612, Laplacian
/// 10.45 < threshold 15.0). Laplacian on a bbox crop measures local
/// high-frequency content, which on a face is dominated by skin texture —
/// so it conflates "sharp" with "textured" and systematically under-scores
/// smooth-skinned subjects (kids, soft lighting) while over-scoring
/// bearded/stubbled ones. The Vision scorer sees all of this and more,
/// so running Laplacian as a *preflight* to it is strictly worse: it can
/// only drop faces the better scorer would have kept. Do not re-add it
/// without a concrete failure case Vision's scorer cannot catch.
///
/// Numeric quality gates still match the Python InsightFace provider —
/// confidence ≥ 0.5, ≥ 40 px face width, ≥ 0.3% image-area fraction,
/// ≥ 15% of the largest-face area. See
/// `src/client/workers/faces/insightface_provider.py` for the shared
/// reference; Python keeps its Laplacian gate because it has no
/// equivalent to Vision's capture-quality scorer.
///
/// Vision supplies bounding boxes and landmarks; embeddings come from
/// `ArcFaceProvider` (CoreML) in the macOS target. The 5-point landmarks
/// Vision produces are converted to ArcFace's canonical template via
/// ``FaceLandmarks/extractAlignmentLandmarks(from:imageWidth:imageHeight:)``.
public enum FaceDetectionProvider {

    // MARK: - Quality gate thresholds (match Python insightface_provider.py)

    /// Minimum Vision detection confidence (Python: MIN_DETECTION_CONFIDENCE = 0.5).
    public static let minDetectionConfidence: Float = 0.5

    /// Minimum bounding box area as fraction of image (Python: MIN_BBOX_AREA_FRACTION = 0.003).
    public static let minBboxAreaFraction: Float = 0.003

    /// Minimum face width in pixels (Python: MIN_FACE_PIXELS = 40).
    public static let minFacePixels: Float = 40

    /// Must be >= 15% area of the largest detected face (Python: MIN_RELATIVE_SIZE = 0.15).
    public static let minRelativeSize: Float = 0.15

    /// Minimum Vision face capture quality (0…1). Apple's rough guidance:
    /// ≥ 0.5 is "identity-document grade," lower values are still usable
    /// for recognition but with diminishing confidence.
    ///
    /// Calibrated against the `face_single.jpg`, `face_group.jpg`, and
    /// `face_crowd.jpg` test fixtures:
    ///
    ///   - sharp frontal iPhone portrait  (`face_single`) → 0.365
    ///   - group shot, close subjects     (`face_group`)  → 0.572 / 0.578
    ///   - crowd, middle-distance subject (`face_crowd`)  → 0.383
    ///   - crowd, tiny background faces   (`face_crowd`)  → 0.176–0.304
    ///
    /// At 0.3 the gate keeps every fixture face that is also ≥ 40 px
    /// wide and ≥ 0.3% of the image area, and drops the tiny
    /// background subjects in the crowd scene (all ≤ 0.304). An earlier
    /// calibration at 0.4 was too aggressive — it dropped the sharp
    /// iPhone portrait at 0.365, which was a real false-negative hidden
    /// by a test suite that was bypassing the gate chain. Do not raise
    /// this back without re-running the fixture calibration.
    ///
    /// This is the only sharpness gate on the Swift path. If the quality
    /// scorer fails to run for any reason, the gate falls open and the
    /// face is accepted — Vision's detector confidence is still checked
    /// independently via ``minDetectionConfidence``.
    public static let minFaceCaptureQuality: Float = 0.3

    /// Detected face with normalized bounding box and optional landmarks.
    public struct DetectedFace: Sendable {
        /// Normalized bounding box (0.0-1.0): x1, y1 (top-left), x2, y2 (bottom-right).
        public let boundingBox: FacesSubmitRequest.BoundingBox
        public let confidence: Float
        /// 5 facial landmarks in pixel coords (left eye, right eye, nose, left mouth, right mouth).
        /// Used for ArcFace alignment. Nil if landmarks unavailable.
        public let landmarks: [CGPoint]?

        public init(
            boundingBox: FacesSubmitRequest.BoundingBox,
            confidence: Float,
            landmarks: [CGPoint]?
        ) {
            self.boundingBox = boundingBox
            self.confidence = confidence
            self.landmarks = landmarks
        }
    }

    public static let detectionModel = "apple_vision"
    public static let detectionModelVersion = "1"

    /// Detect faces in an image at the given URL.
    public static func detectFaces(from imageURL: URL) throws -> [DetectedFace] {
        guard let cgImage = ImageLoading.loadOriented(from: imageURL) else {
            throw FaceDetectionError.unreadableImage(imageURL.lastPathComponent)
        }
        return try detectFaces(from: cgImage)
    }

    /// Detect faces from proxy cache data.
    public static func detectFaces(from imageData: Data) throws -> [DetectedFace] {
        guard let cgImage = ImageLoading.loadOriented(from: imageData) else {
            throw FaceDetectionError.unreadableImage("proxy data")
        }
        return try detectFaces(from: cgImage)
    }

    /// Core face detection from a CGImage with quality gates.
    /// Uses VNDetectFaceLandmarksRequest to get bounding boxes and
    /// facial landmarks, then chains VNDetectFaceCaptureQualityRequest
    /// against those observations to score each face's recognition
    /// quality (blur / pose / lighting / occlusion).
    public static func detectFaces(from cgImage: CGImage) throws -> [DetectedFace] {
        let landmarksRequest = VNDetectFaceLandmarksRequest()

        let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
        try handler.perform([landmarksRequest])

        guard let observations = landmarksRequest.results, !observations.isEmpty else {
            return []
        }

        // Second pass: capture-quality scoring on the already-detected
        // faces. We pass the landmark observations as input; Vision
        // returns the same observations enriched with the
        // ``faceCaptureQuality`` property. Order is preserved 1:1 with
        // the input array, so we can index by position. If this fails
        // for any reason (Vision unavailable, perform throws) the gate
        // falls open — see ``minFaceCaptureQuality`` docs.
        let qualityScores: [Float?] = {
            let qualityRequest = VNDetectFaceCaptureQualityRequest()
            qualityRequest.inputFaceObservations = observations
            do {
                try handler.perform([qualityRequest])
            } catch {
                return Array(repeating: nil, count: observations.count)
            }
            let scored = qualityRequest.results ?? []
            return (0..<observations.count).map { i in
                i < scored.count ? scored[i].faceCaptureQuality : nil
            }
        }()

        let imageWidth = Float(cgImage.width)
        let imageHeight = Float(cgImage.height)

        // First pass: apply per-face quality gates
        struct Candidate {
            let detection: DetectedFace
            let bboxArea: Float
        }

        var candidates: [Candidate] = []

        for (idx, observation) in observations.enumerated() {
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

            // Sharpness gate: Vision's purpose-built capture quality
            // score (blur / pose / lighting / occlusion, all-in-one).
            // Falls open if Vision didn't return a score for this index.
            // See the type docstring for why this is the only sharpness
            // gate on the Swift path.
            if let q = qualityScores[idx], q < minFaceCaptureQuality {
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

    // MARK: - Alignment helpers

    /// Extract an aligned 112x112 face crop for ArcFace embedding.
    ///
    /// If landmarks are available, computes a similarity transform matching InsightFace's
    /// `norm_crop` alignment (canonical `arcface_dst` template) via
    /// ``FaceAlignment/alignedCrop(from:landmarks:)``. This produces embeddings
    /// compatible with existing Python InsightFace embeddings.
    ///
    /// Falls back to a simple padded bbox crop if landmarks are unavailable.
    public static func extractAlignedFaceCrop(from image: CGImage, face: DetectedFace) -> CGImage? {
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
    /// ``ImageLoading/loadOriented(from:)-<data overload>`` so callers always
    /// see right-side-up pixels regardless of how the JPG was stored — the
    /// naive `NSImage(data:)` path this helper used to wrap silently dropped
    /// EXIF orientation, which made face detection operate in the wrong
    /// coordinate system for almost every modern phone photo and was the
    /// dominant cause of cluster collapse on real libraries.
    public static func cgImage(from data: Data) -> CGImage? {
        return ImageLoading.loadOriented(from: data)
    }
}

public enum FaceDetectionError: Error, CustomStringConvertible {
    case unreadableImage(String)

    public var description: String {
        switch self {
        case .unreadableImage(let file): return "Face detection: cannot read image: \(file)"
        }
    }
}
