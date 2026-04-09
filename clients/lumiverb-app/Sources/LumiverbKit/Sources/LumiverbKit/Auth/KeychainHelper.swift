import Foundation
import Security

public enum KeychainError: Error {
    case unexpectedStatus(OSStatus)
    case itemNotFound
    case encodingError
}

/// Abstraction over token persistence so tests can swap in an in-memory store.
public protocol TokenStore: Sendable {
    func save(key: String, value: String) throws
    func read(key: String) throws -> String
    func delete(key: String) throws
}

/// Simple keychain wrapper for storing string values (tokens, keys).
///
/// Uses `kSecClassGenericPassword` with a service identifier to namespace
/// entries. Thread-safe (Security framework is thread-safe).
public struct KeychainHelper: TokenStore, Sendable {
    private let service: String

    public init(service: String = "io.lumiverb.app") {
        self.service = service
    }

    public func save(key: String, value: String) throws {
        guard let data = value.data(using: .utf8) else {
            throw KeychainError.encodingError
        }

        let baseQuery: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
        ]

        // Try update-in-place first. The previous implementation upserted via
        // SecItemDelete + SecItemAdd, which on the legacy macOS keychain
        // triggers a separate ACL prompt for the delete (it has to access
        // the existing protected item to remove it). SecItemUpdate goes
        // straight to a single modify operation.
        let updateAttrs: [String: Any] = [
            kSecValueData as String: data,
        ]
        let updateStatus = SecItemUpdate(
            baseQuery as CFDictionary, updateAttrs as CFDictionary
        )
        if updateStatus == errSecSuccess {
            return
        }
        if updateStatus != errSecItemNotFound {
            throw KeychainError.unexpectedStatus(updateStatus)
        }

        // No existing item — create one.
        var addQuery = baseQuery
        addQuery[kSecValueData as String] = data
        addQuery[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        let addStatus = SecItemAdd(addQuery as CFDictionary, nil)
        guard addStatus == errSecSuccess else {
            throw KeychainError.unexpectedStatus(addStatus)
        }
    }

    public func read(key: String) throws -> String {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]

        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)

        guard status != errSecItemNotFound else {
            throw KeychainError.itemNotFound
        }
        guard status == errSecSuccess,
              let data = result as? Data,
              let string = String(data: data, encoding: .utf8) else {
            throw KeychainError.unexpectedStatus(status)
        }
        return string
    }

    public func delete(key: String) throws {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
        ]
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainError.unexpectedStatus(status)
        }
    }
}
