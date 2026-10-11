import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

// What the page opens the lightbox with: the clip, and where in a video.
const opened: { asset_id: string; media_type: string; startAt?: number }[] = [];
vi.mock("../components/Lightbox", () => ({
  Lightbox: (props: { asset: { asset_id: string; media_type: string }; startAt?: number }) => {
    opened.push({ asset_id: props.asset.asset_id, media_type: props.asset.media_type, startAt: props.startAt });
    return <div data-testid="lightbox" />;
  },
}));

import PersonDetailPage from "./PersonDetailPage";

const fetchMock = vi.fn();
const faces = [
  { face_id: "f_photo", asset_id: "ast_photo", bounding_box: null, detection_confidence: 0.9, rel_path: "beach.jpg",
    media_type: "image", frame_ms: null, seen_ms: null },
  { face_id: "f_video", asset_id: "ast_video", bounding_box: null, detection_confidence: 0.9, rel_path: "party.mov",
    media_type: "video", frame_ms: 8_000, seen_ms: [4_000, 8_000, 95_000] },
];

beforeEach(() => {
  opened.length = 0;
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string) => {
    const path = new URL(url, "http://x").pathname.replace(/^\/v1/, "");
    if (path === "/people/per_1") {
      return new Response(JSON.stringify({ person_id: "per_1", display_name: "Ada", face_count: 2 }));
    }
    if (path === "/people/per_1/faces") return new Response(JSON.stringify({ items: faces, next_cursor: null }));
    if (path === "/me") return new Response(JSON.stringify({ email: "e@x.y", role: "viewer" }));
    return new Response("{}", { status: 404 });
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  fetchMock.mockReset();
});

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/people/per_1"]}>
        <Routes>
          <Route path="/people/:personId" element={<PersonDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("PersonDetailPage", () => {
  it("shows their photos and videos, a video with the moments they're seen", async () => {
    renderPage();
    expect(await screen.findByText("2 clips")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Open beach.jpg" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Play party.mov" })).toBeTruthy();
    const when = screen.getByLabelText("When they're seen in party.mov");
    expect(within(when).getAllByRole("button").map((b) => b.textContent)).toEqual(["0:04–0:08", "1:35"]);
  });

  it("opens a video where they're shown, or at the moment picked", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Play party.mov" }));
    expect(opened[opened.length - 1]).toEqual({ asset_id: "ast_video", media_type: "video", startAt: 8 });
    fireEvent.click(screen.getByRole("button", { name: "1:35" }));
    expect(opened[opened.length - 1]).toEqual({ asset_id: "ast_video", media_type: "video", startAt: 95 });
  });

  it("opens a photo as a photo", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Open beach.jpg" }));
    expect(opened[opened.length - 1]).toEqual({ asset_id: "ast_photo", media_type: "image", startAt: undefined });
  });
});
