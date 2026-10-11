import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { Lightbox } from "./Lightbox";
import type { AssetPageItem } from "../api/types";

vi.mock("../api/client", async (orig) => ({
  ...(await orig<typeof import("../api/client")>()),
  getCurrentUser: vi.fn(async () => ({ role: "editor" })),
  getAsset: vi.fn(async (id: string) => ({
    asset_id: id, library_id: "", rel_path: "party.mov", media_type: "video", status: "active",
    proxy_key: null, thumbnail_key: null, width: 1920, height: 1080, file_size: null, sha256: null,
    taken_at: null, duration_sec: 120, note: null, note_author: null, note_updated_at: null,
    ai_description: null, ai_tags: [], video_facet: null, corrected: [], face_count: 2,
  })),
  findSimilar: vi.fn(async () => ({ hits: [] })),
  listFaces: vi.fn(async () => ({
    faces: [
      { face_id: "f_ada", bounding_box: { x: 0.1, y: 0.1, w: 0.2, h: 0.2 }, detection_confidence: 0.9,
        person: { person_id: "per_ada", display_name: "Ada", dismissed: false }, frame_ms: 8_000,
        seen_ms: [4_000, 8_000, 95_000] },
      { face_id: "f_new", bounding_box: { x: 0.5, y: 0.1, w: 0.2, h: 0.2 }, detection_confidence: 0.8,
        person: null, frame_ms: 4_000, seen_ms: [4_000] },
    ],
  })),
  listPeople: vi.fn(async () => ({ items: [], next_cursor: null })),
  getNearestPeopleForFace: vi.fn(async () => []),
}));
vi.mock("../api/useAuthenticatedImage", () => ({
  useAuthenticatedImage: () => ({ url: "blob:x", isLoading: false, error: null, generating: false }),
}));
vi.mock("../api/usePlayback", () => ({
  usePlayback: () => ({ src: "/v1/stream/party", source: "analysis_proxy", maxSeconds: null, isLoading: false,
                        renew: () => {}, failed: false }),
}));
vi.mock("../api/useFaceCrop", () => ({ useFaceCrop: () => ({ url: null, isLoading: false }) }));

beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// As a page that doesn't say how many faces it has would pass it: the clip's details say.
const video = {
  asset_id: "ast_party", rel_path: "party.mov", file_size: 0, file_mtime: null, sha256: null, media_type: "video",
  width: 1920, height: 1080, taken_at: null, status: "active", duration_sec: 120, camera_make: null,
  camera_model: null, iso: null, aperture: null, focal_length: null, focal_length_35mm: null, lens_model: null,
  flash_fired: null, gps_lat: null, gps_lon: null, created_at: null,
} as AssetPageItem;

function open() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Lightbox asset={video} assets={[video]} onClose={() => {}} onNavigate={() => {}} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("a video's people in the lightbox", () => {
  it("lists who's seen, with when", async () => {
    open();
    const people = await screen.findByRole("list", { name: "People in this video" });
    const rows = within(people).getAllByRole("listitem");
    expect(within(rows[0]).getByRole("button", { name: "Ada" })).toBeTruthy();
    expect(within(rows[0]).getAllByRole("button").map((b) => b.textContent)).toEqual(["Ada", "0:04–0:08", "1:35"]);
    expect(within(rows[1]).getByRole("button", { name: "Who is this?" })).toBeTruthy();
  });

  it("plays from a moment", async () => {
    open();
    fireEvent.click(await screen.findByRole("button", { name: "1:35" }));
    const player = document.querySelector("video") as HTMLVideoElement;
    expect(player.currentTime).toBe(95);
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalled();
  });

  it("names someone with the photos' popover", async () => {
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Who is this?" }));
    expect(await screen.findByRole("button", { name: "+ New person" })).toBeTruthy();
  });
});
