import XCTest
import Foundation
import CoreML
@testable import LumiverbKit

/// End-to-end test of the ArcFace face embedding pipeline.
///
/// Exercises the full runtime path against real face fixtures and the real
/// CoreML model, driving the **same public entry point** the production
/// enrichment pipelines (`EnrichmentPipeline`, `ReEnrichmentRunner`) call:
///
///   fixture Data
///     → FaceDetectionProvider.detectFaces(from: Data)
///         (ImageLoading.loadOriented + VNDetectFaceLandmarksRequest +
///          VNDetectFaceCaptureQualityRequest + all production gates)
///     → FaceDetectionProvider.extractAlignedFaceCrop
///         (FaceAlignment.alignedCrop: similarity warp to 112×112)
///     → FaceEmbedding.embed
///         (CoreML inference, 512-d L2-normalized output)
///
/// The fixtures are arranged so the test can verify identity discrimination
/// without per-face labels: the human user behind this repo appears in
/// `face_single.jpg` and `face_group.jpg` but **not** in `face_crowd.jpg`.
/// We use the solo face as an "anchor" and assert that the best similarity
/// against the group (which contains the user) is meaningfully higher than
/// the best similarity against the crowd (which contains only strangers).
///
/// **Why this test exists.** Before this file landed there was no end-to-end
/// coverage of the embedding pipeline, and the pipeline shipped with *four*
/// stacked bugs that nothing else could detect:
///
///   1. `FaceAlignment.alignedCrop` had broken `CGContext` + CTM math, so
///      it produced bit-identical blank crops for every input regardless of
///      landmarks.
///   2. `FaceEmbedding.embed` read the model's Float16 output as Float32,
///      walking off the end of the buffer and reinterpreting random adjacent
///      memory as embedding values.
///   3. `FaceDetectionProvider.cgImage(from:)` loaded images via
///      `NSImage(data:).cgImage(...)`, which silently drops EXIF orientation
///      — Vision then ran on sideways/upside-down pixels for almost every
///      modern phone photo.
///   4. The first attempt at fixing #1 used reversed memory-row indexing
///      (assuming CG bitmap memory is bottom-up to match its bottom-left
///      coordinate system; it isn't — memory is top-down).
///
/// All four were dormant under the previous low-coverage testing strategy,
/// which only validated the similarity-transform math at the (a, b, tx, ty)
/// tuple level via a re-implementation in the test file. None of them were
/// catchable by the converter validation, which runs in Python via
/// coremltools. This file pins the contract end-to-end so future regressions
/// in any of these layers fail loudly before reaching production.
///
/// **CI behavior.** The model file is large (~80 MB), not in version
/// control, and downloaded on demand by `ModelDownloader`. CI machines
/// without it cause `XCTSkip`, not failure. Developers running locally with
/// `~/.lumiverb/models/ArcFace.mlmodelc` present get the full check.
final class FaceEmbeddingTests: XCTestCase {

    // MARK: - Test infrastructure

    /// Standard local model location, matching `ArcFaceProvider.modelURL`.
    private static var localModelURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".lumiverb/models/ArcFace.mlmodelc")
    }

    /// Load the on-disk ArcFace model, or skip the test if it isn't installed.
    private func loadModelOrSkip() throws -> MLModel {
        let url = Self.localModelURL
        guard FileManager.default.fileExists(atPath: url.path) else {
            throw XCTSkip(
                "ArcFace model not present at \(url.path) — install via the macOS app or run scripts/convert-models/convert_arcface.py"
            )
        }
        return try MLModel(contentsOf: url)
    }

    /// Load the raw bytes of a fixture file.
    private func loadFixtureData(_ name: String) throws -> Data {
        guard let url = Bundle.module.url(forResource: name, withExtension: nil, subdirectory: "Fixtures") else {
            throw FixtureError.notFound(name)
        }
        return try Data(contentsOf: url)
    }

    /// Run the **real** production face-detection pipeline over the fixture
    /// bytes, align every surviving face via `extractAlignedFaceCrop`, and
    /// embed via the CoreML ArcFace model.
    ///
    /// This exercises the entire public contract of the embedding pipeline
    /// exactly as `EnrichmentPipeline` / `ReEnrichmentRunner` do in
    /// production: `ImageLoading.loadOriented` → `FaceDetectionProvider.
    /// detectFaces(from: Data)` (detection + gate chain) → aligned crop →
    /// `FaceEmbedding.embed`. Previously this function ran a private
    /// `VNDetectFaceLandmarksRequest` helper that bypassed the
    /// FaceDetectionProvider gate chain entirely, so the test was
    /// embedding faces the production pipeline would have dropped. After
    /// the `FaceDetectionProvider` → LumiverbKit move, we can drive the
    /// real entry point directly.
    private func embedAllFaces(inFixture name: String, model: MLModel) throws -> [[Float]] {
        let data = try loadFixtureData(name)
        let faces = try FaceDetectionProvider.detectFaces(from: data)
        guard let cgImage = ImageLoading.loadOriented(from: data) else {
            throw FixtureError.unreadable
        }
        var embeddings: [[Float]] = []
        for face in faces {
            guard let aligned = FaceDetectionProvider.extractAlignedFaceCrop(
                from: cgImage, face: face
            ) else { continue }
            let vector = try FaceEmbedding.embed(faceImage: aligned, model: model)
            embeddings.append(vector)
        }
        return embeddings
    }

    /// Cosine similarity for two L2-normalized vectors. With unit-length
    /// inputs this is just the dot product.
    private func cosineSimilarity(_ a: [Float], _ b: [Float]) -> Float {
        precondition(a.count == b.count, "embedding length mismatch")
        var sum: Float = 0
        for i in 0..<a.count { sum += a[i] * b[i] }
        return sum
    }

    // MARK: - Sanity checks

    /// Same vector compared to itself must be 1.0. Catches degenerate cases
    /// like "embedding pipeline returns zeros" or "L2 normalization is broken."
    func testSelfSimilarityIsOne() throws {
        let model = try loadModelOrSkip()
        let solo = try embedAllFaces(inFixture: "face_single.jpg", model: model)
        XCTAssertGreaterThan(solo.count, 0, "no face embedded from face_single.jpg")

        let anchor = solo[0]
        let selfSim = cosineSimilarity(anchor, anchor)
        XCTAssertEqual(Double(selfSim), 1.0, accuracy: 1e-4,
                       "self-similarity must be 1.0; got \(selfSim) — embedding may not be L2-normalized")
    }

    /// Embeddings must be 512-dimensional. Catches "wrong tensor read out
    /// of the model" type bugs (e.g. an intermediate layer instead of the
    /// final output) and dtype mismatches that produce truncated reads.
    func testEmbeddingShapeIs512() throws {
        let model = try loadModelOrSkip()
        let solo = try embedAllFaces(inFixture: "face_single.jpg", model: model)
        XCTAssertGreaterThan(solo.count, 0, "no face embedded from face_single.jpg")
        XCTAssertEqual(solo[0].count, 512,
                       "ArcFace embedding must be 512-d; got \(solo[0].count)")
    }

    // MARK: - Identity discrimination

    /// **The headline test.** With a correctly converted ArcFace model and a
    /// working alignment + inference pipeline, on real photo fixtures we
    /// observe:
    ///
    ///   - same-identity (anchor vs user-in-group): ~0.7–0.8
    ///   - cross-identity (anchor vs stranger):     ~−0.1–0.15
    ///   - separation:                              ~0.6
    ///
    /// We assert a separation of at least 0.30 between the best in-group
    /// match and the best in-crowd match. That's well within the noise floor
    /// of healthy ArcFace embeddings on these fixtures (we measured ~0.62
    /// when the pipeline was correct), but is wildly outside what any of the
    /// historical bugs in alignment / dtype / orientation could produce —
    /// each of those independently collapsed cos-sims into roughly the same
    /// low-variance band regardless of identity, with separations under 0.10
    /// or below zero.
    func testGroupContainsAnchorMoreThanCrowd() throws {
        let model = try loadModelOrSkip()

        let soloEmbeddings  = try embedAllFaces(inFixture: "face_single.jpg", model: model)
        let groupEmbeddings = try embedAllFaces(inFixture: "face_group.jpg",  model: model)
        let crowdEmbeddings = try embedAllFaces(inFixture: "face_crowd.jpg",  model: model)

        XCTAssertGreaterThan(soloEmbeddings.count,  0, "no face embedded from face_single.jpg")
        XCTAssertGreaterThan(groupEmbeddings.count, 0, "no face embedded from face_group.jpg")
        // NOTE: crowd may return 0 embeddings — after the production gate
        // chain kicks in, all crowd subjects are below the 40 px / 0.3% /
        // Vision capture-quality gates except one borderline face, and
        // Vision's capture-quality scorer is non-deterministic across runs
        // (~0.02 variance). An empty crowd is a legitimate outcome; skip
        // the comparison and the headline separation assertion when that
        // happens rather than flaking the build.
        try XCTSkipIf(crowdEmbeddings.isEmpty,
            "face_crowd.jpg produced no embeddings this run (Vision capture-quality flapped all borderline faces below the gate); skipping cross-identity comparison")

        // The solo image has exactly one face — that's the anchor.
        let anchor = soloEmbeddings[0]

        let groupSims = groupEmbeddings.map { cosineSimilarity(anchor, $0) }
        let crowdSims = crowdEmbeddings.map { cosineSimilarity(anchor, $0) }

        let bestInGroup = groupSims.max() ?? -1
        let bestInCrowd = crowdSims.max() ?? -1

        // Headline assertion: the user's face in the group must be a much
        // better match than any random stranger in the crowd.
        let separation = bestInGroup - bestInCrowd
        XCTAssertGreaterThan(separation, 0.30,
            """
            ArcFace embeddings do not discriminate identity.
              best in group  (anchor present): \(bestInGroup)
              best in crowd  (anchor absent):  \(bestInCrowd)
              separation:                       \(separation) (expected > 0.30)
            With a healthy pipeline this should be ~0.6. The most common
            causes of a low separation are: alignedCrop sampling the wrong
            source pixels (orientation / row-indexing bug), FaceEmbedding
            reading the model output with the wrong dtype, or
            FaceDetectionProvider loading images without applying EXIF
            orientation so Vision sees rotated pixels.
            """)

        // Sanity floor on the same-identity match itself. Even with some
        // pose / lighting drift, the user's face in the group should sit
        // well above ~0.5 for a working model.
        XCTAssertGreaterThan(bestInGroup, 0.50,
            "best same-identity cos-sim (\(bestInGroup)) is below 0.50 — embeddings look degenerate")
    }
}

private enum FixtureError: Error {
    case notFound(String)
    case unreadable
}
