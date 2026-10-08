import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { DirectoryTree } from "./DirectoryTree";

const api = vi.hoisted(() => ({ listDirectories: vi.fn() }));
vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  ...api,
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("DirectoryTree folder menu", () => {
  it("archives a folder with its count, subfolders included", async () => {
    api.listDirectories.mockResolvedValue([{ name: "Paris", path: "Trips/Paris", asset_count: 42 }]);
    const onArchiveFolder = vi.fn();
    render(<DirectoryTree libraryId="lib_1" activePath={null} onNavigate={() => {}} onArchiveFolder={onArchiveFolder} />);
    fireEvent.click(await screen.findByRole("button", { name: "Paris: 42 clips, folder actions" }));
    expect(screen.queryByRole("menuitem", { name: /Exclude folder/ })).toBeNull();
    fireEvent.click(screen.getByRole("menuitem", { name: /Archive folder/ }));
    expect(onArchiveFolder).toHaveBeenCalledWith("Trips/Paris", 42);
  });

  it("shows only the count, no menu, when nothing can be done", async () => {
    api.listDirectories.mockResolvedValue([{ name: "Paris", path: "Trips/Paris", asset_count: 42 }]);
    render(<DirectoryTree libraryId="lib_1" activePath={null} onNavigate={() => {}} />);
    expect(await screen.findByText("42")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /folder actions/ })).toBeNull();
  });
});
