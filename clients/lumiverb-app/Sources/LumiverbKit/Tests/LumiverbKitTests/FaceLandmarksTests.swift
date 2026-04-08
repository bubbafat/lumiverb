import XCTest
import Foundation
import Vision
import AppKit
@testable import LumiverbKit

/// Tests for `FaceLandmarks.extractAlignmentLandmarks`.
///
/// Runs real Apple Vision face detection (`VNDetectFaceLandmarksRequest`) on
/// the bundled fixture images, then verifies the extracted 5-point landmarks
/// are anatomically correct — eyes above nose, nose above mouth, left eye
/// left of right eye, left mouth corner left of right mouth corner — and
/// that they land inside the detected face bounding box.
///
/// We can't assert exact pixel coordinates because Apple Vision's model
/// output varies slightly between OS versions, but structural invariants
/// hold regardless.
final class FaceLandmarksTests: XCTestCase {

    private func loadFixtureCGImage(_ name: String) throws -> CGImage {
        guard let url = Bundle.module.url(
            forResource: name, withExtension: nil, subdirectory: "Fixtures"
        ) else {
            throw FixtureError.notFound(name)
        }
        let data = try Data(contentsOf: url)
        guard let nsImage = NSImage(data: data),
              let cg = nsImage.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
            throw FixtureError.unreadable
        }
        return cg
    }

    private func detectLandmarks(in cgImage: CGImage) throws -> [VNFaceObservation] {
        let request = VNDetectFaceLandmarksRequest()
        let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
        try handler.perform([request])
        return request.results ?? []
    }

    // MARK: - Happy path: returns 5 points

    func testReturnsFivePointsForSingleFace() throws {
        let img = try loadFixtureCGImage("face_single.jpg")
        let observations = try detectLandmarks(in: img)
        guard let first = observations.first else {
            XCTFail("No face detected in face_single.jpg")
            return
        }
        let points = FaceLandmarks.extractAlignmentLandmarks(
            from: first,
            imageWidth: Float(img.width),
            imageHeight: Float(img.height)
        )
        XCTAssertNotNil(points)
        XCTAssertEqual(points?.count, 5)
    }

    // MARK: - Anatomical invariants

    func testLandmarksAreInsideFaceBoundingBox() throws {
        let img = try loadFixtureCGImage("face_single.jpg")
        let observations = try detectLandmarks(in: img)
        guard let obs = observations.first,
              let points = FaceLandmarks.extractAlignmentLandmarks(
                from: obs,
                imageWidth: Float(img.width),
                imageHeight: Float(img.height)
              ) else {
            XCTFail("Expected detection and landmarks for face_single.jpg")
            return
        }

        // Vision's observation.boundingBox is in bottom-left normalized coords.
        // Convert to top-left pixel coords for comparison with our landmarks.
        let bbox = obs.boundingBox
        let imgW = CGFloat(img.width)
        let imgH = CGFloat(img.height)
        let bx1 = bbox.origin.x * imgW
        let bx2 = (bbox.origin.x + bbox.width) * imgW
        let by1 = (1.0 - bbox.origin.y - bbox.height) * imgH
        let by2 = (1.0 - bbox.origin.y) * imgH

        // Allow some slack outside the bbox — Vision's pupil landmark occasionally
        // lands just outside the rectangle for faces at angles.
        let slack: CGFloat = 10
        for (i, p) in points.enumerated() {
            XCTAssertGreaterThanOrEqual(p.x, bx1 - slack,
                "Landmark \(i) x=\(p.x) outside bbox [\(bx1), \(bx2)]")
            XCTAssertLessThanOrEqual(p.x, bx2 + slack,
                "Landmark \(i) x=\(p.x) outside bbox [\(bx1), \(bx2)]")
            XCTAssertGreaterThanOrEqual(p.y, by1 - slack,
                "Landmark \(i) y=\(p.y) outside bbox [\(by1), \(by2)]")
            XCTAssertLessThanOrEqual(p.y, by2 + slack,
                "Landmark \(i) y=\(p.y) outside bbox [\(by1), \(by2)]")
        }
    }

    func testLeftEyeIsLeftOfRightEye() throws {
        let img = try loadFixtureCGImage("face_single.jpg")
        let observations = try detectLandmarks(in: img)
        guard let obs = observations.first,
              let points = FaceLandmarks.extractAlignmentLandmarks(
                from: obs,
                imageWidth: Float(img.width),
                imageHeight: Float(img.height)
              ) else {
            XCTFail("Expected detection and landmarks")
            return
        }
        // ArcFace order: [leftEye, rightEye, nose, leftMouth, rightMouth].
        // "Left" / "right" are from the viewer's perspective in Vision.
        let leftEye = points[0]
        let rightEye = points[1]
        XCTAssertLessThan(leftEye.x, rightEye.x,
            "Left eye x=\(leftEye.x) should be less than right eye x=\(rightEye.x)")
    }

    func testEyesAreAboveNoseAboveMouth() throws {
        let img = try loadFixtureCGImage("face_single.jpg")
        let observations = try detectLandmarks(in: img)
        guard let obs = observations.first,
              let points = FaceLandmarks.extractAlignmentLandmarks(
                from: obs,
                imageWidth: Float(img.width),
                imageHeight: Float(img.height)
              ) else {
            XCTFail("Expected detection and landmarks")
            return
        }

        // In top-left pixel coords, "above" means smaller y.
        let leftEye = points[0]
        let rightEye = points[1]
        let nose = points[2]
        let leftMouth = points[3]
        let rightMouth = points[4]

        let eyeY = (leftEye.y + rightEye.y) / 2
        let mouthY = (leftMouth.y + rightMouth.y) / 2

        XCTAssertLessThan(eyeY, nose.y,
            "Eyes (y=\(eyeY)) should be above nose (y=\(nose.y))")
        XCTAssertLessThan(nose.y, mouthY,
            "Nose (y=\(nose.y)) should be above mouth (y=\(mouthY))")
    }

    func testLeftMouthCornerIsLeftOfRightMouthCorner() throws {
        let img = try loadFixtureCGImage("face_single.jpg")
        let observations = try detectLandmarks(in: img)
        guard let obs = observations.first,
              let points = FaceLandmarks.extractAlignmentLandmarks(
                from: obs,
                imageWidth: Float(img.width),
                imageHeight: Float(img.height)
              ) else {
            XCTFail("Expected detection and landmarks")
            return
        }
        let leftMouth = points[3]
        let rightMouth = points[4]
        XCTAssertLessThan(leftMouth.x, rightMouth.x,
            "Left mouth x=\(leftMouth.x) should be less than right mouth x=\(rightMouth.x)")
    }

    // MARK: - Nose tip is bottom of nose region, not centroid

    func testNoseLandmarkIsBelowEyes() throws {
        // The tip of the nose should be well below the eye line — this is the
        // property that was broken when the old implementation used the nose
        // centroid (which landed on the nose bridge, above the tip).
        let img = try loadFixtureCGImage("face_single.jpg")
        let observations = try detectLandmarks(in: img)
        guard let obs = observations.first,
              let points = FaceLandmarks.extractAlignmentLandmarks(
                from: obs,
                imageWidth: Float(img.width),
                imageHeight: Float(img.height)
              ) else {
            XCTFail("Expected detection and landmarks")
            return
        }

        let eyeY = (points[0].y + points[1].y) / 2
        let noseY = points[2].y
        let mouthY = (points[3].y + points[4].y) / 2

        // The nose tip should be roughly at or below the midpoint between
        // eyes and mouth. The old centroid version landed much higher (nose
        // bridge is ~25% of the way down from eyes to mouth).
        let eyeToMouth = mouthY - eyeY
        let noseOffsetFromEyes = noseY - eyeY
        let normalizedNoseDepth = noseOffsetFromEyes / eyeToMouth
        XCTAssertGreaterThan(normalizedNoseDepth, 0.40,
            "Nose tip should be at least 40% of the way from eyes to mouth "
            + "(centroid would be ~25%), got \(normalizedNoseDepth)")
    }

    // MARK: - Multi-face

    func testWorksOnGroupImage() throws {
        let img = try loadFixtureCGImage("face_group.jpg")
        let observations = try detectLandmarks(in: img)
        XCTAssertGreaterThanOrEqual(observations.count, 1)

        for obs in observations {
            let points = FaceLandmarks.extractAlignmentLandmarks(
                from: obs,
                imageWidth: Float(img.width),
                imageHeight: Float(img.height)
            )
            if let points {
                XCTAssertEqual(points.count, 5)
                // Same basic invariant: left eye left of right eye
                XCTAssertLessThan(points[0].x, points[1].x)
            }
        }
    }
}

private enum FixtureError: Error {
    case notFound(String)
    case unreadable
}
