import Foundation
import os.log

private let logger = Logger(subsystem: "io.lumiverb.app", category: "ModelDownloader")

/// Downloads CoreML model bundles from app.lumiverb.io on demand.
///
/// Models are stored as .zip archives on the server. Downloaded to a temp file,
/// unzipped to `~/.lumiverb/models/`, then the temp file is cleaned up.
enum ModelDownloader {

    static let baseURL = "https://app.lumiverb.io/models"
    static let modelsDir = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".lumiverb/models")

    struct ModelSpec {
        let name: String           // e.g. "ArcFace"
        let fileName: String       // e.g. "ArcFace.mlmodelc"
        let zipName: String        // e.g. "ArcFace.mlmodelc.zip"
        let expectedSizeMB: Int    // approximate, for progress display
    }

    static let arcFace = ModelSpec(
        name: "ArcFace",
        fileName: "ArcFace.mlmodelc",
        zipName: "ArcFace.mlmodelc.zip",
        expectedSizeMB: 80
    )

    /// Check if a model exists locally.
    static func isInstalled(_ spec: ModelSpec) -> Bool {
        let path = modelsDir.appendingPathComponent(spec.fileName)
        return FileManager.default.fileExists(atPath: path.path)
    }

    /// Download and install a model if not already present. Returns the local path.
    static func ensureAvailable(_ spec: ModelSpec) async throws -> URL {
        let localPath = modelsDir.appendingPathComponent(spec.fileName)
        if FileManager.default.fileExists(atPath: localPath.path) {
            return localPath
        }

        let url = URL(string: "\(baseURL)/\(spec.zipName)")!
        logger.info("Downloading \(spec.name) model (~\(spec.expectedSizeMB)MB) from \(url)")

        // Download to temp file
        let (tempURL, response) = try await URLSession.shared.download(from: url)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            throw ModelDownloadError.downloadFailed(spec.name, code)
        }

        let fileSize = (try? FileManager.default.attributesOfItem(atPath: tempURL.path)[.size] as? Int) ?? 0
        logger.info("Downloaded \(spec.name): \(fileSize / 1024 / 1024)MB")

        // Ensure models directory exists
        try FileManager.default.createDirectory(at: modelsDir, withIntermediateDirectories: true)

        // Unzip
        let unzipDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: unzipDir, withIntermediateDirectories: true)

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/unzip")
        process.arguments = ["-o", "-q", tempURL.path, "-d", unzipDir.path]
        try process.run()
        process.waitUntilExit()

        guard process.terminationStatus == 0 else {
            throw ModelDownloadError.unzipFailed(spec.name)
        }

        // Move to final location
        let unzippedModel = unzipDir.appendingPathComponent(spec.fileName)
        guard FileManager.default.fileExists(atPath: unzippedModel.path) else {
            throw ModelDownloadError.unzipFailed(spec.name)
        }

        // Remove existing if any (stale/partial)
        if FileManager.default.fileExists(atPath: localPath.path) {
            try FileManager.default.removeItem(at: localPath)
        }
        try FileManager.default.moveItem(at: unzippedModel, to: localPath)

        // Cleanup
        try? FileManager.default.removeItem(at: tempURL)
        try? FileManager.default.removeItem(at: unzipDir)

        logger.info("\(spec.name) model installed at \(localPath.path)")
        return localPath
    }
}

enum ModelDownloadError: Error, CustomStringConvertible {
    case downloadFailed(String, Int)
    case unzipFailed(String)

    var description: String {
        switch self {
        case .downloadFailed(let name, let code):
            return "\(name) model download failed (HTTP \(code))"
        case .unzipFailed(let name):
            return "\(name) model unzip failed"
        }
    }
}
