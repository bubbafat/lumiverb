import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { ApiError } from "../api/client";
import LibraryReaderSection from "./LibraryReaderSection";

const api = vi.hoisted(() => ({
  getCurrentUser: vi.fn(),
  getLibrary: vi.fn(),
  listDevices: vi.fn(),
  chooseLibraryReader: vi.fn(),
}));
vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  ...api,
}));

const STUDIO = { device_id: "dev_1", name: "Studio Mac", registered_at: "2026-10-17T12:00:00Z", has_key: true,
                 key_last_used_at: null, libraries: [] };

beforeEach(() => {
  api.getCurrentUser.mockResolvedValue({ email: "a@b.c", role: "admin", server_admin: true });
  api.getLibrary.mockResolvedValue({ library_id: "lib_1", name: "Trips", root_path: "/Volumes/Trips", is_public: false,
                                     reader: "server", reader_name: null });
  api.listDevices.mockResolvedValue([STUDIO]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <LibraryReaderSection libraryId="lib_1" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("LibraryReaderSection", () => {
  it("hands a library to a device, with its folder as the device sees it", async () => {
    api.chooseLibraryReader.mockResolvedValue({ library_id: "lib_1", name: "Trips", root_path: "/Users/robert/Trips",
                                                is_public: false, reader: "dev_1", reader_name: "Studio Mac" });
    renderSection();
    expect(await screen.findByText(/This server reads its files\./)).toBeTruthy();
    fireEvent.change(await screen.findByLabelText("Read by"), { target: { value: "dev_1" } });
    expect(screen.getByText("Studio Mac reads it from its next check-in, at that folder; this server stops.")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Its folder as Studio Mac sees it"), { target: { value: "/Users/robert/Trips" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.chooseLibraryReader).toHaveBeenCalledWith("lib_1", "dev_1", "/Users/robert/Trips"));
  });

  it("keeps the folder unless it changed, and says why the server refused", async () => {
    api.getLibrary.mockResolvedValue({ library_id: "lib_1", name: "Trips", root_path: "/Users/robert/Trips",
                                       is_public: false, reader: "dev_1", reader_name: "Studio Mac" });
    api.chooseLibraryReader.mockRejectedValue(
      new ApiError(403, "Choose a library's folder on the computer Lumiverb is installed on.", "folder_elsewhere"));
    renderSection();
    expect(await screen.findByText(/Studio Mac reads its files\./)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Read by"), { target: { value: "server" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.chooseLibraryReader).toHaveBeenCalledWith("lib_1", "server", undefined));
    expect((await screen.findByRole("alert")).textContent).toContain("on the computer Lumiverb is installed on");
  });

  it("points to Settings › Devices when there are none", async () => {
    api.listDevices.mockResolvedValue([]);
    renderSection();
    expect((await screen.findByRole("link", { name: "Settings › Devices" })).getAttribute("href")).toBe("/settings/devices");
  });

  it("is its admin's: one who isn't an account admin keeps this server or the device it has", async () => {
    api.getCurrentUser.mockResolvedValue({ email: "a@b.c", role: "editor", server_admin: false });
    api.getLibrary.mockResolvedValue({ library_id: "lib_1", name: "Trips", root_path: "/Users/robert/Trips",
                                       is_public: false, reader: "dev_1", reader_name: "Studio Mac", role: "admin" });
    renderSection();
    expect(await screen.findByText(/Studio Mac reads its files\./)).toBeTruthy();
    const options = Array.from((screen.getByLabelText("Read by") as HTMLSelectElement).options).map((o) => o.text);
    expect(options).toEqual(["This server", "Studio Mac"]);
    expect(api.listDevices).not.toHaveBeenCalled();
    expect(screen.queryByRole("link", { name: "Settings › Devices" })).toBeNull();
  });

  it("is nobody's but its admin's: an editor of it sees nothing", async () => {
    api.getCurrentUser.mockResolvedValue({ email: "a@b.c", role: "admin", server_admin: true });
    api.getLibrary.mockResolvedValue({ library_id: "lib_1", name: "Trips", root_path: "/t", is_public: false,
                                       reader: "server", reader_name: null, role: "editor" });
    const { container } = renderSection();
    await waitFor(() => expect(api.getLibrary).toHaveBeenCalled());
    await waitFor(() => expect(api.getCurrentUser).toHaveBeenCalled());
    expect(container.textContent).toBe("");
  });

  it("is an admin's", async () => {
    api.getCurrentUser.mockResolvedValue({ email: "a@b.c", role: "editor", server_admin: false });
    const { container } = renderSection();
    await waitFor(() => expect(api.getLibrary).toHaveBeenCalled());
    expect(container.textContent).toBe("");
    expect(api.listDevices).not.toHaveBeenCalled();
  });
});
