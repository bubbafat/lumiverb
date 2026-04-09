import Foundation
import AppKit
import LumiverbKit
import os

/// OpenAI-compatible vision API client for generating image descriptions and tags.
///
/// Works with any OpenAI-compatible endpoint (OpenAI, LM Studio, Ollama, vLLM, etc.).
/// Sends the proxy image as a base64 data URL in a chat/completions request.
enum VisionProvider {

    private static let logger = Logger(subsystem: "io.lumiverb", category: "VisionProvider")

    /// Maximum long edge for images sent to the vision API. Matches Python pipeline.
    private static let maxImageEdge = 1280

    /// Prompt matching the Python worker's describe prompt.
    private static let describePrompt = """
        Describe this image in 2-3 sentences, being specific about the subject, setting, \
        and mood. Then provide 5-10 descriptive tags. Respond only with valid JSON in this \
        exact format:
        {"description": "...", "tags": ["tag1", "tag2", ...]}
        """

    struct VisionResult: Sendable {
        let description: String
        let tags: [String]
    }

    /// Generate a description and tags for an image using an OpenAI-compatible vision API.
    static func describe(
        imageData: Data,
        apiURL: String,
        apiKey: String,
        modelId: String
    ) async throws -> VisionResult {
        // Resize image if needed and encode as base64 JPEG
        let base64Image = try prepareImageBase64(imageData)

        // Build chat/completions request
        let endpoint = apiURL.hasSuffix("/")
            ? "\(apiURL)chat/completions"
            : "\(apiURL)/chat/completions"

        guard let url = URL(string: endpoint) else {
            throw VisionError.invalidURL(endpoint)
        }

        let requestBody = ChatCompletionsRequest(
            model: modelId,
            messages: [
                .init(role: "user", content: [
                    .init(type: "image_url", imageUrl: .init(url: "data:image/jpeg;base64,\(base64Image)"), text: nil),
                    .init(type: "text", imageUrl: nil, text: describePrompt),
                ]),
            ],
            maxTokens: 500,
            temperature: 0.2
        )

        let jsonData = try JSONEncoder().encode(requestBody)

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if !apiKey.isEmpty {
            request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        }
        request.httpBody = jsonData
        request.timeoutInterval = 120

        // Retry with exponential backoff (3 attempts, matching Python worker)
        let maxAttempts = 3
        var lastError: Error = VisionError.inferenceFailure("no attempts made")

        for attempt in 0..<maxAttempts {
            if attempt > 0 {
                let base = pow(3.0, Double(attempt - 1))
                let jitter = Double.random(in: 0.5...1.5)
                let delay = base * jitter
                try await Task.sleep(for: .seconds(delay))
            }

            do {
                let (data, response) = try await URLSession.shared.data(for: request)

                guard let httpResponse = response as? HTTPURLResponse else {
                    throw VisionError.inferenceFailure("not an HTTP response")
                }

                if httpResponse.statusCode == 429 {
                    logger.warning("Vision API rate limited (attempt \(attempt + 1)/\(maxAttempts))")
                    lastError = VisionError.rateLimited
                    continue
                }

                guard (200..<300).contains(httpResponse.statusCode) else {
                    let body = String(data: data, encoding: .utf8) ?? ""
                    logger.warning("Vision API error \(httpResponse.statusCode): \(body, privacy: .public)")
                    throw VisionError.apiError(httpResponse.statusCode, body)
                }

                return try parseResponse(data)
            } catch let error as VisionError where error.isRetryable {
                lastError = error
                logger.warning("Vision API retry \(attempt + 1)/\(maxAttempts): \(error.localizedDescription, privacy: .public)")
                continue
            } catch {
                throw error
            }
        }

        throw lastError
    }

    /// Whether vision is configured and available.
    static func isConfigured(apiURL: String, modelId: String) -> Bool {
        !apiURL.isEmpty && !modelId.isEmpty
    }

    // MARK: - Image preparation

    private static func prepareImageBase64(_ imageData: Data) throws -> String {
        // Must route through `ImageLoading.loadOriented` — the naive
        // `NSImage(data:).cgImage(...)` path this replaced silently
        // dropped EXIF 180° rotation on current macOS, so the remote
        // vision API was being asked to describe upside-down images.
        guard let cgImage = ImageLoading.loadOriented(from: imageData) else {
            throw VisionError.unreadableImage
        }

        let width = cgImage.width
        let height = cgImage.height
        let maxDim = max(width, height)

        let targetImage: CGImage
        if maxDim > maxImageEdge {
            // Resize to fit within maxImageEdge
            let scale = Double(maxImageEdge) / Double(maxDim)
            let newWidth = Int(Double(width) * scale)
            let newHeight = Int(Double(height) * scale)

            guard let context = CGContext(
                data: nil,
                width: newWidth,
                height: newHeight,
                bitsPerComponent: 8,
                bytesPerRow: 0,
                space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
            ) else {
                throw VisionError.unreadableImage
            }
            context.interpolationQuality = .high
            context.draw(cgImage, in: CGRect(x: 0, y: 0, width: newWidth, height: newHeight))

            guard let resized = context.makeImage() else {
                throw VisionError.unreadableImage
            }
            targetImage = resized
        } else {
            targetImage = cgImage
        }

        // Encode as JPEG
        let bitmapRep = NSBitmapImageRep(cgImage: targetImage)
        guard let jpegData = bitmapRep.representation(
            using: .jpeg,
            properties: [.compressionFactor: 0.75]
        ) else {
            throw VisionError.unreadableImage
        }

        return jpegData.base64EncodedString()
    }

    // MARK: - Response parsing

    private static func parseResponse(_ data: Data) throws -> VisionResult {
        struct ChatResponse: Decodable {
            struct Choice: Decodable {
                struct Message: Decodable {
                    let content: String?
                }
                let message: Message
            }
            let choices: [Choice]
        }

        let chatResponse = try JSONDecoder().decode(ChatResponse.self, from: data)

        guard let content = chatResponse.choices.first?.message.content else {
            throw VisionError.inferenceFailure("no content in response")
        }

        // Extract JSON from response (may be wrapped in markdown code fences)
        let cleaned = content
            .replacingOccurrences(of: "```json", with: "")
            .replacingOccurrences(of: "```", with: "")
            .trimmingCharacters(in: .whitespacesAndNewlines)

        // Find first JSON object using brace counting
        guard let jsonString = extractJSON(from: cleaned),
              let jsonData = jsonString.data(using: .utf8) else {
            // If no valid JSON, use the whole content as a description
            logger.info("Vision API returned non-JSON; using raw content as description")
            return VisionResult(description: content.trimmingCharacters(in: .whitespacesAndNewlines), tags: [])
        }

        struct VisionJSON: Decodable {
            let description: String?
            let tags: [String]?
        }

        do {
            let parsed = try JSONDecoder().decode(VisionJSON.self, from: jsonData)
            return VisionResult(
                description: parsed.description ?? "",
                tags: parsed.tags ?? []
            )
        } catch {
            // Fallback: use raw content
            logger.info("Vision JSON parse failed; using raw content")
            return VisionResult(description: content.trimmingCharacters(in: .whitespacesAndNewlines), tags: [])
        }
    }

    /// Extract the first complete JSON object from a string using brace counting.
    private static func extractJSON(from string: String) -> String? {
        guard let start = string.firstIndex(of: "{") else { return nil }

        var depth = 0
        var inString = false
        var escape = false

        for i in string[start...].indices {
            let char = string[i]

            if escape {
                escape = false
                continue
            }

            if char == "\\" && inString {
                escape = true
                continue
            }

            if char == "\"" {
                inString.toggle()
                continue
            }

            if !inString {
                if char == "{" { depth += 1 }
                else if char == "}" {
                    depth -= 1
                    if depth == 0 {
                        return String(string[start...i])
                    }
                }
            }
        }

        return nil
    }
}

// MARK: - Request types

private struct ChatCompletionsRequest: Encodable {
    let model: String
    let messages: [Message]
    let maxTokens: Int
    let temperature: Double

    struct Message: Encodable {
        let role: String
        let content: [ContentPart]
    }

    struct ContentPart: Encodable {
        let type: String
        let imageUrl: ImageURL?
        let text: String?

        struct ImageURL: Encodable {
            let url: String
        }

        enum CodingKeys: String, CodingKey {
            case type
            case imageUrl = "image_url"
            case text
        }

        func encode(to encoder: Encoder) throws {
            var container = encoder.container(keyedBy: CodingKeys.self)
            try container.encode(type, forKey: .type)
            if let imageUrl { try container.encode(imageUrl, forKey: .imageUrl) }
            if let text { try container.encode(text, forKey: .text) }
        }
    }

    enum CodingKeys: String, CodingKey {
        case model, messages, temperature
        case maxTokens = "max_tokens"
    }
}

// MARK: - Errors

enum VisionError: Error, CustomStringConvertible {
    case invalidURL(String)
    case unreadableImage
    case apiError(Int, String)
    case rateLimited
    case inferenceFailure(String)
    case notConfigured

    var description: String {
        switch self {
        case .invalidURL(let url): return "Invalid vision API URL: \(url)"
        case .unreadableImage: return "Vision: cannot read image"
        case .apiError(let code, let body): return "Vision API error \(code): \(body)"
        case .rateLimited: return "Vision API rate limited"
        case .inferenceFailure(let msg): return "Vision inference failed: \(msg)"
        case .notConfigured: return "Vision API not configured. Set URL and model in Settings."
        }
    }

    var isRetryable: Bool {
        switch self {
        case .rateLimited: return true
        case .apiError(let code, _): return code >= 500
        default: return false
        }
    }
}
