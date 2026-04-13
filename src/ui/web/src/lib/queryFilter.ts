/**
 * Filter algebra types and URL conversion for the unified query system.
 *
 * Each filter is a `LeafFilter { type, value }` matching the server's
 * `?f=prefix:value` format. The `type` field matches the server's filter
 * `prefix()` (e.g., "camera_make", "media", "iso", "favorite").
 *
 * FilterCapability describes a filter type from the server's
 * GET /v1/filters/capabilities endpoint.
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** A single filter predicate. Matches server's LeafFilter serialization. */
export interface LeafFilter {
  type: string;
  value: string;
}

/** A filter type descriptor from GET /v1/filters/capabilities. */
export interface FilterCapability {
  prefix: string;
  label: string;
  value_kind: string; // "text" | "string" | "boolean" | "enum" | "int_range" | "float_range" | "date_range" | "location"
  faceted?: boolean;
  enum_values?: string[];
}

/** Saved query format for smart collections. */
export interface SavedQueryV2 {
  filters: LeafFilter[];
  sort?: string;
  direction?: string;
}

// ---------------------------------------------------------------------------
// URL ↔ Filter conversion
// ---------------------------------------------------------------------------

/**
 * Convert filters to URL search params (repeated `f=prefix:value`).
 * Sort/direction are separate top-level params.
 */
export function filtersToParams(
  filters: LeafFilter[],
  sort?: string,
  direction?: string,
): URLSearchParams {
  const params = new URLSearchParams();
  for (const f of filters) {
    params.append("f", `${f.type}:${f.value}`);
  }
  if (sort && sort !== "taken_at") params.set("sort", sort);
  if (direction && direction !== "desc") params.set("dir", direction);
  return params;
}

/**
 * Parse URL search params into filters + sort/direction.
 * Reads repeated `f=prefix:value` params.
 */
export function paramsToFilters(params: URLSearchParams): {
  filters: LeafFilter[];
  sort: string;
  direction: "asc" | "desc";
} {
  const filters: LeafFilter[] = [];
  for (const raw of params.getAll("f")) {
    const colon = raw.indexOf(":");
    if (colon <= 0) continue;
    filters.push({
      type: raw.slice(0, colon),
      value: raw.slice(colon + 1),
    });
  }
  const sort = params.get("sort") ?? "taken_at";
  const dir = params.get("dir");
  const direction: "asc" | "desc" = dir === "asc" ? "asc" : "desc";
  return { filters, sort, direction };
}

// ---------------------------------------------------------------------------
// Filter helpers
// ---------------------------------------------------------------------------

/** Get a filter's value by type, or undefined if not present. */
export function getFilterValue(filters: LeafFilter[], type: string): string | undefined {
  return filters.find((f) => f.type === type)?.value;
}

/** Check whether any user-visible filters are active (excludes sort/dir). */
export function hasActiveFilters(filters: LeafFilter[]): boolean {
  return filters.length > 0;
}

/** Set or remove a filter by type. Returns new array. */
export function setFilter(
  filters: LeafFilter[],
  type: string,
  value: string | null,
): LeafFilter[] {
  const without = filters.filter((f) => f.type !== type);
  if (value == null || value === "") return without;
  return [...without, { type, value }];
}

/** Remove all filters. */
export function clearFilters(): LeafFilter[] {
  return [];
}

// ---------------------------------------------------------------------------
// Labels
// ---------------------------------------------------------------------------

/** Human-readable label for a filter, using capabilities if available. */
export function filterLabel(
  filter: LeafFilter,
  capabilities?: FilterCapability[],
): string {
  const cap = capabilities?.find((c) => c.prefix === filter.type);
  const typeLabel = cap?.label ?? FALLBACK_LABELS[filter.type] ?? filter.type;

  switch (filter.type) {
    case "query":
      // No wrapping quotes — users can type literal `"phrase"` for
      // exact-match and the decorative outer quotes would collide
      // with the input, rendering as `""phrase""`.
      return `Search: ${filter.value}`;
    case "media":
      return filter.value === "image" ? "Photos" : filter.value === "video" ? "Videos" : `Media: ${filter.value}`;
    case "favorite":
      return filter.value === "yes" ? "Favorites" : "Not favorites";
    case "has_gps":
      return filter.value === "yes" ? "Has GPS" : "No GPS";
    case "has_faces":
      return filter.value === "yes" ? "Has faces" : "No faces";
    case "has_exposure":
      return filter.value === "yes" ? "Has exposure data" : "No exposure data";
    case "has_rating":
      return filter.value === "yes" ? "Has rating" : "No rating";
    case "has_color":
      return filter.value === "yes" ? "Has color label" : "No color label";
    case "stars": {
      if (filter.value.includes("-")) {
        const [lo, hi] = filter.value.split("-");
        return lo === hi ? `${lo} star${lo === "1" ? "" : "s"}` : `${lo}–${hi} stars`;
      }
      if (filter.value.endsWith("+")) return `${filter.value.slice(0, -1)}+ stars`;
      return `${filter.value} star${filter.value === "1" ? "" : "s"}`;
    }
    case "iso": {
      if (filter.value.includes("-")) {
        const [lo, hi] = filter.value.split("-");
        return `ISO ${lo}–${hi}`;
      }
      if (filter.value.endsWith("+")) return `ISO ${filter.value.slice(0, -1)}+`;
      return `ISO ${filter.value}`;
    }
    case "date":
      return `Date: ${filter.value.replace(",", " – ")}`;
    case "color":
      return `Color: ${filter.value}`;
    case "tag":
      return `Tag: ${filter.value}`;
    case "near":
      return "Near location";
    case "person":
      return "Person filter";
    case "library":
      return typeLabel;
    default:
      return `${typeLabel}: ${filter.value}`;
  }
}

const FALLBACK_LABELS: Record<string, string> = {
  query: "Search",
  library: "Library",
  path: "Path",
  media: "Media Type",
  camera_make: "Camera Make",
  camera_model: "Camera Model",
  lens: "Lens",
  iso: "ISO",
  aperture: "Aperture",
  focal_length: "Focal Length",
  exposure: "Exposure",
  has_exposure: "Has Exposure",
  has_gps: "Has GPS",
  near: "Near",
  date: "Date",
  favorite: "Favorite",
  stars: "Stars",
  color: "Color",
  has_rating: "Has Rating",
  has_color: "Has Color",
  has_faces: "Has Faces",
  person: "Person",
  tag: "Tag",
};

// ---------------------------------------------------------------------------
// Range / compound value parsing helpers (for FilterBar widgets)
// ---------------------------------------------------------------------------

/** Parse "200-800" or "400+" or "-1600" into {min, max}. */
export function parseRange(value: string | undefined): { min: string | null; max: string | null } {
  if (!value) return { min: null, max: null };
  if (value.endsWith("+")) return { min: value.slice(0, -1), max: null };
  if (value.startsWith("-")) return { min: null, max: value.slice(1) };
  const dash = value.indexOf("-");
  if (dash > 0) return { min: value.slice(0, dash), max: value.slice(dash + 1) };
  return { min: value, max: value }; // exact value
}

/** Compose range back to filter value. Returns null if both are empty. */
export function composeRange(min: string | null, max: string | null): string | null {
  if (!min && !max) return null;
  if (min && !max) return `${min}+`;
  if (!min && max) return `-${max}`;
  if (min === max) return min!;
  return `${min}-${max}`;
}

/** Parse "48.85,2.35,5" into {lat, lon, radius}. */
export function parseNear(value: string | undefined): { lat: string; lon: string; radius: string } | null {
  if (!value) return null;
  const parts = value.split(",");
  if (parts.length < 3) return null;
  return { lat: parts[0], lon: parts[1], radius: parts[2] };
}

/** Compose near location back to filter value. */
export function composeNear(lat: string, lon: string, radius: string): string {
  return `${lat},${lon},${radius}`;
}

/** Parse "2024-01-01,2024-12-31" into {from, to}. */
export function parseDate(value: string | undefined): { from: string | null; to: string | null } {
  if (!value) return { from: null, to: null };
  const parts = value.split(",", 2);
  return { from: parts[0] || null, to: parts[1] || null };
}

/** Compose date range back to filter value. Returns null if both are empty. */
export function composeDate(from: string | null, to: string | null): string | null {
  if (!from && !to) return null;
  return `${from ?? ""},${to ?? ""}`;
}

// ---------------------------------------------------------------------------
// Smart collection serialization
// ---------------------------------------------------------------------------

/** Build a SavedQueryV2 from the current filter state. */
export function buildSavedQuery(
  filters: LeafFilter[],
  sort: string,
  direction: string,
): SavedQueryV2 {
  return {
    filters,
    sort,
    direction,
  };
}

/** Generate human-readable labels for all filters in a saved query. */
export function savedQueryLabels(
  sq: SavedQueryV2,
  capabilities?: FilterCapability[],
): string[] {
  return sq.filters
    .filter((f) => f.type !== "library")
    .map((f) => filterLabel(f, capabilities));
}
