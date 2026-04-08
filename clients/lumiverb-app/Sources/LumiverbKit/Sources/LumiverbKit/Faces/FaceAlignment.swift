import Foundation
import CoreGraphics

/// Aligns a detected face to ArcFace's canonical 112×112 template.
///
/// ArcFace was trained on faces warped via a similarity transform that maps
/// 5 facial landmarks (eyes, nose tip, mouth corners) to a fixed canonical
/// position — InsightFace calls this template `arcface_dst`. Skipping or
/// botching this alignment is the difference between embeddings that
/// distinguish identities and embeddings that look valid but contain no
/// identity signal.
///
/// This module is the production alignment math used by both the macOS
/// enrichment runtime and the LumiverbKit test suite. It used to live as
/// private functions inside the macOS-target `FaceDetectionProvider`, where
/// it could only be tested by re-implementing the math in a parallel test
/// file — exactly the kind of duplication that hides bugs.
public enum FaceAlignment {

    /// InsightFace's canonical ArcFace destination landmarks for 112×112,
    /// in `[leftEye, rightEye, noseTip, leftMouth, rightMouth]` order.
    ///
    /// These constants are copied from InsightFace's `face_align.py`
    /// `arcface_dst` matrix and must not be edited — they define the
    /// coordinate space the network was trained against.
    public static let arcfaceDst: [(CGFloat, CGFloat)] = [
        (38.2946, 51.6963),  // left eye
        (73.5318, 51.5014),  // right eye
        (56.0252, 71.7366),  // nose tip
        (41.5493, 92.3655),  // left mouth corner
        (70.7299, 92.2041),  // right mouth corner
    ]

    /// Warp a face image to ArcFace's canonical 112×112 template.
    ///
    /// Computes a similarity transform from the source landmarks to
    /// `arcfaceDst`, then iterates over the destination pixels and bilinearly
    /// samples the source at the inverse-transformed coordinates. Result is
    /// suitable for direct ArcFace inference.
    ///
    /// **Why manual pixel iteration instead of `CGContext` + affine CTM?**
    /// The earlier implementation set up a `CGContext` with `translateBy` +
    /// `scaleBy` + `concatenate(inverse_transform)` and called `context.draw`.
    /// That approach has at least two pitfalls — the inverse `CGAffineTransform`
    /// construction is easy to get wrong (sign / row-vs-column convention
    /// mismatches between the math and `CGAffineTransform.b/c`), and the y-flip
    /// composition with the warp interacts in non-obvious ways with CG's
    /// bottom-left coordinate system. The previous code shipped with both
    /// flaws and produced an entirely empty 112×112 output for every input —
    /// the source image was being drawn at coordinates that did not intersect
    /// the destination buffer. Worst part: the unit tests for
    /// `estimateSimilarityTransform` covered the math at the (a, b, tx, ty)
    /// tuple level, so the bugs were entirely in the matrix-to-`CGAffineTransform`
    /// adapter and the CTM dance — invisible to existing coverage.
    ///
    /// Manual iteration avoids the CG coordinate-system surface area entirely:
    /// we work in plain top-left pixel coordinates throughout, the math is
    /// directly verifiable from the equations in this file, and the output is
    /// independently testable with synthetic inputs.
    ///
    /// - Parameters:
    ///   - image: Source image (full frame, not pre-cropped).
    ///   - landmarks: 5 source landmarks in pixel/top-left coordinates,
    ///     in `[leftEye, rightEye, noseTip, leftMouth, rightMouth]` order
    ///     (the same order `FaceLandmarks.extractAlignmentLandmarks` returns).
    /// - Returns: A 112×112 RGBA `CGImage` with the face aligned to the
    ///   canonical template, or `nil` on math/context failure.
    public static func alignedCrop(from image: CGImage, landmarks: [CGPoint]) -> CGImage? {
        guard landmarks.count == 5 else { return nil }

        // Forward transform F: source(top-left) → dst(top-left).
        //   dx = a*sx - b*sy + tx
        //   dy = b*sx + a*sy + ty
        let (a, b, tx, ty) = estimateSimilarityTransform(
            src: landmarks.map { (Double($0.x), Double($0.y)) },
            dst: arcfaceDst.map { (Double($0.0), Double($0.1)) }
        )

        // Inverse F⁻¹: dst(top-left) → source(top-left).
        //   sx =  ia*(dx - tx) + ib*(dy - ty)
        //   sy = -ib*(dx - tx) + ia*(dy - ty)
        // where det = a² + b², ia = a/det, ib = b/det.
        let det = a * a + b * b
        guard det > 1e-12 else { return nil }
        let ia = a / det
        let ib = b / det

        // Materialize the source image into an RGBA byte buffer. Empirically,
        // when you draw an image into a fresh `CGContext` with no CTM
        // modifications, the resulting memory layout is **top-down**: memory
        // row 0 contains the visual top scanline of the image, memory row
        // (height - 1) contains the visual bottom. CG's bottom-left "device
        // coordinate system" only governs *user-space drawing operations* —
        // the underlying byte order is always top-down for CGContext-rendered
        // bitmaps, contrary to what the bottom-left convention superficially
        // implies. (Verified by sampling memory row 0 of a beach selfie and
        // getting sky-blue pixels; row 0 was the top, not the bottom.)
        //
        // This means top-left logical coordinate `(x, y)` maps directly to
        // memory at offset `(y * bytesPerRow + x * 4)` — no flipping.
        let srcW = image.width
        let srcH = image.height
        let srcBytesPerRow = srcW * 4
        var srcBuf = [UInt8](repeating: 0, count: srcH * srcBytesPerRow)
        let colorspace = CGColorSpaceCreateDeviceRGB()
        let alphaInfo = CGImageAlphaInfo.premultipliedLast.rawValue

        let drawn: Bool = srcBuf.withUnsafeMutableBytes { rawBuf -> Bool in
            guard let base = rawBuf.baseAddress,
                  let ctx = CGContext(
                      data: base,
                      width: srcW,
                      height: srcH,
                      bitsPerComponent: 8,
                      bytesPerRow: srcBytesPerRow,
                      space: colorspace,
                      bitmapInfo: alphaInfo
                  )
            else { return false }
            // No CTM games — just draw the image at its native rect. The
            // resulting bottom-up memory layout is what CG and the PNG
            // encoder both expect.
            ctx.draw(image, in: CGRect(x: 0, y: 0, width: srcW, height: srcH))
            return true
        }
        guard drawn else { return nil }

        // Destination buffer in top-down memory order, matching the source.
        let dstSize = 112
        let dstBytesPerRow = dstSize * 4
        var dstBuf = [UInt8](repeating: 0, count: dstSize * dstBytesPerRow)

        // For each destination pixel (in top-left logical coords), compute
        // the source coordinate via F⁻¹ and bilinearly sample the source.
        // ArcFace's reference InsightFace implementation uses bilinear via
        // OpenCV's `cv2.warpAffine` default, so this matches.
        for dy in 0..<dstSize {
            for dx in 0..<dstSize {
                let dxd = Double(dx) - tx
                let dyd = Double(dy) - ty
                let sxF = ia * dxd + ib * dyd
                let syF = -ib * dxd + ia * dyd

                // Memory is top-down: dy maps directly to memory row.
                let dstOffset = dy * dstBytesPerRow + dx * 4

                // Bilinear sample. If any of the four neighbors fall outside
                // the source bounds, treat that contribution as zero (black
                // padding) — matches OpenCV's `BORDER_CONSTANT` default.
                let x0 = Int(floor(sxF))
                let y0 = Int(floor(syF))
                let x1 = x0 + 1
                let y1 = y0 + 1
                let fx = sxF - Double(x0)
                let fy = syF - Double(y0)

                func sample(_ x: Int, _ y: Int) -> (Double, Double, Double) {
                    guard x >= 0, x < srcW, y >= 0, y < srcH else { return (0, 0, 0) }
                    // Memory is top-down: source y maps directly to memory row.
                    let off = y * srcBytesPerRow + x * 4
                    return (Double(srcBuf[off + 0]), Double(srcBuf[off + 1]), Double(srcBuf[off + 2]))
                }

                let p00 = sample(x0, y0)
                let p10 = sample(x1, y0)
                let p01 = sample(x0, y1)
                let p11 = sample(x1, y1)

                let w00 = (1 - fx) * (1 - fy)
                let w10 = fx       * (1 - fy)
                let w01 = (1 - fx) * fy
                let w11 = fx       * fy

                let r = p00.0 * w00 + p10.0 * w10 + p01.0 * w01 + p11.0 * w11
                let g = p00.1 * w00 + p10.1 * w10 + p01.1 * w01 + p11.1 * w11
                let bC = p00.2 * w00 + p10.2 * w10 + p01.2 * w01 + p11.2 * w11

                dstBuf[dstOffset + 0] = UInt8(max(0, min(255, r.rounded())))
                dstBuf[dstOffset + 1] = UInt8(max(0, min(255, g.rounded())))
                dstBuf[dstOffset + 2] = UInt8(max(0, min(255, bC.rounded())))
                dstBuf[dstOffset + 3] = 255
            }
        }

        // Wrap the destination buffer in a CGImage via a CGDataProvider so the
        // top-left memory layout is preserved end to end (no implicit y-flip
        // when constructing the image).
        let providerData = Data(dstBuf)
        guard let provider = CGDataProvider(data: providerData as CFData) else { return nil }
        return CGImage(
            width: dstSize,
            height: dstSize,
            bitsPerComponent: 8,
            bitsPerPixel: 32,
            bytesPerRow: dstBytesPerRow,
            space: colorspace,
            bitmapInfo: CGBitmapInfo(rawValue: alphaInfo),
            provider: provider,
            decode: nil,
            shouldInterpolate: false,
            intent: .defaultIntent
        )
    }

    /// Padded bounding-box crop, used as a fallback when landmarks are
    /// unavailable. Produces a non-aligned face crop — the resulting
    /// embedding quality is significantly worse than the aligned path,
    /// so callers should treat this as best-effort only.
    public static func bboxCrop(
        from image: CGImage,
        x1: Float, y1: Float, x2: Float, y2: Float
    ) -> CGImage? {
        let imgW = CGFloat(image.width)
        let imgH = CGFloat(image.height)

        let px1 = CGFloat(x1) * imgW
        let py1 = CGFloat(y1) * imgH
        let px2 = CGFloat(x2) * imgW
        let py2 = CGFloat(y2) * imgH

        let faceW = px2 - px1
        let faceH = py2 - py1
        let padX = faceW * 0.2
        let padY = faceH * 0.2

        let cropX = max(0, px1 - padX)
        let cropY = max(0, py1 - padY)
        let cropW = min(imgW - cropX, faceW + padX * 2)
        let cropH = min(imgH - cropY, faceH + padY * 2)

        return image.cropping(to: CGRect(x: cropX, y: cropY, width: cropW, height: cropH))
    }

    /// Compute the similarity transform coefficients (a, b, tx, ty) from src → dst points.
    ///
    /// The transform maps: `dst_x = a*src_x - b*src_y + tx`,  `dst_y = b*src_x + a*src_y + ty`.
    /// Uses a least-squares fit matching scikit-image's `SimilarityTransform.estimate()`.
    public static func estimateSimilarityTransform(
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

        let x = solve4x4(ata, atb)
        return (a: x[0], b: x[1], tx: x[2], ty: x[3])
    }

    /// Solve a 4×4 linear system Ax = b via Gaussian elimination with partial pivoting.
    private static func solve4x4(_ A: [[Double]], _ b: [Double]) -> [Double] {
        var aug = A.enumerated().map { (i, row) in row + [b[i]] }

        for col in 0..<4 {
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
}
