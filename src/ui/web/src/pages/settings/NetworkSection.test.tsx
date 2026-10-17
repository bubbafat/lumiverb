import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import NetworkSection from "./NetworkSection";

const fetchMock = vi.fn();
const ADDRESSES = ["https://macstudio.local:6465", "https://192.168.86.55:6465"];
const CA_ADDRESSES = ["http://macstudio.local:6464/lumiverb-ca.crt", "http://192.168.86.55:6464/lumiverb-ca.crt"];
const FINGERPRINT = "AB:CD:EF:01";
const OURS = {
  source: "lumiverb", names: ["macstudio.local", "192.168.86.55"], expires_at: "2027-11-18T05:00:00Z",
  ca_url: "/lumiverb-ca.crt", ca_addresses: CA_ADDRESSES, ca_fingerprint: FINGERPRINT,
};
const YOURS = {
  source: "yours", names: ["photos.example.com"], expires_at: "2027-01-15T00:00:00Z",
  ca_url: null, ca_addresses: [], ca_fingerprint: null,
};
let role = "admin";
let available = true;
let otherDevices = false;
let certificate: typeof OURS | typeof YOURS = OURS;
let refuse: { code: string; message: string } | null = null;
let refuseCertificate: { code: string; message: string } | null = null;

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/v1/me")) return new Response(JSON.stringify({ email: "a@b.c", role }), { status: 200 });
    const answer = () =>
      new Response(
        JSON.stringify({
          available, other_devices: available && otherDevices, addresses: available ? ADDRESSES : [],
          certificate: available ? certificate : null,
        }),
        { status: 200 },
      );
    if (url.endsWith("/v1/system/network/certificate")) {
      if (init?.method === "PUT") {
        if (refuseCertificate) {
          return new Response(JSON.stringify({ error: { ...refuseCertificate, details: {} } }), { status: 422 });
        }
        certificate = YOURS;
      } else if (init?.method === "DELETE") {
        certificate = OURS;
      }
      return answer();
    }
    if (url.endsWith("/v1/system/network")) {
      if (init?.method === "PUT") {
        if (refuse) return new Response(JSON.stringify({ error: { ...refuse, details: {} } }), { status: 403 });
        otherDevices = JSON.parse(String(init.body)).other_devices;
      }
      return answer();
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
  certificate = OURS;
  refuse = null;
  refuseCertificate = null;
});

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <NetworkSection />
    </QueryClientProvider>,
  );
}

function puts(path = "/v1/system/network") {
  return fetchMock.mock.calls
    .filter(([url, init]) => String(url).endsWith(path) && (init as RequestInit | undefined)?.method === "PUT")
    .map(([, init]) => JSON.parse(String((init as RequestInit).body)));
}

function deletes(path: string) {
  return fetchMock.mock.calls.filter(
    ([url, init]) => String(url).endsWith(path) && (init as RequestInit | undefined)?.method === "DELETE",
  ).length;
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

  it("says how each device trusts Lumiverb's own certificate, with its fingerprint to compare", async () => {
    otherDevices = true;
    renderSection();
    expect(await screen.findByText("Each device trusts Lumiverb once")).toBeTruthy();
    for (const address of CA_ADDRESSES) expect(screen.getByText(address)).toBeTruthy();
    expect(screen.getByText(FINGERPRINT)).toBeTruthy();
    expect(screen.getByText(/Certificate Trust Settings/)).toBeTruthy();
    expect(screen.getByText("Download it here").getAttribute("href")).toBe("/lumiverb-ca.crt");
  });

  it("lets an admin give their own certificate, saying why one can't be used", async () => {
    otherDevices = true;
    refuseCertificate = { code: "certificate_not_usable", message: "The key isn't the certificate's: give the key it was made with." };
    renderSection();
    fireEvent.click(await screen.findByText("Use your own certificate"));
    fireEvent.change(screen.getByLabelText("Certificate"), { target: { value: "-----BEGIN CERTIFICATE-----x" } });
    fireEvent.change(screen.getByLabelText("Private key"), { target: { value: "-----BEGIN PRIVATE KEY-----y" } });
    fireEvent.click(screen.getByText("Use it"));
    expect(await screen.findByText(/The key isn't the certificate's/)).toBeTruthy();
    refuseCertificate = null;
    fireEvent.click(screen.getByText("Use it"));
    expect(await screen.findByText(/Your certificate, for photos.example.com/)).toBeTruthy();
    expect(puts("/v1/system/network/certificate")).toEqual([
      { certificate: "-----BEGIN CERTIFICATE-----x", key: "-----BEGIN PRIVATE KEY-----y" },
      { certificate: "-----BEGIN CERTIFICATE-----x", key: "-----BEGIN PRIVATE KEY-----y" },
    ]);
    expect(screen.queryByText("Each device trusts Lumiverb once")).toBeNull();
    fireEvent.click(screen.getByText("Use Lumiverb's own certificate instead"));
    expect(await screen.findByText("Each device trusts Lumiverb once")).toBeTruthy();
    expect(deletes("/v1/system/network/certificate")).toBe(1);
  });

  it("offers no certificate choice to someone who isn't an admin", async () => {
    role = "viewer";
    otherDevices = true;
    renderSection();
    expect(await screen.findByText("Each device trusts Lumiverb once")).toBeTruthy();
    expect(screen.queryByText("Use your own certificate")).toBeNull();
  });

  it("says nothing of certificates while it's this Mac only", async () => {
    renderSection();
    await screen.findByLabelText(/Let other devices on my network use Lumiverb/);
    expect(screen.queryByText("Each device trusts Lumiverb once")).toBeNull();
  });
});
