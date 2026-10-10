import XCTest
@testable import LumiverbKit

final class ExifLocationTests: XCTestCase {
    func testZeroZeroIsNoFix() {
        XCTAssertNil(ExifLocation.gps(["Latitude": 0.0, "Longitude": 0.0, "LatitudeRef": "N", "LongitudeRef": "E"]))
        XCTAssertNil(ExifLocation.gps(nil))
        let fix = ExifLocation.gps(["Latitude": 48.85, "Longitude": 2.29, "LatitudeRef": "S", "LongitudeRef": "W"])
        XCTAssertEqual(fix?.lat, -48.85)
        XCTAssertEqual(fix?.lon, -2.29)
        XCTAssertFalse(ExifLocation.isFix(lat: 91, lon: 0))
    }

    func testOffsetMinutes() {
        XCTAssertEqual(ExifLocation.offsetMinutes(["OffsetTimeOriginal": "+02:00"]), 120)
        XCTAssertEqual(ExifLocation.offsetMinutes(["OffsetTimeOriginal": "-05:30"]), -330)
        XCTAssertEqual(ExifLocation.offsetMinutes(["OffsetTime": "+0545"]), 345)
        XCTAssertEqual(ExifLocation.offsetMinutes(["OffsetTimeOriginal": "junk", "OffsetTime": "+01:00"]), 60)
        XCTAssertNil(ExifLocation.offsetMinutes(["OffsetTimeOriginal": "+15:00"]))
        XCTAssertNil(ExifLocation.offsetMinutes([:]))
    }

    func testTakenAtIsTheWallClockWhateverThisMacsZone() {
        let saved = NSTimeZone.default
        defer { NSTimeZone.default = saved }
        for zone in ["America/New_York", "Asia/Kathmandu", "UTC"] {
            NSTimeZone.default = TimeZone(identifier: zone)!
            XCTAssertEqual(ExifLocation.wallClockTakenAt("2024:06:15 10:30:00"), "2024-06-15T10:30:00Z")
        }
        XCTAssertEqual(ExifLocation.wallClockTakenAt("2024:03:10 02:30:00.25"), "2024-03-10T02:30:00Z")
        XCTAssertNil(ExifLocation.wallClockTakenAt("junk"))
    }

    func testAccuracy() {
        XCTAssertEqual(ExifLocation.accuracyMeters(["HPositioningError": 4.7]), 4.7)
        XCTAssertNil(ExifLocation.accuracyMeters(["HPositioningError": -1.0]))
        XCTAssertNil(ExifLocation.accuracyMeters(nil))
    }

    func testAssetDetailDecodesAPersonsLocation() throws {
        let json = """
        {"asset_id": "a", "library_id": "l", "rel_path": "x.jpg", "media_type": "image", "status": "ok",
         "gps_lat": 40.0, "gps_lon": -74.0, "gps_accuracy_m": 5.0,
         "location": {"lat": 1.0, "lon": 2.0, "radius_m": 0, "source": "person", "status": "applied",
                      "basis_summary": null, "set_by": "rob@x.io", "set_at": "2026-10-09T00:00:00+00:00"}}
        """.data(using: .utf8)!
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let detail = try decoder.decode(AssetDetail.self, from: json)
        XCTAssertEqual(detail.location?.source, "person")
        XCTAssertEqual(detail.location?.setBy, "rob@x.io")
        XCTAssertEqual(detail.gpsAccuracyM, 5.0)
    }
}
