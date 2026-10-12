import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import ResetPasswordPage from "./ResetPasswordPage";

const fetchMock = vi.fn();

let posted: { token: string; password: string } | null = null;
let answer: Response;

beforeEach(() => {
  posted = null;
  answer = new Response(JSON.stringify({ api_keys_revoked: 0 }), { status: 200 });
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url === "/v1/auth/reset-password" && init?.method === "POST") {
      posted = JSON.parse(String(init.body));
      return answer;
    }
    return new Response("{}", { status: 404 });
  });
});

afterEach(() => {
  cleanup();
  fetchMock.mockReset();
  vi.unstubAllGlobals();
});

function resetWith(password: string) {
  render(
    <MemoryRouter initialEntries={["/reset-password?token=tok-from-the-link"]}>
      <Routes>
        <Route path="/reset-password" element={<ResetPasswordPage />} />
      </Routes>
    </MemoryRouter>,
  );
  fireEvent.change(screen.getByLabelText("New password"), { target: { value: password } });
  fireEvent.change(screen.getByLabelText("Confirm password"), { target: { value: password } });
  fireEvent.click(screen.getByRole("button", { name: "Set new password" }));
}

describe("ResetPasswordPage", () => {
  it("sets the new password from the link", async () => {
    resetWith("correct horse battery");
    expect(await screen.findByText("Password updated. You can now sign in.")).toBeTruthy();
    expect(posted).toEqual({ token: "tok-from-the-link", password: "correct horse battery" });
    expect(screen.queryByText(/API key/)).toBeNull();
  });

  it("says the API keys they made were revoked", async () => {
    answer = new Response(JSON.stringify({ api_keys_revoked: 2 }), { status: 200 });
    resetWith("correct horse battery");
    expect(
      await screen.findByText("The 2 API keys you made were revoked. Make new ones in Settings › API Keys."),
    ).toBeTruthy();
  });

  it("says so for one key", async () => {
    answer = new Response(JSON.stringify({ api_keys_revoked: 1 }), { status: 200 });
    resetWith("correct horse battery");
    expect(
      await screen.findByText("The API key you made was revoked. Make a new one in Settings › API Keys."),
    ).toBeTruthy();
  });

  it("says a used or expired link won't do", async () => {
    answer = new Response(JSON.stringify({ error: { code: "bad_request", message: "x", details: {} } }), { status: 400 });
    resetWith("correct horse battery");
    expect(await screen.findByText("This reset link is invalid, expired, or already used.")).toBeTruthy();
  });
});
