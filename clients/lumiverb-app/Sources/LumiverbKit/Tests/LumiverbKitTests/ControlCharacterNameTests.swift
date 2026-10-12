import XCTest
@testable import LumiverbKit

/// A name with a control character is never scanned (ScanPipeline.discoverFiles)
/// and the server refuses it. The same cases as `has_control_character`'s,
/// its twin in `src/shared/path_filter.py` (`tests/test_path_filter.py`).
final class ControlCharacterNameTests: XCTestCase {

    func testANameWithANewlineIsNeverScanned() {
        XCTAssertTrue(PathFilter.hasControlCharacter("a\nb.jpg"))
        XCTAssertTrue(PathFilter.hasControlCharacter("Shoots/day\r1/a.jpg"))
    }

    func testEveryC0ControlAndDelAreRefused() {
        for value in Array(0x00...0x1F) + [0x7F] {
            let name = "a" + String(UnicodeScalar(UInt8(value))) + "b.jpg"
            XCTAssertTrue(PathFilter.hasControlCharacter(name), "U+\(String(format: "%04X", value))")
        }
    }

    func testOrdinaryNamesAreScanned() {
        for name in [
            "photo.jpg", "Shoots/Day 1/A001.mov", "café/résumé.jpg",
            "Cafe\u{301}/re\u{301}sume\u{301}.jpg",  // NFD, as macOS hands it out
            "🎉 party.jpg", "日本/写真.jpg", "a~b!c@#$%^&()[]{}.jpg", "a\u{A0}b.jpg",
        ] {
            XCTAssertFalse(PathFilter.hasControlCharacter(name), name.debugDescription)
        }
    }
}
