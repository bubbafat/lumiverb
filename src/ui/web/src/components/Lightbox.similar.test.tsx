import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { Lightbox } from "./Lightbox";
import { findSimilar } from "../api/client";
import type { AssetPageItem, SimilarityResponse } from "../api/types";

vi.mock("../api/client", async (orig) => ({
  ...(await orig<typeof import("../api/client")>()),
  getCurrentUser: vi.fn(async () => ({ role: "editor" })),
  getAsset: vi.fn(async (id: string) => ({
    asset_id: id, library_id: "lib_1", rel_path: "walk.mov", media_type: "video", status: "active",
    proxy_key: null, thumbnail_key: null, width: 1920, height: 1080, file_size: null, content_hash: null,
    taken_at: null, duration_sec: 120, note: null, note_author: null, note_updated_at: null,
    ai_description: null, ai_tags: [], video_facet: null, corrected: [], face_count: 0,
  })),
  findSimilar: vi.fn(),
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

const walk = {
  asset_id: "ast_walk", rel_path: "walk.mov", file_size: 0, file_mtime: null, content_hash: null, media_type: "video",
  width: 1920, height: 1080, taken_at: null, status: "active", duration_sec: 120, camera_make: null,
  camera_model: null, iso: null, aperture: null, focal_length: null, focal_length_35mm: null, lens_model: null,
  flash_fired: null, gps_lat: null, gps_lon: null, created_at: null,
} as AssetPageItem;

function hit(asset_id: string, rel_path: string, media_type: string, at_ms: number | null) {
  return { asset_id, rel_path, thumbnail_key: null, proxy_key: null, distance: 0.1, media_type, file_size: null,
           width: null, height: null, at_ms };
}

function open(answer: SimilarityResponse, onSimilarClick = vi.fn()) {
  vi.mocked(findSimilar).mockResolvedValue(answer);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Lightbox asset={walk} assets={[walk]} onClose={() => {}} onNavigate={() => {}} libraryId="lib_1"
                  onSimilarClick={onSimilarClick} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Find similar" }));
  return onSimilarClick;
}

describe("Find similar from a video", () => {
  it("opens a video found by a scene at that scene, and a photo as it is", async () => {
    const opened = open({
      source_asset_id: "ast_walk", total: 2, embedding_available: true,
      hits: [hit("ast_trip", "trip.mov", "video", 42_000), hit("ast_shore", "shore.jpg", "image", null)],
    });
    fireEvent.click(await screen.findByRole("button", { name: "trip.mov" }));
    expect(opened).toHaveBeenLastCalledWith(expect.objectContaining({ asset_id: "ast_trip", media_type: "video" }), 42);
    fireEvent.click(screen.getByRole("button", { name: "shore.jpg" }));
    expect(opened).toHaveBeenLastCalledWith(expect.objectContaining({ asset_id: "ast_shore" }), undefined);
  });

  it("says when its scenes aren't compared yet", async () => {
    open({ source_asset_id: "ast_walk", total: 0, embedding_available: false, hits: [] });
    expect(await screen.findByText("Its scenes aren't compared by their look yet.")).toBeTruthy();
  });
});
