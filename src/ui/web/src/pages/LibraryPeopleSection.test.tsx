import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import LibraryPeopleSection from "./LibraryPeopleSection";

const fetchMock = vi.fn();
let role = "admin";
let isPrivate = false;
let setRoles: Record<string, string | null> = {};
const sent: { method: string; path: string; body: unknown }[] = [];

const PEOPLE = [
  { user_id: "u_admin", email: "owner@example.com", account_role: "admin", every_library: true },
  { user_id: "u_ed", email: "editor@example.com", account_role: "editor", every_library: true },
  { user_id: "u_guest", email: "guest@example.com", account_role: "viewer", every_library: false },
];

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

function people() {
  return PEOPLE.map((p) => {
    const set = setRoles[p.user_id] ?? null;
    const fallback = p.account_role === "admin" ? "admin" : isPrivate || !p.every_library ? "none" : p.account_role;
    return { ...p, role: p.account_role === "admin" ? "admin" : set ?? fallback, set_role: set, set_by: null, set_at: null };
  });
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    const path = new URL(url, "http://x").pathname.replace(/^\/v1/, "");
    sent.push({ method, path, body });
    if (path === "/libraries/lib_1" && method === "GET") {
      return json({ library_id: "lib_1", name: "Trips", root_path: "/t", is_public: false, private: isPrivate, role });
    }
    if (path === "/libraries/lib_1" && method === "PATCH") {
      isPrivate = Boolean((body as { private: boolean }).private);
      return json({ library_id: "lib_1", name: "Trips", root_path: "/t", is_public: false, private: isPrivate, role });
    }
    if (path === "/libraries/lib_1/people") return json({ library_id: "lib_1", private: isPrivate, people: people() });
    const one = path.match(/^\/libraries\/lib_1\/people\/(\w+)$/);
    if (one && method === "PUT") {
      setRoles[one[1]] = (body as { role: string | null }).role;
      return json(people().find((p) => p.user_id === one[1]));
    }
    return json({}, 404);
  });
});

afterEach(() => {
  cleanup();
  fetchMock.mockReset();
  vi.unstubAllGlobals();
  role = "admin";
  isPrivate = false;
  setRoles = {};
  sent.length = 0;
});

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <LibraryPeopleSection libraryId="lib_1" />
    </QueryClientProvider>,
  );
}

describe("LibraryPeopleSection", () => {
  it("lists everyone with their role here and why, account admins fixed", async () => {
    renderSection();
    expect(await screen.findByText("editor@example.com")).toBeTruthy();
    expect(screen.getByText("Account admin: every library's admin")).toBeTruthy();
    expect(screen.queryByLabelText("owner@example.com's role here")).toBeNull();
    const editor = screen.getByLabelText("editor@example.com's role here") as HTMLSelectElement;
    expect(editor.value).toBe("");
    expect(screen.getByRole("option", { name: "Default (Editor)" })).toBeTruthy();
    expect(screen.getByRole("option", { name: "Default (No access)" })).toBeTruthy(); // the guest sees only what's given
    expect(screen.getByText("Sees only the libraries they're given")).toBeTruthy();
  });

  it("gives someone a role here, and puts them back to the default", async () => {
    renderSection();
    fireEvent.change(await screen.findByLabelText("guest@example.com's role here"), { target: { value: "viewer" } });
    await waitFor(() => expect(sent).toContainEqual({ method: "PUT", path: "/libraries/lib_1/people/u_guest",
                                                       body: { role: "viewer" } }));
    expect(await screen.findByText("Set for this library")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("guest@example.com's role here"), { target: { value: "" } });
    await waitFor(() => expect(sent).toContainEqual({ method: "PUT", path: "/libraries/lib_1/people/u_guest",
                                                       body: { role: null } }));
  });

  it("makes the library private, and the defaults follow", async () => {
    renderSection();
    fireEvent.click(await screen.findByRole("checkbox", { name: /Private/ }));
    await waitFor(() => expect(sent).toContainEqual({ method: "PATCH", path: "/libraries/lib_1", body: { private: true } }));
    await waitFor(() => expect(screen.getAllByRole("option", { name: "Default (No access)" })).toHaveLength(2));
  });

  it("isn't shown to someone who doesn't run the library", async () => {
    role = "editor";
    const { container } = renderSection();
    await waitFor(() => expect(sent.some((s) => s.path === "/libraries/lib_1")).toBe(true));
    expect(container.textContent).toBe("");
    expect(sent.some((s) => s.path === "/libraries/lib_1/people")).toBe(false);
  });
});
