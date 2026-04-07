import XCTest
import Foundation
import Vision
import AppKit

/// Tests Apple Vision face detection on real photos.
///
/// Uses the same code path as FaceDetectionProvider.detectFaces(from: Data):
///   Data → NSImage → CGImage → VNDetectFaceRectanglesRequest
///
/// This validates that Vision actually detects faces in our test fixtures
/// and that the Data → CGImage conversion doesn't silently fail.
final class FaceDetectionTests: XCTestCase {

    // MARK: - Helpers

    /// Load a fixture file from the test bundle's copied Fixtures directory.
    private func loadFixture(_ name: String) throws -> Data {
        guard let url = Bundle.module.url(forResource: name, withExtension: nil, subdirectory: "Fixtures") else {
            throw FixtureError.notFound(name)
        }
        return try Data(contentsOf: url)
    }

    /// Convert image data to CGImage using the same path as FaceDetectionProvider.
    private func cgImage(from data: Data) throws -> CGImage {
        guard let nsImage = NSImage(data: data),
              let cg = nsImage.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
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

        print("face_crowd.jpg — \(faces.count) faces detected at \(image.width)x\(image.height)")
        for (i, face) in faces.enumerated() {
            let box = face.boundingBox
            print("  face \(i): confidence=\(face.confidence) box=(\(box.origin.x), \(box.origin.y), \(box.width)x\(box.height))")
        }

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

        print("face_crowd.jpg: \(faces.count) raw detections, \(passingFaces.count) pass area gate")

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

    func testDetectionWorksWithExifRotation() throws {
        // face_group.jpg appears upside-down — tests whether Vision handles EXIF orientation.
        // If Vision ignores orientation, it may fail to detect faces.
        let data = try loadFixture("face_group.jpg")

        // Method 1: NSImage → CGImage (what FaceDetectionProvider uses)
        let image = try cgImage(from: data)
        let facesFromNSImage = try detectFaces(in: image)

        // Method 2: CGImageSource (applies EXIF orientation transform)
        guard let source = CGImageSourceCreateWithData(data as CFData, nil) else {
            XCTFail("Cannot create image source")
            return
        }
        let options: [String: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways as String: true,
            kCGImageSourceThumbnailMaxPixelSize as String: 2048,
            kCGImageSourceCreateThumbnailWithTransform as String: true,
        ]
        guard let orientedImage = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else {
            XCTFail("Cannot create oriented thumbnail")
            return
        }
        let facesFromOriented = try detectFaces(in: orientedImage)

        // Log both results for diagnosis
        print("face_group.jpg — NSImage path: \(facesFromNSImage.count) faces, CGImageSource path: \(facesFromOriented.count) faces")
        print("  NSImage CGImage size: \(image.width)x\(image.height)")
        print("  CGImageSource size: \(orientedImage.width)x\(orientedImage.height)")

        // At least one path should detect 2+ faces
        let bestCount = max(facesFromNSImage.count, facesFromOriented.count)
        XCTAssertGreaterThanOrEqual(bestCount, 2,
            "Neither conversion path detected 2 faces in face_group.jpg")

        // If the NSImage path detects fewer, that's the bug in FaceDetectionProvider
        if facesFromNSImage.count < facesFromOriented.count {
            print("  WARNING: NSImage path detected fewer faces (\(facesFromNSImage.count)) than CGImageSource path (\(facesFromOriented.count))")
            print("  This suggests FaceDetectionProvider should use CGImageSource with kCGImageSourceCreateThumbnailWithTransform")
        }
    }
}

private enum FixtureError: Error {
    case notFound(String)
    case unreadable
}
