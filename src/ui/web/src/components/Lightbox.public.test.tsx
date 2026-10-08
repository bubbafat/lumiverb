import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { Lightbox } from "./Lightbox";
import type { AssetPageItem } from "../api/types";

const detail = {
  asset_id: "ast_1", library_id: "", rel_path: "", media_type: "video", status: "active",
  proxy_key: null, thumbnail_key: null, width: 1280, height: 720, file_size: null, sha256: null,
  taken_at: null, duration_sec: 25, transcript_srt: null, transcript_language: null,
  note: null, note_author: null, note_updated_at: null, ai_tags: [], video_facet: null,
};

vi.mock("../api/client", async (orig) => ({
  ...(await orig<typeof import("../api/client")>()),
  getAsset: vi.fn(async () => detail),
  findSimilar: vi.fn(async () => ({ hits: [] })),
  listFaces: vi.fn(async () => ({ faces: [] })),
}));
vi.mock("../api/useAuthenticatedImage", () => ({
  useAuthenticatedImage: () => ({ url: "blob:x", isLoading: false, error: null, generating: false }),
}));
vi.mock("../api/usePlayback", () => ({
  usePlayback: () => ({ src: "/v1/stream/t", source: "analysis_proxy", maxSeconds: 10, isLoading: false, renew: () => {}, failed: false }),
}));

afterEach(cleanup);

const asset = {
  asset_id: "ast_1", rel_path: "", file_size: 0, file_mtime: null, sha256: null, media_type: "video",
  width: 1280, height: 720, taken_at: null, status: "active", duration_sec: 25, camera_make: null,
  camera_model: null, iso: null, aperture: null, focal_length: null, focal_length_35mm: null,
  lens_model: null, flash_fired: null, gps_lat: null, gps_lon: null, face_count: 0, created_at: null,
} as AssetPageItem;

function renderLightbox(isPublic: boolean) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Lightbox
          asset={asset}
          assets={[asset]}
          onClose={() => {}}
          onNavigate={() => {}}
          onRatingChange={() => {}}
          onAddToProject={() => {}}
          isPublic={isPublic}
          publicProjectId={isPublic ? "prj_1" : undefined}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Lightbox on a public page", () => {
  it("offers a visitor nothing they can't do", async () => {
    renderLightbox(true);
    await screen.findByText(/Dimensions/);
    for (const text of ["Add note", "Upload SRT", "Add to project", "Rating", "Favorite", "Stars", "Colors", "Faces"]) {
      expect(screen.queryByText(text), text).toBeNull();
    }
    // Withheld isn't zero, and an empty section isn't shown.
    expect(screen.queryByText("0 B")).toBeNull();
    expect(screen.queryByText("Notes")).toBeNull();
  });

  it("signed in, the editing controls are there", async () => {
    renderLightbox(false);
    await screen.findByText(/Dimensions/);
    screen.getByText("Add note");
    screen.getByText("Upload SRT");
    screen.getByText("Add to project");
  });
});
