import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DirectoryTree } from "./DirectoryTree";

const api = vi.hoisted(() => ({ listDirectories: vi.fn() }));
vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  ...api,
}));

function renderTree(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 30_000 } } });
  const view = render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
  return { ...view, client };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("DirectoryTree folder menu", () => {
  it("archives a folder with its count, subfolders included", async () => {
    api.listDirectories.mockResolvedValue([{ name: "Paris", path: "Trips/Paris", asset_count: 42 }]);
    const onArchiveFolder = vi.fn();
    renderTree(<DirectoryTree libraryId="lib_1" activePath={null} onNavigate={() => {}} onArchiveFolder={onArchiveFolder} />);
    fireEvent.click(await screen.findByRole("button", { name: "Paris: 42 clips, folder actions" }));
    expect(screen.queryByRole("menuitem", { name: /Exclude folder/ })).toBeNull();
    fireEvent.click(screen.getByRole("menuitem", { name: /Archive folder/ }));
    expect(onArchiveFolder).toHaveBeenCalledWith("Trips/Paris", 42);
  });

  it("shows only the count, no menu, when nothing can be done", async () => {
    api.listDirectories.mockResolvedValue([{ name: "Paris", path: "Trips/Paris", asset_count: 42 }]);
    renderTree(<DirectoryTree libraryId="lib_1" activePath={null} onNavigate={() => {}} />);
    expect(await screen.findByText("42")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /folder actions/ })).toBeNull();
  });
});

describe("DirectoryTree counts", () => {
  let paris = 3;
  function serve() {
    api.listDirectories.mockImplementation(async (_lib: string, parent?: string) =>
      parent === "Trips"
        ? [{ name: "Paris", path: "Trips/Paris", asset_count: paris }]
        : [{ name: "Trips", path: "Trips", asset_count: 10 }],
    );
  }

  it("refetches the top level and every open folder when the library's listings are invalidated", async () => {
    paris = 3;
    serve();
    const { client } = renderTree(<DirectoryTree libraryId="lib_1" activePath={null} onNavigate={() => {}} />);
    await screen.findByText("Trips");
    fireEvent.click(screen.getByRole("button", { name: "Expand" }));
    expect(await screen.findByText("3")).toBeTruthy();

    paris = 4;
    await act(() => client.invalidateQueries({ queryKey: ["directories", "lib_1"] }));
    expect(await screen.findByText("4")).toBeTruthy();
    expect(api.listDirectories).toHaveBeenCalledWith("lib_1", "Trips");
  });

  it("shows a closed folder's last listing when it's opened again, and refetches it if it went stale", async () => {
    paris = 3;
    serve();
    const { client } = renderTree(<DirectoryTree libraryId="lib_1" activePath={null} onNavigate={() => {}} />);
    await screen.findByText("Trips");
    fireEvent.click(screen.getByRole("button", { name: "Expand" }));
    await screen.findByText("3");
    fireEvent.click(screen.getByRole("button", { name: "Collapse" }));
    expect(screen.queryByText("Paris")).toBeNull();

    // The library changes while Trips is closed: nothing refetches it now...
    paris = 5;
    const calls = api.listDirectories.mock.calls.length;
    await act(() => client.invalidateQueries({ queryKey: ["directories", "lib_1"] }));
    expect(api.listDirectories.mock.calls.filter(([, p]) => p === "Trips").length).toBe(1);
    expect(api.listDirectories.mock.calls.length).toBe(calls + 1);

    // ...but opening it shows what it held right away, then the new count.
    fireEvent.click(screen.getByRole("button", { name: "Expand" }));
    expect(screen.getByText("3")).toBeTruthy();
    expect(await screen.findByText("5")).toBeTruthy();
  });

  it("starts another library with every folder closed", async () => {
    paris = 3;
    serve();
    const { rerender, client } = renderTree(
      <DirectoryTree libraryId="lib_1" activePath={null} onNavigate={() => {}} />,
    );
    await screen.findByText("Trips");
    fireEvent.click(screen.getByRole("button", { name: "Expand" }));
    await screen.findByText("Paris");
    rerender(
      <QueryClientProvider client={client}>
        <DirectoryTree libraryId="lib_2" activePath={null} onNavigate={() => {}} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(api.listDirectories).toHaveBeenCalledWith("lib_2"));
    expect(screen.queryByText("Paris")).toBeNull();
    expect(api.listDirectories).not.toHaveBeenCalledWith("lib_2", "Trips");
  });
});

describe("DirectoryTree nested folders", () => {
  it("doesn't fetch or refresh an open folder while its parent is closed", async () => {
    api.listDirectories.mockImplementation(async (_lib: string, parent?: string) => {
      if (!parent) return [{ name: "Trips", path: "Trips", asset_count: 10 }];
      if (parent === "Trips") return [{ name: "Paris", path: "Trips/Paris", asset_count: 4 }];
      if (parent === "Trips/Paris") return [{ name: "Day 1", path: "Trips/Paris/Day 1", asset_count: 2 }];
      return [];
    });
    const { client } = renderTree(<DirectoryTree libraryId="lib_1" activePath={null} onNavigate={() => {}} />);
    await screen.findByText("Trips");
    fireEvent.click(screen.getByRole("button", { name: "Expand" }));
    await screen.findByText("Paris");
    fireEvent.click(screen.getAllByRole("button", { name: "Expand" })[0]);
    await screen.findByText("Day 1");

    // Close Trips: Paris stays open inside it, off screen.
    fireEvent.click(screen.getAllByRole("button", { name: "Collapse" })[0]);
    const parisFetches = () => api.listDirectories.mock.calls.filter(([, p]) => p === "Trips/Paris").length;
    expect(parisFetches()).toBe(1);
    await act(() => client.invalidateQueries({ queryKey: ["directories", "lib_1"] }));
    expect(parisFetches()).toBe(1);

    // Open Trips again: Paris is still open, and refreshes.
    fireEvent.click(screen.getByRole("button", { name: "Expand" }));
    expect(await screen.findByText("Day 1")).toBeTruthy();
    await waitFor(() => expect(parisFetches()).toBe(2));
  });
});
