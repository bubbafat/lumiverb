/**
 * A running slideshow says how to stop it (Robert, Oct 29: as a browser says how to leave full screen): shown as it
 * starts, gone after a while, back when the mouse moves, and gone once it stops.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { Lightbox, SLIDESHOW_HINT_MS } from "./Lightbox";
import type { AssetPageItem } from "../api/types";

vi.mock("../api/client", async (orig) => ({
  ...(await orig<typeof import("../api/client")>()),
  getCurrentUser: vi.fn(async () => ({ role: "editor" })),
  getAsset: vi.fn(async () => new Promise(() => {})),
  findSimilar: vi.fn(async () => ({ hits: [] })),
  listFaces: vi.fn(async () => ({ faces: [] })),
  listPeople: vi.fn(async () => ({ items: [], next_cursor: null })),
}));
vi.mock("../api/useAuthenticatedImage", () => ({
  useAuthenticatedImage: () => ({ url: "blob:x", isLoading: false, error: null, generating: false }),
}));

function photo(n: number): AssetPageItem {
  return {
    asset_id: `ast_${n}`, rel_path: `trip/${n}.jpg`, file_size: 0, file_mtime: null, content_hash: null,
    media_type: "image", width: 1600, height: 1200, taken_at: null, status: "active", duration_sec: null,
    camera_make: null, camera_model: null, iso: null, aperture: null, focal_length: null, focal_length_35mm: null,
    lens_model: null, flash_fired: null, gps_lat: null, gps_lon: null, created_at: null,
  };
}

const assets = [photo(1), photo(2), photo(3)];

beforeEach(() => vi.useFakeTimers());

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
});

function open() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { container } = render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Lightbox asset={assets[0]} assets={assets} onClose={() => {}} onNavigate={() => {}} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return container.querySelector("[data-lightbox]") as HTMLElement;
}

const space = () => act(() => { fireEvent.keyDown(window, { key: " " }); });
const later = (ms: number) => act(() => { vi.advanceTimersByTime(ms); });
const hint = () => screen.queryByText(/Slideshow on/)?.closest("[role=status]") ?? null;
const shown = () => {
  const h = hint();
  return h !== null && h.classList.contains("opacity-100") && h.getAttribute("aria-hidden") === "false";
};

describe("a running slideshow", () => {
  it("says Space stops it as it starts, then fades", () => {
    open();
    expect(hint()).toBeNull();
    space();
    expect(shown()).toBe(true);
    expect(hint()!.textContent).toMatch(/press Space to stop/);
    later(SLIDESHOW_HINT_MS - 100);
    expect(shown()).toBe(true);
    later(200);
    expect(shown()).toBe(false);
    expect(hint()!.classList.contains("opacity-0")).toBe(true);
  });

  it("says it again when the mouse moves, for as long again", () => {
    const lightbox = open();
    space();
    later(SLIDESHOW_HINT_MS + 100);
    expect(shown()).toBe(false);
    act(() => { fireEvent.mouseMove(lightbox); });
    expect(shown()).toBe(true);
    later(SLIDESHOW_HINT_MS - 100);
    act(() => { fireEvent.mouseMove(lightbox); });
    later(SLIDESHOW_HINT_MS - 100);
    expect(shown()).toBe(true);  // the last move counts
    later(200);
    expect(shown()).toBe(false);
  });

  it("says nothing once stopped, nor when the mouse moves without one", () => {
    const lightbox = open();
    space();
    space();
    expect(hint()).toBeNull();
    act(() => { fireEvent.mouseMove(lightbox); });
    expect(hint()).toBeNull();
  });
});
