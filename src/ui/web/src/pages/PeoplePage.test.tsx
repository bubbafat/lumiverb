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

describe("PeoplePage cluster order", () => {
  const clusterUrls = () =>
    fetchMock.mock.calls
      .map(([url]) => new URL(url as string, "http://x"))
      .filter((u) => u.pathname.endsWith("/faces/clusters"));

  beforeEach(() => {
    try {
      window.localStorage.clear();
    } catch {
      /* no storage */
    }
  });

  it("asks for the largest first by default", async () => {
    renderPage();
    const select = (await screen.findByLabelText("Sort clusters")) as HTMLSelectElement;
    expect(select.value).toBe("size_desc");
    expect(screen.getByRole("option", { name: "Largest first" })).toBeTruthy();
    expect(clusterUrls()[0].searchParams.get("sort")).toBe("size_desc");
  });

  it("asks again in the order chosen and remembers it", async () => {
    renderPage();
    const select = await screen.findByLabelText("Sort clusters");
    fireEvent.change(select, { target: { value: "size_asc" } });
    await vi.waitFor(() => expect(clusterUrls().map((u) => u.searchParams.get("sort"))).toContain("size_asc"));
    expect(window.localStorage.getItem("lv_cluster_sort")).toBe(JSON.stringify("size_asc"));
  });

  it("starts in the order remembered", async () => {
    window.localStorage.setItem("lv_cluster_sort", JSON.stringify("newest"));
    renderPage();
    const select = (await screen.findByLabelText("Sort clusters")) as HTMLSelectElement;
    expect(select.value).toBe("newest");
    expect(clusterUrls()[0].searchParams.get("sort")).toBe("newest");
  });

  it("ignores an order it doesn't know", async () => {
    window.localStorage.setItem("lv_cluster_sort", JSON.stringify("random"));
    renderPage();
    const select = (await screen.findByLabelText("Sort clusters")) as HTMLSelectElement;
    expect(select.value).toBe("size_desc");
    expect(clusterUrls()[0].searchParams.get("sort")).toBe("size_desc");
  });
});

describe("PeoplePage cluster actions", () => {
  function as(role: string) {
    fetchMock.mockImplementation(async (url: string) => {
      const path = new URL(url, "http://x").pathname.replace(/^\/v1/, "");
      if (path === "/me") return new Response(JSON.stringify({ user_id: "u", email: "e", role }));
      if (path === "/people") return new Response(JSON.stringify({ items: [], next_cursor: null }));
      if (path === "/faces/clusters") return new Response(JSON.stringify(clusters));
      return new Response("{}", { status: 404 });
    });
  }

  it("shows naming and dismissing to an editor", async () => {
    as("editor");
    renderPage();
    expect(await screen.findByText("Name all 5")).toBeTruthy();
    expect(screen.getAllByText("Dismiss").length).toBeGreaterThan(0);
  });

  it("hides them from a viewer: the server would refuse", async () => {
    as("viewer");
    renderPage();
    await screen.findByLabelText("Min faces");
    await new Promise((r) => setTimeout(r, 50)); // the role has arrived by now
    expect(screen.queryByText("Name all 5")).toBeNull();
    expect(screen.queryByText("Dismiss")).toBeNull();
  });
});
