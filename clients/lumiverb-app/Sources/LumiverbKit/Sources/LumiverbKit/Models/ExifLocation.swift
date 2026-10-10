import Foundation

/// What the scan keeps of a file's position and time zone (ADR-017).
/// Twin of `src/shared/location.py` `is_fix` and
/// `src/client/workers/exif_extract.py` `parse_gps`,
/// `parse_taken_at_offset_min`, `parse_gps_accuracy_m`.
public enum ExifLocation {
    /// A real position: finite, in range, and not (0, 0), which some
    /// devices write when they have no fix.
    public static func isFix(lat: Double, lon: Double) -> Bool {
        guard lat.isFinite, lon.isFinite, abs(lat) <= 90, abs(lon) <= 180 else { return false }
        return !(lat == 0 && lon == 0)
    }

    /// Signed (lat, lon) from ImageIO's `{GPS}` dictionary, or nil when the
    /// file has no real fix.
    public static func gps(_ gps: [String: Any]?) -> (lat: Double, lon: Double)? {
        guard let gps,
              let lat = gps["Latitude"] as? Double,
              let lon = gps["Longitude"] as? Double else { return nil }
        let signedLat = (gps["LatitudeRef"] as? String) == "S" ? -abs(lat) : lat
        let signedLon = (gps["LongitudeRef"] as? String) == "W" ? -abs(lon) : lon
        return isFix(lat: signedLat, lon: signedLon) ? (signedLat, signedLon) : nil
    }

    /// `GPSHPositioningError` in metres, or nil.
    public static func accuracyMeters(_ gps: [String: Any]?) -> Double? {
        guard let value = gps?["HPositioningError"] as? Double, value.isFinite, value >= 0 else { return nil }
        return value
    }

    /// `OffsetTimeOriginal` (or `OffsetTime`), e.g. "+02:00", as minutes
    /// east of UTC; nil when the file doesn't say.
    public static func offsetMinutes(_ exif: [String: Any]?) -> Int? {
        for key in ["OffsetTimeOriginal", "OffsetTime"] {
            guard let raw = exif?[key] as? String else { continue }
            if let minutes = parseOffset(raw) { return minutes }
        }
        return nil
    }

    static func parseOffset(_ raw: String) -> Int? {
        let s = raw.trimmingCharacters(in: .whitespaces)
        if s == "Z" { return 0 }
        guard let sign = s.first, sign == "+" || sign == "-" else { return nil }
        let digits = s.dropFirst().replacingOccurrences(of: ":", with: "")
        guard digits.count == 3 || digits.count == 4, digits.allSatisfy(\.isNumber),
              let hours = Int(digits.dropLast(2)), let mins = Int(digits.suffix(2)), mins < 60 else { return nil }
        let total = hours * 60 + mins
        guard total <= 14 * 60 else { return nil }
        return sign == "-" ? -total : total
    }
}
