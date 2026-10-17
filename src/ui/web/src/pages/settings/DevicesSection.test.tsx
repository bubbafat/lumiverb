import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError } from "../../api/client";
import DevicesSection from "./DevicesSection";

const api = vi.hoisted(() => ({
  getCurrentUser: vi.fn(),
  listDevices: vi.fn(),
  listLibraries: vi.fn(),
  registerDevice: vi.fn(),
  renameDevice: vi.fn(),
  newDeviceKey: vi.fn(),
  endDeviceKey: vi.fn(),
  removeDevice: vi.fn(),
  getNetworkAccess: vi.fn(),
}));
vi.mock("../../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/client")>()),
  ...api,
}));

const STUDIO = { device_id: "dev_1", name: "Studio Mac", registered_at: "2026-10-17T12:00:00Z", has_key: true,
                 key_last_used_at: null, libraries: ["lib_1"] };

beforeEach(() => {
  api.getCurrentUser.mockResolvedValue({ email: "a@b.c", role: "admin", server_admin: true });
  api.listDevices.mockResolvedValue([STUDIO]);
  api.listLibraries.mockResolvedValue([{ library_id: "lib_1", name: "Footage", root_path: "/f", status: "active",
                                         is_public: false, last_scan_at: null }]);
  api.getNetworkAccess.mockResolvedValue({ available: true, other_devices: true, addresses: ["http://studio.local:6464"] });
  Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <DevicesSection />
    </QueryClientProvider>,
  );
}

describe("DevicesSection", () => {
  it("lists each device with what it reads and its key", async () => {
    renderSection();
    const row = await screen.findByRole("listitem", { name: "Studio Mac" });
    expect(within(row).getByText(/Reads Footage\./)).toBeTruthy();
    expect(within(row).getByText(/Its key hasn't been used yet\./)).toBeTruthy();
  });

  it("registers one and shows its key once, with what to run on it at this server's address", async () => {
    api.registerDevice.mockResolvedValue({ ...STUDIO, device_id: "dev_2", name: "Laptop", libraries: [], key: "lv_secret" });
    renderSection();
    fireEvent.change(await screen.findByPlaceholderText("e.g. Studio Mac"), { target: { value: " Laptop " } });
    fireEvent.click(screen.getByRole("button", { name: "Register" }));
    const panel = await screen.findByRole("region", { name: "Its key" });
    expect(api.registerDevice).toHaveBeenCalledWith("Laptop");
    expect(within(panel).getByText("lv_secret")).toBeTruthy();
    expect(within(panel).getByText("lumiverb config set --api-url http://studio.local:6464 --api-key -")).toBeTruthy();
    expect(within(panel).getByText("lumiverb device run")).toBeTruthy();
    fireEvent.click(within(panel).getByRole("button", { name: "Done" }));
    expect(screen.queryByText("lv_secret")).toBeNull();
  });

  it("asks before a new key, which shows once and stops the old one", async () => {
    api.newDeviceKey.mockResolvedValue({ ...STUDIO, key: "lv_new" });
    renderSection();
    const row = await screen.findByRole("listitem", { name: "Studio Mac" });
    fireEvent.click(within(row).getByRole("button", { name: "New key" }));
    expect(within(row).getByText(/its old one stops working at once/)).toBeTruthy();
    expect(api.newDeviceKey).not.toHaveBeenCalled();
    fireEvent.click(within(within(row).getByRole("alertdialog")).getByRole("button", { name: "New key" }));
    expect(await screen.findByText("lv_new")).toBeTruthy();
    expect(api.newDeviceKey).toHaveBeenCalledWith("dev_1");
  });

  it("says which libraries to hand to another reader before it can be removed", async () => {
    api.removeDevice.mockRejectedValue(new ApiError(409, "The device reads 1 library", "device_reads_libraries"));
    renderSection();
    const row = await screen.findByRole("listitem", { name: "Studio Mac" });
    fireEvent.click(within(row).getByRole("button", { name: "Remove" }));
    fireEvent.click(within(within(row).getByRole("alertdialog")).getByRole("button", { name: "Remove" }));
    expect((await within(row).findByRole("alert")).textContent).toBe(
      "It reads Footage: choose another reader for it first.");
  });

  it("renames one", async () => {
    api.renameDevice.mockResolvedValue({ ...STUDIO, name: "Studio" });
    renderSection();
    const row = await screen.findByRole("listitem", { name: "Studio Mac" });
    fireEvent.click(within(row).getByRole("button", { name: "Rename" }));
    fireEvent.change(within(row).getByLabelText("New name"), { target: { value: "Studio" } });
    fireEvent.click(within(row).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.renameDevice).toHaveBeenCalledWith("dev_1", "Studio"));
  });

  it("is an admin's", async () => {
    api.getCurrentUser.mockResolvedValue({ email: "a@b.c", role: "editor", server_admin: false });
    renderSection();
    expect(await screen.findByText("Only admins manage devices.")).toBeTruthy();
    expect(api.listDevices).not.toHaveBeenCalled();
  });
});
