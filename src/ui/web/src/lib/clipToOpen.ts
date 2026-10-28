import type { AssetPageItem } from "../api/types";

/** A clip as the lightbox takes it, from what a list says of it: the lightbox reads the rest itself. */
export function clipToOpen(clip: { asset_id: string; rel_path: string; media_type: string; taken_at?: string | null }):
    AssetPageItem {
  return {
    asset_id: clip.asset_id,
    rel_path: clip.rel_path,
    file_size: 0,
    file_mtime: null,
    content_hash: null,
    media_type: clip.media_type,
    width: null,
    height: null,
    taken_at: clip.taken_at ?? null,
    status: "active",
    duration_sec: null,
    camera_make: null,
    camera_model: null,
    iso: null,
    aperture: null,
    focal_length: null,
    focal_length_35mm: null,
    lens_model: null,
    flash_fired: null,
    gps_lat: null,
    gps_lon: null,
    created_at: null,
  };
}
