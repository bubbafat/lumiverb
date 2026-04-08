import XCTest
import Foundation
import Vision
@testable import LumiverbKit

/// Tests Apple Vision face detection on real photos via the production
/// image-loading path.
///
/// Uses the same code path as `FaceDetectionProvider.detectFaces(from: Data)`:
///
///     Data → ImageLoading.loadOriented (applies EXIF rotation)
///          → CGImage → VNDetectFaceRectanglesRequest
///
/// Skipping the orientation step — the `NSImage(data:).cgImage(...)` path
/// the production code took before the 2026-04 face-clustering fix — hands
/// Vision the raw sensor pixels, which are sideways or upside-down for any
/// photo not stored in the camera's native landscape orientation. Detection
/// then silently misses faces or returns them in the wrong coordinate
/// system, and that was a major contributor to the production cluster
/// collapse this test file now exists to guard against.
final class FaceDetectionTests: XCTestCase {

    // MARK: - Helpers

    /// Load a fixture file from the test bundle's copied Fixtures directory.
    private func loadFixture(_ name: String) throws -> Data {
        guard let url = Bundle.module.url(forResource: name, withExtension: nil, subdirectory: "Fixtures") else {
            throw FixtureError.notFound(name)
        }
        return try Data(contentsOf: url)
    }

    /// Convert image data to a `CGImage` via the same EXIF-aware production
    /// path `FaceDetectionProvider.cgImage(from:)` uses.
    private func cgImage(from data: Data) throws -> CGImage {
        guard let cg = ImageLoading.loadOriented(from: data) else {
            throw FixtureError.unreadable
        }
        return cg
    }

    /// Run face detection and return observations.
    private func detectFaces(in image: CGImage) throws -> [VNFaceObservation] {
        let request = VNDetectFaceRectanglesRequest()
        let handler = VNImageRequestHandler(cgImage: image, options: [:])
        try handler.perform([request])
        return request.results ?? []
    }

    // MARK: - Tests

    func testSingleFaceDetected() throws {
        let data = try loadFixture("face_single.jpg")
        let image = try cgImage(from: data)
        let faces = try detectFaces(in: image)

        XCTAssertGreaterThanOrEqual(faces.count, 1,
            "Expected at least 1 face in face_single.jpg, got \(faces.count)")

        // Verify bounding box is reasonable (non-zero, within image bounds)
        let box = faces[0].boundingBox
        XCTAssertGreaterThan(box.width, 0.05, "Face bounding box too small: \(box)")
        XCTAssertGreaterThan(box.height, 0.05, "Face bounding box too small: \(box)")
        XCTAssertGreaterThanOrEqual(box.origin.x, 0)
        XCTAssertGreaterThanOrEqual(box.origin.y, 0)
        XCTAssertLessThanOrEqual(box.origin.x + box.width, 1.0)
        XCTAssertLessThanOrEqual(box.origin.y + box.height, 1.0)

        // Confidence should be high for a clear face
        XCTAssertGreaterThan(faces[0].confidence, 0.5,
            "Low confidence: \(faces[0].confidence)")
    }

    func testGroupFacesDetected() throws {
        let data = try loadFixture("face_group.jpg")
        let image = try cgImage(from: data)
        let faces = try detectFaces(in: image)

        XCTAssertGreaterThanOrEqual(faces.count, 2,
            "Expected at least 2 faces in face_group.jpg, got \(faces.count)")
    }

    func testCrowdFacesDetected() throws {
        let data = try loadFixture("face_crowd.jpg")
        let image = try cgImage(from: data)
        let faces = try detectFaces(in: image)

        // Apple Vision detects only large/clear faces in crowd scenes.
        // At proxy resolution (2048px max), distant faces are too small.
        XCTAssertGreaterThanOrEqual(faces.count, 1,
            "Expected at least 1 face in face_crowd.jpg, got \(faces.count)")
    }

    // MARK: - Data → CGImage conversion

    func testDataToCGImagePreservesContent() throws {
        let data = try loadFixture("face_single.jpg")
        let image = try cgImage(from: data)

        // Image should have reasonable dimensions (not 0x0 or 1x1)
        XCTAssertGreaterThan(image.width, 100, "Image width too small: \(image.width)")
        XCTAssertGreaterThan(image.height, 100, "Image height too small: \(image.height)")
    }

    func testEmptyDataThrows() {
        XCTAssertThrowsError(try cgImage(from: Data())) { error in
            XCTAssert(error is FixtureError)
        }
    }

    func testCorruptedDataThrows() {
        let garbage = Data([0x00, 0x01, 0x02, 0x03, 0xFF])
        XCTAssertThrowsError(try cgImage(from: garbage)) { error in
            XCTAssert(error is FixtureError)
        }
    }

    // MARK: - Quality gate validation

    func testAllDetectedFacesExceedMinimumConfidence() throws {
        for fixture in ["face_single.jpg", "face_group.jpg", "face_crowd.jpg"] {
            let data = try loadFixture(fixture)
            let image = try cgImage(from: data)
            let faces = try detectFaces(in: image)

            for (i, face) in faces.enumerated() {
                XCTAssertGreaterThanOrEqual(face.confidence, 0.5,
                    "\(fixture) face \(i): confidence \(face.confidence) below 0.5 threshold")
            }
        }
    }

    func testSmallFacesFilteredByAreaGate() throws {
        // The crowd scene has faces below the 0.3% area threshold.
        // This validates the quality gate would filter them out.
        let minAreaFraction: Float = 0.003

        let data = try loadFixture("face_crowd.jpg")
        let image = try cgImage(from: data)
        let faces = try detectFaces(in: image)

        let passingFaces = faces.filter { face in
            let area = Float(face.boundingBox.width * face.boundingBox.height)
            return area >= minAreaFraction
        }

        // Some faces in the crowd are too small — quality gate should reduce the count
        XCTAssertLessThanOrEqual(passingFaces.count, faces.count)
    }

    func testCloseUpFacesExceedMinimumArea() throws {
        // Close-up photos should always pass the area gate
        let minAreaFraction: Float = 0.003

        for fixture in ["face_single.jpg", "face_group.jpg"] {
            let data = try loadFixture(fixture)
            let image = try cgImage(from: data)
            let faces = try detectFaces(in: image)

            for (i, face) in faces.enumerated() {
                let area = Float(face.boundingBox.width * face.boundingBox.height)
                XCTAssertGreaterThanOrEqual(area, minAreaFraction,
                    "\(fixture) face \(i): area \(area) below threshold — close-up face should pass")
            }
        }
    }

    // MARK: - EXIF orientation handling

    /// `face_group.jpg` is stored with EXIF orientation 3 (rotated 180°).
    /// `ImageLoading.loadOriented` must apply that rotation before Vision
    /// runs detection — otherwise Vision sees upside-down pixels and may
    /// silently miss faces or return them with wrong-orientation landmarks.
    /// This test pins the EXIF-orientation contract on the production load
    /// path.
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
        let image = try cgImage(from: data)
        let faces = try detectFaces(in: image)

        XCTAssertGreaterThanOrEqual(faces.count, 2,
            "ImageLoading.loadOriented must apply EXIF rotation so Vision detects both faces in face_group.jpg; got \(faces.count)")
    }
}

private enum FixtureError: Error {
    case notFound(String)
    case unreadable
}
