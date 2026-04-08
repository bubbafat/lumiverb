import Foundation
import CoreGraphics
import CoreImage
import ImageIO

/// Load image files / data into a `CGImage` with EXIF orientation applied.
///
/// Most modern cameras (and almost all phones) store image pixels in *sensor*
/// orientation and rely on an EXIF orientation tag to tell viewers how to
/// rotate the bytes for display. The naive load paths (`NSImage(data:)`,
/// `CGImageSourceCreateImageAtIndex`) return the raw stored pixels — which
/// are sideways or upside-down for any photo not taken in the camera's native
/// landscape orientation. Anything that runs face detection or face alignment
/// on those raw pixels operates in the wrong coordinate system: Vision
/// reports landmarks relative to sideways pixels, the alignment warp samples
/// the wrong source region, and the resulting embeddings encode rotated
/// non-face content.
///
/// Production face detection in `FaceDetectionProvider` predates this helper
/// and consumed the raw stored pixels, which is why the macOS-side face
/// clustering catastrophically over-merged on real photo libraries — it
/// effectively had no idea which way was up for most of its inputs.
///
/// This helper applies the orientation in one place so callers can write
/// downstream code as if all images were already right-side-up.
public enum ImageLoading {

    /// Load an image from a file URL with EXIF orientation applied.
    ///
    /// Returns `nil` if the URL doesn't point at a decodable image.
    public static func loadOriented(from url: URL) -> CGImage? {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
        return orientedImage(from: source)
    }

    /// Load an image from in-memory data with EXIF orientation applied.
    ///
    /// Returns `nil` if the data isn't a decodable image.
    public static func loadOriented(from data: Data) -> CGImage? {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
        return orientedImage(from: source)
    }

    // MARK: - Private

    private static func orientedImage(from source: CGImageSource) -> CGImage? {
        guard let original = CGImageSourceCreateImageAtIndex(source, 0, nil) else { return nil }

        // Read the EXIF orientation. Default 1 (.up) means no transform needed.
        let props = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any]
        let rawOrientation = (props?[kCGImagePropertyOrientation] as? Int) ?? 1
        if rawOrientation == 1 { return original }

        guard let orientation = CGImagePropertyOrientation(rawValue: UInt32(rawOrientation)) else {
            return original  // unknown value, return as-is rather than corrupting
        }

        // Core Image's `oriented(_:)` is the simplest correct implementation:
        // it knows about all 8 EXIF orientations including the mirrored ones,
        // and avoids the fragile CGContext + CGAffineTransform dance that the
        // alignment code in this package was historically tripping over. The
        // CIContext is created per-call here for clarity; if profiling later
        // shows this on a hot path, cache it as a static.
        let ci = CIImage(cgImage: original).oriented(orientation)
        let ctx = CIContext()
        return ctx.createCGImage(ci, from: ci.extent)
    }
}
