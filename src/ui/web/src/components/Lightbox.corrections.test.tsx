import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { Lightbox } from "./Lightbox";
import type { AssetPageItem } from "../api/types";

let finishSave: () => void = () => {};

vi.mock("../api/client", async (orig) => ({
  ...(await orig<typeof import("../api/client")>()),
  getCurrentUser: vi.fn(async () => ({ role: "editor" })),
  getAsset: vi.fn(async (id: string) => ({
    asset_id: id, library_id: "", rel_path: `${id}.jpg`, media_type: "image", status: "active",
    proxy_key: null, thumbnail_key: null, width: 100, height: 100, file_size: null, sha256: null,
    taken_at: null, duration_sec: null, note: null, note_author: null, note_updated_at: null,
    ai_description: `about ${id}`, ai_tags: [], video_facet: null, corrected: [],
  })),
  findSimilar: vi.fn(async () => ({ hits: [] })),
  listFaces: vi.fn(async () => ({ faces: [] })),
  correctAsset: vi.fn(() => new Promise((resolve) => (finishSave = () => resolve({})))),
}));
vi.mock("../api/useAuthenticatedImage", () => ({
  useAuthenticatedImage: () => ({ url: "blob:x", isLoading: false, error: null, generating: false }),
}));

afterEach(cleanup);

const item = (id: string) => ({
  asset_id: id, rel_path: `${id}.jpg`, file_size: 0, file_mtime: null, sha256: null, media_type: "image",
  width: 100, height: 100, taken_at: null, status: "active", duration_sec: null, camera_make: null,
  camera_model: null, iso: null, aperture: null, focal_length: null, focal_length_35mm: null,
  lens_model: null, flash_fired: null, gps_lat: null, gps_lon: null, face_count: 0, created_at: null,
}) as AssetPageItem;

describe("Lightbox corrections", () => {
  it("a save that returns after moving on refreshes the clip it changed", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const a = item("ast_a"), b = item("ast_b");
    const view = (asset: AssetPageItem) => (
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <Lightbox asset={asset} assets={[a, b]} onClose={() => {}} onNavigate={() => {}} />
        </MemoryRouter>
      </QueryClientProvider>
    );
    const { rerender } = render(view(a));
    fireEvent.click(await screen.findByRole("button", { name: "Edit description" }));
    fireEvent.change(screen.getByLabelText("Description"), { target: { value: "Mittens" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    rerender(view(b));
    await screen.findByText("about ast_b");
    finishSave();

    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ["asset", "ast_a"] }));
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: ["asset", "ast_b"] });
  });
});
