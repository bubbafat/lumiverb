import XCTest
import Foundation
@testable import LumiverbKit

// MARK: - Asset Page Item

final class AssetPageItemTests: XCTestCase {

    private var decoder: JSONDecoder {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }

    func testDecodesFullAssetPageItem() throws {
        let json = """
        {
            "asset_id": "ast_001",
            "rel_path": "2024/vacation/IMG_001.jpg",
            "file_size": 5242880,
            "file_mtime": "2024-06-15T10:30:00+00:00",
            "content_hash": "sha256:abc123",
            "media_type": "image",
            "width": 6000,
            "height": 4000,
            "taken_at": "2024-06-15T10:30:00+00:00",
            "status": "described",
            "duration_sec": null,
            "camera_make": "Canon",
            "camera_model": "EOS R5",
            "iso": 400,
            "aperture": 2.8,
            "focal_length": 50.0,
            "focal_length_35mm": 50.0,
            "lens_model": "RF 50mm F1.2L USM",
            "flash_fired": false,
            "gps_lat": 48.8566,
            "gps_lon": 2.3522,
            "face_count": 2,
            "created_at": "2024-06-15T12:00:00+00:00"
        }
        """.data(using: .utf8)!

        let item = try decoder.decode(AssetPageItem.self, from: json)
        XCTAssertEqual(item.assetId, "ast_001")
        XCTAssertEqual(item.relPath, "2024/vacation/IMG_001.jpg")
        XCTAssertEqual(item.fileSize, 5242880)
        XCTAssertEqual(item.mediaType, "image")
        XCTAssertEqual(item.width, 6000)
        XCTAssertEqual(item.height, 4000)
        XCTAssertEqual(item.cameraMake, "Canon")
        XCTAssertEqual(item.iso, 400)
        XCTAssertEqual(item.aperture, 2.8)
        XCTAssertEqual(item.focalLength, 50.0)
        XCTAssertEqual(item.flashFired, false)
        XCTAssertEqual(item.gpsLat!, 48.8566, accuracy: 0.0001)
        XCTAssertEqual(item.faceCount, 2)
        XCTAssertEqual(item.id, "ast_001")
        XCTAssertFalse(item.isVideo)
    }

    func testDecodesMinimalAssetPageItem() throws {
        let json = """
        {
            "asset_id": "ast_002",
            "rel_path": "photo.jpg",
            "file_size": 1024,
            "media_type": "image",
            "status": "pending"
        }
        """.data(using: .utf8)!

        let item = try decoder.decode(AssetPageItem.self, from: json)
        XCTAssertEqual(item.assetId, "ast_002")
        XCTAssertNil(item.width)
        XCTAssertNil(item.height)
        XCTAssertNil(item.cameraMake)
        XCTAssertNil(item.takenAt)
        XCTAssertNil(item.faceCount)
    }

    func testVideoAssetDetection() throws {
        let json = """
        {
            "asset_id": "ast_003",
            "rel_path": "clip.mp4",
            "file_size": 10485760,
            "media_type": "video",
            "status": "described",
            "duration_sec": 125.5,
            "width": 1920,
            "height": 1080
        }
        """.data(using: .utf8)!

        let item = try decoder.decode(AssetPageItem.self, from: json)
        XCTAssertTrue(item.isVideo)
        XCTAssertEqual(item.durationSec, 125.5)
    }

    func testAspectRatio() throws {
        let json = """
        {
            "asset_id": "ast_004",
            "rel_path": "wide.jpg",
            "file_size": 1024,
            "media_type": "image",
            "status": "pending",
            "width": 1920,
            "height": 1080
        }
        """.data(using: .utf8)!

        let item = try decoder.decode(AssetPageItem.self, from: json)
        XCTAssertEqual(item.aspectRatio, 1920.0 / 1080.0, accuracy: 0.001)
    }

    func testAspectRatioDefaultsToOne() throws {
        let json = """
        {
            "asset_id": "ast_005",
            "rel_path": "unknown.jpg",
            "file_size": 1024,
            "media_type": "image",
            "status": "pending"
        }
        """.data(using: .utf8)!

        let item = try decoder.decode(AssetPageItem.self, from: json)
        XCTAssertEqual(item.aspectRatio, 1.0)
    }
}

// MARK: - Asset Detail

final class AssetDetailTests: XCTestCase {

    private var decoder: JSONDecoder {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }

    func testDecodesFullAssetDetail() throws {
        let json = """
        {
            "asset_id": "ast_100",
            "library_id": "lib_1",
            "rel_path": "2024/photo.jpg",
            "media_type": "image",
            "status": "described",
            "proxy_key": "proxies/ast_100.webp",
            "thumbnail_key": "thumbs/ast_100.webp",
            "video_preview_key": null,
            "duration_sec": null,
            "width": 4000,
            "height": 3000,
            "content_hash": "sha256:deadbeef",
            "exif_extracted_at": "2024-01-01T00:00:00+00:00",
            "camera_make": "Sony",
            "camera_model": "A7 IV",
            "taken_at": "2024-06-15T10:00:00+00:00",
            "gps_lat": null,
            "gps_lon": null,
            "iso": 800,
            "exposure_time_us": 4000,
            "aperture": 5.6,
            "focal_length": 85.0,
            "focal_length_35mm": 85.0,
            "lens_model": "FE 85mm F1.4 GM",
            "flash_fired": false,
            "orientation": 1,
            "ai_description": "A sunset over the ocean with golden light",
            "ai_tags": ["sunset", "ocean", "golden hour"],
            "ocr_text": null,
            "transcript_srt": null,
            "transcript_language": null,
            "transcribed_at": null,
            "note": "Best shot of the trip",
            "note_author": "admin",
            "note_updated_at": "2024-07-01T12:00:00+00:00"
        }
        """.data(using: .utf8)!

        let detail = try decoder.decode(AssetDetail.self, from: json)
        XCTAssertEqual(detail.assetId, "ast_100")
        XCTAssertEqual(detail.libraryId, "lib_1")
        XCTAssertEqual(detail.proxyKey, "proxies/ast_100.webp")
        XCTAssertEqual(detail.iso, 800)
        XCTAssertEqual(detail.exposureTimeUs, 4000)
        XCTAssertEqual(detail.aiDescription, "A sunset over the ocean with golden light")
        XCTAssertEqual(detail.aiTags, ["sunset", "ocean", "golden hour"])
        XCTAssertEqual(detail.note, "Best shot of the trip")
        XCTAssertFalse(detail.isVideo)
    }

    func testCameraDescription() throws {
        let json = """
        {
            "asset_id": "a1", "library_id": "l1", "rel_path": "p.jpg",
            "media_type": "image", "status": "described",
            "camera_make": "Canon", "camera_model": "EOS R5"
        }
        """.data(using: .utf8)!

        let detail = try decoder.decode(AssetDetail.self, from: json)
        XCTAssertEqual(detail.cameraDescription, "Canon EOS R5")
    }

    func testCameraDescriptionDeduplication() throws {
        // When model already contains the make (e.g., "Canon Canon EOS R5")
        let json = """
        {
            "asset_id": "a1", "library_id": "l1", "rel_path": "p.jpg",
            "media_type": "image", "status": "described",
            "camera_make": "Canon", "camera_model": "Canon EOS R5"
        }
        """.data(using: .utf8)!

        let detail = try decoder.decode(AssetDetail.self, from: json)
        XCTAssertEqual(detail.cameraDescription, "Canon EOS R5")
    }

    func testCameraDescriptionModelOnly() throws {
        let json = """
        {
            "asset_id": "a1", "library_id": "l1", "rel_path": "p.jpg",
            "media_type": "image", "status": "described",
            "camera_model": "iPhone 15 Pro"
        }
        """.data(using: .utf8)!

        let detail = try decoder.decode(AssetDetail.self, from: json)
        XCTAssertEqual(detail.cameraDescription, "iPhone 15 Pro")
    }

    func testExposureDescription() throws {
        let json = """
        {
            "asset_id": "a1", "library_id": "l1", "rel_path": "p.jpg",
            "media_type": "image", "status": "described",
            "exposure_time_us": 4000
        }
        """.data(using: .utf8)!

        let detail = try decoder.decode(AssetDetail.self, from: json)
        XCTAssertEqual(detail.exposureDescription, "1/250s")
    }

    func testExposureDescriptionLong() throws {
        let json = """
        {
            "asset_id": "a1", "library_id": "l1", "rel_path": "p.jpg",
            "media_type": "image", "status": "described",
            "exposure_time_us": 2000000
        }
        """.data(using: .utf8)!

        let detail = try decoder.decode(AssetDetail.self, from: json)
        XCTAssertEqual(detail.exposureDescription, "2.0s")
    }

    func testDimensionsDescription() throws {
        let json = """
        {
            "asset_id": "a1", "library_id": "l1", "rel_path": "p.jpg",
            "media_type": "image", "status": "described",
            "width": 6000, "height": 4000
        }
        """.data(using: .utf8)!

        let detail = try decoder.decode(AssetDetail.self, from: json)
        XCTAssertEqual(detail.dimensionsDescription, "6000 x 4000")
    }

    func testFilename() throws {
        let json = """
        {
            "asset_id": "a1", "library_id": "l1",
            "rel_path": "2024/vacation/IMG_0001.jpg",
            "media_type": "image", "status": "pending"
        }
        """.data(using: .utf8)!

        let detail = try decoder.decode(AssetDetail.self, from: json)
        XCTAssertEqual(detail.filename, "IMG_0001.jpg")
    }

    func testDecodesMinimalAssetDetail() throws {
        let json = """
        {
            "asset_id": "a1", "library_id": "l1", "rel_path": "p.jpg",
            "media_type": "image", "status": "pending"
        }
        """.data(using: .utf8)!

        let detail = try decoder.decode(AssetDetail.self, from: json)
        XCTAssertNil(detail.proxyKey)
        XCTAssertNil(detail.thumbnailKey)
        XCTAssertNil(detail.cameraMake)
        XCTAssertNil(detail.aiDescription)
        XCTAssertNil(detail.aiTags)
        XCTAssertNil(detail.note)
        XCTAssertNil(detail.cameraDescription)
        XCTAssertNil(detail.exposureDescription)
        XCTAssertNil(detail.dimensionsDescription)
    }

    func testAssetDetailDecodesAPersonsLocation() throws {
        let json = """
        {"asset_id": "a", "library_id": "l", "rel_path": "x.jpg", "media_type": "image", "status": "ok",
         "gps_lat": 40.0, "gps_lon": -74.0, "gps_accuracy_m": 5.0,
         "location": {"lat": 1.0, "lon": 2.0, "radius_m": 0, "source": "person", "status": "applied",
                      "basis_summary": null, "set_by": "rob@x.io", "set_at": "2026-10-09T00:00:00+00:00"}}
        """.data(using: .utf8)!
        let detail = try decoder.decode(AssetDetail.self, from: json)
        XCTAssertEqual(detail.location?.source, "person")
        XCTAssertEqual(detail.location?.setBy, "rob@x.io")
        XCTAssertEqual(detail.gpsAccuracyM, 5.0)
    }
}

// MARK: - Search Response

final class SearchResponseTests: XCTestCase {

    private var decoder: JSONDecoder {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }

    func testDecodesSearchResponse() throws {
        let json = """
        {
            "query": "sunset",
            "hits": [
                {
                    "type": "image",
                    "asset_id": "ast_001",
                    "library_id": "lib_1",
                    "library_name": "Photos",
                    "rel_path": "sunset.jpg",
                    "thumbnail_key": "thumbs/ast_001.webp",
                    "proxy_key": "proxies/ast_001.webp",
                    "description": "A golden sunset over the sea",
                    "tags": ["sunset", "ocean"],
                    "score": 0.95,
                    "source": "quickwit",
                    "camera_make": "Sony",
                    "camera_model": "A7 IV",
                    "media_type": "image",
                    "file_size": 5242880,
                    "width": 6000,
                    "height": 4000,
                    "taken_at": "2024-06-15T10:00:00+00:00"
                }
            ],
            "total": 1,
            "source": "quickwit"
        }
        """.data(using: .utf8)!

        let response = try decoder.decode(SearchResponse.self, from: json)
        XCTAssertEqual(response.query, "sunset")
        XCTAssertEqual(response.total, 1)
        XCTAssertEqual(response.source, "quickwit")
        XCTAssertEqual(response.hits.count, 1)

        let hit = response.hits[0]
        XCTAssertEqual(hit.type, "image")
        XCTAssertEqual(hit.assetId, "ast_001")
        XCTAssertEqual(hit.description, "A golden sunset over the sea")
        XCTAssertEqual(hit.tags, ["sunset", "ocean"])
        XCTAssertEqual(hit.score, 0.95)
        XCTAssertEqual(hit.id, "ast_001")
    }

    func testSearchHitSceneId() throws {
        let json = """
        {
            "type": "scene",
            "asset_id": "ast_001",
            "rel_path": "video.mp4",
            "description": "Beach scene",
            "tags": [],
            "score": 0.8,
            "source": "quickwit_scenes",
            "scene_id": "scene_42",
            "start_ms": 10000,
            "end_ms": 25000
        }
        """.data(using: .utf8)!

        let hit = try decoder.decode(SearchHit.self, from: json)
        XCTAssertEqual(hit.type, "scene")
        XCTAssertEqual(hit.sceneId, "scene_42")
        XCTAssertEqual(hit.startMs, 10000)
        XCTAssertEqual(hit.endMs, 25000)
        // ID should include type and scene_id for uniqueness
        XCTAssertEqual(hit.id, "ast_001-scene-scene_42")
    }

    func testSearchHitTranscript() throws {
        let json = """
        {
            "type": "transcript",
            "asset_id": "ast_002",
            "rel_path": "interview.mp4",
            "description": "Interview transcript",
            "tags": [],
            "score": 0.7,
            "source": "quickwit_transcripts",
            "snippet": "...talking about the weather...",
            "language": "en"
        }
        """.data(using: .utf8)!

        let hit = try decoder.decode(SearchHit.self, from: json)
        XCTAssertEqual(hit.snippet, "...talking about the weather...")
        XCTAssertEqual(hit.language, "en")
    }

    func testDecodesEmptySearchResponse() throws {
        let json = """
        {
            "query": "nonexistent",
            "hits": [],
            "total": 0,
            "source": "postgres"
        }
        """.data(using: .utf8)!

        let response = try decoder.decode(SearchResponse.self, from: json)
        XCTAssertTrue(response.hits.isEmpty)
        XCTAssertEqual(response.total, 0)
    }
}

// MARK: - Similarity Response

final class SimilarityResponseTests: XCTestCase {

    private var decoder: JSONDecoder {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }

    func testDecodesSimilarityResponse() throws {
        let json = """
        {
            "source_asset_id": "ast_001",
            "hits": [
                {
                    "asset_id": "ast_010",
                    "rel_path": "similar1.jpg",
                    "thumbnail_key": "thumbs/ast_010.webp",
                    "proxy_key": "proxies/ast_010.webp",
                    "distance": 0.15,
                    "media_type": "image",
                    "file_size": 3145728,
                    "width": 4000,
                    "height": 3000
                },
                {
                    "asset_id": "ast_020",
                    "rel_path": "similar2.jpg",
                    "thumbnail_key": null,
                    "proxy_key": null,
                    "distance": 0.32,
                    "media_type": "image",
                    "file_size": 2097152,
                    "width": 3000,
                    "height": 2000
                }
            ],
            "total": 2,
            "embedding_available": true
        }
        """.data(using: .utf8)!

        let response = try decoder.decode(SimilarityResponse.self, from: json)
        XCTAssertEqual(response.sourceAssetId, "ast_001")
        XCTAssertEqual(response.total, 2)
        XCTAssertTrue(response.embeddingAvailable)
        XCTAssertEqual(response.hits.count, 2)

        let first = response.hits[0]
        XCTAssertEqual(first.assetId, "ast_010")
        XCTAssertEqual(first.distance, 0.15)
        XCTAssertEqual(first.thumbnailKey, "thumbs/ast_010.webp")
        XCTAssertEqual(first.id, "ast_010")

        let second = response.hits[1]
        XCTAssertNil(second.thumbnailKey)
        XCTAssertEqual(second.distance, 0.32)
    }

    func testDecodesNoEmbedding() throws {
        let json = """
        {
            "source_asset_id": "ast_001",
            "hits": [],
            "total": 0,
            "embedding_available": false
        }
        """.data(using: .utf8)!

        let response = try decoder.decode(SimilarityResponse.self, from: json)
        XCTAssertFalse(response.embeddingAvailable)
        XCTAssertTrue(response.hits.isEmpty)
    }

    func testSimilarHitMinimalFields() throws {
        let json = """
        {
            "asset_id": "ast_099",
            "rel_path": "minimal.jpg",
            "distance": 0.05
        }
        """.data(using: .utf8)!

        let hit = try decoder.decode(SimilarHit.self, from: json)
        XCTAssertEqual(hit.assetId, "ast_099")
        XCTAssertEqual(hit.distance, 0.05)
        XCTAssertNil(hit.thumbnailKey)
        XCTAssertNil(hit.mediaType)
        XCTAssertNil(hit.width)
    }
}

// MARK: - Directory Node

final class DirectoryNodeTests: XCTestCase {

    private var decoder: JSONDecoder {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }

    func testDecodesDirectoryNode() throws {
        let json = """
        {
            "name": "2024",
            "path": "photos/2024",
            "asset_count": 1250
        }
        """.data(using: .utf8)!

        let node = try decoder.decode(DirectoryNode.self, from: json)
        XCTAssertEqual(node.name, "2024")
        XCTAssertEqual(node.path, "photos/2024")
        XCTAssertEqual(node.assetCount, 1250)
        XCTAssertEqual(node.id, "photos/2024")
    }

    func testDecodesDirectoryList() throws {
        let json = """
        [
            {"name": "photos", "path": "photos", "asset_count": 5000},
            {"name": "screenshots", "path": "screenshots", "asset_count": 42},
            {"name": "videos", "path": "videos", "asset_count": 300}
        ]
        """.data(using: .utf8)!

        let nodes = try decoder.decode([DirectoryNode].self, from: json)
        XCTAssertEqual(nodes.count, 3)
        XCTAssertEqual(nodes[0].name, "photos")
        XCTAssertEqual(nodes[0].assetCount, 5000)
        XCTAssertEqual(nodes[2].name, "videos")
    }

    func testDecodesEmptyDirectoryList() throws {
        let json = "[]".data(using: .utf8)!
        let nodes = try decoder.decode([DirectoryNode].self, from: json)
        XCTAssertTrue(nodes.isEmpty)
    }
}

// MARK: - Image Cache

final class ImageCacheTests: XCTestCase {

    func testCacheStoresAndRetrievesImage() {
        let cache = ImageCache.shared
        let image = NSImage(size: NSSize(width: 100, height: 100))
        cache.setImage(image, forKey: "test-cache-key")

        let retrieved = cache.image(forKey: "test-cache-key")
        XCTAssertNotNil(retrieved)
        // Clean up
        cache.removeAll()
    }

    func testCacheReturnsNilForMissingKey() {
        let cache = ImageCache.shared
        XCTAssertNil(cache.image(forKey: "nonexistent-key-12345"))
    }

    func testCacheRemoveAll() {
        let cache = ImageCache.shared
        let image = NSImage(size: NSSize(width: 50, height: 50))
        cache.setImage(image, forKey: "remove-test-key")
        XCTAssertNotNil(cache.image(forKey: "remove-test-key"))

        cache.removeAll()
        XCTAssertNil(cache.image(forKey: "remove-test-key"))
    }
}

// MARK: - Mac Proxy Disk Cache

final class MacProxyDiskCacheTests: XCTestCase {

    private var tempDir: URL!
    private var cache: MacProxyDiskCache!

    override func setUp() {
        super.setUp()
        tempDir = FileManager.default.temporaryDirectory
            .appendingPathComponent("proxy-cache-test-\(UUID().uuidString)")
        cache = MacProxyDiskCache(cacheDir: tempDir)
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: tempDir)
        super.tearDown()
    }

    func testHas() {
        XCTAssertFalse(cache.has(assetId: "ast_006"))

        cache.put(assetId: "ast_006", data: Data("bytes".utf8))
        XCTAssertTrue(cache.has(assetId: "ast_006"))
    }

    func testRemove() throws {
        // A proxy with a `.sha` sidecar, as the old shared folder had them
        // (the Python CLI's scan wrote them there): remove takes both.
        cache.put(assetId: "ast_007", data: Data("image".utf8))
        let sidecar = tempDir.appendingPathComponent("ast_007.sha")
        try Data("sha".utf8).write(to: sidecar)

        XCTAssertTrue(cache.has(assetId: "ast_007"))

        cache.remove(assetId: "ast_007")

        XCTAssertFalse(cache.has(assetId: "ast_007"))
        XCTAssertFalse(FileManager.default.fileExists(atPath: sidecar.path))
    }

    func testPutForBrowse() {
        let data = Data("server-proxy-bytes".utf8)
        cache.put(assetId: "ast_008", data: data)

        let retrieved = cache.get(assetId: "ast_008")
        XCTAssertEqual(retrieved, data)
        // No SHA sidecar for browse-cached items
        XCTAssertFalse(FileManager.default.fileExists(
            atPath: tempDir.appendingPathComponent("ast_008.sha").path
        ))
    }

    func testGetMissing() {
        XCTAssertNil(cache.get(assetId: "nonexistent"))
    }
}

// MARK: - Mac Thumbnail Disk Cache

final class MacThumbnailDiskCacheTests: XCTestCase {

    private var tempDir: URL!
    private var cache: MacThumbnailDiskCache!

    override func setUp() {
        super.setUp()
        tempDir = FileManager.default.temporaryDirectory
            .appendingPathComponent("thumbnail-cache-test-\(UUID().uuidString)")
        cache = MacThumbnailDiskCache(cacheDir: tempDir)
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: tempDir)
        super.tearDown()
    }

    func testPutAndGet() {
        let data = Data("fake-thumbnail-bytes".utf8)
        cache.put(assetId: "ast_001", data: data)

        let retrieved = cache.get(assetId: "ast_001")
        XCTAssertEqual(retrieved, data)
    }

    func testGetMissing() {
        XCTAssertNil(cache.get(assetId: "nonexistent"))
    }

    func testHas() {
        XCTAssertFalse(cache.has(assetId: "ast_002"))
        cache.put(assetId: "ast_002", data: Data("bytes".utf8))
        XCTAssertTrue(cache.has(assetId: "ast_002"))
    }

    func testRemove() {
        cache.put(assetId: "ast_003", data: Data("bytes".utf8))
        XCTAssertTrue(cache.has(assetId: "ast_003"))
        cache.remove(assetId: "ast_003")
        XCTAssertFalse(cache.has(assetId: "ast_003"))
    }

    func testRemoveAllClearsEverything() {
        cache.put(assetId: "ast_a", data: Data("a".utf8))
        cache.put(assetId: "ast_b", data: Data("b".utf8))
        cache.put(assetId: "ast_c", data: Data("c".utf8))
        XCTAssertTrue(cache.has(assetId: "ast_a"))
        XCTAssertTrue(cache.has(assetId: "ast_b"))
        XCTAssertTrue(cache.has(assetId: "ast_c"))

        cache.removeAll()

        XCTAssertFalse(cache.has(assetId: "ast_a"))
        XCTAssertFalse(cache.has(assetId: "ast_b"))
        XCTAssertFalse(cache.has(assetId: "ast_c"))
        // Cache dir must still exist — the instance should be usable
        // after a flush, not require re-initialization.
        cache.put(assetId: "ast_d", data: Data("d".utf8))
        XCTAssertTrue(cache.has(assetId: "ast_d"))
    }

    func testPutOverwritesExisting() {
        cache.put(assetId: "ast_004", data: Data("old".utf8))
        cache.put(assetId: "ast_004", data: Data("new".utf8))
        XCTAssertEqual(cache.get(assetId: "ast_004"), Data("new".utf8))
    }

    func testCacheDirDoesNotClashWithProxyCache() {
        // Regression: the thumbnail cache must use a distinct directory
        // from the proxy cache so writes don't overwrite each other when
        // the same asset id is cached as both a thumbnail and a proxy.
        let proxyDir = tempDir.appendingPathComponent("proxy")
        let thumbDir = tempDir.appendingPathComponent("thumb")
        let proxyCache = MacProxyDiskCache(cacheDir: proxyDir)
        let thumbCache = MacThumbnailDiskCache(cacheDir: thumbDir)

        proxyCache.put(assetId: "shared_id", data: Data("proxy-bytes".utf8))
        thumbCache.put(assetId: "shared_id", data: Data("thumb-bytes".utf8))

        XCTAssertEqual(proxyCache.get(assetId: "shared_id"), Data("proxy-bytes".utf8))
        XCTAssertEqual(thumbCache.get(assetId: "shared_id"), Data("thumb-bytes".utf8))
    }
}
