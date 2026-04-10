import Foundation

/// Token persistence backed by a plain JSON file in
/// `~/Library/Application Support/<bundleIdentifier>/credentials.json`,
/// chmod 0600.
///
/// This replaces `KeychainHelper` as the default `TokenStore` for the
/// macOS app because the legacy macOS keychain prompts the user (per
/// access, per modifying binary identity) for ad-hoc / unsigned dev
/// builds. Each rebuild rotates the binary's identity, so even
/// "Always Allow" stops working after the next build.
///
/// **Threat model:** the file is mode 0600, owner-only. Another local
/// user on the machine cannot read it. A malicious process running as
/// the same user can. This is a strict improvement over the legacy
/// keychain on dev builds (which has no per-app ACL there either, since
/// rebuilds rotate identity), and is comparable to what the Python CLI
/// does for credential persistence. Once the macOS app ships as a
/// properly signed `.app` we may revisit and switch to the data-
/// protection keychain (`kSecUseDataProtectionKeychain`).
///
/// **No migration** is performed from `KeychainHelper`. The previous
/// store would have triggered the keychain prompt we're trying to
/// avoid, defeating the purpose of switching. Users re-authenticate
/// once on first launch after upgrade and never see another prompt.
public struct FileTokenStore: TokenStore, Sendable {
    private let fileURL: URL

    /// Default location at `~/Library/Application Support/<bundleId>/credentials.json`.
    /// Falls back to the temp directory only if Application Support is
    /// genuinely unavailable, which on macOS is essentially never. The
    /// fallback exists so init can be non-throwing and usable as a
    /// default parameter value.
    ///
    /// **iOS:** this initializer is intentionally a `fatalError` on iOS
    /// (ADR-015 M1 defensive guard). The iOS app uses `KeychainHelper`
    /// — the data-protection keychain doesn't prompt and is the right
    /// home for credentials. `FileTokenStore`'s app-support file path is
    /// outside the iOS sandbox model and there is no legitimate reason
    /// to instantiate this on iOS. The struct still exists in the
    /// LumiverbKit module on iOS so cross-platform code that references
    /// the type compiles, but constructing it traps loudly.
    public init(bundleIdentifier: String = "io.lumiverb.app") {
        #if os(iOS)
        fatalError(
            "FileTokenStore is macOS-only. Use KeychainHelper on iOS — " +
            "the data-protection keychain never prompts and is the " +
            "supported credentials store on iOS."
        )
        #else
        let appSupport = FileManager.default.urls(
            for: .applicationSupportDirectory, in: .userDomainMask
        ).first ?? URL(fileURLWithPath: NSTemporaryDirectory())
        let dir = appSupport.appendingPathComponent(bundleIdentifier, isDirectory: true)
        self.fileURL = dir.appendingPathComponent("credentials.json")
        #endif
    }

    /// Test seam: explicit file path. The parent directory will be
    /// created on the first `save` call.
    public init(fileURL: URL) {
        self.fileURL = fileURL
    }

    public func save(key: String, value: String) throws {
        var dict = readDictOrEmpty()
        dict[key] = value
        try writeDict(dict)
    }

    public func read(key: String) throws -> String {
        let dict = try readDict()
        guard let value = dict[key] else {
            throw KeychainError.itemNotFound
        }
        return value
    }

    public func delete(key: String) throws {
        // Reading errors that are NOT "file missing" should propagate —
        // a corrupt file or unreadable parent should fail loudly rather
        // than silently dropping the delete.
        var dict: [String: String]
        do {
            dict = try readDict()
        } catch KeychainError.itemNotFound {
            return
        }
        guard dict[key] != nil else { return }
        dict.removeValue(forKey: key)
        if dict.isEmpty {
            // Remove the file entirely so a fresh logout leaves no
            // trace on disk. Ignore "doesn't exist" errors — race-safe.
            try? FileManager.default.removeItem(at: fileURL)
        } else {
            try writeDict(dict)
        }
    }

    // MARK: - Private

    private func readDict() throws -> [String: String] {
        let data: Data
        do {
            data = try Data(contentsOf: fileURL)
        } catch {
            // Treat any read failure (file missing, permission denied,
            // etc.) as "item not found" so callers can use the same
            // error path as the keychain implementation. Distinguishing
            // missing-vs-unreadable would change the auth flow without
            // benefit — both mean "log in again".
            throw KeychainError.itemNotFound
        }
        guard let dict = try? JSONSerialization.jsonObject(with: data) as? [String: String] else {
            throw KeychainError.encodingError
        }
        return dict
    }

    private func readDictOrEmpty() -> [String: String] {
        (try? readDict()) ?? [:]
    }

    private func writeDict(_ dict: [String: String]) throws {
        // Ensure the parent directory exists with mode 0700 — set
        // BEFORE writing the file so an interrupted save doesn't leave
        // a world-readable directory containing a 0600 file.
        let dir = fileURL.deletingLastPathComponent()
        if !FileManager.default.fileExists(atPath: dir.path) {
            try FileManager.default.createDirectory(
                at: dir,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
        }

        let data = try JSONSerialization.data(
            withJSONObject: dict, options: [.sortedKeys]
        )
        // `.atomic` writes to a temp file and renames into place, so a
        // crashed save can't truncate or partially overwrite the live
        // credentials file.
        try data.write(to: fileURL, options: .atomic)

        // chmod 0600 every save — `.atomic` write recreates the file
        // with whatever umask is set, so we can't assume a previous
        // chmod sticks across writes. Owner read/write only.
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o600],
            ofItemAtPath: fileURL.path
        )
    }
}
