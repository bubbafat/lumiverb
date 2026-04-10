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
            "sha256": "abc123",
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

    func testDecodesAssetPageResponse() throws {
        let json = """
        {
            "items": [
                {"asset_id": "a1", "rel_path": "a.jpg", "file_size": 100, "media_type": "image", "status": "pending"},
                {"asset_id": "a2", "rel_path": "b.jpg", "file_size": 200, "media_type": "image", "status": "described"}
            ],
            "next_cursor": "eyJ2IjogIjIwMjQtMDEtMDEiLCAiaWQiOiAiYTIifQ"
        }
        """.data(using: .utf8)!

        let response = try decoder.decode(AssetPageResponse.self, from: json)
        XCTAssertEqual(response.items.count, 2)
        XCTAssertEqual(response.items[0].assetId, "a1")
        XCTAssertEqual(response.nextCursor, "eyJ2IjogIjIwMjQtMDEtMDEiLCAiaWQiOiAiYTIifQ")
    }

    func testDecodesAssetPageResponseNullCursor() throws {
        let json = """
        {
            "items": [],
            "next_cursor": null
        }
        """.data(using: .utf8)!

        let response = try decoder.decode(AssetPageResponse.self, from: json)
        XCTAssertTrue(response.items.isEmpty)
        XCTAssertNil(response.nextCursor)
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
            "sha256": "deadbeef",
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

// MARK: - File Extensions

final class FileExtensionTests: XCTestCase {

    func testImageExtensionsSupported() {
        XCTAssertTrue(FileExtensions.isSupported("photo.jpg"))
        XCTAssertTrue(FileExtensions.isSupported("photo.JPG"))
        XCTAssertTrue(FileExtensions.isSupported("photo.jpeg"))
        XCTAssertTrue(FileExtensions.isSupported("photo.png"))
        XCTAssertTrue(FileExtensions.isSupported("photo.webp"))
        XCTAssertTrue(FileExtensions.isSupported("photo.bmp"))
        XCTAssertTrue(FileExtensions.isSupported("photo.tif"))
        XCTAssertTrue(FileExtensions.isSupported("photo.tiff"))
    }

    func testRAWExtensionsSupported() {
        XCTAssertTrue(FileExtensions.isSupported("photo.cr2"))
        XCTAssertTrue(FileExtensions.isSupported("photo.cr3"))
        XCTAssertTrue(FileExtensions.isSupported("photo.nef"))
        XCTAssertTrue(FileExtensions.isSupported("photo.arw"))
        XCTAssertTrue(FileExtensions.isSupported("photo.raf"))
        XCTAssertTrue(FileExtensions.isSupported("photo.orf"))
        XCTAssertTrue(FileExtensions.isSupported("photo.rw2"))
        XCTAssertTrue(FileExtensions.isSupported("photo.dng"))
    }

    func testVideoExtensionsSupported() {
        XCTAssertTrue(FileExtensions.isSupported("clip.mp4"))
        XCTAssertTrue(FileExtensions.isSupported("clip.mkv"))
        XCTAssertTrue(FileExtensions.isSupported("clip.mov"))
        XCTAssertTrue(FileExtensions.isSupported("clip.MOV"))
    }

    func testUnsupportedExtensions() {
        XCTAssertFalse(FileExtensions.isSupported("document.pdf"))
        XCTAssertFalse(FileExtensions.isSupported("archive.zip"))
        XCTAssertFalse(FileExtensions.isSupported("readme.txt"))
        XCTAssertFalse(FileExtensions.isSupported("noext"))
        XCTAssertFalse(FileExtensions.isSupported(".hidden"))
    }

    func testMediaTypeDetection() {
        XCTAssertEqual(FileExtensions.mediaType(for: "photo.jpg"), "image")
        XCTAssertEqual(FileExtensions.mediaType(for: "photo.CR2"), "image")
        XCTAssertEqual(FileExtensions.mediaType(for: "clip.mp4"), "video")
        XCTAssertEqual(FileExtensions.mediaType(for: "clip.MOV"), "video")
        XCTAssertNil(FileExtensions.mediaType(for: "readme.txt"))
        XCTAssertNil(FileExtensions.mediaType(for: "noext"))
    }

    func testPathWithDirectories() {
        XCTAssertTrue(FileExtensions.isSupported("2024/vacation/IMG_001.jpg"))
        XCTAssertTrue(FileExtensions.isSupported("/mnt/photos/raw/DSC_0001.nef"))
        XCTAssertEqual(FileExtensions.mediaType(for: "videos/2024/clip.mp4"), "video")
    }
}

// MARK: - Path Filter

final class PathFilterTests: XCTestCase {

    func testDefaultAllowsAll() {
        let filter = PathFilter()
        XCTAssertTrue(filter.isAllowed("photo.jpg"))
        XCTAssertTrue(filter.isAllowed("2024/vacation/photo.jpg"))
    }

    func testTenantExclude() {
        let rules = [PathFilterRule(pattern: "**/*.tmp", filterType: "exclude")]
        let filter = PathFilter(tenantRules: rules)

        XCTAssertFalse(filter.isAllowed("cache/file.tmp"))
        XCTAssertFalse(filter.isAllowed("deep/nested/file.tmp"))
        XCTAssertTrue(filter.isAllowed("photo.jpg"))
    }

    func testTenantInclude() {
        let rules = [PathFilterRule(pattern: "photos/**", filterType: "include")]
        let filter = PathFilter(tenantRules: rules)

        XCTAssertTrue(filter.isAllowed("photos/2024/img.jpg"))
        XCTAssertFalse(filter.isAllowed("trash/junk.jpg"))
    }

    func testLibraryExcludeOverridesTenantInclude() {
        let tenantRules = [PathFilterRule(pattern: "**/*.jpg", filterType: "include")]
        let libraryRules = [PathFilterRule(pattern: "Trash/**", filterType: "exclude")]
        let filter = PathFilter(tenantRules: tenantRules, libraryRules: libraryRules)

        XCTAssertFalse(filter.isAllowed("Trash/deleted.jpg"))
        XCTAssertTrue(filter.isAllowed("photos/img.jpg"))
    }

    func testLibraryIncludeOverridesTenantExclude() {
        let tenantRules = [PathFilterRule(pattern: "raw/**", filterType: "exclude")]
        let libraryRules = [PathFilterRule(pattern: "raw/important/**", filterType: "include")]
        let filter = PathFilter(tenantRules: tenantRules, libraryRules: libraryRules)

        XCTAssertTrue(filter.isAllowed("raw/important/keep.jpg"))
        XCTAssertFalse(filter.isAllowed("raw/other/skip.jpg"))
    }

    func testStarMatchesSingleSegment() {
        let rules = [PathFilterRule(pattern: "*.tmp", filterType: "exclude")]
        let filter = PathFilter(tenantRules: rules)

        XCTAssertFalse(filter.isAllowed("file.tmp"))
        XCTAssertTrue(filter.isAllowed("dir/file.tmp")) // * doesn't cross /
    }

    func testDoubleStarMatchesAcrossSegments() {
        let rules = [PathFilterRule(pattern: "**/*.tmp", filterType: "exclude")]
        let filter = PathFilter(tenantRules: rules)

        XCTAssertFalse(filter.isAllowed("file.tmp"))
        XCTAssertFalse(filter.isAllowed("dir/file.tmp"))
        XCTAssertFalse(filter.isAllowed("a/b/c/file.tmp"))
    }

    func testQuestionMarkMatchesSingleChar() {
        let rules = [PathFilterRule(pattern: "IMG_????.jpg", filterType: "include")]
        let filter = PathFilter(tenantRules: rules)

        XCTAssertTrue(filter.isAllowed("IMG_0001.jpg"))
        XCTAssertTrue(filter.isAllowed("IMG_9999.jpg"))
        XCTAssertFalse(filter.isAllowed("IMG_1.jpg"))
    }

    func testCaseInsensitive() {
        let rules = [PathFilterRule(pattern: "**/*.JPG", filterType: "include")]
        let filter = PathFilter(tenantRules: rules)

        XCTAssertTrue(filter.isAllowed("photo.jpg"))
        XCTAssertTrue(filter.isAllowed("photo.JPG"))
        XCTAssertTrue(filter.isAllowed("photo.Jpg"))
    }

    func testBackslashNormalized() {
        let rules = [PathFilterRule(pattern: "photos/**", filterType: "include")]
        let filter = PathFilter(tenantRules: rules)

        XCTAssertTrue(filter.isAllowed("photos\\2024\\img.jpg"))
    }

    func testPathFilterRuleDecoding() throws {
        let json = """
        {"pattern": "**/*.tmp", "filter_type": "exclude"}
        """.data(using: .utf8)!

        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        let rule = try d.decode(PathFilterRule.self, from: json)
        XCTAssertEqual(rule.pattern, "**/*.tmp")
        XCTAssertEqual(rule.filterType, "exclude")
    }
}

// MARK: - Ingest Response

final class IngestResponseTests: XCTestCase {

    private var decoder: JSONDecoder {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }

    func testDecodesIngestResponse() throws {
        let json = """
        {
            "asset_id": "ast_new_001",
            "proxy_key": "proxies/ast_new_001.webp",
            "proxy_sha256": "abc123",
            "thumbnail_key": "thumbs/ast_new_001.webp",
            "thumbnail_sha256": "def456",
            "status": "PROXY_READY",
            "width": 2048,
            "height": 1365,
            "created": true
        }
        """.data(using: .utf8)!

        let response = try decoder.decode(IngestResponse.self, from: json)
        XCTAssertEqual(response.assetId, "ast_new_001")
        XCTAssertEqual(response.proxyKey, "proxies/ast_new_001.webp")
        XCTAssertEqual(response.status, "PROXY_READY")
        XCTAssertEqual(response.width, 2048)
        XCTAssertTrue(response.created)
    }

    func testDecodesIngestResponseUpdate() throws {
        let json = """
        {
            "asset_id": "ast_existing",
            "proxy_key": "proxies/ast_existing.webp",
            "proxy_sha256": "abc",
            "thumbnail_key": "thumbs/ast_existing.webp",
            "thumbnail_sha256": "def",
            "status": "DESCRIBED",
            "width": 1920,
            "height": 1080,
            "created": false
        }
        """.data(using: .utf8)!

        let response = try decoder.decode(IngestResponse.self, from: json)
        XCTAssertFalse(response.created)
        XCTAssertEqual(response.status, "DESCRIBED")
    }

    func testDecodesBatchDeleteResponse() throws {
        let json = """
        {
            "trashed": ["id1", "id2"],
            "not_found": ["id3"]
        }
        """.data(using: .utf8)!

        let response = try decoder.decode(BatchDeleteResponse.self, from: json)
        XCTAssertEqual(response.trashed.count, 2)
        XCTAssertEqual(response.notFound.count, 1)
    }

    func testDecodesBatchMoveResponse() throws {
        let json = """
        {"updated": 5, "skipped": 1}
        """.data(using: .utf8)!

        let response = try decoder.decode(BatchMoveResponse.self, from: json)
        XCTAssertEqual(response.updated, 5)
        XCTAssertEqual(response.skipped, 1)
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

    func testPutScanAndGet() {
        let data = Data("fake-jpeg-bytes".utf8)
        cache.putScan(assetId: "ast_001", jpegData: data, sourceSHA256: "abc123")

        let retrieved = cache.get(assetId: "ast_001")
        XCTAssertEqual(retrieved, data)
    }

    func testGetSHA() {
        let data = Data("image".utf8)
        cache.putScan(assetId: "ast_002", jpegData: data, sourceSHA256: "deadbeef")

        let sha = cache.getSHA(assetId: "ast_002")
        XCTAssertEqual(sha, "deadbeef")
    }

    func testGetSHAMissing() {
        XCTAssertNil(cache.getSHA(assetId: "nonexistent"))
    }

    func testIsValidTrue() {
        let data = Data("image".utf8)
        cache.putScan(assetId: "ast_003", jpegData: data, sourceSHA256: "match123")

        XCTAssertTrue(cache.isValid(assetId: "ast_003", sourceSHA256: "match123"))
    }

    func testIsValidFalseMismatch() {
        let data = Data("image".utf8)
        cache.putScan(assetId: "ast_004", jpegData: data, sourceSHA256: "old_sha")

        XCTAssertFalse(cache.isValid(assetId: "ast_004", sourceSHA256: "new_sha"))
    }

    func testIsValidFalseMissing() {
        XCTAssertFalse(cache.isValid(assetId: "ast_005", sourceSHA256: "any"))
    }

    func testHas() {
        XCTAssertFalse(cache.has(assetId: "ast_006"))

        cache.put(assetId: "ast_006", data: Data("bytes".utf8))
        XCTAssertTrue(cache.has(assetId: "ast_006"))
    }

    func testRemove() {
        let data = Data("image".utf8)
        cache.putScan(assetId: "ast_007", jpegData: data, sourceSHA256: "sha")

        XCTAssertTrue(cache.has(assetId: "ast_007"))
        XCTAssertNotNil(cache.getSHA(assetId: "ast_007"))

        cache.remove(assetId: "ast_007")

        XCTAssertFalse(cache.has(assetId: "ast_007"))
        XCTAssertNil(cache.getSHA(assetId: "ast_007"))
    }

    func testPutForBrowse() {
        let data = Data("server-proxy-bytes".utf8)
        cache.put(assetId: "ast_008", data: data)

        let retrieved = cache.get(assetId: "ast_008")
        XCTAssertEqual(retrieved, data)
        // No SHA sidecar for browse-cached items
        XCTAssertNil(cache.getSHA(assetId: "ast_008"))
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

// MARK: - Enrichment Models

final class EnrichmentModelTests: XCTestCase {

    private var decoder: JSONDecoder {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }

    func testDecodesBatchOCRResponse() throws {
        let json = """
        {"updated": 10, "skipped": 2}
        """.data(using: .utf8)!

        let response = try decoder.decode(BatchOCRResponse.self, from: json)
        XCTAssertEqual(response.updated, 10)
        XCTAssertEqual(response.skipped, 2)
    }

    func testDecodesBatchEmbeddingsResponse() throws {
        let json = """
        {"updated": 50, "skipped": 0}
        """.data(using: .utf8)!

        let response = try decoder.decode(BatchEmbeddingsResponse.self, from: json)
        XCTAssertEqual(response.updated, 50)
    }

    func testDecodesFacesSubmitResponse() throws {
        let json = """
        {"face_count": 3, "face_ids": ["f1", "f2", "f3"]}
        """.data(using: .utf8)!

        let response = try decoder.decode(FacesSubmitResponse.self, from: json)
        XCTAssertEqual(response.faceCount, 3)
        XCTAssertEqual(response.faceIds.count, 3)
    }

    func testDecodesTranscriptSubmitResponse() throws {
        let json = """
        {"asset_id": "ast_001", "status": "transcribed"}
        """.data(using: .utf8)!

        let response = try decoder.decode(TranscriptSubmitResponse.self, from: json)
        XCTAssertEqual(response.assetId, "ast_001")
        XCTAssertEqual(response.status, "transcribed")
    }

    func testDecodesRepairSummary() throws {
        let json = """
        {
            "total_assets": 15000,
            "missing_proxy": 0,
            "missing_exif": 5,
            "missing_vision": 1200,
            "missing_embeddings": 1500,
            "missing_faces": 14000,
            "missing_face_embeddings": 8000,
            "missing_ocr": 13000,
            "missing_video_scenes": 50,
            "missing_scene_vision": 10,
            "missing_transcription": 100,
            "stale_search_sync": 0
        }
        """.data(using: .utf8)!

        let summary = try decoder.decode(RepairSummary.self, from: json)
        XCTAssertEqual(summary.totalAssets, 15000)
        XCTAssertEqual(summary.missingVision, 1200)
        XCTAssertEqual(summary.missingEmbeddings, 1500)
        XCTAssertEqual(summary.missingFaces, 14000)
        XCTAssertEqual(summary.missingOcr, 13000)
        XCTAssertEqual(summary.missingTranscription, 100)
    }

    func testEncodesOCRRequest() throws {
        let request = BatchOCRRequest(items: [
            BatchOCRRequest.Item(assetId: "ast_001", ocrText: "Hello World"),
            BatchOCRRequest.Item(assetId: "ast_002", ocrText: ""),
        ])

        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        let data = try encoder.encode(request)
        let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]

        let items = json?["items"] as? [[String: Any]]
        XCTAssertEqual(items?.count, 2)
        XCTAssertEqual(items?[0]["asset_id"] as? String, "ast_001")
        XCTAssertEqual(items?[0]["ocr_text"] as? String, "Hello World")
    }

    func testEncodesFacesRequest() throws {
        let request = FacesSubmitRequest(
            detectionModel: "apple_vision",
            detectionModelVersion: "1",
            faces: [
                FacesSubmitRequest.FaceItem(
                    boundingBox: FacesSubmitRequest.BoundingBox(x1: 0.1, y1: 0.2, x2: 0.5, y2: 0.8),
                    detectionConfidence: 0.95,
                    embedding: nil
                ),
            ]
        )

        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        let data = try encoder.encode(request)
        let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]

        XCTAssertEqual(json?["detection_model"] as? String, "apple_vision")
        let faces = json?["faces"] as? [[String: Any]]
        XCTAssertEqual(faces?.count, 1)
        let box = faces?[0]["bounding_box"] as? [String: Any]
        XCTAssertEqual((box?["x1"] as? Double)!, 0.1, accuracy: 0.01)
    }

    func testDecodesBatchVisionResponse() throws {
        let json = """
        {"updated": 8, "skipped": 1}
        """.data(using: .utf8)!

        let response = try decoder.decode(BatchVisionResponse.self, from: json)
        XCTAssertEqual(response.updated, 8)
        XCTAssertEqual(response.skipped, 1)
    }

    func testEncodesVisionRequest() throws {
        let request = BatchVisionRequest(items: [
            BatchVisionRequest.Item(
                assetId: "ast_001",
                modelId: "openai-compatible",
                modelVersion: "gpt-4o",
                description: "A sunset over the ocean",
                tags: ["sunset", "ocean", "landscape"]
            ),
        ])

        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        let data = try encoder.encode(request)
        let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]

        let items = json?["items"] as? [[String: Any]]
        XCTAssertEqual(items?.count, 1)
        XCTAssertEqual(items?[0]["asset_id"] as? String, "ast_001")
        XCTAssertEqual(items?[0]["model_id"] as? String, "openai-compatible")
        XCTAssertEqual(items?[0]["description"] as? String, "A sunset over the ocean")
        XCTAssertEqual(items?[0]["tags"] as? [String], ["sunset", "ocean", "landscape"])
    }

    func testDecodesTenantContext() throws {
        let json = """
        {
            "tenant_id": "tnt_001",
            "vision_api_url": "http://localhost:1234/v1",
            "vision_api_key": "sk-test",
            "vision_model_id": "gpt-4o"
        }
        """.data(using: .utf8)!

        let ctx = try decoder.decode(TenantContext.self, from: json)
        XCTAssertEqual(ctx.tenantId, "tnt_001")
        XCTAssertEqual(ctx.visionApiUrl, "http://localhost:1234/v1")
        XCTAssertEqual(ctx.visionApiKey, "sk-test")
        XCTAssertEqual(ctx.visionModelId, "gpt-4o")
    }

    func testDecodesTenantContextEmptyFields() throws {
        let json = """
        {
            "tenant_id": "tnt_001",
            "vision_api_url": "",
            "vision_api_key": "",
            "vision_model_id": ""
        }
        """.data(using: .utf8)!

        let ctx = try decoder.decode(TenantContext.self, from: json)
        XCTAssertEqual(ctx.tenantId, "tnt_001")
        XCTAssertTrue(ctx.visionApiUrl.isEmpty)
    }

    func testEnrichmentOperationIncludesVision() {
        XCTAssertTrue(EnrichmentOperation.allCases.contains(.vision))
        XCTAssertEqual(EnrichmentOperation.vision.rawValue, "Generate Descriptions")
    }

    func testEnrichmentOperationIncludesVideoPreview() {
        XCTAssertTrue(EnrichmentOperation.allCases.contains(.videoPreview))
        XCTAssertEqual(EnrichmentOperation.videoPreview.rawValue, "Generate Preview")
    }

    func testEncodesEmbeddingRequest() throws {
        let request = BatchEmbeddingsRequest(items: [
            BatchEmbeddingsRequest.Item(
                assetId: "ast_001",
                modelId: "clip",
                modelVersion: "ViT-B-32-openai",
                vector: [0.1, 0.2, 0.3]
            ),
        ])

        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        let data = try encoder.encode(request)
        let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]

        let items = json?["items"] as? [[String: Any]]
        XCTAssertEqual(items?[0]["model_id"] as? String, "clip")
        XCTAssertEqual(items?[0]["model_version"] as? String, "ViT-B-32-openai")
    }
}
