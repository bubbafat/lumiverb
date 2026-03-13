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
