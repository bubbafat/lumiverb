import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import PublicProjectPage from "./PublicProjectPage";

vi.mock("../api/useAuthenticatedImage", () => ({
  useAuthenticatedImage: () => ({ url: "blob:thumb", isLoading: false, error: null }),
}));

// jsdom has no layout: give every element a size and a ResizeObserver that
// reports it, so the grid can measure itself the way a browser would.
class FakeResizeObserver {
  constructor(private cb: ResizeObserverCallback) {}
  observe(target: Element) {
    this.cb(
      [{ target, contentRect: { width: 800, height: 800 } } as unknown as ResizeObserverEntry],
      this as unknown as ResizeObserver,
    );
  }
  unobserve() {}
  disconnect() {}
}

function asset(id: string) {
  return { asset_id: id, media_type: "image", width: 1600, height: 1200, taken_at: "2026-04-09T12:00:00Z", duration_sec: null };
}

const fetchMock = vi.fn();

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", FakeResizeObserver);
  vi.stubGlobal("fetch", fetchMock);
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(800);
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(800);
  fetchMock.mockImplementation(async (url: string) => {
    const body = url.includes("/assets")
      ? url.includes("after=c1")
        ? { items: [asset("ast_3")], next_cursor: null }
        : { items: [asset("ast_1"), asset("ast_2")], next_cursor: "c1" }
      : { project_id: "prj_1", name: "Shared", description: null, asset_count: 3 };
    return new Response(JSON.stringify(body), { status: 200 });
  });
});

afterEach(() => {
  cleanup();
  fetchMock.mockReset();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/public/projects/prj_1"]}>
        <Routes>
          <Route path="/public/projects/:projectId" element={<PublicProjectPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const thumbs = (root: HTMLElement) => root.querySelectorAll('img[src="blob:thumb"]');

describe("PublicProjectPage", () => {
  it("shows the clips when the project loads after the first render", async () => {
    // A cold load renders "Loading..." first, so the grid mounts later.
    const { container } = renderPage();
    await waitFor(() => expect(thumbs(container).length).toBe(2));
  });

  it("loads the next page when scrolled near the bottom", async () => {
    const { container } = renderPage();
    await waitFor(() => expect(thumbs(container).length).toBe(2));
    const scroller = container.querySelector(".overflow-y-auto") as HTMLElement;
    Object.defineProperty(scroller, "scrollHeight", { value: 1000, configurable: true });
    Object.defineProperty(scroller, "clientHeight", { value: 800, configurable: true });
    scroller.scrollTop = 100;
    fireEvent.scroll(scroller);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("after=c1")));
  });
});
