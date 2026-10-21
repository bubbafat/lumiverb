import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

vi.mock("../../api/client", async (importOriginal) => {
  const real = await importOriginal<typeof import("../../api/client")>();
  return { ...real, getCurrentUser: vi.fn(), logout: vi.fn(), changePassword: vi.fn() };
});

import { ApiError, changePassword, getCurrentUser, logout } from "../../api/client";
import AccountSection, { whyNot } from "./AccountSection";

beforeEach(() => {
  vi.mocked(getCurrentUser).mockResolvedValue(
    { user_id: "u1", email: "owner@example.com", role: "admin" } as Awaited<ReturnType<typeof getCurrentUser>>);
  vi.mocked(logout).mockReset().mockResolvedValue();
  vi.mocked(changePassword).mockReset();
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

  it("offers a password's change to someone signed in as themselves, never to someone trying it as a viewer", async () => {
    renderSection();
    expect(await screen.findByRole("button", { name: "Change password" })).toBeTruthy();
    cleanup();
    vi.mocked(getCurrentUser).mockResolvedValue(
      { user_id: "u2", email: "demo@example.com", role: "viewer", server_admin: false, demo: true });
    renderSection();
    expect(await screen.findByText("demo@example.com")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Change password" })).toBeNull();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeTruthy();
  });

  async function fillIn(current: string, next: string, again = next) {
    fireEvent.click(await screen.findByRole("button", { name: "Change password" }));
    fireEvent.change(screen.getByLabelText("Current password"), { target: { value: current } });
    fireEvent.change(screen.getByLabelText("New password"), { target: { value: next } });
    fireEvent.change(screen.getByLabelText("New password again"), { target: { value: again } });
  }

  it("changes it with the one they have, and says the rest sign in again", async () => {
    vi.mocked(changePassword).mockResolvedValue({ api_keys_revoked: 0 });
    renderSection();
    await fillIn("the old password!", "a brand new password");
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    expect((await screen.findByRole("status")).textContent).toContain("Other browsers and devices sign in again");
    expect(changePassword).toHaveBeenCalledWith("the old password!", "a brand new password", false);
    expect(screen.queryByLabelText("Current password")).toBeNull();
  });

  it("names the API keys a new password ends, and ends them only once asked to", async () => {
    vi.mocked(changePassword)
      .mockRejectedValueOnce(new ApiError(409, "A new password ends the 2 API keys you made", "keys_end_with_password", {
        keys: [
          { key_id: "k1", label: "laptop cli", role: "editor", device: false, last_used_at: null },
          { key_id: "k2", label: "Studio Mac", role: "device", device: true, last_used_at: null },
        ],
      }))
      .mockResolvedValueOnce({ api_keys_revoked: 2 });
    renderSection();
    await fillIn("the old password!", "a brand new password");
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("laptop cli");
    expect(alert.textContent).toContain("Studio Mac (a device: it stops reading its libraries)");
    fireEvent.click(screen.getByRole("button", { name: "Change it and end them" }));
    expect((await screen.findByRole("status")).textContent).toContain("The 2 API keys you made ended");
    expect(vi.mocked(changePassword).mock.calls.map((c) => c[2])).toEqual([false, true]);
  });

  it("says when the current password is wrong, and keeps what was typed", async () => {
    vi.mocked(changePassword).mockRejectedValue(
      new ApiError(400, "That isn't your current password.", "wrong_password"));
    renderSection();
    await fillIn("not it", "a brand new password");
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    expect((await screen.findByRole("alert")).textContent).toBe("That isn't your current password.");
    expect((screen.getByLabelText("New password again") as HTMLInputElement).value).toBe("a brand new password");
  });

  it("won't send a new password that's too short or typed two ways", async () => {
    renderSection();
    await fillIn("the old password!", "a brand new password", "a brand new passwork");
    expect(screen.getByText("The new passwords don't match.")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Change password" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("New password"), { target: { value: "too short" } });
    fireEvent.change(screen.getByLabelText("New password again"), { target: { value: "too short" } });
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Use at least 12 characters.");
    expect(changePassword).not.toHaveBeenCalled();
  });
});
