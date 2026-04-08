import Foundation
import ImageIO
import CryptoKit
import AppKit

/// Generates proxy images and thumbnails using native ImageIO.
///
/// Supports all formats ImageIO handles: JPEG, PNG, WebP, HEIC, TIFF, RAW
/// (Canon CR2/CR3, Nikon NEF, Sony ARW, Fuji RAF, DNG, etc.)
enum ProxyGenerator {

    /// Maximum long edge for proxy images (matches Python pipeline).
    static let proxyMaxSize = 2048

    /// Result of proxy generation.
    struct ProxyResult: @unchecked Sendable {
        let proxyData: Data          // JPEG bytes (server normalizes to WebP on ingest)
        let originalWidth: Int
        let originalHeight: Int
        let sha256: String           // SHA-256 of the source file
        let exifProperties: [String: Any]?
    }

    /// Generate a WebP proxy image from a source file.
    ///
    /// Uses `CGImageSourceCreateThumbnailAtPixelSize` to decode-and-resize
    /// in one pass — never loads the full-resolution image into memory.
    static func generateProxy(at sourceURL: URL) throws -> ProxyResult {
        // Compute SHA-256 of source file
        let sha256 = try computeSHA256(of: sourceURL)

        // Open image source
        guard let source = CGImageSourceCreateWithURL(sourceURL as CFURL, nil) else {
            throw ProxyError.unreadableImage(sourceURL.lastPathComponent)
        }

        // Get original dimensions
        guard let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [String: Any],
              let width = properties[kCGImagePropertyPixelWidth as String] as? Int,
              let height = properties[kCGImagePropertyPixelHeight as String] as? Int else {
            throw ProxyError.noDimensions(sourceURL.lastPathComponent)
        }

        // Read EXIF
        let exifProperties = properties[kCGImagePropertyExifDictionary as String] as? [String: Any]

        // Calculate proxy size (fit within proxyMaxSize maintaining aspect ratio)
        let maxDim = max(width, height)
        let thumbnailSize = maxDim > proxyMaxSize ? proxyMaxSize : maxDim

        // Generate thumbnail (decode + resize in one pass)
        let thumbnailOptions: [String: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways as String: true,
            kCGImageSourceThumbnailMaxPixelSize as String: thumbnailSize,
            kCGImageSourceCreateThumbnailWithTransform as String: true, // Apply EXIF orientation
        ]

        guard let cgImage = CGImageSourceCreateThumbnailAtIndex(source, 0, thumbnailOptions as CFDictionary) else {
            throw ProxyError.thumbnailFailed(sourceURL.lastPathComponent)
        }

        // Encode as JPEG (server normalizes to WebP; JPEG is universally supported)
        let proxyData = try encodeJPEG(cgImage: cgImage, quality: 0.75)

        return ProxyResult(
            proxyData: proxyData,
            originalWidth: width,
            originalHeight: height,
            sha256: sha256,
            exifProperties: exifProperties
        )
    }

    /// Generate a video poster frame using ffmpeg.
    static func generateVideoPoster(at sourceURL: URL) throws -> ProxyResult {
        let sha256 = try computeSHA256(of: sourceURL)

        // Extract poster frame with ffmpeg
        let tempDir = FileManager.default.temporaryDirectory
        let posterPath = tempDir.appendingPathComponent("\(UUID().uuidString).jpg")

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/local/bin/ffmpeg")
        // Try common paths
        if !FileManager.default.fileExists(atPath: "/usr/local/bin/ffmpeg") {
            if FileManager.default.fileExists(atPath: "/opt/homebrew/bin/ffmpeg") {
                process.executableURL = URL(fileURLWithPath: "/opt/homebrew/bin/ffmpeg")
            } else {
                throw ProxyError.ffmpegNotFound
            }
        }

        process.arguments = [
            "-i", sourceURL.path,
            "-vframes", "1",
            "-q:v", "2",
            "-y",
            posterPath.path,
        ]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice

        try process.run()
        process.waitUntilExit()

        guard process.terminationStatus == 0,
              FileManager.default.fileExists(atPath: posterPath.path) else {
            throw ProxyError.ffmpegFailed(sourceURL.lastPathComponent)
        }

        defer { try? FileManager.default.removeItem(at: posterPath) }

        // Get video dimensions via ffprobe
        let (width, height) = try probeVideoDimensions(at: sourceURL)

        // Load the poster and resize to proxy max
        guard let source = CGImageSourceCreateWithURL(posterPath as CFURL, nil) else {
            throw ProxyError.unreadableImage("poster frame")
        }

        let maxDim = max(width, height)
        let thumbnailSize = maxDim > proxyMaxSize ? proxyMaxSize : maxDim

        let options: [String: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways as String: true,
            kCGImageSourceThumbnailMaxPixelSize as String: thumbnailSize,
            kCGImageSourceCreateThumbnailWithTransform as String: true,
        ]

        guard let cgImage = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else {
            throw ProxyError.thumbnailFailed("poster frame")
        }

        let proxyData = try encodeJPEG(cgImage: cgImage, quality: 0.75)

        return ProxyResult(
            proxyData: proxyData,
            originalWidth: width,
            originalHeight: height,
            sha256: sha256,
            exifProperties: nil
        )
    }

    // MARK: - Helpers

    /// Encode a CGImage as JPEG data.
    /// Server normalizes to WebP on ingest, so JPEG is fine for upload.
    /// JPEG encoding is universally supported by ImageIO for all source formats.
    private static func encodeJPEG(cgImage: CGImage, quality: Double) throws -> Data {
        let data = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(
            data as CFMutableData,
            "public.jpeg" as CFString,
            1, nil
        ) else {
            throw ProxyError.encodingFailed("JPEG")
        }

        let options: [String: Any] = [
            kCGImageDestinationLossyCompressionQuality as String: quality,
        ]

        CGImageDestinationAddImage(dest, cgImage, options as CFDictionary)

        guard CGImageDestinationFinalize(dest) else {
            throw ProxyError.encodingFailed("JPEG finalize")
        }

        return data as Data
    }

    /// Compute SHA-256 hash of a file, streaming in 64KB chunks.
    static func computeSHA256(of url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }

        var hasher = SHA256()
        let bufferSize = 65536

        while autoreleasepool(invoking: {
            let data = handle.readData(ofLength: bufferSize)
            if data.isEmpty { return false }
            hasher.update(data: data)
            return true
        }) {}

        let digest = hasher.finalize()
        return digest.map { String(format: "%02x", $0) }.joined()
    }

    /// Get video dimensions via ffprobe.
    private static func probeVideoDimensions(at url: URL) throws -> (Int, Int) {
        let process = Process()
        let ffprobePath: String
        if FileManager.default.fileExists(atPath: "/opt/homebrew/bin/ffprobe") {
            ffprobePath = "/opt/homebrew/bin/ffprobe"
        } else if FileManager.default.fileExists(atPath: "/usr/local/bin/ffprobe") {
            ffprobePath = "/usr/local/bin/ffprobe"
        } else {
            throw ProxyError.ffmpegNotFound
        }

        process.executableURL = URL(fileURLWithPath: ffprobePath)
        process.arguments = [
            "-v", "quiet",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            url.path,
        ]

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice

        try process.run()
        process.waitUntilExit()

        let data = pipe.fileHandleForReading.readDataToEndOfFile()

        struct FFProbeResult: Decodable {
            struct Stream: Decodable {
                let width: Int
                let height: Int
            }
            let streams: [Stream]
        }

        let result = try JSONDecoder().decode(FFProbeResult.self, from: data)
        guard let stream = result.streams.first else {
            throw ProxyError.noDimensions(url.lastPathComponent)
        }

        return (stream.width, stream.height)
    }
}

// MARK: - Errors

enum ProxyError: Error, CustomStringConvertible {
    case unreadableImage(String)
    case noDimensions(String)
    case thumbnailFailed(String)
    case encodingFailed(String)
    case ffmpegNotFound
    case ffmpegFailed(String)

    var description: String {
        switch self {
        case .unreadableImage(let file): return "Cannot read image: \(file)"
        case .noDimensions(let file): return "Cannot determine dimensions: \(file)"
        case .thumbnailFailed(let file): return "Thumbnail generation failed: \(file)"
        case .encodingFailed(let fmt): return "Image encoding failed: \(fmt)"
        case .ffmpegNotFound: return "ffmpeg not found. Install with: brew install ffmpeg"
        case .ffmpegFailed(let file): return "ffmpeg failed for: \(file)"
        }
    }
}
