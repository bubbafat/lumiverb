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
    // Pure math helpers — `FaceLandmarks.extractAlignmentLandmarks` and
    // `ImageQuality.laplacianVariance` — live in LumiverbKit so they can be
    // unit-tested directly from the Swift package test target.

    /// Extract an aligned 112x112 face crop for ArcFace embedding.
    ///
    /// If landmarks are available, computes a similarity transform matching InsightFace's
    /// `norm_crop` alignment (canonical `arcface_dst` template). This produces embeddings
    /// compatible with existing Python InsightFace embeddings.
    ///
    /// Falls back to a simple padded bbox crop if landmarks are unavailable.
    static func extractAlignedFaceCrop(from image: CGImage, face: DetectedFace) -> CGImage? {
        if let landmarks = face.landmarks {
            return alignedCrop(from: image, landmarks: landmarks)
        }
        return bboxCrop(from: image, bbox: face.boundingBox)
    }

    // MARK: - Aligned crop (similarity transform)

    /// InsightFace's canonical ArcFace destination landmarks for 112x112.
    private static let arcfaceDst: [(CGFloat, CGFloat)] = [
        (38.2946, 51.6963),  // left eye
        (73.5318, 51.5014),  // right eye
        (56.0252, 71.7366),  // nose
        (41.5493, 92.3655),  // left mouth
        (70.7299, 92.2041),  // right mouth
    ]

    /// Compute a similarity transform from source landmarks to arcface_dst,
    /// then warp the image to produce a 112x112 aligned face.
    private static func alignedCrop(from image: CGImage, landmarks: [CGPoint]) -> CGImage? {
        guard landmarks.count == 5 else { return nil }

        // Forward transform: dst = [a, -b; b, a] * src + [tx, ty]
        let (a, b, tx, ty) = estimateSimilarityTransform(
            src: landmarks.map { (Double($0.x), Double($0.y)) },
            dst: arcfaceDst.map { (Double($0.0), Double($0.1)) }
        )

        let size = 112
        guard let context = CGContext(
            data: nil,
            width: size,
            height: size,
            bitsPerComponent: 8,
            bytesPerRow: 0,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ) else { return nil }

        context.interpolationQuality = .high

        // CGContext uses bottom-left origin, flip Y for our top-left landmark coords
        context.translateBy(x: 0, y: CGFloat(size))
        context.scaleBy(x: 1, y: -1)

        // Inverse of the forward similarity transform.
        // Forward: [dx] = [a, -b] * [sx] + [tx]
        //          [dy]   [b,  a]   [sy]   [ty]
        // Inverse: [sx] = (1/(a²+b²)) * [ a, b] * [dx - tx]
        //          [sy]                  [-b, a]   [dy - ty]
        let det = a * a + b * b
        guard det > 1e-12 else { return nil }
        let ia =  a / det
        let ib = b / det

        let transform = CGAffineTransform(
            a: CGFloat(ia), b: CGFloat(ib),
            c: CGFloat(-ib), d: CGFloat(ia),
            tx: CGFloat(-(ia * tx + ib * ty)),
            ty: CGFloat(-(-ib * tx + ia * ty))
        )
        context.concatenate(transform)

        context.draw(image, in: CGRect(x: 0, y: 0, width: image.width, height: image.height))
        return context.makeImage()
    }

    /// Compute the similarity transform coefficients (a, b, tx, ty) from src → dst points.
    ///
    /// The transform maps: `dst_x = a*src_x - b*src_y + tx`,  `dst_y = b*src_x + a*src_y + ty`.
    /// Uses least-squares fit matching scikit-image's `SimilarityTransform.estimate()`.
    private static func estimateSimilarityTransform(
        src: [(Double, Double)],
        dst: [(Double, Double)]
    ) -> (a: Double, b: Double, tx: Double, ty: Double) {
        // Solve the 2Nx4 linear system:
        //   For each point i:  [sx, -sy, 1, 0] * [a, b, tx, ty]^T = dx
        //                      [sy,  sx, 0, 1] * [a, b, tx, ty]^T = dy
        // Via normal equations (A^T A x = A^T b)
        let n = src.count
        var ata = [[Double]](repeating: [Double](repeating: 0, count: 4), count: 4)
        var atb = [Double](repeating: 0, count: 4)

        for i in 0..<n {
            let sx = src[i].0, sy = src[i].1
            let dx = dst[i].0, dy = dst[i].1

            // Row 1: [sx, -sy, 1, 0]
            // Row 2: [sy,  sx, 0, 1]
            let rows: [[Double]] = [
                [sx, -sy, 1, 0],
                [sy,  sx, 0, 1],
            ]
            let rhs = [dx, dy]

            for r in 0..<2 {
                for c in 0..<4 {
                    atb[c] += rows[r][c] * rhs[r]
                    for c2 in 0..<4 {
                        ata[c][c2] += rows[r][c] * rows[r][c2]
                    }
                }
            }
        }

        // Solve 4x4 system via Gaussian elimination
        let x = solve4x4(ata, atb)
        return (a: x[0], b: x[1], tx: x[2], ty: x[3])
    }

    /// Solve a 4x4 linear system Ax = b via Gaussian elimination with partial pivoting.
    private static func solve4x4(_ A: [[Double]], _ b: [Double]) -> [Double] {
        var aug = A.enumerated().map { (i, row) in row + [b[i]] }

        for col in 0..<4 {
            // Partial pivot
            var maxRow = col
            var maxVal = abs(aug[col][col])
            for row in (col + 1)..<4 {
                if abs(aug[row][col]) > maxVal {
                    maxVal = abs(aug[row][col])
                    maxRow = row
                }
            }
            if maxRow != col { aug.swapAt(col, maxRow) }

            let pivot = aug[col][col]
            guard abs(pivot) > 1e-12 else { return [0, 0, 0, 0] }

            for row in (col + 1)..<4 {
                let factor = aug[row][col] / pivot
                for j in col..<5 {
                    aug[row][j] -= factor * aug[col][j]
                }
            }
        }

        // Back substitution
        var x = [Double](repeating: 0, count: 4)
        for col in stride(from: 3, through: 0, by: -1) {
            var sum = aug[col][4]
            for j in (col + 1)..<4 {
                sum -= aug[col][j] * x[j]
            }
            x[col] = sum / aug[col][col]
        }
        return x
    }

    // MARK: - Fallback bbox crop

    /// Simple padded bounding box crop (no alignment). Used when landmarks unavailable.
    private static func bboxCrop(from image: CGImage, bbox: FacesSubmitRequest.BoundingBox) -> CGImage? {
        let imgW = CGFloat(image.width)
        let imgH = CGFloat(image.height)

        let x1 = CGFloat(bbox.x1) * imgW
        let y1 = CGFloat(bbox.y1) * imgH
        let x2 = CGFloat(bbox.x2) * imgW
        let y2 = CGFloat(bbox.y2) * imgH

        let faceW = x2 - x1
        let faceH = y2 - y1
        let padX = faceW * 0.2
        let padY = faceH * 0.2

        let cropX = max(0, x1 - padX)
        let cropY = max(0, y1 - padY)
        let cropW = min(imgW - cropX, faceW + padX * 2)
        let cropH = min(imgH - cropY, faceH + padY * 2)

        return image.cropping(to: CGRect(x: cropX, y: cropY, width: cropW, height: cropH))
    }

    /// Create a CGImage from proxy image data.
    static func cgImage(from data: Data) -> CGImage? {
        guard let nsImage = NSImage(data: data),
              let cg = nsImage.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
            return nil
        }
        return cg
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
