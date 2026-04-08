import Foundation
import CoreGraphics
import Vision

/// Landmark extraction for ArcFace alignment.
///
/// ArcFace was trained on 5-point landmarks — left eye, right eye, nose tip,
/// left mouth corner, right mouth corner — aligned to a canonical `arcface_dst`
/// template at 112×112. Apple Vision's `VNDetectFaceLandmarksRequest` produces
/// *regions* (multi-point contours) rather than single anatomical points, so
/// this helper derives the 5 ArcFace points from those regions as precisely as
/// possible. Misaligned input here measurably degrades embedding quality.
public enum FaceLandmarks {

    /// Extract the 5 ArcFace alignment landmarks from a Vision face observation.
    ///
    /// Returns pixel coordinates in **top-left origin**, in the order ArcFace
    /// expects: `[leftEye, rightEye, noseTip, leftMouthCorner, rightMouthCorner]`.
    ///
    /// Point selection:
    /// - **Eyes** — prefers single-point `leftPupil` / `rightPupil` landmarks
    ///   when available; falls back to the centroid of the eye contour region.
    ///   Pupil centers are exactly the landmark ArcFace was trained against.
    /// - **Nose tip** — the *bottommost* point of the nose crest region. Vision's
    ///   `nose` region runs from the bridge (between the eyes) downward to the
    ///   tip, so in bottom-left normalized coordinates the tip is the minimum
    ///   y. The centroid of this region would land on the nose bridge, shifting
    ///   every warped crop vertically.
    /// - **Mouth corners** — the leftmost-x and rightmost-x points of the
    ///   `outerLips` contour (falls back to `innerLips` if absent). These are
    ///   the anatomical corners regardless of the point order Vision uses.
    ///   Selecting by index — e.g. "first point is left, midpoint is right" —
    ///   is unreliable because Vision's traversal order is not guaranteed.
    ///
    /// - Parameters:
    ///   - observation: A face observation produced by
    ///     `VNDetectFaceLandmarksRequest`. The observation must have
    ///     `.landmarks != nil`, otherwise this returns `nil`.
    ///   - imageWidth: Pixel width of the source image the observation was run on.
    ///   - imageHeight: Pixel height of the source image the observation was run on.
    /// - Returns: 5 pixel-coordinate points in top-left origin, or `nil` if any
    ///   required landmark region is missing.
    public static func extractAlignmentLandmarks(
        from observation: VNFaceObservation,
        imageWidth: Float,
        imageHeight: Float
    ) -> [CGPoint]? {
        guard let landmarks = observation.landmarks else { return nil }
        let bbox = observation.boundingBox

        // Convert a Vision landmark point (normalized to the face bbox,
        // bottom-left origin) to pixel coords in top-left origin.
        func toPixel(_ point: CGPoint) -> CGPoint {
            let px = (bbox.origin.x + point.x * bbox.width) * CGFloat(imageWidth)
            let py = (1.0 - (bbox.origin.y + point.y * bbox.height)) * CGFloat(imageHeight)
            return CGPoint(x: px, y: py)
        }

        // Single-point eye landmark: pupil if Vision provides it (single point),
        // otherwise the centroid of the full eye contour.
        func eyePoint(
            pupil: VNFaceLandmarkRegion2D?,
            region: VNFaceLandmarkRegion2D?
        ) -> CGPoint? {
            if let pupil, pupil.pointCount > 0 {
                return toPixel(pupil.normalizedPoints[0])
            }
            guard let region, region.pointCount > 0 else { return nil }
            let points = region.normalizedPoints
            let cx = points.map(\.x).reduce(0, +) / CGFloat(points.count)
            let cy = points.map(\.y).reduce(0, +) / CGFloat(points.count)
            return toPixel(CGPoint(x: cx, y: cy))
        }

        guard let leftEye = eyePoint(pupil: landmarks.leftPupil, region: landmarks.leftEye),
              let rightEye = eyePoint(pupil: landmarks.rightPupil, region: landmarks.rightEye) else {
            return nil
        }

        // Nose tip: bottommost point of the nose crest. Vision's bottom-left
        // origin means the minimum y is the anatomically-lowest (most chin-ward)
        // point of the contour.
        guard let noseRegion = landmarks.nose, noseRegion.pointCount > 0,
              let tip = noseRegion.normalizedPoints.min(by: { $0.y < $1.y }) else {
            return nil
        }
        let noseTip = toPixel(tip)

        // Mouth corners: leftmost-x and rightmost-x points of the contour.
        func mouthCorners(_ region: VNFaceLandmarkRegion2D?) -> (CGPoint, CGPoint)? {
            guard let region, region.pointCount >= 2 else { return nil }
            let pts = region.normalizedPoints
            guard let left = pts.min(by: { $0.x < $1.x }),
                  let right = pts.max(by: { $0.x < $1.x }) else { return nil }
            return (toPixel(left), toPixel(right))
        }

        guard let (leftMouth, rightMouth) = mouthCorners(landmarks.outerLips)
                ?? mouthCorners(landmarks.innerLips) else {
            return nil
        }

        return [leftEye, rightEye, noseTip, leftMouth, rightMouth]
    }
}
