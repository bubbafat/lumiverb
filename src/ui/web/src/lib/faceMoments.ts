import type { PersonFaceItem } from "../api/client";
import type { AssetPageItem } from "../api/types";

/** A face in a video is a person seen there: when, as spans to show and jump to. */
export interface Moment {
  start: number; // ms
  end: number; // ms
}

/** Times seen this close together are one span: the person is on screen between them. */
export const SAME_MOMENT_MS = 10_000;

/** The times a person was seen (ms, any order) as spans, in order. */
export function moments(seenMs: readonly number[] | null | undefined, within = SAME_MOMENT_MS): Moment[] {
  const times = [...(seenMs ?? [])].sort((a, b) => a - b);
  const out: Moment[] = [];
  for (const t of times) {
    const last = out[out.length - 1];
    if (last && t - last.end <= within) last.end = t;
    else out.push({ start: t, end: t });
  }
  return out;
}

/** 83_000 → "1:23"; an hour or more → "1:02:03". */
export function clock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = String(total % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

export function momentLabel(m: Moment): string {
  return m.end > m.start ? `${clock(m.start)}–${clock(m.end)}` : clock(m.start);
}

export function isVideoFace(face: Pick<PersonFaceItem, "media_type" | "frame_ms">): boolean {
  return face.media_type === "video" || face.frame_ms != null;
}

/** The clip a face is in, as the lightbox takes it (what the face lists say of it). */
export function clipOfFace(face: PersonFaceItem): AssetPageItem {
  return {
    asset_id: face.asset_id,
    rel_path: face.rel_path ?? "",
    file_size: 0,
    file_mtime: null,
    sha256: null,
    media_type: isVideoFace(face) ? "video" : "image",
    width: null,
    height: null,
    taken_at: face.taken_at ?? null,
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
    face_count: 1,
    created_at: null,
  };
}

/** A person's clips: their faces grouped by clip, in the order first listed. A video's faces
 * (usually one: the person seen) bring every moment they're seen at. */
export interface PersonClip {
  face: PersonFaceItem; // the first listed: the one shown
  faces: PersonFaceItem[];
  moments: Moment[];
}

export function personClips(faces: readonly PersonFaceItem[]): PersonClip[] {
  const byClip = new Map<string, PersonClip>();
  for (const face of faces) {
    const clip = byClip.get(face.asset_id);
    if (clip) clip.faces.push(face);
    else byClip.set(face.asset_id, { face, faces: [face], moments: [] });
  }
  for (const clip of byClip.values()) {
    if (isVideoFace(clip.face)) clip.moments = moments(clip.faces.flatMap((f) => f.seen_ms ?? (f.frame_ms != null ? [f.frame_ms] : [])));
  }
  return [...byClip.values()];
}
