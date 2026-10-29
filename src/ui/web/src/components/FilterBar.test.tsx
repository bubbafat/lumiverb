import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { FilterBar } from "./FilterBar";
import { ApiError, queryAssets, searchRefusal } from "../api/client";

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

describe("A search the server refuses", () => {
  it("is said under the search box, in the server's words", async () => {
    const said = "A search is at most 32 words; this one has 40. Leave some out.";
    fetchMock.mockResolvedValue(new Response(JSON.stringify({
      error: { code: "search_too_long", message: said, details: { max_words: 32, max_searches: 16 } },
    }), { status: 400 }));
    const refused = await queryAssets([{ type: "query", value: "word ".repeat(40) }]).catch((e: unknown) => e);
    expect(searchRefusal(refused)).toBe(said);
    expect(searchRefusal(new ApiError(500, "boom", "internal_error"))).toBeNull();
    expect(searchRefusal(null)).toBeNull();

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <FilterBar filters={[{ type: "query", value: "word ".repeat(40) }]} sort="taken_at" dir="desc"
            onSetFilter={() => {}} onSetSort={() => {}} onClearAll={() => {}} facets={facets}
            searchError={searchRefusal(refused)} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.getByRole("alert").textContent).toBe(said);
  });

  it("says a NUL character the server refused there too", async () => {
    const said = "Nothing sent to Lumiverb can hold a NUL character (\\0): send it without one.";
    fetchMock.mockResolvedValue(new Response(JSON.stringify({
      error: { code: "not_text", message: said, details: { found: "nul", in: "query" } },
    }), { status: 400 }));
    const refused = await queryAssets([{ type: "query", value: "a\u0000b" }]).catch((e: unknown) => e);
    expect(searchRefusal(refused)).toBe(said);
  });

  it("says nothing when nothing was refused", () => {
    renderBar(false);
    expect(screen.queryByRole("alert")).toBeNull();
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

describe("A search understood", () => {
  const understood = [
    { kind: "kind", words: "photos", label: "photos" },
    { kind: "person", words: "art", label: "Arthur Tuttle" },
    { kind: "place", words: "anaheim", label: "Anaheim" },
  ] as const;

  function renderUnderstood(onSetFilter: (type: string, value: string | null) => void) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <FilterBar filters={[{ type: "query", value: "photos of art in Anaheim" }]} sort="taken_at" dir="desc"
            onSetFilter={onSetFilter} onSetSort={() => {}} onClearAll={() => {}} facets={facets}
            understood={[...understood]} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it("says what it was taken for, a card each", () => {
    renderUnderstood(() => {});
    const said = screen.getByLabelText("Searched for").textContent ?? "";
    expect(said).toContain("Arthur Tuttle");
    expect(said).toContain("Anaheim");
    expect(said).toContain("photos");
  });

  it("dismissing a card searches its words as words", () => {
    const onSetFilter = vi.fn();
    renderUnderstood(onSetFilter);
    fireEvent.click(screen.getByRole("button", { name: "Not Arthur Tuttle: search “art” as words" }));
    expect(onSetFilter).toHaveBeenCalledWith("query", 'photos of "art" in Anaheim');
  });
});
