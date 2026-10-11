import { describe, expect, it } from "vitest";
import { clipOfFace, clock, momentLabel, moments, personClips } from "./faceMoments";
import type { PersonFaceItem } from "../api/client";

function face(over: Partial<PersonFaceItem>): PersonFaceItem {
  return { face_id: "f", asset_id: "a", bounding_box: null, detection_confidence: 0.9, rel_path: "a.mov", ...over };
}

describe("moments", () => {
  it("joins times seen close together into one span, in order", () => {
    expect(moments([40_000, 1_000, 4_000, 9_000, 61_000])).toEqual([
      { start: 1_000, end: 9_000 }, { start: 40_000, end: 40_000 }, { start: 61_000, end: 61_000 }]);
    expect(moments(null)).toEqual([]);
  });

  it("says a span as clock times", () => {
    expect(clock(83_000)).toBe("1:23");
    expect(clock(3_723_000)).toBe("1:02:03");
    expect(momentLabel({ start: 4_000, end: 9_500 })).toBe("0:04–0:09");
    expect(momentLabel({ start: 4_000, end: 4_000 })).toBe("0:04");
  });
});

describe("a person's clips", () => {
  it("is one per clip, a video's moments from all its faces of them", () => {
    const clips = personClips([
      face({ face_id: "p1", asset_id: "photo", rel_path: "p.jpg", media_type: "image" }),
      face({ face_id: "v1", asset_id: "video", media_type: "video", frame_ms: 4_000, seen_ms: [1_000, 4_000] }),
      face({ face_id: "v2", asset_id: "video", media_type: "video", frame_ms: 70_000, seen_ms: [70_000] }),
    ]);
    expect(clips.map((c) => [c.face.face_id, c.faces.length])).toEqual([["p1", 1], ["v1", 2]]);
    expect(clips[0].moments).toEqual([]);
    expect(clips[1].moments).toEqual([{ start: 1_000, end: 4_000 }, { start: 70_000, end: 70_000 }]);
  });

  it("opens a video face's clip as a video, a photo's as a photo", () => {
    expect(clipOfFace(face({ media_type: "video", frame_ms: 1 })).media_type).toBe("video");
    expect(clipOfFace(face({ media_type: "image" })).media_type).toBe("image");
  });
});
