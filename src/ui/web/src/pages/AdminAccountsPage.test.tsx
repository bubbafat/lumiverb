import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { API_KEY_STORAGE_KEY } from "../api/client";
import AdminAccountsPage from "./AdminAccountsPage";

const fetchMock = vi.fn();
let serverAdmin = true;
let accounts: Record<string, unknown>[] = [];
let admins: Record<string, unknown>[] = [];
const sent: { method: string; path: string; body: unknown; query: string }[] = [];

function json(body: unknown, status = 200) {
  return new Response(status === 204 ? null : JSON.stringify(body), { status });
}

function token(tenantId: string): string {
  const part = (o: object) => btoa(JSON.stringify(o)).replace(/=+$/, "");
  return `${part({ alg: "HS256" })}.${part({ sub: "usr_robert", role: "admin", tenant_id: tenantId })}.sig`;
}

const MINE = { tenant_id: "ten_mine", name: "Robert's", plan: "free", status: "active", users: 2, created_at: "2026-10-01T00:00:00Z",
               folders: null };
const ANA = { tenant_id: "ten_ana", name: "Ana's", plan: "free", status: "active", users: 1, created_at: "2026-10-20T00:00:00Z",
              folders: null };
const OLD = { tenant_id: "ten_old", name: "Old one", plan: "free", status: "deleted", users: 1, created_at: null };

beforeEach(() => {
  localStorage.setItem(API_KEY_STORAGE_KEY, token("ten_mine"));
  accounts = [MINE, ANA, OLD];
  admins = [{ user_id: "usr_robert", email: "robert@example.com", tenant_id: "ten_mine", account: "Robert's" }];
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    const parsed = new URL(url, "http://x");
    const path = parsed.pathname.replace(/^\/v1/, "");
    sent.push({ method, path, body, query: decodeURIComponent(parsed.search) });
    if (path.endsWith("/me")) return json({ user_id: "usr_robert", email: "robert@example.com", role: "admin", server_admin: serverAdmin });
    if (path === "/system/network") return json({ available: false });
    if (path === "/admin/tenants" && method === "GET") return json(accounts);
    if (path === "/admin/tenants" && method === "POST") {
      return json({ tenant_id: "ten_sam", api_key: "lv_x", invite_urls: ["http://studio.local:6464/join?token=abc"],
                    invite_expires_at: "2026-10-27T00:00:00Z" });
    }
    if (path === "/admin/tenants/ten_ana" && method === "DELETE") {
      if (parsed.searchParams.get("confirm") !== "Ana's") {
        return json({ error: { code: "confirm_delete_account", message: "Type its name.",
                               details: { name: "Ana's", users: 1, libraries: 2, clips: 3400 } } }, 409);
      }
      accounts = [MINE, OLD];
      return json(null, 204);
    }
    if (path.endsWith("/folders") && method === "PUT") {
      const folders = (body as { folders: string[] | null }).folders;
      if (folders?.includes("/Volumes/Shared")) {
        return json({ error: { code: "folders_overlap",
                               message: "/Volumes/Shared overlaps /Volumes/Shared/robert, a folder of Robert's: each account's folders are its own.",
                               details: {} } }, 409);
      }
      const tenantId = path.split("/")[3];
      accounts = accounts.map((a) => (a.tenant_id === tenantId ? { ...a, folders } : a));
      return json({ tenant_id: tenantId, folders });
    }
    if (path === "/admin/server-admins" && method === "GET") return json({ server_admins: admins });
    if (path === "/admin/server-admins" && method === "POST") {
      if ((body as { email: string }).email !== "ana@example.com") {
        return json({ error: { code: "user_not_found", message: "Nobody.", details: {} } }, 404);
      }
      admins = [...admins, { user_id: "usr_ana", email: "ana@example.com", tenant_id: "ten_ana", account: "Ana's" }];
      return json({ server_admins: admins, links_ended: 1 });
    }
    if (path.startsWith("/admin/server-admins/") && method === "DELETE") {
      return json({ error: { code: "last_server_admin", message: "The last one.", details: {} } }, 409);
    }
    return json({}, 404);
  });
});

afterEach(() => {
  cleanup();
  fetchMock.mockReset();
  vi.unstubAllGlobals();
  localStorage.clear();
  serverAdmin = true;
  sent.length = 0;
});

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AdminAccountsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Admin › Accounts", () => {
  it("lists the accounts, yours with no Delete, and the deleted ones apart", async () => {
    renderPage();
    const mine = await screen.findByRole("listitem", { name: "Robert's" });
    expect(mine.textContent).toContain("(yours)");
    expect(mine.textContent).toContain("2 people");
    expect(within(mine).queryByRole("button", { name: "Delete" })).toBeNull();
    expect(within(screen.getByRole("listitem", { name: "Ana's" })).getByRole("button", { name: "Delete" })).toBeTruthy();
    expect(screen.queryByRole("listitem", { name: "Old one" })).toBeNull();
    expect(screen.getByText(/Deleted, kept on the server and never signed in to: Old one/)).toBeTruthy();
  });

  it("makes an account with its first admin invited, and shows the link to send them", async () => {
    renderPage();
    await screen.findByRole("listitem", { name: "Robert's" });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Sam's" } });
    fireEvent.change(screen.getByLabelText("Its first admin's email"), { target: { value: "sam@example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Make the account" }));
    expect(((await screen.findByLabelText("Invite link")) as HTMLInputElement).value)
      .toBe("http://studio.local:6464/join?token=abc");
    expect(sent.find((s) => s.method === "POST")?.body).toEqual({ name: "Sam's", email: "sam@example.com" });
  });

  it("says what an account holds before deleting it, and deletes it only once its name is typed", async () => {
    renderPage();
    const ana = await screen.findByRole("listitem", { name: "Ana's" });
    fireEvent.click(within(ana).getByRole("button", { name: "Delete" }));
    const asked = await within(ana).findByRole("alertdialog", { name: "Delete this account?" });
    expect(asked.textContent).toContain("Ana's holds 1 person, 2 libraries, 3,400 clips");
    const go = within(asked).getByRole("button", { name: "Delete Ana's" }) as HTMLButtonElement;
    expect(go.disabled).toBe(true);
    fireEvent.change(within(asked).getByLabelText("Its name"), { target: { value: "ana's" } });
    expect(go.disabled).toBe(true);
    fireEvent.change(within(asked).getByLabelText("Its name"), { target: { value: "Ana's" } });
    fireEvent.click(go);
    await waitFor(() => expect(screen.queryByRole("listitem", { name: "Ana's" })).toBeNull());
    expect(sent.filter((s) => s.method === "DELETE").map((s) => s.query)).toEqual(["", "?confirm=Ana's"]);
  });

  it("names a server admin by email, says the links it ended, and why the last one can't be removed", async () => {
    renderPage();
    const list = await screen.findByRole("list", { name: "Server admins" });
    await within(list).findByRole("listitem", { name: "robert@example.com" });
    fireEvent.change(screen.getByLabelText("Email of someone who signs in here"), { target: { value: "nobody@example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Make server admin" }));
    expect(await screen.findByText("Nobody signs in here with that email.")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Email of someone who signs in here"), { target: { value: "ana@example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Make server admin" }));
    expect(await within(list).findByRole("listitem", { name: "ana@example.com" })).toBeTruthy();
    // An invite an account admin made her before ended with it.
    expect(screen.getByRole("status").textContent).toBe(
      "The link ana@example.com hadn't used has stopped working: only a server admin makes them a new one.");
    fireEvent.click(within(within(list).getByRole("listitem", { name: "robert@example.com" })).getByRole("button", { name: "Remove" }));
    expect(await screen.findByText("They're the last server admin: name another first.")).toBeTruthy();
  });

  it("shows each account's folders, every folder the server reads until given some", async () => {
    accounts = [MINE, { ...ANA, folders: ["/Volumes/Ana", "/srv/ana"] }, OLD];
    renderPage();
    const mine = await screen.findByRole("listitem", { name: "Robert's" });
    expect(mine.textContent).toContain("Folders: Every folder the server reads");
    expect(screen.getByRole("listitem", { name: "Ana's" }).textContent).toContain("Folders: /Volumes/Ana, /srv/ana");
  });

  it("says, with several accounts, that one without folders of its own can choose any folder, the others' too", async () => {
    renderPage();
    const note = await screen.findByRole("note");
    expect(note.textContent).toContain("Each account without folders of its own (Robert's, Ana's) can choose any folder");
    expect(note.textContent).toContain("the other accounts' too");
  });

  it("says nothing of folders on a server with one account", async () => {
    accounts = [MINE, OLD];
    renderPage();
    await screen.findByRole("listitem", { name: "Robert's" });
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("says nothing once every account has folders of its own", async () => {
    accounts = [{ ...MINE, folders: ["/Volumes/Robert"] }, { ...ANA, folders: [] }, OLD];
    renderPage();
    const ana = await screen.findByRole("listitem", { name: "Ana's" });
    expect(ana.textContent).toContain("None on this server");
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("gives an account its folders, one per line, and lifts them again", async () => {
    renderPage();
    const ana = await screen.findByRole("listitem", { name: "Ana's" });
    fireEvent.click(within(ana).getByRole("button", { name: "Change folders" }));
    const form = within(ana).getByRole("form", { name: "Folders for Ana's" });
    const lines = within(form).getByLabelText("Its folders, one per line") as HTMLTextAreaElement;
    expect(lines.disabled).toBe(true);
    fireEvent.click(within(form).getByLabelText("Only these folders"));
    fireEvent.change(lines, { target: { value: " /Volumes/Ana \n\n/srv/ana\n" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save folders" }));
    await waitFor(() => expect(within(screen.getByRole("listitem", { name: "Ana's" })).queryByRole("form")).toBeNull());
    expect(sent.find((s) => s.method === "PUT")).toMatchObject({ path: "/admin/tenants/ten_ana/folders",
                                                                 body: { folders: ["/Volumes/Ana", "/srv/ana"] } });
    expect((await screen.findByRole("listitem", { name: "Ana's" })).textContent).toContain("Folders: /Volumes/Ana, /srv/ana");

    fireEvent.click(within(screen.getByRole("listitem", { name: "Ana's" })).getByRole("button", { name: "Change folders" }));
    fireEvent.click(screen.getByLabelText("Every folder the server reads"));
    fireEvent.click(screen.getByRole("button", { name: "Save folders" }));
    await waitFor(() => expect(sent.filter((s) => s.method === "PUT").map((s) => s.body)).toEqual(
      [{ folders: ["/Volumes/Ana", "/srv/ana"] }, { folders: null }]));
  });

  it("says why the server refused the folders, as it says it", async () => {
    renderPage();
    const ana = await screen.findByRole("listitem", { name: "Ana's" });
    fireEvent.click(within(ana).getByRole("button", { name: "Change folders" }));
    fireEvent.click(within(ana).getByLabelText("Only these folders"));
    fireEvent.change(within(ana).getByLabelText("Its folders, one per line"), { target: { value: "/Volumes/Shared" } });
    fireEvent.click(within(ana).getByRole("button", { name: "Save folders" }));
    expect((await within(ana).findByRole("alert")).textContent)
      .toBe("/Volumes/Shared overlaps /Volumes/Shared/robert, a folder of Robert's: each account's folders are its own.");
    expect(within(ana).getByRole("form")).toBeTruthy();
  });

  it("tells someone who isn't a server admin that only one manages accounts", async () => {
    serverAdmin = false;
    renderPage();
    expect(await screen.findByText("Only a server admin manages the accounts on this server.")).toBeTruthy();
    expect(screen.queryByRole("list", { name: "Accounts" })).toBeNull();
  });
});
