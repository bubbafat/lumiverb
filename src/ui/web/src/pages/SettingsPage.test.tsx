import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { API_KEY_STORAGE_KEY } from "../api/client";
import SettingsPage from "./SettingsPage";

const fetchMock = vi.fn();
let me: Record<string, unknown> = {};
const asked: string[] = [];

beforeEach(() => {
  localStorage.setItem(API_KEY_STORAGE_KEY, "token");
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string) => {
    const path = new URL(url, "http://x").pathname.replace(/^\/v1/, "");
    asked.push(path);
    if (path === "/me") return new Response(JSON.stringify(me), { status: 200 });
    if (path === "/ai") return new Response(JSON.stringify({ machines: [], jobs: [] }), { status: 200 });
    return new Response("{}", { status: 404 });
  });
});

afterEach(() => {
  cleanup();
  fetchMock.mockReset();
  vi.unstubAllGlobals();
  localStorage.clear();
  asked.length = 0;
});

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SettingsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Settings' sections", () => {
  it("are a viewer's own, AI, Processing and Network among them", async () => {
    me = { user_id: "u1", email: "ana@example.com", role: "viewer", server_admin: false, demo: false };
    renderPage();
    expect(await screen.findByRole("link", { name: "AI" })).toBeTruthy();
    for (const name of ["Processing", "Network", "Account", "Playback"]) {
      expect(screen.getByRole("link", { name })).toBeTruthy();
    }
    await waitFor(() => expect(asked).toContain("/ai"));
  });

  it("leave out what a demo server doesn't answer when someone is trying it as a viewer", async () => {
    me = { user_id: "u2", email: "demo@example.com", role: "viewer", server_admin: false, demo: true };
    renderPage();
    expect(await screen.findByRole("link", { name: "Account" })).toBeTruthy();
    await waitFor(() => expect(asked).toContain("/me"));
    for (const name of ["AI", "Processing", "Network"]) {
      expect(screen.queryByRole("link", { name })).toBeNull();
    }
    for (const name of ["Preferences", "Playback", "Files", "Security"]) {
      expect(screen.getByRole("link", { name })).toBeTruthy();
    }
    expect(asked).not.toContain("/ai");
  });
});
