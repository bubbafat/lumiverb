import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import ForgotPasswordPage from "./ForgotPasswordPage";

const fetchMock = vi.fn();
let answer: () => Response;

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async () => answer());
});

afterEach(() => {
  cleanup();
  fetchMock.mockReset();
  vi.unstubAllGlobals();
});

function Where() {
  const location = useLocation();
  return <p>at {location.pathname}{location.search}</p>;
}

function ask(email: string) {
  render(
    <MemoryRouter initialEntries={["/forgot-password"]}>
      <Routes>
        <Route path="/forgot-password" element={<ForgotPasswordPage />} />
        <Route path="/reset-password" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  );
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: email } });
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
}

describe("ForgotPasswordPage", () => {
  it("on the Mac goes straight to setting a new password", async () => {
    answer = () => new Response(JSON.stringify({ reset_url: "/reset-password?token=t0k" }), { status: 200 });
    ask("robert@example.com");
    expect(await screen.findByText("at /reset-password?token=t0k")).toBeTruthy();
  });

  it("elsewhere, with email set up, says a link is on its way", async () => {
    answer = () => new Response(null, { status: 204 });
    ask("robert@example.com");
    expect(await screen.findByText(/you'll receive a reset link shortly/)).toBeTruthy();
  });

  it("says what the server says when it can't", async () => {
    const message = "Password reset isn't set up here. On the computer Lumiverb is installed on, use Forgot password there, or ask whoever looks after it.";
    answer = () => new Response(JSON.stringify({ error: { code: "reset_elsewhere", message, details: {} } }), { status: 501 });
    ask("robert@example.com");
    expect(await screen.findByText(message)).toBeTruthy();
  });
});
