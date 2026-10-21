import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { FilterBar } from "./FilterBar";

const fetchMock = vi.fn();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockResolvedValue(new Response(JSON.stringify({ items: [] }), { status: 200 }));
  try {
    localStorage.setItem("lv_show_filters", "true");
  } catch {
    /* ignore */
  }
});

afterEach(() => {
  cleanup();
  fetchMock.mockReset();
  vi.unstubAllGlobals();
});

const facets = {
  media_types: ["video"], camera_makes: [], camera_models: [], lens_models: [],
  iso_range: [null, null], aperture_range: [null, null], focal_length_range: [null, null],
  has_gps_count: 0, has_face_count: 0,
};

function renderBar(isPublic: boolean) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <FilterBar
          filters={[]}
          sort="taken_at"
          dir="desc"
          onSetFilter={() => {}}
          onSetSort={() => {}}
          onClearAll={() => {}}
          facets={facets}
          onSaveAsProject={() => {}}
          isPublic={isPublic}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function openFilters() {
  const toggle = screen.queryByTitle("Show filters");
  if (toggle) fireEvent.click(toggle);
}

describe("FilterBar for a public page's visitor", () => {
  it("offers no rating or people filters, which belong to signed-in people", () => {
    renderBar(true);
    openFilters();
    expect(screen.queryByText("Favorites")).toBeNull();
    expect(screen.queryByText("Rating")).toBeNull();
    expect(screen.queryByTitle("3 stars")).toBeNull();
    expect(screen.queryByPlaceholderText("Search people...")).toBeNull();
  });

  it("doesn't look people up while a visitor types", () => {
    renderBar(true);
    const search = screen.getAllByRole("searchbox")[0] ?? screen.getAllByRole("textbox")[0];
    fireEvent.focus(search);
    fireEvent.change(search, { target: { value: "ann" } });
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes("/people"))).toBe(false);
  });

  it("signed in, they're all there", () => {
    renderBar(false);
    openFilters();
    screen.getByText("Favorites");
    screen.getByTitle("3 stars");
  });
});

describe("FilterBar's Photos nearby", () => {
  function barNear(near: string) {
    const onSetFilter = vi.fn();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <FilterBar filters={[{ type: "near", value: near }]} sort="taken_at" dir="desc" onSetFilter={onSetFilter}
            onSetSort={() => {}} onClearAll={() => {}} facets={facets} isPublic={false} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    return onSetFilter;
  }

  it("changes how far it looks on its chip, the filters closed, and clears it there", () => {
    try {
      localStorage.setItem("lv_show_filters", "false");
    } catch {
      /* ignore */
    }
    const onSetFilter = barNear("40.6892,-74.0445,1");
    const distance = screen.getByLabelText("Distance") as HTMLSelectElement;
    expect(distance.value).toBe("1");
    expect(Array.from(distance.options).map((o) => o.text)).toContain("500 km");
    fireEvent.change(distance, { target: { value: "25" } });
    expect(onSetFilter).toHaveBeenCalledWith("near", "40.6892,-74.0445,25");
    fireEvent.click(screen.getByRole("button", { name: "Clear distance" }));
    expect(onSetFilter).toHaveBeenCalledWith("near", null);
  });

  it("shows a distance from a link that isn't one of its own, in order", () => {
    barNear("40.6892,-74.0445,3");
    const distance = screen.getByLabelText("Distance") as HTMLSelectElement;
    expect(distance.value).toBe("3");
    const shown = Array.from(distance.options).map((o) => o.value);
    expect(shown.indexOf("3")).toBe(shown.indexOf("2") + 1);
  });
});

describe("FilterBar saving a search", () => {
  function barWith(filters: { type: string; value: string }[], onSaveAsProject = vi.fn(), folder?: string) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <FilterBar filters={filters} sort="taken_at" dir="desc" onSetFilter={() => {}} onSetSort={() => {}}
            onClearAll={() => {}} facets={facets} onSaveAsProject={onSaveAsProject} folder={folder} isPublic={false} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    return onSaveAsProject;
  }

  it("a folder's search can be saved as a project", () => {
    const save = barWith([{ type: "library", value: "lib_1" }, { type: "path", value: "Shoots/2026" }]);
    fireEvent.click(screen.getByRole("button", { name: "Save as project" }));
    expect(save).toHaveBeenCalled();
    screen.getByRole("button", { name: "Clear filters" });
  });

  it("a folder picked in the library's tree can be saved as a project", () => {
    // The library page narrows by ?path=, outside the filters.
    const save = barWith([{ type: "library", value: "lib_1" }], vi.fn(), "Shoots/2026");
    fireEvent.click(screen.getByRole("button", { name: "Save as project" }));
    expect(save).toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Clear filters" })).toBeNull();  // nothing to clear
  });

  it("a library alone isn't a search to save", () => {
    barWith([{ type: "library", value: "lib_1" }]);
    expect(screen.queryByRole("button", { name: "Save as project" })).toBeNull();
  });
});
