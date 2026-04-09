import XCTest
import Foundation
@testable import LumiverbKit

/// Tests `FaceAlignment.estimateSimilarityTransform`, the least-squares
/// solver that drives ArcFace face alignment.
///
/// Validates that the Swift solver produces coefficients matching Python's
/// scikit-image `SimilarityTransform.estimate()` so Mac-generated face
/// embeddings stay compatible with InsightFace Python embeddings.
///
/// **History note:** this file used to re-implement the math inline because
/// the production code lived as a private function inside the macOS-target
/// `FaceDetectionProvider` and couldn't be imported. The math has since
/// moved into `LumiverbKit.FaceAlignment` (alongside `FaceLandmarks`) and
/// these tests now exercise the real production code.
final class SimilarityTransformTests: XCTestCase {

    // ArcFace canonical destination landmarks (112x112)
    private let arcfaceDst: [(Double, Double)] = [
        (38.2946, 51.6963),  // left eye
        (73.5318, 51.5014),  // right eye
        (56.0252, 71.7366),  // nose
        (41.5493, 92.3655),  // left mouth
        (70.7299, 92.2041),  // right mouth
    ]

    // Realistic test landmarks (pixels in a 1280x960 image)
    private let testSrcLandmarks: [(Double, Double)] = [
        (450.0, 320.0),   // left eye
        (550.0, 315.0),   // right eye
        (505.0, 380.0),   // nose
        (460.0, 430.0),   // left mouth
        (540.0, 425.0),   // right mouth
    ]

    // Expected values from Python's SimilarityTransform.estimate()
    // a = scale * cos(angle), b = scale * sin(angle)
    private let expectedA = 0.3631286395
    private let expectedB = 0.0058423707
    private let expectedTx = -123.716242
    private let expectedTy = -66.836358

    // MARK: - Tests

    func testCoefficientsMatchPython() {
        let (a, b, tx, ty) = FaceAlignment.estimateSimilarityTransform(
            src: testSrcLandmarks, dst: arcfaceDst
        )

        XCTAssertEqual(a, expectedA, accuracy: 1e-6,
            "a coefficient doesn't match Python: got \(a), expected \(expectedA)")
        XCTAssertEqual(b, expectedB, accuracy: 1e-6,
            "b coefficient doesn't match Python: got \(b), expected \(expectedB)")
        XCTAssertEqual(tx, expectedTx, accuracy: 1e-3,
            "tx doesn't match Python: got \(tx), expected \(expectedTx)")
        XCTAssertEqual(ty, expectedTy, accuracy: 1e-3,
            "ty doesn't match Python: got \(ty), expected \(expectedTy)")
    }

    func testTransformedPointsMatchPython() {
        let (a, b, tx, ty) = FaceAlignment.estimateSimilarityTransform(
            src: testSrcLandmarks, dst: arcfaceDst
        )

        // Expected transformed points from Python (verified against skimage)
        let expectedDst = [
            (37.8221, 51.9939),
            (74.1642, 50.7625),
            (57.4436, 74.1029),
            (40.8107, 91.9964),
            (69.8902, 90.6482),
        ]

        for i in 0..<5 {
            let sx = testSrcLandmarks[i].0
            let sy = testSrcLandmarks[i].1
            let dx = a * sx - b * sy + tx
            let dy = b * sx + a * sy + ty

            XCTAssertEqual(dx, expectedDst[i].0, accuracy: 0.01,
                "Point \(i) x: got \(dx), expected \(expectedDst[i].0)")
            XCTAssertEqual(dy, expectedDst[i].1, accuracy: 0.01,
                "Point \(i) y: got \(dy), expected \(expectedDst[i].1)")
        }
    }

    func testIdentityTransformWhenSrcEqualsDst() {
        let points: [(Double, Double)] = [
            (38.2946, 51.6963),
            (73.5318, 51.5014),
            (56.0252, 71.7366),
            (41.5493, 92.3655),
            (70.7299, 92.2041),
        ]

        let (a, b, tx, ty) = FaceAlignment.estimateSimilarityTransform(src: points, dst: points)

        // Should be identity: a=1, b=0, tx=0, ty=0
        XCTAssertEqual(a, 1.0, accuracy: 1e-6)
        XCTAssertEqual(b, 0.0, accuracy: 1e-6)
        XCTAssertEqual(tx, 0.0, accuracy: 1e-3)
        XCTAssertEqual(ty, 0.0, accuracy: 1e-3)
    }

    func testPureTranslation() {
        let src: [(Double, Double)] = [(0, 0), (10, 0), (5, 8), (2, 12), (8, 12)]
        let dst = src.map { ($0.0 + 50, $0.1 + 30) }

        let (a, b, tx, ty) = FaceAlignment.estimateSimilarityTransform(src: src, dst: dst)

        XCTAssertEqual(a, 1.0, accuracy: 1e-6, "Expected no rotation/scale")
        XCTAssertEqual(b, 0.0, accuracy: 1e-6, "Expected no rotation")
        XCTAssertEqual(tx, 50.0, accuracy: 1e-3)
        XCTAssertEqual(ty, 30.0, accuracy: 1e-3)
    }

    func testPureScale() {
        let src: [(Double, Double)] = [(0, 0), (10, 0), (5, 8), (2, 12), (8, 12)]
        let scale = 2.5
        let dst = src.map { ($0.0 * scale, $0.1 * scale) }

        let (a, b, tx, ty) = FaceAlignment.estimateSimilarityTransform(src: src, dst: dst)

        XCTAssertEqual(a, scale, accuracy: 1e-6)
        XCTAssertEqual(b, 0.0, accuracy: 1e-6)
        XCTAssertEqual(tx, 0.0, accuracy: 1e-3)
        XCTAssertEqual(ty, 0.0, accuracy: 1e-3)
    }

    func testMaxTransformError() {
        // The best-fit transform won't map all 5 points exactly.
        // Verify the maximum error is within a reasonable bound (< 3px for 112x112 output).
        let (a, b, tx, ty) = FaceAlignment.estimateSimilarityTransform(
            src: testSrcLandmarks, dst: arcfaceDst
        )

        var maxErr = 0.0
        for i in 0..<5 {
            let sx = testSrcLandmarks[i].0
            let sy = testSrcLandmarks[i].1
            let dx = a * sx - b * sy + tx
            let dy = b * sx + a * sy + ty
            let err = sqrt(pow(dx - arcfaceDst[i].0, 2) + pow(dy - arcfaceDst[i].1, 2))
            maxErr = max(maxErr, err)
        }

        XCTAssertLessThan(maxErr, 3.0,
            "Maximum transform error \(maxErr)px is too high for reliable face alignment")
    }
}
