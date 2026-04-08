import XCTest
@testable import LumiverbKit

/// Tests for IngestResponse.swift models that lack coverage:
/// BatchMoveRequest encoding, BatchDeleteRequest encoding.
final class IngestModelTests: XCTestCase {

    private let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.keyEncodingStrategy = .convertToSnakeCase
        e.outputFormatting = .sortedKeys
        return e
    }()

    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    // MARK: - BatchMoveRequest encoding

    func testBatchMoveRequestEncoding() throws {
        let request = BatchMoveRequest(items: [
            BatchMoveRequest.Item(assetId: "ast_1", relPath: "photos/moved.jpg"),
            BatchMoveRequest.Item(assetId: "ast_2", relPath: "archive/old.png"),
        ])

        let data = try encoder.encode(request)
        let json = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        let items = json["items"] as! [[String: Any]]
        XCTAssertEqual(items.count, 2)
        XCTAssertEqual(items[0]["asset_id"] as? String, "ast_1")
        XCTAssertEqual(items[0]["rel_path"] as? String, "photos/moved.jpg")
        XCTAssertEqual(items[1]["asset_id"] as? String, "ast_2")
        XCTAssertEqual(items[1]["rel_path"] as? String, "archive/old.png")
    }

    // MARK: - BatchDeleteRequest encoding

    func testBatchDeleteRequestEncoding() throws {
        let request = BatchDeleteRequest(assetIds: ["ast_a", "ast_b", "ast_c"])
        let data = try encoder.encode(request)
        let json = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        let ids = json["asset_ids"] as! [String]
        XCTAssertEqual(ids, ["ast_a", "ast_b", "ast_c"])
    }

    // MARK: - IngestResponse with all fields populated

    func testDecodesIngestResponseWithDimensions() throws {
        let json = """
        {
            "asset_id": "ast_new",
            "proxy_key": "proxies/abc.jpg",
            "proxy_sha256": "deadbeef",
            "thumbnail_key": "thumbnails/abc.jpg",
            "thumbnail_sha256": "cafebabe",
            "status": "created",
            "width": 4000,
            "height": 3000,
            "created": true
        }
        """.data(using: .utf8)!

        let response = try decoder.decode(IngestResponse.self, from: json)
        XCTAssertEqual(response.assetId, "ast_new")
        XCTAssertEqual(response.proxyKey, "proxies/abc.jpg")
        XCTAssertEqual(response.proxySha256, "deadbeef")
        XCTAssertEqual(response.thumbnailKey, "thumbnails/abc.jpg")
        XCTAssertEqual(response.thumbnailSha256, "cafebabe")
        XCTAssertEqual(response.status, "created")
        XCTAssertEqual(response.width, 4000)
        XCTAssertEqual(response.height, 3000)
        XCTAssertTrue(response.created)
    }

    func testDecodesIngestResponseUpdated() throws {
        let json = """
        {
            "asset_id": "ast_existing",
            "proxy_key": null,
            "proxy_sha256": null,
            "thumbnail_key": null,
            "thumbnail_sha256": null,
            "status": "updated",
            "width": null,
            "height": null,
            "created": false
        }
        """.data(using: .utf8)!

        let response = try decoder.decode(IngestResponse.self, from: json)
        XCTAssertEqual(response.assetId, "ast_existing")
        XCTAssertNil(response.proxyKey)
        XCTAssertNil(response.proxySha256)
        XCTAssertNil(response.thumbnailKey)
        XCTAssertNil(response.thumbnailSha256)
        XCTAssertEqual(response.status, "updated")
        XCTAssertNil(response.width)
        XCTAssertNil(response.height)
        XCTAssertFalse(response.created)
    }
}
