import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { Lightbox } from "./Lightbox";
import type { AssetPageItem, SearchContext } from "../api/types";

vi.mock("../api/client", async (orig) => ({
  ...(await orig<typeof import("../api/client")>()),
  getCurrentUser: vi.fn(async () => ({ role: "editor" })),
  getAsset: vi.fn(async (id: string) => ({
    asset_id: id, library_id: "lib_1", rel_path: "walk.mov", media_type: "video", status: "active",
    proxy_key: null, thumbnail_key: null, width: 1920, height: 1080, file_size: null, content_hash: null,
    taken_at: null, duration_sec: 120, note: null, note_author: null, note_updated_at: null,
    ai_description: null, ai_tags: [], video_facet: null, corrected: [], face_count: 0,
  })),
  findSimilar: vi.fn(async () => ({ hits: [] })),
  listFaces: vi.fn(async () => ({ faces: [] })),
  listPeople: vi.fn(async () => ({ items: [], next_cursor: null })),
}));
vi.mock("../api/useAuthenticatedImage", () => ({
  useAuthenticatedImage: () => ({ url: "blob:x", isLoading: false, error: null, generating: false }),
}));
vi.mock("../api/usePlayback", () => ({
  usePlayback: () => ({ src: "/v1/stream/walk", source: "lumiverb.video.analysis_proxy", whole: true, maxSeconds: null,
                        isLoading: false, renew: () => {}, failed: false }),
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function walk(search_context: SearchContext | null): AssetPageItem {
  return {
    asset_id: "ast_walk", rel_path: "walk.mov", file_size: 0, file_mtime: null, content_hash: null,
    media_type: "video", width: 1920, height: 1080, taken_at: null, status: "active", duration_sec: 120,
    camera_make: null, camera_model: null, iso: null, aperture: null, focal_length: null, focal_length_35mm: null,
    lens_model: null, flash_fired: null, gps_lat: null, gps_lon: null, created_at: null, search_context,
  };
}

function open(asset: AssetPageItem) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Lightbox asset={asset} assets={[asset]} onClose={() => {}} onNavigate={() => {}} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("what found a search's clip, in the lightbox", () => {
  it("says it, and opens a video at the scene that matched", () => {
    const { container } = open(walk({ score: 1, hit_type: "scene", snippet: "A sign for Dundee by the road",
                                      start_ms: 42_000, end_ms: 45_000, matched_in: "scene" }));
    const said = screen.getByRole("region", { name: "Found by" });
    expect(within(said).getByText("Scene at 0:42")).toBeTruthy();
    expect(within(said).getByText(/A sign for Dundee by the road/)).toBeTruthy();
    const video = container.querySelector("video") as HTMLVideoElement;
    Object.defineProperty(video, "currentTime", { value: 0, writable: true, configurable: true });
    Object.defineProperty(video, "duration", { value: 120, configurable: true });
    fireEvent.loadedMetadata(video);
    expect(video.currentTime).toBe(42);
  });

  it("says nothing when its description found it, or outside a search", () => {
    open(walk({ score: 1, hit_type: "asset", snippet: "A walk", start_ms: null, end_ms: null,
                matched_in: "description" }));
    expect(screen.queryByRole("region", { name: "Found by" })).toBeNull();
    cleanup();
    open(walk(null));
    expect(screen.queryByRole("region", { name: "Found by" })).toBeNull();
  });
});
