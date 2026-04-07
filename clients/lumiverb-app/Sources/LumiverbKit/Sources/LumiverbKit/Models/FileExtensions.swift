import Foundation

/// Supported file extensions for scanning, matching the server's definitions.
public enum FileExtensions {
    public static let video: Set<String> = [
        ".mp4", ".mkv", ".mov",
    ]

    public static let image: Set<String> = [
        // Common
        ".jpg", ".jpeg", ".png", ".webp", ".bmp",
        // Canon
        ".cr2", ".cr3", ".crw",
        // Nikon
        ".nef", ".nrw",
        // Sony
        ".arw", ".sr2", ".srf",
        // Fuji
        ".raf",
        // Olympus
        ".orf",
        // Panasonic
        ".rw2", ".raw",
        // Leica
        ".rwl",
        // Universal RAW
        ".dng", ".tif", ".tiff",
    ]

    public static let supported: Set<String> = video.union(image)

    /// Check if a file path has a supported extension.
    public static func isSupported(_ path: String) -> Bool {
        let ext = (path as NSString).pathExtension.lowercased()
        guard !ext.isEmpty else { return false }
        return supported.contains(".\(ext)")
    }

    /// Determine media type from file path.
    public static func mediaType(for path: String) -> String? {
        let ext = (path as NSString).pathExtension.lowercased()
        guard !ext.isEmpty else { return nil }
        let dotExt = ".\(ext)"
        if video.contains(dotExt) { return "video" }
        if image.contains(dotExt) { return "image" }
        return nil
    }
}
