import Foundation

public enum LibraryPath {
    /// `root/relPath` with "." and ".." resolved, or nil when that lands
    /// outside `root`. A rel path comes from the server, so it is never
    /// opened or read until it is known to be inside the library.
    public static func fileURL(root: String, relPath: String) -> URL? {
        let rootURL = URL(fileURLWithPath: root, isDirectory: true).standardizedFileURL
        let fileURL = rootURL.appendingPathComponent(relPath).standardizedFileURL
        let rootPrefix = rootURL.path.hasSuffix("/") ? rootURL.path : rootURL.path + "/"
        return fileURL.path.hasPrefix(rootPrefix) ? fileURL : nil
    }
}
