import XCTest
import Foundation
import Vision
@testable import LumiverbKit

/// End-to-end tests for the production face-detection pipeline.
///
/// These tests call `FaceDetectionProvider.detectFaces(from:)` directly —
/// the exact same entry point the macOS enrichment pipeline invokes — so
/// they exercise the whole gate chain (confidence, bbox area, face pixel
/// width, Vision capture quality, relative size) on real fixture images.
///
/// History: an earlier version of this file re-implemented detection via
/// a private `VNDetectFaceRectanglesRequest` helper, bypassing every
/// gate `FaceDetectionProvider` applies. That is exactly the pattern the
/// `feedback_test_production_not_copies` memory warns against: the
/// helper-only tests still passed while production was dropping faces
/// the helper-only path was keeping (DSC05984.ARW — smooth-skinned
/// subject killed by the Laplacian gate in 2026-04). The provider has
/// since been moved into LumiverbKit so tests can call it directly.
final class FaceDetectionTests: XCTestCase {

    // MARK: - Helpers

    private func loadFixture(_ name: String) throws -> Data {
        guard let url = Bundle.module.url(
            forResource: name, withExtension: nil, subdirectory: "Fixtures"
        ) else {
            throw FixtureError.notFound(name)
        }
        return try Data(contentsOf: url)
    }

    // MARK: - End-to-end production pipeline

    func testSingleFaceDetected() throws {
        let data = try loadFixture("face_single.jpg")
        let faces = try FaceDetectionProvider.detectFaces(from: data)

        XCTAssertEqual(faces.count, 1,
            "Expected exactly 1 face from face_single.jpg (full production gate chain), got \(faces.count)")

        let face = faces[0]
        XCTAssertGreaterThanOrEqual(face.confidence, FaceDetectionProvider.minDetectionConfidence)
        XCTAssertNotNil(face.landmarks, "Single-face fixture should produce 5 alignment landmarks")
        XCTAssertEqual(face.landmarks?.count, 5)

        // Bounding box should be well-formed and inside the image.
        let bb = face.boundingBox
        XCTAssertGreaterThan(bb.x2 - bb.x1, 0)
        XCTAssertGreaterThan(bb.y2 - bb.y1, 0)
        XCTAssertGreaterThanOrEqual(bb.x1, 0); XCTAssertLessThanOrEqual(bb.x2, 1)
        XCTAssertGreaterThanOrEqual(bb.y1, 0); XCTAssertLessThanOrEqual(bb.y2, 1)
    }

    func testGroupFacesDetected() throws {
        let data = try loadFixture("face_group.jpg")
        let faces = try FaceDetectionProvider.detectFaces(from: data)

        XCTAssertGreaterThanOrEqual(faces.count, 2,
            "Expected at least 2 faces from face_group.jpg (full production gate chain), got \(faces.count)")
    }

    func testCrowdSceneDoesNotCrash() throws {
        // Smoke test: the gate chain must handle a complex multi-face
        // scene without throwing.
        //
        // We deliberately do NOT assert on a specific face count here.
        // `face_crowd.jpg` has every subject sitting right around the
        // Vision `faceCaptureQuality` gate (measured scores 0.17–0.40),
        // and Vision's scorer is *not* deterministic across runs —
        // empirically the same subject can score 0.383 in one process
        // and 0.404 in another, apparently because Vision dispatches
        // across CPU/GPU/ANE based on system load and the scorer is
        // sensitive to the backend. Asserting a specific count on this
        // fixture produces a test that passes in isolation and flakes
        // under the full suite. The close-subject fixtures
        // (`face_single`, `face_group`) score 0.365+ and 0.572+, well
        // above the gate, and are the reliable coverage for "happy
        // path yields faces." This test's job is just to confirm the
        // pipeline *completes* on a noisy input.
        let data = try loadFixture("face_crowd.jpg")
        _ = try FaceDetectionProvider.detectFaces(from: data)
    }

    // MARK: - Regression: smooth-skin false negative (DSC05984 class)

    /// Guard against re-adding a Laplacian-variance (or similar texture-as-
    /// sharpness) gate. `face_single.jpg` produces a single, sharp, frontal
    /// face that the production pipeline *must* emit — if a future change
    /// silently drops it, the full-chain assertion above catches it. We add
    /// this separate, explicitly-named test as a tripwire so the failure
    /// message points at the right historical context instead of a generic
    /// "face count 0 ≠ 1" at the top of the file.
    ///
    /// See the ``FaceDetectionProvider`` docstring for the DSC05984 case
    /// (smooth-skinned subject dropped by a bbox-scoped Laplacian variance
    /// gate because bbox-scoped variance is a texture signal, not a
    /// sharpness signal, on faces).
    func testSharpFaceSurvivesGateChain_doNotReAddLaplacian() throws {
        let data = try loadFixture("face_single.jpg")
        let faces = try FaceDetectionProvider.detectFaces(from: data)
        XCTAssertFalse(faces.isEmpty,
            """
            Production face detection returned zero faces on face_single.jpg.
            If you just re-introduced a Laplacian-variance (or similar
            bbox-scoped 'sharpness' on raw pixel variance) gate, DELETE IT
            and read the FaceDetectionProvider docstring for why that class
            of gate is a texture-detector, not a sharpness-detector, and
            systematically kills smooth-skinned subjects (DSC05984.ARW —
            Vision confidence 1.000, capture quality 0.612, Laplacian 10.45).
            """)
    }

    // MARK: - Gate-chain invariants

    func testAllDetectedFacesPassAllNumericGates() throws {
        for fixture in ["face_single.jpg", "face_group.jpg", "face_crowd.jpg"] {
            let data = try loadFixture(fixture)
            let faces = try FaceDetectionProvider.detectFaces(from: data)

            // Any face the pipeline emits must have cleared every numeric
            // gate. These are invariants of the public API — if one fails,
            // either the gate was bypassed or the threshold constants are
            // out of sync with the implementation.
            guard let cg = ImageLoading.loadOriented(from: data) else {
                XCTFail("\(fixture): could not decode"); continue
            }
            let imgW = Float(cg.width)

            for (i, face) in faces.enumerated() {
                XCTAssertGreaterThanOrEqual(face.confidence,
                    FaceDetectionProvider.minDetectionConfidence,
                    "\(fixture) face \(i): confidence below gate")

                let bw = face.boundingBox.x2 - face.boundingBox.x1
                let bh = face.boundingBox.y2 - face.boundingBox.y1
                XCTAssertGreaterThanOrEqual(bw * bh,
                    FaceDetectionProvider.minBboxAreaFraction,
                    "\(fixture) face \(i): area fraction below gate")

                XCTAssertGreaterThanOrEqual(bw * imgW,
                    FaceDetectionProvider.minFacePixels,
                    "\(fixture) face \(i): pixel width below gate")
            }

            // Relative-size gate: every face is ≥ 15% of the largest.
            if let largest = faces.map({ f -> Float in
                let bw = f.boundingBox.x2 - f.boundingBox.x1
                let bh = f.boundingBox.y2 - f.boundingBox.y1
                return bw * bh
            }).max() {
                for (i, face) in faces.enumerated() {
                    let bw = face.boundingBox.x2 - face.boundingBox.x1
                    let bh = face.boundingBox.y2 - face.boundingBox.y1
                    XCTAssertGreaterThanOrEqual(bw * bh,
                        largest * FaceDetectionProvider.minRelativeSize,
                        "\(fixture) face \(i): relative size below gate")
                }
            }
        }
    }

    // MARK: - Input handling

    func testEmptyDataThrows() {
        XCTAssertThrowsError(try FaceDetectionProvider.detectFaces(from: Data())) { error in
            XCTAssert(error is FaceDetectionError,
                      "Expected FaceDetectionError, got \(type(of: error))")
        }
    }

    func testCorruptedDataThrows() {
        let garbage = Data([0x00, 0x01, 0x02, 0x03, 0xFF])
        XCTAssertThrowsError(try FaceDetectionProvider.detectFaces(from: garbage)) { error in
            XCTAssert(error is FaceDetectionError)
        }
    }

    func testCgImageHelperReturnsNilOnGarbage() {
        XCTAssertNil(FaceDetectionProvider.cgImage(from: Data()))
        XCTAssertNil(FaceDetectionProvider.cgImage(from: Data([0xDE, 0xAD, 0xBE, 0xEF])))
    }

    // MARK: - EXIF orientation handling

    /// `face_group.jpg` is stored with EXIF orientation 3 (rotated 180°).
    /// `ImageLoading.loadOriented` must apply that rotation before Vision
    /// runs detection — otherwise Vision sees upside-down pixels and may
    /// silently miss faces or return them with wrong-orientation landmarks.
    ///
    /// History: an earlier version of this test ran two parallel loaders
    /// (`NSImage(data:).cgImage(...)` vs `CGImageSourceCreateThumbnailAtIndex`
    /// with `kCGImageSourceCreateThumbnailWithTransform`) and `print`-ed a
    /// `WARNING:` if the two disagreed, but the assertion was `max(...)`
    /// across both — so the test passed even when the production path was
    /// broken. The warning was a sticky note nobody read; the bug it
    /// flagged stayed in production for months and contributed to the
    /// 2026-04 face-clustering catastrophe. The test now asserts the
    /// production path produces the right result, full stop.
    func testDetectionWorksWithExifRotation() throws {
        let data = try loadFixture("face_group.jpg")
        let faces = try FaceDetectionProvider.detectFaces(from: data)

        XCTAssertGreaterThanOrEqual(faces.count, 2,
            "EXIF rotation must be applied before detection — got \(faces.count) faces on face_group.jpg")
    }
}

private enum FixtureError: Error {
    case notFound(String)
    case unreadable
}
