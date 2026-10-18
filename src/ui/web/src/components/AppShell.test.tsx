import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

vi.mock("../api/client", async (importOriginal) => {
  const real = await importOriginal<typeof import("../api/client")>();
  return { ...real, getApiKey: () => "token", getCurrentUser: vi.fn() };
});
vi.mock("./Sidebar", () => ({ Sidebar: () => null }));
vi.mock("./CommandPalette", () => ({ CommandPalette: () => null }));
vi.mock("./KeyboardShortcuts", () => ({ KeyboardShortcuts: () => null }));
vi.mock("../lib/useSystemHealth", () => ({
  useSystemHealth: () => ({ data: undefined, isError: false }),
  healthAlert: () => null,
}));

import { getCurrentUser } from "../api/client";
import AppShell from "./AppShell";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("AppShell", () => {
  it("asks who's signed in again when the server didn't answer (a restart), so an admin's controls come back", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(getCurrentUser)
      .mockRejectedValueOnce(new Error("Couldn't reach the server"))
      .mockResolvedValue({ user_id: "u1", email: "owner@example.com", role: "admin" } as Awaited<
        ReturnType<typeof getCurrentUser>>);
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={["/"]}>
          <Routes>
            <Route path="/" element={<AppShell />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(screen.queryByRole("link", { name: "Admin" })).toBeNull(); // not known yet
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(await screen.findByRole("link", { name: "Admin" })).toBeTruthy();
    expect(getCurrentUser).toHaveBeenCalledTimes(2);
  });
});
