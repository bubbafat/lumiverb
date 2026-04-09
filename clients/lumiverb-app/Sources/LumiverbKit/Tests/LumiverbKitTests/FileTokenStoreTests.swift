import XCTest
@testable import LumiverbKit

final class FileTokenStoreTests: XCTestCase {

    private var tempDir: URL!
    private var fileURL: URL!
    private var store: FileTokenStore!

    override func setUp() {
        super.setUp()
        tempDir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("FileTokenStoreTests-\(UUID().uuidString)", isDirectory: true)
        fileURL = tempDir.appendingPathComponent("credentials.json")
        store = FileTokenStore(fileURL: fileURL)
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: tempDir)
        super.tearDown()
    }

    // MARK: - Basic round-trip

    func testSaveAndRead() throws {
        try store.save(key: "accessToken", value: "abc123")
        let value = try store.read(key: "accessToken")
        XCTAssertEqual(value, "abc123")
    }

    func testReadMissingFileThrowsItemNotFound() {
        // No save call — file doesn't exist yet. read() should map all
        // file-not-found / unreadable cases to itemNotFound so callers
        // can use the same "no token, log in again" branch as the old
        // KeychainHelper.
        XCTAssertThrowsError(try store.read(key: "accessToken")) { error in
            guard case KeychainError.itemNotFound = error else {
                XCTFail("Expected itemNotFound, got \(error)")
                return
            }
        }
    }

    func testReadMissingKeyAfterSavingDifferentKeyThrowsItemNotFound() throws {
        try store.save(key: "accessToken", value: "abc")
        XCTAssertThrowsError(try store.read(key: "refreshToken")) { error in
            guard case KeychainError.itemNotFound = error else {
                XCTFail("Expected itemNotFound, got \(error)")
                return
            }
        }
    }

    func testOverwriteExistingValue() throws {
        try store.save(key: "accessToken", value: "v1")
        try store.save(key: "accessToken", value: "v2")
        XCTAssertEqual(try store.read(key: "accessToken"), "v2")
    }

    func testMultipleKeysCoexist() throws {
        try store.save(key: "accessToken", value: "a")
        try store.save(key: "refreshToken", value: "r")
        XCTAssertEqual(try store.read(key: "accessToken"), "a")
        XCTAssertEqual(try store.read(key: "refreshToken"), "r")
    }

    // MARK: - Delete

    func testDeleteRemovesValue() throws {
        try store.save(key: "accessToken", value: "abc")
        try store.delete(key: "accessToken")
        XCTAssertThrowsError(try store.read(key: "accessToken"))
    }

    func testDeleteIsIdempotent() throws {
        // Deleting a non-existent key is a no-op, not an error. Mirrors
        // the keychain implementation, which uses errSecItemNotFound as
        // a success case for delete.
        try store.delete(key: "nonExistent")
    }

    func testDeleteOneKeyPreservesOthers() throws {
        try store.save(key: "accessToken", value: "a")
        try store.save(key: "refreshToken", value: "r")
        try store.delete(key: "accessToken")
        XCTAssertThrowsError(try store.read(key: "accessToken"))
        XCTAssertEqual(try store.read(key: "refreshToken"), "r")
    }

    func testDeleteLastKeyRemovesFile() throws {
        // When the last key is deleted, the file itself should be
        // removed so a fresh logout leaves no trace on disk. Avoids
        // an empty `{}` sitting around.
        try store.save(key: "accessToken", value: "a")
        try store.delete(key: "accessToken")
        XCTAssertFalse(FileManager.default.fileExists(atPath: fileURL.path))
    }

    // MARK: - Filesystem properties

    func testParentDirectoryIsCreatedOnFirstSave() throws {
        // Sanity: parent doesn't exist, save creates it.
        XCTAssertFalse(FileManager.default.fileExists(atPath: tempDir.path))
        try store.save(key: "accessToken", value: "abc")
        XCTAssertTrue(FileManager.default.fileExists(atPath: tempDir.path))
    }

    func testFilePermissionsAre0600() throws {
        try store.save(key: "accessToken", value: "abc")
        let attrs = try FileManager.default.attributesOfItem(atPath: fileURL.path)
        let perms = attrs[.posixPermissions] as? NSNumber
        XCTAssertEqual(perms?.int16Value, 0o600,
                       "Credentials file must be owner-read/write only")
    }

    func testParentDirectoryPermissionsAre0700() throws {
        try store.save(key: "accessToken", value: "abc")
        let attrs = try FileManager.default.attributesOfItem(atPath: tempDir.path)
        let perms = attrs[.posixPermissions] as? NSNumber
        XCTAssertEqual(perms?.int16Value, 0o700,
                       "Credentials directory must be owner-only")
    }

    func testFilePermissionsStickAcrossOverwrites() throws {
        // .atomic write recreates the file with whatever umask gives,
        // so we re-chmod every save. Verify that an overwrite still
        // ends in 0600 (regression guard against forgetting the chmod
        // on a code path that touches save).
        try store.save(key: "accessToken", value: "v1")
        try store.save(key: "accessToken", value: "v2")
        let attrs = try FileManager.default.attributesOfItem(atPath: fileURL.path)
        let perms = attrs[.posixPermissions] as? NSNumber
        XCTAssertEqual(perms?.int16Value, 0o600)
    }

    // MARK: - Persistence across instances

    func testNewInstanceReadsExistingFile() throws {
        try store.save(key: "accessToken", value: "abc")
        // Drop the original instance and construct a new one against
        // the same file — verifies persistence isn't tied to in-memory
        // state on the struct.
        let other = FileTokenStore(fileURL: fileURL)
        XCTAssertEqual(try other.read(key: "accessToken"), "abc")
    }
}
