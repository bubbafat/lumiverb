import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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

let role = "editor";

vi.mock("../api/client", async (orig) => ({
  ...(await orig<typeof import("../api/client")>()),
  getCurrentUser: vi.fn(async () => ({ role })),
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

afterEach(() => {
  cleanup();
  role = "editor";
});

const asset = {
  asset_id: "ast_1", rel_path: "", file_size: 0, file_mtime: null, sha256: null, media_type: "video",
  width: 1280, height: 720, taken_at: null, status: "active", duration_sec: 25, camera_make: null,
  camera_model: null, iso: null, aperture: null, focal_length: null, focal_length_35mm: null,
  lens_model: null, flash_fired: null, gps_lat: null, gps_lon: null, face_count: 0, created_at: null,
} as AssetPageItem;

const onArchive = vi.fn();
const onTrash = vi.fn();

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
          onArchive={onArchive}
          onTrash={onTrash}
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
    for (const text of ["Add note", "Upload SRT", "Add to project", "Rating", "Favorite", "Stars", "Colors", "Faces",
                        "Archive", "Move to trash"]) {
      expect(screen.queryByText(text), text).toBeNull();
    }
    // Withheld isn't zero, and an empty section isn't shown.
    expect(screen.queryByText("0 B")).toBeNull();
    expect(screen.queryByText("Notes")).toBeNull();
  });

  it("offers a visitor no faces on a photo that has some", async () => {
    const photo = { ...asset, asset_id: "ast_2", media_type: "image", face_count: 3 } as AssetPageItem;
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <Lightbox asset={photo} assets={[photo]} onClose={() => {}} onNavigate={() => {}} isPublic publicLibraryId="lib_1" />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await screen.findByText(/Dimensions/);
    expect(screen.queryByText(/Show faces/)).toBeNull();
    expect(screen.queryByText("Faces")).toBeNull();
  });

  it("signed in, the editing controls are there", async () => {
    renderLightbox(false);
    await screen.findByText(/Dimensions/);
    screen.getByText("Add note");
    await screen.findByText("Upload SRT");
    screen.getByText("Add to project");
  });

  it("a viewer can't upload a transcript", async () => {
    role = "viewer";
    renderLightbox(false);
    await screen.findByText(/Dimensions/);
    await screen.findByText("No transcript");
    expect(screen.queryByText("Upload SRT")).toBeNull();
  });
});

describe("Lightbox signed in", () => {
  it("shows a video's still before it plays", async () => {
    const { container } = renderLightbox(false);
    await screen.findByText(/Dimensions/);
    const video = container.querySelector("video") as HTMLVideoElement;
    expect(video.getAttribute("poster")).toBe("blob:x");
    expect(video.getAttribute("src")).toBe("/v1/stream/t");
  });

  it("archives or trashes the clip it shows", async () => {
    renderLightbox(false);
    fireEvent.click(await screen.findByRole("button", { name: "Archive" }));
    expect(onArchive).toHaveBeenCalledWith("ast_1");
    fireEvent.click(screen.getByRole("button", { name: "Move to trash" }));
    expect(onTrash).toHaveBeenCalledWith("ast_1");
  });

  it("fills the window where the browser has no element fullscreen (an iPhone)", async () => {
    // jsdom, like an iPhone, has no Element.requestFullscreen.
    expect(typeof HTMLElement.prototype.requestFullscreen).not.toBe("function");
    const { container } = renderLightbox(false);
    await screen.findByText(/Dimensions/);
    fireEvent.click(screen.getByRole("button", { name: "Enter fullscreen" }));
    expect(screen.getByRole("button", { name: "Exit fullscreen" })).toBeTruthy();
    expect(screen.queryByText(/Dimensions/)).toBeNull();  // the details are hidden
    expect(container.querySelector("video")!.className).not.toContain("55svh");
  });
});
