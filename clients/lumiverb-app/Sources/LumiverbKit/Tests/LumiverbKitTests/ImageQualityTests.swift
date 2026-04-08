import XCTest
import CoreGraphics
import AppKit
@testable import LumiverbKit

/// Tests for `ImageQuality.laplacianVariance`. Uses the face fixture images to
/// verify realistic sharpness scores, then validates behavior on synthetic
/// inputs where the expected variance is known analytically.
final class ImageQualityTests: XCTestCase {

    // MARK: - Fixture helpers

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

    // MARK: - Real image sharpness

    func testSharpFixtureProducesHighVariance() throws {
        // The single-face fixture is a sharp photo — variance should be well
        // above the 15.0 rejection threshold (Python has logged values in the
        // hundreds for clear shots).
        let img = try loadFixtureCGImage("face_single.jpg")
        let variance = ImageQuality.laplacianVariance(of: img)
        XCTAssertGreaterThan(variance, 15.0,
            "Sharp fixture should have variance well above 15.0 threshold, got \(variance)")
    }

    func testAllFixturesHaveNonZeroVariance() throws {
        for name in ["face_single.jpg", "face_group.jpg", "face_crowd.jpg"] {
            let img = try loadFixtureCGImage(name)
            let variance = ImageQuality.laplacianVariance(of: img)
            XCTAssertGreaterThan(variance, 0,
                "\(name) should have non-zero Laplacian variance")
        }
    }

    // MARK: - Synthetic: uniform image has zero variance

    func testUniformImageHasZeroVariance() throws {
        // A solid-color image has a uniformly-zero Laplacian, so variance = 0.
        let img = makeUniformGrayImage(width: 100, height: 100, gray: 128)
        let variance = ImageQuality.laplacianVariance(of: img)
        XCTAssertEqual(variance, 0, accuracy: 1e-9,
            "Uniform image should have zero Laplacian variance")
    }

    // MARK: - Synthetic: checkerboard has very high variance

    func testCheckerboardHasHighVariance() throws {
        // An alternating 0/255 checkerboard has strong edges at every pixel,
        // producing a large Laplacian response at every interior pixel.
        let img = makeCheckerboardImage(width: 64, height: 64)
        let variance = ImageQuality.laplacianVariance(of: img)
        XCTAssertGreaterThan(variance, 1000,
            "Checkerboard should have very high variance (strong edges), got \(variance)")
    }

    // MARK: - Bounding box crop scoping

    func testBboxCropRestrictsComputation() throws {
        // Put a sharp checkerboard in the top-left quarter of an image, and
        // a uniform field in the bottom-right. Scoring only the top-left
        // quarter should give high variance; scoring only the bottom-right
        // should give ~0.
        let img = makeHalfCheckerboardImage(size: 100)

        let topLeftVar = ImageQuality.laplacianVariance(
            of: img, bboxTopLeft: (0.0, 0.0, 0.5, 0.5)
        )
        let bottomRightVar = ImageQuality.laplacianVariance(
            of: img, bboxTopLeft: (0.5, 0.5, 1.0, 1.0)
        )

        XCTAssertGreaterThan(topLeftVar, 1000,
            "Checkerboard quadrant should have high variance, got \(topLeftVar)")
        XCTAssertLessThan(bottomRightVar, 1.0,
            "Uniform quadrant should have near-zero variance, got \(bottomRightVar)")
    }

    // MARK: - Edge cases

    func testTinyCropReturnsZero() throws {
        let img = makeUniformGrayImage(width: 100, height: 100, gray: 200)
        // A 2-pixel bbox isn't enough for the 3x3 kernel — should return 0.
        let variance = ImageQuality.laplacianVariance(
            of: img, bboxTopLeft: (0.0, 0.0, 0.02, 0.02)
        )
        XCTAssertEqual(variance, 0, accuracy: 1e-9)
    }

    func testDefaultBboxScoresWholeImage() throws {
        let img = try loadFixtureCGImage("face_single.jpg")
        let defaultVar = ImageQuality.laplacianVariance(of: img)
        let explicitVar = ImageQuality.laplacianVariance(
            of: img, bboxTopLeft: (0.0, 0.0, 1.0, 1.0)
        )
        XCTAssertEqual(defaultVar, explicitVar, accuracy: 1e-6)
    }

    // MARK: - Image factories

    /// Create an N×M grayscale image filled with a single gray value.
    private func makeUniformGrayImage(width: Int, height: Int, gray: UInt8) -> CGImage {
        let bytesPerRow = width * 4
        var bytes = [UInt8](repeating: 0, count: width * height * 4)
        for i in 0..<(width * height) {
            bytes[i * 4 + 0] = gray // B
            bytes[i * 4 + 1] = gray // G
            bytes[i * 4 + 2] = gray // R
            bytes[i * 4 + 3] = 255   // A
        }
        return makeBGRAImage(bytes: &bytes, width: width, height: height, bytesPerRow: bytesPerRow)
    }

    /// Create an N×N checkerboard with 1-pixel cells alternating 0 and 255.
    private func makeCheckerboardImage(width: Int, height: Int) -> CGImage {
        let bytesPerRow = width * 4
        var bytes = [UInt8](repeating: 0, count: width * height * 4)
        for y in 0..<height {
            for x in 0..<width {
                let gray: UInt8 = ((x + y) % 2 == 0) ? 0 : 255
                let base = (y * width + x) * 4
                bytes[base + 0] = gray
                bytes[base + 1] = gray
                bytes[base + 2] = gray
                bytes[base + 3] = 255
            }
        }
        return makeBGRAImage(bytes: &bytes, width: width, height: height, bytesPerRow: bytesPerRow)
    }

    /// Create an N×N image whose top-left quarter is a checkerboard and
    /// whose remainder is uniform mid-gray.
    private func makeHalfCheckerboardImage(size: Int) -> CGImage {
        let width = size, height = size
        let bytesPerRow = width * 4
        var bytes = [UInt8](repeating: 128, count: width * height * 4) // default gray
        for y in 0..<height {
            for x in 0..<width {
                let base = (y * width + x) * 4
                if x < size / 2 && y < size / 2 {
                    let gray: UInt8 = ((x + y) % 2 == 0) ? 0 : 255
                    bytes[base + 0] = gray
                    bytes[base + 1] = gray
                    bytes[base + 2] = gray
                } else {
                    bytes[base + 0] = 128
                    bytes[base + 1] = 128
                    bytes[base + 2] = 128
                }
                bytes[base + 3] = 255
            }
        }
        return makeBGRAImage(bytes: &bytes, width: width, height: height, bytesPerRow: bytesPerRow)
    }

    private func makeBGRAImage(bytes: inout [UInt8], width: Int, height: Int, bytesPerRow: Int) -> CGImage {
        let colorSpace = CGColorSpaceCreateDeviceRGB()
        let bitmapInfo = CGImageAlphaInfo.premultipliedLast.rawValue
        let context = bytes.withUnsafeMutableBufferPointer { ptr -> CGContext? in
            CGContext(
                data: ptr.baseAddress,
                width: width,
                height: height,
                bitsPerComponent: 8,
                bytesPerRow: bytesPerRow,
                space: colorSpace,
                bitmapInfo: bitmapInfo
            )
        }
        return context!.makeImage()!
    }
}

private enum FixtureError: Error {
    case notFound(String)
    case unreadable
}
