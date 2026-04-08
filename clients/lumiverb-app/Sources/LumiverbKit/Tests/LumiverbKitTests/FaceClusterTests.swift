import XCTest
@testable import LumiverbKit

/// Tests decoding of `ClustersResponse`, `ClusterItem`, `ClusterFacesResponse`,
/// and `ClusterDismissResult`, plus encoding of `ClusterNameRequest`.
final class FaceClusterTests: XCTestCase {

    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    private let snakeEncoder: JSONEncoder = {
        let e = JSONEncoder()
        e.keyEncodingStrategy = .convertToSnakeCase
        e.outputFormatting = [.sortedKeys]
        return e
    }()

    // MARK: - ClustersResponse (the cluster review summary)

    func testDecodesClustersResponse() throws {
        let json = """
        {
            "clusters": [
                {
                    "cluster_index": 0,
                    "size": 42,
                    "faces": [
                        {
                            "face_id": "f1",
                            "asset_id": "a1",
                            "bounding_box": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
                            "detection_confidence": 0.95,
                            "rel_path": "img1.jpg"
                        },
                        {
                            "face_id": "f2",
                            "asset_id": "a2",
                            "bounding_box": null,
                            "detection_confidence": 0.88,
                            "rel_path": "img2.jpg"
                        }
                    ]
                },
                {
                    "cluster_index": 1,
                    "size": 7,
                    "faces": []
                }
            ],
            "truncated": false,
            "max_cluster_size": 42
        }
        """.data(using: .utf8)!

        let response = try decoder.decode(ClustersResponse.self, from: json)
        XCTAssertEqual(response.clusters.count, 2)
        XCTAssertFalse(response.truncated)
        XCTAssertEqual(response.maxClusterSize, 42)

        let first = response.clusters[0]
        XCTAssertEqual(first.clusterIndex, 0)
        XCTAssertEqual(first.size, 42)
        XCTAssertEqual(first.faces.count, 2)
        XCTAssertEqual(first.id, 0)
        XCTAssertEqual(first.faces[0].faceId, "f1")
        XCTAssertEqual(first.faces[0].boundingBox?.width, 0.3)
        XCTAssertNil(first.faces[1].boundingBox)
    }

    func testDecodesClustersResponseTruncated() throws {
        let json = """
        {
            "clusters": [],
            "truncated": true,
            "max_cluster_size": 1528
        }
        """.data(using: .utf8)!

        let response = try decoder.decode(ClustersResponse.self, from: json)
        XCTAssertTrue(response.truncated)
        XCTAssertEqual(response.maxClusterSize, 1528)
        XCTAssertTrue(response.clusters.isEmpty)
    }

    // MARK: - ClusterFacesResponse (paginated full face list per cluster)

    func testDecodesClusterFacesResponse() throws {
        let json = """
        {
            "items": [
                {
                    "face_id": "f1",
                    "asset_id": "a1",
                    "bounding_box": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
                    "detection_confidence": 0.9,
                    "rel_path": "img1.jpg"
                }
            ],
            "total": 200,
            "next_cursor": "f1"
        }
        """.data(using: .utf8)!

        let response = try decoder.decode(ClusterFacesResponse.self, from: json)
        XCTAssertEqual(response.items.count, 1)
        XCTAssertEqual(response.total, 200)
        XCTAssertEqual(response.nextCursor, "f1")
    }

    func testDecodesClusterFacesResponseLastPage() throws {
        let json = """
        {"items": [], "total": 0, "next_cursor": null}
        """.data(using: .utf8)!

        let response = try decoder.decode(ClusterFacesResponse.self, from: json)
        XCTAssertTrue(response.items.isEmpty)
        XCTAssertEqual(response.total, 0)
        XCTAssertNil(response.nextCursor)
    }

    // MARK: - ClusterDismissResult

    func testDecodesClusterDismissResult() throws {
        let json = #"{"person_id": "per_dismissed_xyz"}"#.data(using: .utf8)!
        let result = try decoder.decode(ClusterDismissResult.self, from: json)
        XCTAssertEqual(result.personId, "per_dismissed_xyz")
    }

    // MARK: - ClusterNameRequest encoding

    /// Default Swift JSONEncoder *omits* nil optional fields rather than
    /// encoding them as `"key":null`. The server-side Pydantic
    /// `ClusterNameRequest` accepts both forms (Optional fields default
    /// to None), so omit-nil is the cleaner wire format.
    func testEncodesClusterNameRequestNewPerson() throws {
        let req = ClusterNameRequest(newPersonName: "Aunt Susan")
        let data = try snakeEncoder.encode(req)
        let json = String(data: data, encoding: .utf8)!
        XCTAssertEqual(json, #"{"display_name":"Aunt Susan"}"#)
    }

    func testEncodesClusterNameRequestExistingPerson() throws {
        let req = ClusterNameRequest(existingPersonId: "per_existing")
        let data = try snakeEncoder.encode(req)
        let json = String(data: data, encoding: .utf8)!
        XCTAssertEqual(json, #"{"person_id":"per_existing"}"#)
    }
}
