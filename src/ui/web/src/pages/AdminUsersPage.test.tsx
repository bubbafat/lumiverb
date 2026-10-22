import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import AdminUsersPage from "./AdminUsersPage";

const fetchMock = vi.fn();
const hoursAgo = (h: number) => new Date(Date.now() - h * 3600_000).toISOString();
const LINK = "http://macstudio.local:6464/join?token=abc";
const BY_ADDRESS = "http://192.168.86.55:6464/join?token=abc";
let network = { available: true, other_devices: true, addresses: ["http://macstudio.local:6464"] };
let posts: { url: string; body: unknown }[] = [];
let patches: { url: string; body: unknown }[] = [];
let deletes: string[] = [];
// Who's looking (/v1/me), and whether u2 is a server admin.
let viewerIsServerAdmin = false;
let u2ServerAdmin = false;

beforeEach(() => {
  network = { available: true, other_devices: true, addresses: ["http://macstudio.local:6464"] };
  posts = [];
  patches = [];
  deletes = [];
  viewerIsServerAdmin = false;
  u2ServerAdmin = false;
  vi.stubGlobal("fetch", fetchMock);
  vi.spyOn(window, "confirm").mockReturnValue(true);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") posts.push({ url, body: init.body ? JSON.parse(String(init.body)) : null });
    if (init?.method === "DELETE") {
      deletes.push(url);
      return new Response(JSON.stringify({ error: { code: "last_server_admin",
                                                    message: "They're the last server admin: name another first.",
                                                    details: {} } }), { status: 409 });
    }
    if (url.endsWith("/v1/me")) {
      return new Response(JSON.stringify({ user_id: "u1", email: "a@b.c", role: "admin",
                                           server_admin: viewerIsServerAdmin }), { status: 200 });
    }
    if (init?.method === "PATCH") {
      const body = JSON.parse(String(init.body));
      patches.push({ url, body });
      return new Response(JSON.stringify({ user_id: "u2", email: "d@e.f", role: "viewer", created_at: hoursAgo(100),
                                           last_login_at: null, ...body }), { status: 200 });
    }
    if (url.endsWith("/v1/users") && init?.method === "POST") {
      const body = JSON.parse(String(init.body));
      return new Response(
        JSON.stringify({ user_id: "u3", email: body.email, role: body.role, created_at: hoursAgo(0),
                         last_login_at: null, invite_urls: [LINK, BY_ADDRESS], invite_expires_at: hoursAgo(-168) }),
        { status: 201 },
      );
    }
    if (url.endsWith("/v1/users/u2/invite")) {
      return new Response(JSON.stringify({ invite_urls: [LINK + "2"], invite_expires_at: hoursAgo(-168) }), { status: 200 });
    }
    if (url.endsWith("/v1/users")) {
      return new Response(
        JSON.stringify([
          { user_id: "u1", email: "a@b.c", role: "admin", created_at: hoursAgo(100), last_login_at: hoursAgo(2) },
          { user_id: "u2", email: "d@e.f", role: "viewer", server_admin: u2ServerAdmin, created_at: hoursAgo(100),
            last_login_at: null },
        ]),
        { status: 200 },
      );
    }
    if (url.endsWith("/v1/system/network")) return new Response(JSON.stringify(network), { status: 200 });
    return new Response("{}", { status: 404 });
  });
});

afterEach(() => {
  cleanup();
  fetchMock.mockReset();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AdminUsersPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("AdminUsersPage", () => {
  it("spells out the last sign-in time", async () => {
    renderPage();
    expect(await screen.findByText("2 hours ago")).toBeTruthy();
    expect(screen.getByText("never")).toBeTruthy();
  });

  it("invites by email and role, and shows the link to send", async () => {
    renderPage();
    fireEvent.change(await screen.findByLabelText("Email"), { target: { value: " new@b.co " } });
    fireEvent.change(screen.getByLabelText("Role"), { target: { value: "editor" } });
    fireEvent.click(screen.getByRole("button", { name: "Make invite link" }));
    const link = (await screen.findByLabelText("Invite link")) as HTMLInputElement;
    expect(link.value).toBe(LINK);
    expect((screen.getByLabelText("Invite link by address") as HTMLInputElement).value).toBe(BY_ADDRESS);
    expect(screen.getByText(/If that one doesn't open on their device/)).toBeTruthy();
    expect(posts).toEqual([{ url: "/v1/users", body: { email: "new@b.co", role: "editor" } }]);
    expect(screen.getByText(/It works once, for 7 days/)).toBeTruthy();
    expect(screen.queryByText(/Only this Mac can open it/)).toBeNull();
    expect(screen.queryByLabelText("Password")).toBeNull();
  });

  it("says a Mac's link opens only there until Settings › Network lets other devices in", async () => {
    network = { available: true, other_devices: false, addresses: ["http://macstudio.local:6464"] };
    renderPage();
    fireEvent.change(await screen.findByLabelText("Email"), { target: { value: "new@b.co" } });
    fireEvent.click(screen.getByRole("button", { name: "Make invite link" }));
    expect(await screen.findByText(/Only this Mac can open it until/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Settings › Network" }).getAttribute("href")).toBe("/settings/network");
  });

  it("makes a new link only for someone who hasn't signed in", async () => {
    renderPage();
    await screen.findByText("2 hours ago");
    const buttons = screen.getAllByRole("button", { name: "New link" });
    expect(buttons).toHaveLength(1);
    fireEvent.click(buttons[0]);
    await waitFor(() => expect((screen.getByLabelText("Invite link") as HTMLInputElement).value).toBe(LINK + "2"));
    expect(posts.map((p) => p.url)).toEqual(["/v1/users/u2/invite"]);
  });

  it("says which libraries each sees, and changes it", async () => {
    renderPage();
    const viewer = (await screen.findByLabelText("Libraries d@e.f sees")) as HTMLSelectElement;
    expect(viewer.value).toBe("every");
    expect(screen.queryByLabelText("Libraries a@b.c sees")).toBeNull(); // an admin sees every library
    fireEvent.change(viewer, { target: { value: "given" } });
    await waitFor(() => expect(patches).toEqual([{ url: "/v1/users/u2", body: { every_library: false } }]));
  });

  it("offers an account admin nothing to change on a server admin", async () => {
    u2ServerAdmin = true;
    renderPage();
    expect(await screen.findByText("Only a server admin changes a server admin.")).toBeTruthy();
    const row = within(screen.getByText("d@e.f").closest("tr")!);
    expect(row.getByText("Server admin")).toBeTruthy();
    expect((row.getByLabelText("Role of d@e.f") as HTMLSelectElement).disabled).toBe(true);
    expect((row.getByLabelText("Libraries d@e.f sees") as HTMLSelectElement).disabled).toBe(true);
    expect(row.queryByRole("button", { name: "New link" })).toBeNull();
    expect(row.queryByRole("button", { name: "Remove" })).toBeNull();
    // Anyone else's row is as it was.
    expect(within(screen.getByText("a@b.c").closest("tr")!).getByRole("button", { name: "Remove" })).toBeTruthy();
  });

  it("offers a server admin another's controls, and says why one can't go", async () => {
    u2ServerAdmin = true;
    viewerIsServerAdmin = true;
    renderPage();
    await waitFor(() => expect(screen.getAllByRole("button", { name: "New link" })).toHaveLength(1));
    expect(screen.queryByText("Only a server admin changes a server admin.")).toBeNull();
    expect((screen.getByLabelText("Role of d@e.f") as HTMLSelectElement).disabled).toBe(false);
    fireEvent.click(within(screen.getByText("d@e.f").closest("tr")!).getByRole("button", { name: "Remove" }));
    expect(await screen.findByText("They're the last server admin: name another first.")).toBeTruthy();
    expect(deletes).toEqual(["/v1/users/u2"]);
  });

  it("invites someone who sees only the libraries they're given", async () => {
    renderPage();
    fireEvent.change(await screen.findByLabelText("Email"), { target: { value: "guest@b.co" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /Only the libraries they're given/ }));
    fireEvent.click(screen.getByRole("button", { name: "Make invite link" }));
    await screen.findByLabelText("Invite link");
    expect(posts).toEqual([{ url: "/v1/users", body: { email: "guest@b.co", role: "viewer", every_library: false } }]);
  });
});
