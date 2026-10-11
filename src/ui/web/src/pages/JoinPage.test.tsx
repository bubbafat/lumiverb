import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import JoinPage from "./JoinPage";
import { getApiKey } from "../api/client";

const fetchMock = vi.fn();

let posted: { token: string; password: string } | null = null;
let answer: Response;

beforeEach(() => {
  posted = null;
  answer = new Response(JSON.stringify({ access_token: "tok-new-person" }), { status: 200 });
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url === "/v1/auth/join" && init?.method === "POST") {
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
  localStorage.clear();
});

function at(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/join" element={<JoinPage />} />
        <Route path="/" element={<p>Libraries</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

function fill(password: string, again: string) {
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: password } });
  fireEvent.change(screen.getByLabelText("Password again"), { target: { value: again } });
  fireEvent.click(screen.getByRole("button", { name: "Join" }));
}

describe("JoinPage", () => {
  it("sets their password from the link and signs them in", async () => {
    at("/join?token=tok-from-the-link");
    fill("correct horse battery", "correct horse battery");
    expect(await screen.findByText("Libraries")).toBeTruthy();
    expect(posted).toEqual({ token: "tok-from-the-link", password: "correct horse battery" });
    expect(getApiKey()).toBe("tok-new-person");
  });

  it("checks the password before sending it", async () => {
    at("/join?token=t");
    fill("short", "short");
    expect(await screen.findByText("Use at least 12 characters for your password.")).toBeTruthy();
    fill("correct horse battery", "correct horse batterY");
    expect(await screen.findByText("The passwords don't match.")).toBeTruthy();
    expect(posted).toBeNull();
  });

  it("says what the server says about a used or expired link", async () => {
    const message = "This link has been used or has expired. Ask whoever invited you for a new one.";
    answer = new Response(JSON.stringify({ error: { code: "invite_not_valid", message, details: {} } }), { status: 400 });
    at("/join?token=old");
    fill("correct horse battery", "correct horse battery");
    expect(await screen.findByText(message)).toBeTruthy();
    expect(getApiKey()).toBeFalsy();
  });

  it("without its token, says to copy the whole link", async () => {
    at("/join");
    expect(await screen.findByText(/missing its last part/)).toBeTruthy();
    expect(screen.queryByLabelText("Password")).toBeNull();
  });
});
