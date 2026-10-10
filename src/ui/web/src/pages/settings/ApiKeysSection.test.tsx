import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ApiKeysSection from "./ApiKeysSection";

const fetchMock = vi.fn();
const minutesAgo = (m: number) => new Date(Date.now() - m * 60_000).toISOString();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string) => {
    if (url.endsWith("/v1/me")) return new Response(JSON.stringify({ email: "a@b.c", role: "admin" }), { status: 200 });
    if (url.endsWith("/v1/keys")) {
      return new Response(
        JSON.stringify({
          keys: [
            { key_id: "k1", label: "laptop", role: "editor", created_at: minutesAgo(3 * 24 * 60), last_used_at: minutesAgo(5) },
          ],
        }),
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
});

describe("ApiKeysSection", () => {
  it("spells out when a key was made and last used", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ApiKeysSection />
      </QueryClientProvider>,
    );
    expect(await screen.findByText("3 days ago")).toBeTruthy();
    expect(screen.getByText("5 minutes ago")).toBeTruthy();
  });
});
