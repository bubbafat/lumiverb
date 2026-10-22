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
  getTenantSettings: vi.fn(),
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
  api.getTenantSettings.mockResolvedValue({ video_preview_max_seconds: null, public_video_preview_max_seconds: 10,
                                           folders: null });
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

  it("says the account's folders under a folder on this server, as a server admin gave them", async () => {
    api.getTenantSettings.mockResolvedValue({ video_preview_max_seconds: null, public_video_preview_max_seconds: 10,
                                             folders: ["/Volumes/Ana", "/srv/ana"] });
    api.chooseLibraryReader.mockRejectedValue(new ApiError(403, "This account's folders are /Volumes/Ana, /srv/ana: "
      + "choose one inside them, or ask a server admin.", "outside_account_folders"));
    renderSection();
    expect(await screen.findByText("/Volumes/Ana, /srv/ana")).toBeTruthy();
    expect(screen.getByText(/This account's folders:/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Its folder on this server"), { target: { value: "/Volumes/Bob" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "This account's folders are /Volumes/Ana, /srv/ana: choose one inside them, or ask a server admin.");
    fireEvent.change(screen.getByLabelText("Read by"), { target: { value: "dev_1" } });
    expect(screen.queryByText(/This account's folders:/)).toBeNull();
  });

  it("says nothing of folders while the account may choose any the server reads", async () => {
    renderSection();
    expect(await screen.findByLabelText("Its folder on this server")).toBeTruthy();
    await waitFor(() => expect(api.getTenantSettings).toHaveBeenCalled());
    expect(screen.queryByText(/This account/)).toBeNull();
  });

  it("points to Settings › Devices when there are none", async () => {
    api.listDevices.mockResolvedValue([]);
    renderSection();
    expect((await screen.findByRole("link", { name: "Settings › Devices" })).getAttribute("href")).toBe("/settings/devices");
  });

  it("is an account admin's: the library's own admin sees who reads it and its folder, and who chooses them", async () => {
    api.getCurrentUser.mockResolvedValue({ email: "a@b.c", role: "editor", server_admin: false });
    api.getLibrary.mockResolvedValue({ library_id: "lib_1", name: "Trips", root_path: "/Users/robert/Trips",
                                       is_public: false, reader: "dev_1", reader_name: "Studio Mac", role: "admin" });
    renderSection();
    const section = await screen.findByLabelText("Who reads it");
    expect(await screen.findByText(/Studio Mac reads its files\./)).toBeTruthy();
    expect(section.textContent).toContain("Its folder as Studio Mac sees it: /Users/robert/Trips");
    expect(screen.getByText("An account admin chooses its folder and who reads it.")).toBeTruthy();
    expect(screen.queryByLabelText("Read by")).toBeNull();
    expect(section.querySelector("input, select, button")).toBeNull();
    expect(api.listDevices).not.toHaveBeenCalled();
    expect(api.chooseLibraryReader).not.toHaveBeenCalled();
  });

  it("says this server's folder as it is to the library's own admin", async () => {
    api.getCurrentUser.mockResolvedValue({ email: "a@b.c", role: "viewer", server_admin: false });
    api.getLibrary.mockResolvedValue({ library_id: "lib_1", name: "Trips", root_path: "/Volumes/Trips", is_public: false,
                                       reader: "server", reader_name: null, role: "admin" });
    renderSection();
    const section = await screen.findByLabelText("Who reads it");
    expect(section.textContent).toContain("This server reads its files.");
    expect(section.textContent).toContain("Its folder on this server: /Volumes/Trips");
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("gives an account admin the choice", async () => {
    // An account admin is every library's admin; a server from before roles per library says no role.
    api.getLibrary.mockResolvedValue({ library_id: "lib_1", name: "Trips", root_path: "/Volumes/Trips", is_public: false,
                                       reader: "server", reader_name: null });
    renderSection();
    expect(await screen.findByLabelText("Read by")).toBeTruthy();
    expect(screen.getByLabelText("Its folder on this server")).toBeTruthy();
    expect(screen.queryByText("An account admin chooses its folder and who reads it.")).toBeNull();
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
