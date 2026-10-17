import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import NetworkSection from "./NetworkSection";

const fetchMock = vi.fn();
const ADDRESSES = ["http://macstudio.local:6464", "http://192.168.86.55:6464"];
const HTTPS_ADDRESSES = ["https://macstudio.local:6443", "https://192.168.86.55:6443"];
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
const SEEN = { at: "2026-10-17T14:02:00Z", address: "192.168.86.20" };
let role = "admin";
let available = true;
let otherDevices = false;
let https = false;
let plainHttp = true;
let seen: typeof SEEN | null = null;
let certificate: typeof OURS | typeof YOURS = OURS;
let refuse: { code: string; message: string } | null = null;
let refuseCertificate: { code: string; message: string } | null = null;

function error(status: number, refused: { code: string; message: string }) {
  return new Response(JSON.stringify({ error: { ...refused, details: {} } }), { status });
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/v1/me")) return new Response(JSON.stringify({ email: "a@b.c", role }), { status: 200 });
    // The server's rules (routers/system.py): HTTPS off brings plain HTTP back; plain HTTP off only once seen.
    const answer = () =>
      new Response(
        JSON.stringify({
          available, other_devices: available && otherDevices, https, plain_http: plainHttp,
          addresses: available ? ADDRESSES : [], https_addresses: available ? HTTPS_ADDRESSES : [],
          https_seen: https ? seen : null, certificate: available && https ? certificate : null,
        }),
        { status: 200 },
      );
    if (url.endsWith("/v1/system/network/certificate")) {
      if (init?.method === "PUT") {
        if (refuseCertificate) return error(422, refuseCertificate);
        certificate = YOURS;
      } else if (init?.method === "DELETE") {
        certificate = OURS;
      }
      return answer();
    }
    if (url.endsWith("/v1/system/network")) {
      if (init?.method === "PUT") {
        if (refuse) return error(403, refuse);
        const body = JSON.parse(String(init.body));
        if (body.plain_http === false && !seen) {
          return error(409, { code: "https_not_seen", message: "No device has opened Lumiverb over HTTPS yet." });
        }
        otherDevices = body.other_devices ?? otherDevices;
        https = body.https ?? https;
        plainHttp = https ? body.plain_http ?? plainHttp : true;
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
  https = false;
  plainHttp = true;
  seen = null;
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

const OTHERS = /Let other devices on my network use Lumiverb/;
const HTTPS = /Also offer an encrypted connection/;
const PLAIN = /Keep plain HTTP for other devices too/;

describe("NetworkSection", () => {
  it("starts as this Mac only, with no addresses shown", async () => {
    renderSection();
    const box = (await screen.findByLabelText(OTHERS)) as HTMLInputElement;
    expect(box.checked).toBe(false);
    expect(screen.queryByText(ADDRESSES[0])).toBeNull();
    expect(screen.queryByLabelText(HTTPS)).toBeNull();
  });

  it("lets an admin open it to other devices over plain HTTP, with nothing to set up on them", async () => {
    renderSection();
    fireEvent.click(await screen.findByLabelText(OTHERS));
    await waitFor(() => expect(puts()).toEqual([{ other_devices: true }]));
    for (const address of ADDRESSES) expect(await screen.findByText(address)).toBeTruthy();
    expect(screen.getByText(/choose Allow/)).toBeTruthy();
    expect((screen.getByLabelText(HTTPS) as HTMLInputElement).checked).toBe(false);
    expect(screen.queryByText("Each device trusts Lumiverb once")).toBeNull();
    fireEvent.click(screen.getByLabelText(OTHERS));
    await waitFor(() => expect(puts()[1]).toEqual({ other_devices: false }));
    await waitFor(() => expect(screen.queryByText(ADDRESSES[0])).toBeNull());
  });

  it("adds HTTPS beside plain HTTP, saying how each device trusts it, with its fingerprint to compare", async () => {
    otherDevices = true;
    renderSection();
    fireEvent.click(await screen.findByLabelText(HTTPS));
    await waitFor(() => expect(puts()).toEqual([{ https: true }]));
    expect(await screen.findByText("Each device trusts Lumiverb once")).toBeTruthy();
    for (const address of [...ADDRESSES, ...HTTPS_ADDRESSES, ...CA_ADDRESSES]) expect(screen.getByText(address)).toBeTruthy();
    expect(screen.getByText(FINGERPRINT)).toBeTruthy();
    expect(screen.getByText(/Certificate Trust Settings/)).toBeTruthy();
    expect(screen.getByText("Download it here").getAttribute("href")).toBe("/lumiverb-ca.crt");
    expect(screen.getByText("No device has opened Lumiverb over HTTPS yet.")).toBeTruthy();
  });

  it("keeps plain HTTP on until a device has used HTTPS", async () => {
    otherDevices = true;
    https = true;
    renderSection();
    const plain = (await screen.findByLabelText(PLAIN)) as HTMLInputElement;
    expect(plain.checked).toBe(true);
    expect(plain.disabled).toBe(true);
    expect(screen.getByText("You can turn it off once a device has opened Lumiverb over HTTPS.")).toBeTruthy();
  });

  it("turns plain HTTP off once a device has used HTTPS, and back on", async () => {
    otherDevices = true;
    https = true;
    seen = SEEN;
    renderSection();
    expect(await screen.findByText(/A device \(192\.168\.86\.20\) opened Lumiverb over HTTPS/)).toBeTruthy();
    fireEvent.click(screen.getByLabelText(PLAIN));
    await waitFor(() => expect(puts()).toEqual([{ plain_http: false }]));
    await waitFor(() => expect(screen.queryByText(ADDRESSES[0])).toBeNull());
    expect(screen.getByText(HTTPS_ADDRESSES[0])).toBeTruthy();
    fireEvent.click(screen.getByLabelText(PLAIN));
    await waitFor(() => expect(puts()[1]).toEqual({ plain_http: true }));
    expect(await screen.findByText(ADDRESSES[0])).toBeTruthy();
  });

  it("brings plain HTTP back when HTTPS is turned off", async () => {
    otherDevices = true;
    https = true;
    plainHttp = false;
    seen = SEEN;
    renderSection();
    await screen.findByText(HTTPS_ADDRESSES[0]);
    expect(screen.queryByText(ADDRESSES[0])).toBeNull();
    fireEvent.click(screen.getByLabelText(HTTPS));
    await waitFor(() => expect(puts()).toEqual([{ https: false }]));
    expect(await screen.findByText(ADDRESSES[0])).toBeTruthy();
    expect(screen.queryByText(HTTPS_ADDRESSES[0])).toBeNull();
  });

  it("says why when the server refuses", async () => {
    refuse = { code: "network_elsewhere", message: "Change this on the computer Lumiverb is installed on." };
    renderSection();
    fireEvent.click(await screen.findByLabelText(OTHERS));
    expect(await screen.findByText("Change this on the computer Lumiverb is installed on.")).toBeTruthy();
    expect((screen.getByLabelText(OTHERS) as HTMLInputElement).checked).toBe(false);
  });

  it("shows others what it is, without a switch", async () => {
    role = "viewer";
    otherDevices = true;
    renderSection();
    expect(await screen.findByText("Other devices on your network can use Lumiverb.")).toBeTruthy();
    expect(screen.getByText("Only admins can change this.")).toBeTruthy();
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.getByText(ADDRESSES[1])).toBeTruthy();
    expect(screen.queryByText(/encrypted connection/)).toBeNull(); // HTTPS is off: nothing said of it
  });

  it("isn't changed here where Lumiverb doesn't run the web server (a server's install)", async () => {
    available = false;
    renderSection();
    expect(await screen.findByText(/set up where it's installed/)).toBeTruthy();
    expect(screen.queryByRole("checkbox")).toBeNull();
  });

  it("lets an admin give their own certificate, saying why one can't be used", async () => {
    otherDevices = true;
    https = true;
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
    https = true;
    renderSection();
    expect(await screen.findByText("Each device trusts Lumiverb once")).toBeTruthy();
    expect(screen.getByText("Other devices can use an encrypted connection (HTTPS) too.")).toBeTruthy();
    expect(screen.queryByText("Use your own certificate")).toBeNull();
    expect(screen.queryByLabelText(PLAIN)).toBeNull();
  });

  it("says nothing of HTTPS while it's this Mac only", async () => {
    https = true;
    renderSection();
    await screen.findByLabelText(OTHERS);
    expect(screen.queryByText("Each device trusts Lumiverb once")).toBeNull();
    expect(screen.queryByLabelText(HTTPS)).toBeNull();
  });
});
