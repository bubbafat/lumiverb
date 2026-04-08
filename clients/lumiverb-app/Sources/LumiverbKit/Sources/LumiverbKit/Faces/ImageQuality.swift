import Foundation
import CoreGraphics

/// Image quality helpers for face detection pipelines.
///
/// These live in LumiverbKit (rather than the macOS target) so they can be
/// unit-tested against fixture images in the Swift package test suite.
public enum ImageQuality {

    /// Compute the variance of the Laplacian over a grayscale crop of the image.
    ///
    /// Matches Python's `cv2.Laplacian(gray, cv2.CV_64F).var()` with the default
    /// 3×3 4-neighbor kernel `[0,1,0; 1,-4,1; 0,1,0]`. Higher values indicate
    /// sharper images; blurry / out-of-focus crops score low.
    ///
    /// The source RGB image is drawn into an 8-bit grayscale `CGContext`, which
    /// performs the RGB→luminance conversion. The Laplacian is then computed by
    /// a 4-neighbor finite-difference kernel over the interior pixels, and the
    /// variance of the response is returned in double precision.
    ///
    /// - Parameters:
    ///   - cgImage: Source image (typically the full proxy).
    ///   - bboxTopLeft: `(x1, y1, x2, y2)` as fractions 0.0–1.0 of the image,
    ///     using a top-left origin. The crop is computed from these bounds and
    ///     clamped to the image. Pass `(0, 0, 1, 1)` to score the whole image.
    /// - Returns: Variance of the Laplacian in double precision. Returns 0 if
    ///   the crop is smaller than the 3×3 kernel or the grayscale context
    ///   cannot be created.
    public static func laplacianVariance(
        of cgImage: CGImage,
        bboxTopLeft: (Float, Float, Float, Float) = (0, 0, 1, 1)
    ) -> Double {
        let (x1, y1, x2, y2) = bboxTopLeft
        let imgW = Double(cgImage.width)
        let imgH = Double(cgImage.height)

        // Convert bbox fractions to pixel coords; clamp to image bounds.
        let px1 = max(0, Int((Double(x1) * imgW).rounded()))
        let py1 = max(0, Int((Double(y1) * imgH).rounded()))
        let px2 = min(cgImage.width, Int((Double(x2) * imgW).rounded()))
        let py2 = min(cgImage.height, Int((Double(y2) * imgH).rounded()))
        let w = px2 - px1
        let h = py2 - py1

        // Need at least 3×3 for the kernel to produce any output.
        guard w >= 3, h >= 3 else { return 0 }

        // Draw the crop into an 8-bit grayscale CGContext. DeviceGray +
        // alphaNone does RGB→luminance internally (Rec. 709 weights).
        let colorSpace = CGColorSpaceCreateDeviceGray()
        guard let context = CGContext(
            data: nil,
            width: w,
            height: h,
            bitsPerComponent: 8,
            bytesPerRow: w,
            space: colorSpace,
            bitmapInfo: CGImageAlphaInfo.none.rawValue
        ) else { return 0 }

        // CGContext uses bottom-left origin; the caller's bbox uses top-left.
        // To draw only the cropped region at (0, 0) in the destination, we
        // draw the full source image at an offset such that (px1, py1) in
        // top-left coords lands at the destination origin. Because CGContext
        // is bottom-left, the y offset is `-(imgH - py2)`.
        let drawRect = CGRect(
            x: -CGFloat(px1),
            y: -CGFloat(cgImage.height - py2),
            width: CGFloat(cgImage.width),
            height: CGFloat(cgImage.height)
        )
        context.draw(cgImage, in: drawRect)

        guard let rawPtr = context.data else { return 0 }
        let pixels = rawPtr.bindMemory(to: UInt8.self, capacity: w * h)

        // 4-neighbor Laplacian: lap[x,y] = top + bottom + left + right − 4·center
        // Sum and sum-of-squares are accumulated inline so we compute variance
        // in one pass without allocating a separate Laplacian buffer.
        var sum: Double = 0
        var sumSq: Double = 0
        var count: Int = 0

        for y in 1..<(h - 1) {
            let rowBase = y * w
            let rowAbove = (y - 1) * w
            let rowBelow = (y + 1) * w
            for x in 1..<(w - 1) {
                let center = Double(pixels[rowBase + x])
                let top = Double(pixels[rowAbove + x])
                let bottom = Double(pixels[rowBelow + x])
                let left = Double(pixels[rowBase + x - 1])
                let right = Double(pixels[rowBase + x + 1])
                let lap = top + bottom + left + right - 4.0 * center
                sum += lap
                sumSq += lap * lap
                count += 1
            }
        }

        guard count > 0 else { return 0 }
        let mean = sum / Double(count)
        let variance = (sumSq / Double(count)) - (mean * mean)
        return max(0, variance)
    }
}
