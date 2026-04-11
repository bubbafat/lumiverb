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
    public var hasRating: Bool?
    public var hasColor: Bool?

    // MARK: - Tag
    public var tag: String?

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
        favorite != nil || starMin != nil || starMax != nil || color != nil || hasRating != nil || hasColor != nil ||
        tag != nil ||
        dateFrom != nil || dateTo != nil
    }

    /// Each active filter as a displayable chiclet with a label and
    /// a mutating closure that clears just that filter.
    public struct ActiveFilter: Identifiable {
        public let id: String
        public let label: String
        public let clear: (inout BrowseFilter) -> Void
    }

    /// List of currently active filters for display in a chiclet bar.
    public var activeFilters: [ActiveFilter] {
        var result: [ActiveFilter] = []

        if let mediaType {
            result.append(ActiveFilter(id: "mediaType", label: mediaType.capitalized) { f in f.mediaType = nil })
        }
        if cameraMake != nil || cameraModel != nil {
            let label = [cameraMake, cameraModel].compactMap { $0 }.joined(separator: " ")
            result.append(ActiveFilter(id: "camera", label: label) { f in f.cameraMake = nil; f.cameraModel = nil })
        }
        if let lensModel {
            result.append(ActiveFilter(id: "lens", label: lensModel) { f in f.lensModel = nil })
        }
        if let isoMin {
            let label = isoMin == isoMax ? "ISO \(isoMin)" : "ISO \(isoMin)–\(isoMax ?? isoMin)"
            result.append(ActiveFilter(id: "iso", label: label) { f in f.isoMin = nil; f.isoMax = nil })
        }
        if exposureMinUs != nil {
            let label = "Exposure"
            result.append(ActiveFilter(id: "exposure", label: label) { f in f.exposureMinUs = nil; f.exposureMaxUs = nil })
        }
        if hasExposure != nil {
            result.append(ActiveFilter(id: "hasExposure", label: hasExposure == true ? "Has exposure" : "No exposure") { f in f.hasExposure = nil })
        }
        if let apertureMin {
            let label = apertureMin == apertureMax ? String(format: "f/%.1f", apertureMin) : String(format: "f/%.1f–%.1f", apertureMin, apertureMax ?? apertureMin)
            result.append(ActiveFilter(id: "aperture", label: label) { f in f.apertureMin = nil; f.apertureMax = nil })
        }
        if let focalLengthMin {
            let label = focalLengthMin == focalLengthMax ? String(format: "%.0fmm", focalLengthMin) : String(format: "%.0f–%.0fmm", focalLengthMin, focalLengthMax ?? focalLengthMin)
            result.append(ActiveFilter(id: "focal", label: label) { f in f.focalLengthMin = nil; f.focalLengthMax = nil })
        }
        if let tag {
            result.append(ActiveFilter(id: "tag", label: "Tag: \(tag)") { f in f.tag = nil })
        }
        if let dateFrom {
            let label = dateFrom == dateTo ? dateFrom : "\(dateFrom) – \(dateTo ?? "")"
            result.append(ActiveFilter(id: "date", label: label) { f in f.dateFrom = nil; f.dateTo = nil })
        }
        if favorite == true {
            result.append(ActiveFilter(id: "favorite", label: "Favorites") { f in f.favorite = nil })
        }
        if let starMin {
            let label = starMin == starMax ? "\(starMin)★" : "\(starMin)–\(starMax ?? starMin)★"
            result.append(ActiveFilter(id: "stars", label: label) { f in f.starMin = nil; f.starMax = nil })
        }
        if let color {
            result.append(ActiveFilter(id: "color", label: color.capitalized) { f in f.color = nil })
        }
        if let personDisplayName {
            result.append(ActiveFilter(id: "person", label: personDisplayName) { f in f.personId = nil; f.personDisplayName = nil })
        }
        if hasFaces == true {
            result.append(ActiveFilter(id: "hasFaces", label: "Has faces") { f in f.hasFaces = nil })
        }
        if hasRating != nil {
            result.append(ActiveFilter(id: "hasRating", label: hasRating == true ? "Has rating" : "No rating") { f in f.hasRating = nil })
        }
        if hasColor != nil {
            result.append(ActiveFilter(id: "hasColor", label: hasColor == true ? "Has color" : "No color") { f in f.hasColor = nil })
        }
        if hasGps == true {
            result.append(ActiveFilter(id: "hasGps", label: "Has GPS") { f in f.hasGps = nil })
        }
        // Non-default sort
        if sortField != "taken_at" || sortDirection != "desc" {
            let fieldLabel: String
            switch sortField {
            case "created_at": fieldLabel = "Created"
            case "file_size": fieldLabel = "Size"
            case "rel_path": fieldLabel = "Path"
            case "asset_id": fieldLabel = "ID"
            default: fieldLabel = sortField
            }
            let dir = sortDirection == "asc" ? "↑" : "↓"
            result.append(ActiveFilter(id: "sort", label: "Sort: \(fieldLabel) \(dir)") { f in
                f.sortField = "taken_at"
                f.sortDirection = "desc"
            })
        }

        return result
    }

    /// Clear all filters, keeping sort settings.
    public mutating func clearAll() {
        let sort = sortField
        let dir = sortDirection
        self = BrowseFilter()
        self.sortField = sort
        self.sortDirection = dir
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
        if let hasRating { params["has_rating"] = String(hasRating) }
        if let hasColor { params["has_color"] = String(hasColor) }
        if let tag { params["tag"] = tag }
        if let dateFrom { params["date_from"] = dateFrom }
        if let dateTo { params["date_to"] = dateTo }
        return params
    }

    // MARK: - Filter Algebra Conversion

    /// Convert to an array of LeafFilter for the unified /v1/query endpoint.
    /// Optionally includes library scope and search query as filters.
    public func toLeafFilters(
        libraryId: String? = nil,
        pathPrefix: String? = nil,
        searchQuery: String? = nil
    ) -> [LeafFilter] {
        var filters: [LeafFilter] = []

        if let searchQuery, !searchQuery.isEmpty {
            filters.append(LeafFilter(type: "query", value: searchQuery))
        }
        if let libraryId {
            filters.append(LeafFilter(type: "library", value: libraryId))
        }
        if let pathPrefix {
            filters.append(LeafFilter(type: "path", value: pathPrefix))
        }
        if let mediaType {
            filters.append(LeafFilter(type: "media", value: mediaType))
        }
        if let cameraMake {
            filters.append(LeafFilter(type: "camera_make", value: cameraMake))
        }
        if let cameraModel {
            filters.append(LeafFilter(type: "camera_model", value: cameraModel))
        }
        if let lensModel {
            filters.append(LeafFilter(type: "lens", value: lensModel))
        }
        if let val = composeRange(
            min: isoMin.map(String.init),
            max: isoMax.map(String.init)
        ) {
            filters.append(LeafFilter(type: "iso", value: val))
        }
        if let val = composeRange(
            min: apertureMin.map { String($0) },
            max: apertureMax.map { String($0) }
        ) {
            filters.append(LeafFilter(type: "aperture", value: val))
        }
        if let val = composeRange(
            min: focalLengthMin.map { String($0) },
            max: focalLengthMax.map { String($0) }
        ) {
            filters.append(LeafFilter(type: "focal_length", value: val))
        }
        if let val = composeRange(
            min: exposureMinUs.map(String.init),
            max: exposureMaxUs.map(String.init)
        ) {
            filters.append(LeafFilter(type: "exposure", value: val))
        }
        if let hasExposure {
            filters.append(LeafFilter(type: "has_exposure", value: hasExposure ? "yes" : "no"))
        }
        if let hasGps, hasGps {
            filters.append(LeafFilter(type: "has_gps", value: "yes"))
        }
        if let hasFaces, hasFaces {
            filters.append(LeafFilter(type: "has_faces", value: "yes"))
        }
        if let personId {
            filters.append(LeafFilter(type: "person", value: personId))
        }
        if let favorite, favorite {
            filters.append(LeafFilter(type: "favorite", value: "yes"))
        }
        if let val = composeRange(
            min: starMin.map(String.init),
            max: starMax.map(String.init)
        ) {
            filters.append(LeafFilter(type: "stars", value: val))
        }
        if let color {
            filters.append(LeafFilter(type: "color", value: color))
        }
        if let hasRating {
            filters.append(LeafFilter(type: "has_rating", value: hasRating ? "yes" : "no"))
        }
        if let hasColor {
            filters.append(LeafFilter(type: "has_color", value: hasColor ? "yes" : "no"))
        }
        if let tag {
            filters.append(LeafFilter(type: "tag", value: tag))
        }
        if let val = composeDate(from: dateFrom, to: dateTo) {
            filters.append(LeafFilter(type: "date", value: val))
        }

        return filters
    }

    /// Build URL query items for the unified /v1/query endpoint.
    public func queryItems(
        libraryId: String? = nil,
        pathPrefix: String? = nil,
        searchQuery: String? = nil,
        after: String? = nil,
        limit: Int? = nil
    ) -> [URLQueryItem] {
        filtersToQueryItems(
            toLeafFilters(libraryId: libraryId, pathPrefix: pathPrefix, searchQuery: searchQuery),
            sort: sortField,
            direction: sortDirection,
            after: after,
            limit: limit
        )
    }
}
