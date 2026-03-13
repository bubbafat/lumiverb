export interface LibraryListItem {
  library_id: string;
  name: string;
  root_path: string;
  scan_status: string;
  last_scan_at: string | null;
  status: string;
  vision_model_id: string;
}

export interface LibraryResponse {
  library_id: string;
  name: string;
  root_path: string;
  scan_status: string;
  vision_model_id: string;
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
}

export interface AssetDetail {
  asset_id: string;
  library_id: string;
  rel_path: string;
  media_type: string;
  status: string;
  proxy_key: string | null;
  thumbnail_key: string | null;
  width: number | null;
  height: number | null;
  sha256: string | null;
  exif_extracted_at: string | null;
  camera_make: string | null;
  camera_model: string | null;
  taken_at: string | null;
  gps_lat: number | null;
  gps_lon: number | null;
  ai_description?: string | null;
}
