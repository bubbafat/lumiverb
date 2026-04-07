import Foundation
import Vision
import AppKit

/// Extracts text from images using Apple Vision framework.
///
/// Uses `VNRecognizeTextRequest` with accurate recognition level.
/// Fully local, no model downloads needed — built into macOS 14+.
enum OCRProvider {

    /// Extract text from an image at the given URL.
    /// Returns the extracted text, or an empty string if no text found.
    static func extractText(from imageURL: URL) throws -> String {
        guard let cgImage = loadCGImage(from: imageURL) else {
            throw OCRError.unreadableImage(imageURL.lastPathComponent)
        }

        return try extractText(from: cgImage)
    }

    /// Extract text from an image loaded from proxy cache data.
    static func extractText(from imageData: Data) throws -> String {
        guard let nsImage = NSImage(data: imageData),
              let cgImage = nsImage.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
            throw OCRError.unreadableImage("proxy data")
        }

        return try extractText(from: cgImage)
    }

    /// Core text extraction from a CGImage.
    static func extractText(from cgImage: CGImage) throws -> String {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = true
        // Recognize multiple languages
        request.recognitionLanguages = ["en", "de", "fr", "es", "it", "pt", "nl", "sv", "da", "no", "fi"]

        let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
        try handler.perform([request])

        guard let observations = request.results else {
            return ""
        }

        let lines = observations.compactMap { observation -> String? in
            guard observation.confidence > 0.3 else { return nil }
            return observation.topCandidates(1).first?.string
        }

        return lines.joined(separator: "\n")
    }

    /// Load a CGImage from a file URL using ImageIO.
    private static func loadCGImage(from url: URL) -> CGImage? {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
        return CGImageSourceCreateImageAtIndex(source, 0, nil)
    }
}

enum OCRError: Error, CustomStringConvertible {
    case unreadableImage(String)

    var description: String {
        switch self {
        case .unreadableImage(let file): return "OCR: cannot read image: \(file)"
        }
    }
}
