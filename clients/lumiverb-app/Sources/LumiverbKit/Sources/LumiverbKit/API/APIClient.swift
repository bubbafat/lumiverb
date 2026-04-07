import Foundation

// MARK: - Errors

/// Errors from the Lumiverb API client.
public enum APIError: Error, Equatable {
    case unauthorized
    case serverError(statusCode: Int, message: String)
    case decodingError(String)
    case networkError(String)
    case noToken
}

/// Matches the server error envelope: `{"error": {"code": "...", "message": "..."}}`.
struct ErrorEnvelope: Decodable {
    struct Detail: Decodable {
        let code: String
        let message: String
    }
    let error: Detail
}

// MARK: - Client

/// Thread-safe API client for the Lumiverb REST API.
///
/// Uses `actor` for safe concurrent access to the mutable token.
/// All requests go through the shared `URLSession` with `async/await`.
public actor APIClient {
    public let baseURL: URL
    private let session: URLSession
    private var accessToken: String?
    private let decoder: JSONDecoder
    /// Callback for automatic token refresh on 401. Set by AuthManager.
    private var refreshHandler: (@Sendable () async -> Bool)?

    public init(baseURL: URL, accessToken: String? = nil) {
        self.baseURL = baseURL
        self.session = URLSession.shared
        self.accessToken = accessToken

        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        self.decoder = decoder
    }

    public func setAccessToken(_ token: String?) {
        self.accessToken = token
    }

    /// Set a handler that will be called to refresh the token on 401.
    /// The handler should return true if refresh succeeded.
    public func setRefreshHandler(_ handler: @escaping @Sendable () async -> Bool) {
        self.refreshHandler = handler
    }

    public func currentToken() -> String? {
        accessToken
    }

    // MARK: - HTTP methods

    public func get<T: Decodable>(
        _ path: String,
        query: [String: String]? = nil
    ) async throws -> T {
        try await request("GET", path: path, query: query)
    }

    public func post<T: Decodable>(
        _ path: String,
        body: (any Encodable)? = nil
    ) async throws -> T {
        try await request("POST", path: path, body: body)
    }

    public func put<T: Decodable>(
        _ path: String,
        body: (any Encodable)? = nil
    ) async throws -> T {
        try await request("PUT", path: path, body: body)
    }

    public func delete(_ path: String) async throws {
        let _: EmptyResponse = try await request("DELETE", path: path)
    }

    /// POST that skips auto-refresh on 401 (used for token refresh itself).
    public func postNoRetry<T: Decodable>(
        _ path: String,
        body: (any Encodable)? = nil
    ) async throws -> T {
        try await request("POST", path: path, body: body, skipRefresh: true)
    }

    /// POST without requiring an access token (used for login).
    public func postUnauthenticated<T: Decodable>(
        _ path: String,
        body: (any Encodable)? = nil
    ) async throws -> T {
        try await request("POST", path: path, body: body, authenticated: false)
    }

    // MARK: - Multipart upload

    /// POST multipart/form-data with file data and text fields.
    /// Used for `/v1/ingest` which requires a proxy image plus metadata fields.
    public func postMultipart<T: Decodable>(
        _ path: String,
        fields: [String: String],
        fileField: String,
        fileData: Data,
        fileName: String,
        mimeType: String
    ) async throws -> T {
        guard let token = accessToken else {
            throw APIError.noToken
        }

        let boundary = "Boundary-\(UUID().uuidString)"
        let url = baseURL.appendingPathComponent(path)
        var urlRequest = URLRequest(url: url)
        urlRequest.httpMethod = "POST"
        urlRequest.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        urlRequest.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")

        var body = Data()

        // Text fields
        for (key, value) in fields {
            body.append("--\(boundary)\r\n")
            body.append("Content-Disposition: form-data; name=\"\(key)\"\r\n\r\n")
            body.append("\(value)\r\n")
        }

        // File field
        body.append("--\(boundary)\r\n")
        body.append("Content-Disposition: form-data; name=\"\(fileField)\"; filename=\"\(fileName)\"\r\n")
        body.append("Content-Type: \(mimeType)\r\n\r\n")
        body.append(fileData)
        body.append("\r\n")

        body.append("--\(boundary)--\r\n")

        urlRequest.httpBody = body

        var data: Data
        var response: URLResponse
        do {
            (data, response) = try await session.data(for: urlRequest)
        } catch {
            throw APIError.networkError(error.localizedDescription)
        }

        guard let http = response as? HTTPURLResponse else {
            throw APIError.networkError("Invalid response")
        }

        if http.statusCode == 401 {
            if let refreshHandler, await refreshHandler(), let newToken = accessToken {
                urlRequest.setValue("Bearer \(newToken)", forHTTPHeaderField: "Authorization")
                do {
                    (data, response) = try await session.data(for: urlRequest)
                } catch {
                    throw APIError.networkError(error.localizedDescription)
                }
                guard let retryHttp = response as? HTTPURLResponse else {
                    throw APIError.networkError("Invalid response")
                }
                if retryHttp.statusCode == 401 { throw APIError.unauthorized }
            } else {
                throw APIError.unauthorized
            }
        }

        guard let finalHttp = response as? HTTPURLResponse else {
            throw APIError.networkError("Invalid response")
        }

        if finalHttp.statusCode >= 400 {
            if let envelope = try? decoder.decode(ErrorEnvelope.self, from: data) {
                throw APIError.serverError(
                    statusCode: finalHttp.statusCode,
                    message: envelope.error.message
                )
            }
            let bodyStr = String(data: data, encoding: .utf8) ?? "Unknown error"
            throw APIError.serverError(statusCode: finalHttp.statusCode, message: bodyStr)
        }

        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            let preview = String(data: data.prefix(500), encoding: .utf8) ?? "(binary)"
            throw APIError.decodingError("Multipart response decode failed: \(error) — response: \(preview)")
        }
    }

    /// POST multipart/form-data for DELETE body (JSON body with asset IDs).
    public func deleteWithBody<T: Decodable>(
        _ path: String,
        body: (any Encodable)? = nil
    ) async throws -> T {
        try await request("DELETE", path: path, body: body)
    }

    // MARK: - Binary data (images)

    /// Fetch raw bytes (thumbnails, proxies) with authentication.
    /// Returns `nil` for 404 (no proxy/thumbnail generated yet).
    public func getData(_ path: String) async throws -> Data? {
        guard let token = accessToken else {
            throw APIError.noToken
        }

        let url = baseURL.appendingPathComponent(path)
        var urlRequest = URLRequest(url: url)
        urlRequest.httpMethod = "GET"
        urlRequest.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")

        var data: Data
        var response: URLResponse
        do {
            (data, response) = try await session.data(for: urlRequest)
        } catch {
            throw APIError.networkError(error.localizedDescription)
        }

        guard var http = response as? HTTPURLResponse else {
            throw APIError.networkError("Invalid response")
        }

        if http.statusCode == 401 {
            if let refreshHandler, await refreshHandler(), let newToken = accessToken {
                urlRequest.setValue("Bearer \(newToken)", forHTTPHeaderField: "Authorization")
                do {
                    (data, response) = try await session.data(for: urlRequest)
                } catch {
                    throw APIError.networkError(error.localizedDescription)
                }
                guard let retryHttp = response as? HTTPURLResponse else {
                    throw APIError.networkError("Invalid response")
                }
                http = retryHttp
            }
            if http.statusCode == 401 { throw APIError.unauthorized }
        }
        if http.statusCode == 404 { return nil }
        if http.statusCode >= 400 {
            let body = String(data: data, encoding: .utf8) ?? "Unknown error"
            throw APIError.serverError(statusCode: http.statusCode, message: body)
        }

        return data
    }

    // MARK: - Core request

    private func request<T: Decodable>(
        _ method: String,
        path: String,
        query: [String: String]? = nil,
        body: (any Encodable)? = nil,
        authenticated: Bool = true,
        skipRefresh: Bool = false
    ) async throws -> T {
        if authenticated && accessToken == nil {
            throw APIError.noToken
        }

        var components = URLComponents(
            url: baseURL.appendingPathComponent(path),
            resolvingAgainstBaseURL: false
        )!
        if let query, !query.isEmpty {
            components.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) }
        }

        var urlRequest = URLRequest(url: components.url!)
        urlRequest.httpMethod = method
        urlRequest.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if authenticated, let token = accessToken {
            urlRequest.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }

        if let body {
            let encoder = JSONEncoder()
            encoder.keyEncodingStrategy = .convertToSnakeCase
            urlRequest.httpBody = try encoder.encode(body)
        }

        var data: Data
        var response: URLResponse
        do {
            (data, response) = try await session.data(for: urlRequest)
        } catch {
            throw APIError.networkError(error.localizedDescription)
        }

        guard var http = response as? HTTPURLResponse else {
            throw APIError.networkError("Invalid response")
        }

        if http.statusCode == 401 && authenticated && !skipRefresh {
            if let refreshHandler, await refreshHandler(), let newToken = accessToken {
                urlRequest.setValue("Bearer \(newToken)", forHTTPHeaderField: "Authorization")
                do {
                    (data, response) = try await session.data(for: urlRequest)
                } catch {
                    throw APIError.networkError(error.localizedDescription)
                }
                guard let retryHttp = response as? HTTPURLResponse else {
                    throw APIError.networkError("Invalid response")
                }
                http = retryHttp
            }
            if http.statusCode == 401 {
                throw APIError.unauthorized
            }
        } else if http.statusCode == 401 {
            throw APIError.unauthorized
        }

        if http.statusCode >= 400 {
            if let envelope = try? decoder.decode(ErrorEnvelope.self, from: data) {
                throw APIError.serverError(
                    statusCode: http.statusCode,
                    message: envelope.error.message
                )
            }
            let body = String(data: data, encoding: .utf8) ?? "Unknown error"
            throw APIError.serverError(statusCode: http.statusCode, message: body)
        }

        // Handle empty responses (204 No Content, or empty body)
        if data.isEmpty || http.statusCode == 204 {
            if let empty = EmptyResponse() as? T {
                return empty
            }
        }

        do {
            return try decoder.decode(T.self, from: data)
        } catch let decodingError as DecodingError {
            let context: String
            switch decodingError {
            case .keyNotFound(let key, let ctx):
                context = "Missing key '\(key.stringValue)' at \(ctx.codingPath.map(\.stringValue).joined(separator: "."))"
            case .typeMismatch(let type, let ctx):
                context = "Type mismatch for \(type) at \(ctx.codingPath.map(\.stringValue).joined(separator: "."))"
            case .valueNotFound(let type, let ctx):
                context = "Null value for \(type) at \(ctx.codingPath.map(\.stringValue).joined(separator: "."))"
            case .dataCorrupted(let ctx):
                context = "Corrupted data at \(ctx.codingPath.map(\.stringValue).joined(separator: "."))"
            @unknown default:
                context = decodingError.localizedDescription
            }
            let preview = String(data: data.prefix(500), encoding: .utf8) ?? "(binary)"
            throw APIError.decodingError("\(context) — response: \(preview)")
        } catch {
            throw APIError.decodingError(error.localizedDescription)
        }
    }
}

// MARK: - Empty response

/// Placeholder for endpoints that return no body.
public struct EmptyResponse: Decodable {
    public init() {}
}

// MARK: - Data helpers

extension Data {
    /// Append a UTF-8 encoded string to the data buffer.
    mutating func append(_ string: String) {
        if let data = string.data(using: .utf8) {
            append(data)
        }
    }
}
