import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { API_KEY_STORAGE_KEY } from "../api/client";
import LoginPage from "./LoginPage";

const fetchMock = vi.fn();
// A demo server's: anyone may try it as a viewer, and typed sign-in isn't there from elsewhere.
let demo = false;

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url === "/v1/setup") return new Response(JSON.stringify({ needed: false }), { status: 200 });
    if (url === "/v1/auth/login" && !demo) return new Response(JSON.stringify({ access_token: "t" }), { status: 200 });
    if (url === "/v1/auth/demo" && demo) {
      return new Response(JSON.stringify(init?.method === "POST" ? { access_token: "demo" } : { on: true }), { status: 200 });
    }
    return new Response("{}", { status: 404 });
  });
});

afterEach(() => {
  cleanup();
  fetchMock.mockReset();
  vi.unstubAllGlobals();
  localStorage.clear();
  demo = false;
});

function Where() {
  const location = useLocation();
  return <p data-testid="where">{location.pathname + location.search}</p>;
}

function renderAt(entry: string) {
  render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  );
}

async function signInFrom(entry: string): Promise<string> {
  renderAt(entry);
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

  it("offers no demo where there's none", async () => {
    renderAt("/login");
    expect(await screen.findByRole("link", { name: "Forgot password?" })).toBeTruthy();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/v1/auth/demo"));
    expect(screen.queryByRole("button", { name: "Try it as a viewer" })).toBeNull();
  });
});

describe("LoginPage on a demo server", () => {
  it("signs a visitor in as a viewer, with no password, and goes where they were headed", async () => {
    demo = true;
    renderAt(`/login?next=${encodeURIComponent("/people")}`);
    fireEvent.click(await screen.findByRole("button", { name: "Try it as a viewer" }));
    expect((await screen.findByTestId("where")).textContent).toBe("/people");
    expect(localStorage.getItem(API_KEY_STORAGE_KEY)).toBe("demo");
    expect(fetchMock).toHaveBeenCalledWith("/v1/auth/demo", { method: "POST" });
    expect(screen.queryByRole("link", { name: "Forgot password?" })).toBeNull();
  });

  it("says a password's sign-in isn't open here", async () => {
    demo = true;
    renderAt("/login");
    await screen.findByRole("button", { name: "Try it as a viewer" });
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "a@b.c" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Signing in with a password isn't open here.")).toBeTruthy();
  });
});
