import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import LoginPage from "./LoginPage";

const fetchMock = vi.fn();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string) => {
    if (url === "/v1/setup") return new Response(JSON.stringify({ needed: false }), { status: 200 });
    if (url === "/v1/auth/login") return new Response(JSON.stringify({ access_token: "t" }), { status: 200 });
    return new Response("{}", { status: 404 });
  });
});

afterEach(() => {
  cleanup();
  fetchMock.mockReset();
  vi.unstubAllGlobals();
  localStorage.clear();
});

function Where() {
  const location = useLocation();
  return <p data-testid="where">{location.pathname + location.search}</p>;
}

async function signInFrom(entry: string): Promise<string> {
  render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  );
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: "a@b.c" } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  return (await screen.findByTestId("where")).textContent ?? "";
}

describe("LoginPage", () => {
  it("goes back to the page that sent the visitor to sign in", async () => {
    expect(await signInFrom(`/login?next=${encodeURIComponent("/browse?f=library:lib_1")}`))
      .toBe("/browse?f=library:lib_1");
  });

  it("goes to the path as the browser parses it, when next is this site's full address", async () => {
    expect(await signInFrom(`/login?next=${encodeURIComponent(`${window.location.origin}/people/p_1?x=1`)}`))
      .toBe("/people/p_1?x=1");
  });

  it("goes home when next is another site", async () => {
    expect(await signInFrom(`/login?next=${encodeURIComponent("https://example.com/x")}`)).toBe("/");
  });
});
