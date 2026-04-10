import Foundation

/// All filter state for the browse view. Value type for easy equality checks.
public struct BrowseFilter: Equatable, Sendable {
    // MARK: - Sort
    public var sortField: String = "taken_at"
    public var sortDirection: String = "desc"

    // MARK: - Media type
    public var mediaType: String?  // "image" or "video"

    // MARK: - Camera / EXIF
    public var cameraMake: String?
    public var cameraModel: String?
    public var lensModel: String?
    public var isoMin: Int?
    public var isoMax: Int?
    public var apertureMin: Double?
    public var apertureMax: Double?
    public var exposureMinUs: Int?
    public var exposureMaxUs: Int?
    public var focalLengthMin: Double?
    public var focalLengthMax: Double?
    public var hasExposure: Bool?

    // MARK: - Flags
    public var hasGps: Bool?
    public var hasFaces: Bool?
    public var personId: String?
    public var personDisplayName: String?

    // MARK: - Rating
    public var favorite: Bool?
    public var starMin: Int?
    public var starMax: Int?
    public var color: String?

    // MARK: - Date
    public var dateFrom: String?  // "YYYY-MM-DD"
    public var dateTo: String?

    public init() {}

    /// Whether any filter beyond default sort is active.
    public var hasActiveFilters: Bool {
        mediaType != nil ||
        cameraMake != nil || cameraModel != nil || lensModel != nil ||
        isoMin != nil || isoMax != nil ||
        exposureMinUs != nil || exposureMaxUs != nil ||
        apertureMin != nil || apertureMax != nil ||
        focalLengthMin != nil || focalLengthMax != nil ||
        hasExposure != nil ||
        hasGps != nil || hasFaces != nil || personId != nil ||
        favorite != nil || starMin != nil || starMax != nil || color != nil ||
        dateFrom != nil || dateTo != nil
    }

    /// Build query parameters for the API call.
    public var queryParams: [String: String] {
        var params: [String: String] = [
            "sort": sortField,
            "dir": sortDirection,
        ]
        if let mediaType { params["media_type"] = mediaType }
        if let cameraMake { params["camera_make"] = cameraMake }
        if let cameraModel { params["camera_model"] = cameraModel }
        if let lensModel { params["lens_model"] = lensModel }
        if let isoMin { params["iso_min"] = String(isoMin) }
        if let isoMax { params["iso_max"] = String(isoMax) }
        if let exposureMinUs { params["exposure_min_us"] = String(exposureMinUs) }
        if let exposureMaxUs { params["exposure_max_us"] = String(exposureMaxUs) }
        if let apertureMin { params["aperture_min"] = String(apertureMin) }
        if let apertureMax { params["aperture_max"] = String(apertureMax) }
        if let focalLengthMin { params["focal_length_min"] = String(focalLengthMin) }
        if let focalLengthMax { params["focal_length_max"] = String(focalLengthMax) }
        if let hasExposure { params["has_exposure"] = String(hasExposure) }
        if let hasGps { params["has_gps"] = String(hasGps) }
        if let hasFaces { params["has_faces"] = String(hasFaces) }
        if let personId { params["person_id"] = personId }
        if let favorite { params["favorite"] = String(favorite) }
        if let starMin { params["star_min"] = String(starMin) }
        if let starMax { params["star_max"] = String(starMax) }
        if let color { params["color"] = color }
        if let dateFrom { params["date_from"] = dateFrom }
        if let dateTo { params["date_to"] = dateTo }
        return params
    }
}
