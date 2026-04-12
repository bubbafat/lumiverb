export interface LibraryListItem {
  library_id: string;
  name: string;
  root_path: string;
  last_scan_at: string | null;
  status: string;
  is_public: boolean;
}

export interface DirectoryNode {
  name: string;
  path: string;
  asset_count: number;
}

export interface LibraryResponse {
  library_id: string;
  name: string;
  root_path: string;
  is_public: boolean;
}

export interface EmptyTrashResponse {
  deleted: number;
}

export interface AssetPageItem {
  asset_id: string;
  rel_path: string;
  file_size: number;
  file_mtime: string | null;
  sha256: string | null;
  media_type: string;
  width: number | null;
  height: number | null;
  taken_at: string | null;
  status: string;
  duration_sec: number | null;
  camera_make: string | null;
  camera_model: string | null;
  iso: number | null;
  aperture: number | null;
  focal_length: number | null;
  focal_length_35mm: number | null;
  lens_model: string | null;
  flash_fired: boolean | null;
  gps_lat: number | null;
  gps_lon: number | null;
  face_count?: number | null;
  created_at: string | null;
}

export interface AssetPageResponse {
  items: AssetPageItem[];
  next_cursor: string | null;
}

export interface FacetsResponse {
  media_types: string[];
  camera_makes: string[];
  camera_models: string[];
  lens_models: string[];
  iso_range: (number | null)[];
  aperture_range: (number | null)[];
  focal_length_range: (number | null)[];
  has_gps_count: number;
  has_face_count: number;
}

export interface FaceItem {
  face_id: string;
  bounding_box: { x: number; y: number; w: number; h: number } | null;
  detection_confidence: number | null;
  person: { person_id: string; display_name: string; dismissed: boolean } | null;
}

export interface FaceListResponse {
  faces: FaceItem[];
}

export interface SearchHit {
  type: "image" | "scene" | "transcript";
  asset_id: string;
  library_id: string | null;
  library_name: string | null;
  rel_path: string;
  thumbnail_key: string | null;
  proxy_key: string | null;
  description: string;
  tags: string[];
  score: number;
  source: string;
  camera_make: string | null;
  camera_model: string | null;
  scene_id: string | null;
  start_ms: number | null;
  end_ms: number | null;
  media_type: string | null;
  file_size: number | null;
  duration_sec?: number | null;
  width: number | null;
  height: number | null;
  taken_at: string | null;
  snippet: string | null;
  language: string | null;
}

export interface SearchResponse {
  query: string;
  hits: SearchHit[];
  total: number;
  source: string;
}

export interface AssetDetail {
  asset_id: string;
  library_id: string;
  rel_path: string;
  media_type: string;
  status: string;
  proxy_key: string | null;
  thumbnail_key: string | null;
  video_preview_key: string | null;
  duration_sec: number | null;
  width: number | null;
  height: number | null;
  // Source-file size in bytes. Optional because the lightbox uses the
  // page item's file_size first and only falls back to detail when the
  // page item is a synthesized placeholder (e.g. cluster review face
  // drill-down).
  file_size?: number | null;
  sha256: string | null;
  exif_extracted_at: string | null;
  camera_make: string | null;
  camera_model: string | null;
  taken_at: string | null;
  gps_lat: number | null;
  gps_lon: number | null;
  iso: number | null;
  exposure_time_us: number | null;
  aperture: number | null;
  focal_length: number | null;
  focal_length_35mm: number | null;
  lens_model: string | null;
  flash_fired: boolean | null;
  orientation: number | null;
  ai_description?: string | null;
  ai_tags: string[];
  ocr_text?: string | null;
  transcript_srt?: string | null;
  transcript_language?: string | null;
  transcribed_at?: string | null;
  note?: string | null;
  note_author?: string | null;
  note_updated_at?: string | null;
}

export interface SimilarHit {
  asset_id: string;
  rel_path: string;
  thumbnail_key: string | null;
  proxy_key: string | null;
  distance: number;
  media_type: string | null;
  file_size: number | null;
  width: number | null;
  height: number | null;
}

export interface UserItem {
  user_id: string;
  email: string;
  role: string;
  created_at: string;
  last_login_at: string | null;
}

export interface SimilarityResponse {
  source_asset_id: string;
  hits: SimilarHit[];
  total: number;
  embedding_available: boolean;
}

export interface CurrentUser {
  user_id: string | null;
  email: string | null;
  role: string;
}

export interface ApiKeyItem {
  key_id: string;
  label: string | null;
  role: string;
  last_used_at: string | null;
  created_at: string;
}

export interface ApiKeyCreateResponse extends ApiKeyItem {
  plaintext: string;
}

export interface LibraryRevision {
  library_id: string;
  revision: number;
  asset_count: number;
}

export interface LibraryHealthItem {
  library_id: string;
  healthy: boolean;
  pending: number;
}

/** Legacy saved query format (deprecated). */
export interface SavedQueryLegacy {
  q?: string;
  filters: Record<string, unknown>;
  library_id?: string;
}

/** New saved query format using filter algebra. */
export interface SavedQuery {
  filters: Array<{ type: string; value: string }>;
  sort?: string;
  direction?: string;
}

export interface CollectionItem {
  collection_id: string;
  name: string;
  description: string | null;
  cover_asset_id: string | null;
  owner_user_id: string | null;
  visibility: string;  // "private" | "shared" | "public"
  ownership: string;   // "own" | "shared"
  sort_order: string;
  type: string;        // "static" | "smart"
  saved_query: SavedQuery | null;
  asset_count: number;
  created_at: string;
  updated_at: string;
}

export interface CollectionListResponse {
  items: CollectionItem[];
}

export interface CollectionAssetItem {
  asset_id: string;
  rel_path: string;
  file_size: number;
  media_type: string;
  width: number | null;
  height: number | null;
  taken_at: string | null;
  status: string;
  duration_sec: number | null;
  camera_make: string | null;
  camera_model: string | null;
}

export interface CollectionAssetsResponse {
  items: CollectionAssetItem[];
  next_cursor: string | null;
}

export interface BatchAddResponse {
  added: number;
}

export interface BatchRemoveResponse {
  removed: number;
}

// ---------------------------------------------------------------------------
// Unified Browse (ADR-008)
// ---------------------------------------------------------------------------

export interface BrowseItem extends AssetPageItem {
  library_id: string;
  library_name: string;
}

export interface BrowseResponse {
  items: BrowseItem[];
  next_cursor: string | null;
}

// ---------------------------------------------------------------------------
// Ratings (ADR-007)
// ---------------------------------------------------------------------------

export type RatingColor = "red" | "orange" | "yellow" | "green" | "blue" | "purple";

export const RATING_COLORS: RatingColor[] = ["red", "orange", "yellow", "green", "blue", "purple"];

export const COLOR_HEX: Record<RatingColor, string> = {
  red: "#ef4444",
  orange: "#f97316",
  yellow: "#eab308",
  green: "#22c55e",
  blue: "#3b82f6",
  purple: "#a855f7",
};

export interface AssetRating {
  favorite: boolean;
  stars: number;
  color: RatingColor | null;
}

export interface RatingResponse {
  asset_id: string;
  favorite: boolean;
  stars: number;
  color: RatingColor | null;
}

export interface RatingLookupResponse {
  ratings: Record<string, AssetRating>;
}

export interface BatchRatingResponse {
  updated: number;
}

