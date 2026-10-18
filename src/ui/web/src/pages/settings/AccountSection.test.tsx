import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

vi.mock("../../api/client", async (importOriginal) => {
  const real = await importOriginal<typeof import("../../api/client")>();
  return { ...real, getCurrentUser: vi.fn(), logout: vi.fn() };
});

import { getCurrentUser, logout } from "../../api/client";
import AccountSection from "./AccountSection";

beforeEach(() => {
  vi.mocked(getCurrentUser).mockResolvedValue(
    { user_id: "u1", email: "owner@example.com", role: "admin" } as Awaited<ReturnType<typeof getCurrentUser>>);
  vi.mocked(logout).mockReset().mockResolvedValue();
});
afterEach(cleanup);

function renderSection() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <AccountSection />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("AccountSection", () => {
  it("says who's signed in, and signs out (a phone has no sidebar to do it from)", async () => {
    renderSection();
    expect(await screen.findByText("owner@example.com")).toBeTruthy();
    expect(screen.getByText("Admin")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(logout).toHaveBeenCalled());
  });
});
