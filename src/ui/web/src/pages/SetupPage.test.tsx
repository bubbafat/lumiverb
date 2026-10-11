import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import SetupPage from "./SetupPage";
import LoginPage from "./LoginPage";
import { getApiKey } from "../api/client";

const fetchMock = vi.fn();

let status = { needed: true, on_this_machine: true };
let posted: { email: string; password: string } | null = null;
let answer: Response = new Response(JSON.stringify({ access_token: "tok-first-admin" }), { status: 201 });

beforeEach(() => {
  status = { needed: true, on_this_machine: true };
  posted = null;
  answer = new Response(JSON.stringify({ access_token: "tok-first-admin" }), { status: 201 });
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url === "/v1/setup" && init?.method === "POST") {
      posted = JSON.parse(String(init.body));
      return answer;
    }
    if (url === "/v1/setup") return new Response(JSON.stringify(status), { status: 200 });
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
        <Route path="/setup" element={<SetupPage />} />
        <Route path="/login" element={<LoginPage />} />
        <Route path="/" element={<p>Libraries</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

function fill(email: string, password: string, again: string) {
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: email } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: password } });
  fireEvent.change(screen.getByLabelText("Password again"), { target: { value: again } });
  fireEvent.click(screen.getByRole("button", { name: "Create account" }));
}

describe("SetupPage", () => {
  it("makes the first admin and logs them in", async () => {
    at("/setup");
    await screen.findByText(/Create your account/);
    fill(" robert@example.com ", "correct horse battery", "correct horse battery");
    expect(await screen.findByText("Libraries")).toBeTruthy();
    expect(posted).toEqual({ email: "robert@example.com", password: "correct horse battery" });
    expect(getApiKey()).toBe("tok-first-admin");
  });

  it("on another device says where to finish, with no form", async () => {
    status = { needed: true, on_this_machine: false };
    at("/setup");
    expect(await screen.findByText(/open Lumiverb on the computer it's installed on/)).toBeTruthy();
    expect(screen.queryByLabelText("Email")).toBeNull();
  });

  it("checks the password before sending it", async () => {
    at("/setup");
    await screen.findByText(/Create your account/);
    fill("a@example.com", "short", "short");
    expect(await screen.findByText("Use at least 12 characters for your password.")).toBeTruthy();
    fill("a@example.com", "correct horse battery", "correct horse batterY");
    expect(await screen.findByText("The passwords don't match.")).toBeTruthy();
    expect(posted).toBeNull();
  });

  it("shows what the server says when it refuses", async () => {
    answer = new Response(
      JSON.stringify({ error: { code: "already_set_up", message: "This Lumiverb is set up already. Log in instead.", details: {} } }),
      { status: 409 },
    );
    at("/setup");
    await screen.findByText(/Create your account/);
    fill("a@example.com", "correct horse battery", "correct horse battery");
    expect(await screen.findByText("This Lumiverb is set up already. Log in instead.")).toBeTruthy();
    expect(getApiKey()).toBeFalsy();
  });

  it("once someone can log in, is the login page", async () => {
    status = { needed: false, on_this_machine: true };
    at("/setup");
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeTruthy();
  });
});

describe("LoginPage", () => {
  it("sends a new install to setup", async () => {
    at("/login");
    expect(await screen.findByText(/Create your account/)).toBeTruthy();
  });

  it("stays the login page once there's someone to log in", async () => {
    status = { needed: false, on_this_machine: false };
    at("/login");
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeTruthy();
    expect(screen.queryByText(/Create your account/)).toBeNull();
  });
});
