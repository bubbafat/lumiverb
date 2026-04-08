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
/// **Two sharpness gates run in series.** A face must pass both to be
/// emitted:
///
/// 1. ``LumiverbKit.ImageQuality.laplacianVariance`` ≥ 15.0 — the
///    classic Python-compatible gate. Cheap, runs on the proxy crop,
///    easily tested in the shared package. Catches obvious motion blur
///    and totally out-of-focus crops, but is noisy: a flat bald face
///    can score lower than a bearded face of the same sharpness, and
///    background subjects with high-frequency texture (foliage, fabric)
///    sneak through with deceptively high variance.
/// 2. ``VNDetectFaceCaptureQualityRequest`` ≥ ``minFaceCaptureQuality``
///    — Apple's purpose-built "is this face usable for recognition"
///    score. It evaluates blur, lighting, occlusion, *and* pose
///    together, which is exactly what we want. Catches the case where
///    Laplacian was fooled by background texture (the canonical example:
///    a defocused subject behind a sharp foreground person scores
///    deceptively high on Laplacian but low on Vision's quality).
///
/// Quality gates otherwise match the Python InsightFace provider to
/// ensure consistent results across clients — confidence ≥ 0.5, ≥ 40 px
/// face width, ≥ 0.3% image-area fraction, ≥ 15% of the largest-face
/// area. See `src/client/workers/faces/insightface_provider.py` for the
/// shared reference.
///
/// Vision supplies bounding boxes and landmarks; embeddings come from
/// `ArcFaceProvider` (CoreML). The 5-point landmarks Vision produces are
/// converted to ArcFace's canonical template via
/// `LumiverbKit.FaceLandmarks.extractAlignmentLandmarks`, and the
/// Laplacian gate is computed by `LumiverbKit.ImageQuality.laplacianVariance`
/// — both live in the shared package so they can be unit-tested.
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

    /// Minimum Vision face capture quality (0…1). Apple recommends ≥ 0.5
    /// for identity-document quality; 0.4 is the conventional clustering
    /// threshold. Calibrated against a real library where the Laplacian
    /// gate (15.0) was passing visibly-unrecognizable defocused
    /// background subjects scoring 25+ — Vision's quality scorer
    /// catches them because it accounts for pose and global blur, not
    /// just local high-frequency content.
    ///
    /// Used as a *second* gate after the Laplacian variance check.
    /// Faces must pass both to be emitted; if Vision's quality scorer
    /// fails to run for any reason the gate falls open and only the
    /// Laplacian check applies (the prior behavior).
    static let minFaceCaptureQuality: Float = 0.4

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
    /// Uses VNDetectFaceLandmarksRequest to get bounding boxes and
    /// facial landmarks, then chains VNDetectFaceCaptureQualityRequest
    /// against those observations to score each face's recognition
    /// quality (blur / pose / lighting / occlusion).
    static func detectFaces(from cgImage: CGImage) throws -> [DetectedFace] {
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
        // falls open and only the Laplacian check applies — see
        // ``minFaceCaptureQuality`` docs.
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

            // Sharpness gate 1: Laplacian variance on the face crop.
            // Cheap, runs on every detected face. Catches obvious motion
            // blur and totally out-of-focus crops.
            let sharpness = ImageQuality.laplacianVariance(
                of: cgImage, bboxTopLeft: (x1, y1, x2, y2)
            )
            if sharpness < minLaplacianVariance {
                continue
            }

            // Sharpness gate 2: Vision's purpose-built capture quality
            // score. Catches the case where a defocused background
            // subject scores deceptively high on Laplacian (because of
            // ambient texture) but is visibly unrecognizable. Falls
            // open if Vision didn't return a score for this index.
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
