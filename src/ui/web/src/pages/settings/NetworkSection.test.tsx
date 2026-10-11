import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import NetworkSection from "./NetworkSection";

const fetchMock = vi.fn();
const ADDRESSES = ["http://macstudio.local:6464", "http://192.168.86.55:6464"];
let role = "admin";
let available = true;
let otherDevices = false;
let refuse: { code: string; message: string } | null = null;

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/v1/me")) return new Response(JSON.stringify({ email: "a@b.c", role }), { status: 200 });
    if (url.endsWith("/v1/system/network")) {
      if (init?.method === "PUT") {
        if (refuse) return new Response(JSON.stringify({ error: { ...refuse, details: {} } }), { status: 403 });
        otherDevices = JSON.parse(String(init.body)).other_devices;
      }
      return new Response(
        JSON.stringify({ available, other_devices: available && otherDevices, addresses: available ? ADDRESSES : [] }),
        { status: 200 },
      );
    }
    return new Response("{}", { status: 404 });
  });
});

afterEach(() => {
  cleanup();
  fetchMock.mockReset();
  vi.unstubAllGlobals();
  role = "admin";
  available = true;
  otherDevices = false;
  refuse = null;
});

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <NetworkSection />
    </QueryClientProvider>,
  );
}

function puts() {
  return fetchMock.mock.calls
    .filter(([, init]) => (init as RequestInit | undefined)?.method === "PUT")
    .map(([, init]) => JSON.parse(String((init as RequestInit).body)));
}

describe("NetworkSection", () => {
  it("starts as this Mac only, with no addresses shown", async () => {
    renderSection();
    const box = (await screen.findByLabelText(/Let other devices on my network use Lumiverb/)) as HTMLInputElement;
    expect(box.checked).toBe(false);
    expect(screen.queryByText(ADDRESSES[0])).toBeNull();
  });

  it("lets an admin open it to other devices, then shows where they reach it", async () => {
    renderSection();
    fireEvent.click(await screen.findByLabelText(/Let other devices on my network use Lumiverb/));
    await waitFor(() => expect(puts()).toEqual([{ other_devices: true }]));
    for (const address of ADDRESSES) expect(await screen.findByText(address)).toBeTruthy();
    expect(screen.getByText(/choose Allow/)).toBeTruthy();
    fireEvent.click(screen.getByLabelText(/Let other devices on my network use Lumiverb/));
    await waitFor(() => expect(puts()[1]).toEqual({ other_devices: false }));
    await waitFor(() => expect(screen.queryByText(ADDRESSES[0])).toBeNull());
  });

  it("says why when the server refuses", async () => {
    refuse = { code: "network_elsewhere", message: "Change this on the computer Lumiverb is installed on." };
    renderSection();
    fireEvent.click(await screen.findByLabelText(/Let other devices on my network use Lumiverb/));
    expect(await screen.findByText("Change this on the computer Lumiverb is installed on.")).toBeTruthy();
    expect((screen.getByLabelText(/Let other devices/) as HTMLInputElement).checked).toBe(false);
  });

  it("shows others what it is, without a switch", async () => {
    role = "viewer";
    otherDevices = true;
    renderSection();
    expect(await screen.findByText("Other devices on your network can use Lumiverb.")).toBeTruthy();
    expect(screen.getByText("Only admins can change this.")).toBeTruthy();
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.getByText(ADDRESSES[1])).toBeTruthy();
  });

  it("isn't changed here where Lumiverb doesn't run the web server (a server's install)", async () => {
    available = false;
    renderSection();
    expect(await screen.findByText(/set up where it's installed/)).toBeTruthy();
    expect(screen.queryByRole("checkbox")).toBeNull();
  });
});
