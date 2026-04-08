import XCTest
@testable import LumiverbKit

/// Tests the real KeychainHelper against the macOS Keychain.
/// Uses a unique service name per test run to avoid conflicts.
final class KeychainHelperTests: XCTestCase {

    private var keychain: KeychainHelper!
    private let testService = "io.lumiverb.app.test.\(UUID().uuidString)"

    override func setUp() {
        super.setUp()
        keychain = KeychainHelper(service: testService)
    }

    override func tearDown() {
        // Clean up test entries
        try? keychain.delete(key: "testKey")
        try? keychain.delete(key: "anotherKey")
        super.tearDown()
    }

    // MARK: - Save and read

    func testSaveAndReadRoundTrip() throws {
        try keychain.save(key: "testKey", value: "secret-token-123")
        let value = try keychain.read(key: "testKey")
        XCTAssertEqual(value, "secret-token-123")
    }

    func testSaveOverwritesExistingValue() throws {
        try keychain.save(key: "testKey", value: "first-value")
        try keychain.save(key: "testKey", value: "second-value")
        let value = try keychain.read(key: "testKey")
        XCTAssertEqual(value, "second-value")
    }

    func testSavePreservesUTF8() throws {
        let token = "eyJhbGciOiJIUzI1NiJ9.dG9rZW4=.signature"
        try keychain.save(key: "testKey", value: token)
        let value = try keychain.read(key: "testKey")
        XCTAssertEqual(value, token)
    }

    // MARK: - Read missing key

    func testReadMissingKeyThrowsItemNotFound() {
        do {
            _ = try keychain.read(key: "nonexistent")
            XCTFail("Expected KeychainError.itemNotFound")
        } catch let error as KeychainError {
            if case .itemNotFound = error {
                // Expected
            } else {
                XCTFail("Expected .itemNotFound, got \(error)")
            }
        } catch {
            XCTFail("Unexpected error type: \(error)")
        }
    }

    // MARK: - Delete

    func testDeleteRemovesEntry() throws {
        try keychain.save(key: "testKey", value: "to-delete")
        try keychain.delete(key: "testKey")

        do {
            _ = try keychain.read(key: "testKey")
            XCTFail("Expected KeychainError.itemNotFound after delete")
        } catch let error as KeychainError {
            if case .itemNotFound = error {
                // Expected
            } else {
                XCTFail("Expected .itemNotFound, got \(error)")
            }
        }
    }

    func testDeleteNonexistentKeyDoesNotThrow() throws {
        // Should not throw — delete tolerates errSecItemNotFound
        try keychain.delete(key: "never-existed")
    }

    // MARK: - Multiple keys

    func testMultipleKeysAreIndependent() throws {
        try keychain.save(key: "testKey", value: "value-a")
        try keychain.save(key: "anotherKey", value: "value-b")

        XCTAssertEqual(try keychain.read(key: "testKey"), "value-a")
        XCTAssertEqual(try keychain.read(key: "anotherKey"), "value-b")

        try keychain.delete(key: "testKey")
        // anotherKey should still be readable
        XCTAssertEqual(try keychain.read(key: "anotherKey"), "value-b")
    }

    // MARK: - TokenStore protocol conformance

    func testConformsToTokenStoreProtocol() {
        let store: any TokenStore = keychain
        XCTAssertNotNil(store, "KeychainHelper should conform to TokenStore")
    }
}
