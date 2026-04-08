import Foundation

/// Discovers available models from an OpenAI-compatible `/models` endpoint.
enum VisionModelDiscovery {

    /// Fetch the list of model IDs from the API.
    static func fetchModels(apiURL: String, apiKey: String) async throws -> [String] {
        let endpoint = apiURL.hasSuffix("/")
            ? "\(apiURL)models"
            : "\(apiURL)/models"

        guard let url = URL(string: endpoint) else {
            throw VisionDiscoveryError.invalidURL(endpoint)
        }

        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        if !apiKey.isEmpty {
            request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        }
        request.timeoutInterval = 10

        let (data, response) = try await URLSession.shared.data(for: request)

        guard let httpResponse = response as? HTTPURLResponse else {
            throw VisionDiscoveryError.connectionFailed("Not an HTTP response")
        }

        guard (200..<300).contains(httpResponse.statusCode) else {
            let body = String(data: data, encoding: .utf8) ?? ""
            throw VisionDiscoveryError.connectionFailed("HTTP \(httpResponse.statusCode): \(body)")
        }

        struct ModelsResponse: Decodable {
            struct Model: Decodable {
                let id: String
            }
            let data: [Model]?

            // Some servers return a flat array instead of {data: [...]}
            init(from decoder: Decoder) throws {
                let container = try decoder.singleValueContainer()
                if let wrapper = try? container.decode(Wrapper.self) {
                    data = wrapper.data
                } else if let array = try? container.decode([Model].self) {
                    data = array
                } else {
                    data = nil
                }
            }

            private struct Wrapper: Decodable {
                let data: [Model]
            }
        }

        let models = try JSONDecoder().decode(ModelsResponse.self, from: data)
        let ids = models.data?.map(\.id) ?? []

        guard !ids.isEmpty else {
            throw VisionDiscoveryError.noModels
        }

        return ids
    }
}

enum VisionDiscoveryError: Error, CustomStringConvertible {
    case invalidURL(String)
    case connectionFailed(String)
    case noModels

    var description: String {
        switch self {
        case .invalidURL(let url): return "Invalid URL: \(url)"
        case .connectionFailed(let msg): return "Connection failed: \(msg)"
        case .noModels: return "No models available at this endpoint"
        }
    }
}
