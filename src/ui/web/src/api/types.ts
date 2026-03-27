export interface LibraryListItem {
  library_id: string;
  name: string;
  root_path: string;
  scan_status: string;
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
  scan_status: string;
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
}

export interface SearchHit {
  type: "image" | "scene";
  asset_id: string;
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
  sha256: string | null;
  exif_extracted_at: string | null;
  camera_make: string | null;
  camera_model: string | null;
  taken_at: string | null;
  gps_lat: number | null;
  gps_lon: number | null;
  iso: number | null;
  shutter_speed: string | null;
  aperture: number | null;
  focal_length: number | null;
  focal_length_35mm: number | null;
  lens_model: string | null;
  flash_fired: boolean | null;
  orientation: number | null;
  ai_description?: string | null;
  ai_tags: string[];
}

export interface JobListItem {
  job_id: string;
  job_type: string;
  status: string;
  priority: number;
  asset_id: string | null;
  worker_id: string | null;
  fail_count: number;
  error_message: string | null;
  created_at: string;
  claimed_at: string | null;
  completed_at: string | null;
}

export interface JobStatRow {
  job_type: string;
  pending: number;
  claimed: number;
  failed: number;
}

export interface JobStatsResponse {
  rows: JobStatRow[];
  total_pending: number;
  total_claimed: number;
  total_failed: number;
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

