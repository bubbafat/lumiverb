import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

vi.mock("../../api/client", async (importOriginal) => {
  const real = await importOriginal<typeof import("../../api/client")>();
  return { ...real, getCurrentUser: vi.fn(), logout: vi.fn() };
});

import { ApiError, getCurrentUser, logout } from "../../api/client";
import AccountSection, { whyNot } from "./AccountSection";

beforeEach(() => {
  vi.mocked(getCurrentUser).mockResolvedValue(
    { user_id: "u1", email: "owner@example.com", role: "admin" } as Awaited<ReturnType<typeof getCurrentUser>>);
  vi.mocked(logout).mockReset().mockResolvedValue();
});
afterEach(cleanup);

function renderSection() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <AccountSection />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("AccountSection", () => {
  it("says why it can't tell who's signed in, never that it's an API key or a viewer", async () => {
    vi.mocked(getCurrentUser).mockRejectedValue(new ApiError(403, "Not for you", "forbidden"));
    renderSection();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Couldn't tell who's signed in");
    expect(alert.textContent).toContain("HTTP 403 · forbidden · Not for you");
    expect(screen.queryByText(/API key/)).toBeNull();
    expect(screen.queryByText("Viewer")).toBeNull();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeTruthy();
  });

  it("tells a browser's own failure apart from the server's answer", () => {
    expect(whyNot(new TypeError("Load failed"))).toBe("TypeError: Load failed");
    expect(whyNot(new ApiError(502, "Bad Gateway"))).toBe("HTTP 502 · Bad Gateway");
  });

  it("says an API key has no email, when it is one", async () => {
    vi.mocked(getCurrentUser).mockResolvedValue(
      { user_id: null, email: null, role: "editor" } as unknown as Awaited<ReturnType<typeof getCurrentUser>>);
    renderSection();
    expect(await screen.findByText("None: signed in with an API key")).toBeTruthy();
    expect(screen.getByText("Editor")).toBeTruthy();
  });

  it("says who's signed in, and signs out (a phone has no sidebar to do it from)", async () => {
    renderSection();
    expect(await screen.findByText("owner@example.com")).toBeTruthy();
    expect(screen.getByText("Admin")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(logout).toHaveBeenCalled());
  });
});
