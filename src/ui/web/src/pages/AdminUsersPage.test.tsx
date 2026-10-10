import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import AdminUsersPage from "./AdminUsersPage";

const fetchMock = vi.fn();
const hoursAgo = (h: number) => new Date(Date.now() - h * 3600_000).toISOString();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string) => {
    if (url.endsWith("/v1/users")) {
      return new Response(
        JSON.stringify([
          { user_id: "u1", email: "a@b.c", role: "admin", created_at: hoursAgo(100), last_login_at: hoursAgo(2) },
          { user_id: "u2", email: "d@e.f", role: "viewer", created_at: hoursAgo(100), last_login_at: null },
        ]),
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

describe("AdminUsersPage", () => {
  it("spells out the last sign-in time", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <AdminUsersPage />
      </QueryClientProvider>,
    );
    expect(await screen.findByText("2 hours ago")).toBeTruthy();
    expect(screen.getByText("never")).toBeTruthy();
  });
});
