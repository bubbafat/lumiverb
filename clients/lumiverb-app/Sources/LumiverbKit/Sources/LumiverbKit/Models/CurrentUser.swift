import Foundation

/// Current user info as returned by `GET /v1/me`.
public struct CurrentUser: Decodable, Sendable {
    public let userId: String
    public let email: String
    public let role: String
}
