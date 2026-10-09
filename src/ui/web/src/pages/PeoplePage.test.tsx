import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import PeoplePage from "./PeoplePage";

const fetchMock = vi.fn();

function face(i: number) {
  return { face_id: `face_${i}`, asset_id: `ast_${i}`, bounding_box: null, detection_confidence: 0.9, rel_path: `${i}.jpg` };
}

// Clusters of 5 and 3 faces. The largest the server knows of is 9: it was
// named, or is past the clusters sent, so the slider goes further than any shown.
const clusters = {
  clusters: [
    { cluster_index: 0, size: 5, faces: [face(1), face(2)] },
    { cluster_index: 1, size: 3, faces: [face(3)] },
  ],
  truncated: false,
  max_cluster_size: 9,
};

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string) => {
    const path = new URL(url, "http://x").pathname.replace(/^\/v1/, "");
    if (path === "/people") return new Response(JSON.stringify({ items: [], next_cursor: null }));
    if (path === "/faces/clusters") return new Response(JSON.stringify(clusters));
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
      <MemoryRouter>
        <PeoplePage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("PeoplePage clusters", () => {
  it("keeps the slider when no cluster has that many faces, so it can be moved back", async () => {
    renderPage();
    const slider = await screen.findByLabelText("Min faces");
    expect(screen.getByText(/8 faces in 2 clusters/)).toBeTruthy();

    fireEvent.change(slider, { target: { value: "9" } });
    expect(screen.getByLabelText("Min faces")).toBeTruthy();
    expect(screen.getByText(/No clusters with 9 or more faces/)).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Min faces"), { target: { value: "4" } });
    expect(screen.getByText(/5 faces in 1 clusters/)).toBeTruthy();
  });
});
