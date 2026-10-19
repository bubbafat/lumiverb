import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import PluginsSection from "./PluginsSection";

const fetchMock = vi.fn();
let role = "admin";
let state: "on" | "off" | "waiting" = "off";
let version = "1";
let accepted: { version: string; by: string | null; at: string } | null = null;
const sent: { method: string; path: string; body: unknown }[] = [];

const ACK = () => ({
  version,
  title: "GeoNames: Creative Commons Attribution 4.0",
  text: "Place names come from GeoNames.\n\nThe data is provided as is.",
  links: [{ label: "The license (CC BY 4.0)", url: "https://creativecommons.org/licenses/by/4.0/" }],
});

function plugin() {
  return { plugin_id: "geonames", title: "Place names (GeoNames)", description: "Find a place by name.",
           state, acknowledgment: ACK(), acknowledged: state === "off" ? null : accepted };
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    const path = new URL(url, "http://x").pathname.replace(/^\/v1/, "");
    sent.push({ method, path, body });
    if (path === "/me" || path === "/auth/me" || path.endsWith("/me")) {
      return json({ user_id: "u1", email: "owner@example.com", role, server_admin: false });
    }
    if (path === "/plugins") return json({ plugins: [plugin()] });
    if (path === "/plugins/geonames" && method === "PUT") {
      const { enabled, acknowledged } = body as { enabled: boolean; acknowledged?: string };
      if (!enabled) {
        state = "off";
        return json(plugin());
      }
      if (state === "on") return json(plugin()); // on already: nothing asked
      if (acknowledged !== version) {
        return json({ error: { code: "acknowledgment_required", message: "Read it first.",
                               details: { plugin_id: "geonames", ...ACK() } } }, 409);
      }
      state = "on";
      accepted = { version, by: "owner@example.com", at: "2026-10-18T12:00:00Z" };
      return json(plugin());
    }
    return json({}, 404);
  });
});

afterEach(() => {
  cleanup();
  fetchMock.mockReset();
  vi.unstubAllGlobals();
  role = "admin";
  state = "off";
  version = "1";
  accepted = null;
  sent.length = 0;
});

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <PluginsSection />
    </QueryClientProvider>,
  );
}

const puts = () => sent.filter((s) => s.method === "PUT").map((s) => s.body);
const toggle = () => screen.findByRole("checkbox", { name: "Place names (GeoNames): on" });

describe("PluginsSection", () => {
  it("shows the license in full on turning it on, and OK accepts that version", async () => {
    renderSection();
    fireEvent.click(await toggle());
    const dialog = await screen.findByRole("dialog", { name: "GeoNames: Creative Commons Attribution 4.0" });
    expect(within(dialog).getByText("Place names come from GeoNames.")).toBeTruthy();
    expect(within(dialog).getByText("The data is provided as is.")).toBeTruthy();
    const link = within(dialog).getByRole("link", { name: "The license (CC BY 4.0)" }) as HTMLAnchorElement;
    expect(link.href).toBe("https://creativecommons.org/licenses/by/4.0/");
    expect(((await toggle()) as HTMLInputElement).checked).toBe(true); // on while it's read
    expect(puts()).toEqual([]); // nothing sent until it's accepted

    fireEvent.click(within(dialog).getByRole("button", { name: "OK" }));
    await waitFor(() => expect(puts()).toEqual([{ enabled: true, acknowledged: "1" }]));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(await screen.findByText(/accepted .* by owner@example.com/)).toBeTruthy();
    expect(((await toggle()) as HTMLInputElement).checked).toBe(true);
  });

  it("Cancel turns the switch back off and sends nothing", async () => {
    renderSection();
    fireEvent.click(await toggle());
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(((await toggle()) as HTMLInputElement).checked).toBe(false);
    expect(puts()).toEqual([]);
  });

  it("shows the newer text when it changed since the page read it", async () => {
    renderSection();
    fireEvent.click(await toggle());
    version = "2"; // the server's text is newer now
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "OK" }));
    await waitFor(() => expect(puts()).toEqual([{ enabled: true, acknowledged: "1" }]));
    expect(screen.getByRole("dialog")).toBeTruthy(); // asked again, with version 2
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "OK" }));
    await waitFor(() => expect(puts()).toEqual([{ enabled: true, acknowledged: "1" },
                                                { enabled: true, acknowledged: "2" }]));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("turns it off without asking", async () => {
    state = "on";
    accepted = { version: "1", by: "owner@example.com", at: "2026-10-18T12:00:00Z" };
    renderSection();
    fireEvent.click(await toggle());
    await waitFor(() => expect(puts()).toEqual([{ enabled: false }]));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("says why it's off when its text changed since it was accepted", async () => {
    state = "waiting";
    accepted = { version: "0", by: "owner@example.com", at: "2026-10-01T12:00:00Z" };
    renderSection();
    expect(await screen.findByText(/changed since it was accepted/)).toBeTruthy();
    expect(((await toggle()) as HTMLInputElement).checked).toBe(false);
    fireEvent.click(await toggle());
    expect(await screen.findByRole("dialog")).toBeTruthy();
  });

  it("shows everyone else which are on, with nothing to change", async () => {
    role = "editor";
    state = "on";
    accepted = { version: "1", by: null, at: "2026-10-18T12:00:00Z" };
    renderSection();
    expect(await screen.findByText("Place names (GeoNames)")).toBeTruthy();
    await waitFor(() => expect(screen.getByText(/An admin turns them on/)).toBeTruthy());
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.getByText("On")).toBeTruthy();
  });
});
