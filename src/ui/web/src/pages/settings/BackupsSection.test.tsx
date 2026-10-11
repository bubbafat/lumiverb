import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import BackupsSection, { backupsHaveProblem, size } from "./BackupsSection";
import type { BackupItem, Backups } from "../../api/client";

const fetchMock = vi.fn();
let role = "admin";
let state: Backups;
const sent: { method: string; url: string; body: unknown }[] = [];

const HOUR = 3600 * 1000;

function backup(over: Partial<BackupItem> = {}): BackupItem {
  return {
    name: "lumiverb-2026-10-11-030000-nightly", made_at: new Date(Date.now() - 5 * HOUR).toISOString(),
    kind: "nightly", bytes: 12 * 1024 * 1024, accounts: 1,
    restore: ["brew services stop lumiverb",
              "lumiverb-server restore --env /opt/homebrew/etc/lumiverb/env /opt/homebrew/var/lumiverb/backups/lumiverb-2026-10-11-030000-nightly",
              "brew services start lumiverb"],
    ...over,
  };
}

function backups(over: Partial<Backups> = {}): Backups {
  return {
    folder: "/opt/homebrew/var/lumiverb/backups", default_folder: "/opt/homebrew/var/lumiverb/backups",
    changeable_here: true, running: null, last_error: null, last_failed_at: null,
    next_at: new Date(Date.now() + 10 * HOUR).toISOString(), backups: [backup()], ...over,
  };
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

beforeEach(() => {
  state = backups();
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    sent.push({ method, url, body });
    const path = new URL(url, "http://x").pathname.replace(/^\/v1/, "");
    if (path === "/me") return json({ email: "a@b.c", role });
    if (path === "/system/backups" && method === "GET") return json(state);
    if (path === "/system/backups" && method === "POST") {
      state = { ...state, running: { kind: "now", started_at: null, done: 0, of: 2 } };
      return json(state, 202);
    }
    if (path === "/system/backups/folder") {
      if (body.folder === "/nowhere") {
        return json({ error: { code: "folder_not_usable", message: "There's no folder at /nowhere.", details: {} } }, 400);
      }
      state = { ...state, folder: body.folder ?? state.default_folder };
      return json(state);
    }
    return json({}, 404);
  });
});

afterEach(() => {
  cleanup();
  fetchMock.mockReset();
  vi.unstubAllGlobals();
  role = "admin";
  sent.length = 0;
});

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <BackupsSection />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("BackupsSection", () => {
  it("says when the last one was, how big, and when the next is", async () => {
    renderSection();
    expect(await screen.findByText(/^Last: .*, 12\.0 MB\. Next: /)).toBeTruthy();
    const made = screen.getByRole("list");
    expect(within(made).getByText(/Nightly · 12\.0 MB/)).toBeTruthy();
    expect(screen.getByText(/On the same drive as Lumiverb's data/)).toBeTruthy();
  });

  it("backs up now and shows it working", async () => {
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Back up now" }));
    expect(await screen.findByText("Backing up now (1 of 2)…")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Back up now" }) as HTMLButtonElement).disabled).toBe(true);
    expect(sent.some((s) => s.method === "POST" && s.url.endsWith("/system/backups"))).toBe(true);
  });

  it("says plainly when the last one failed", async () => {
    state = backups({ last_error: "pg_dump couldn't back up ten_a: disk full",
                      last_failed_at: new Date().toISOString() });
    renderSection();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("The last backup failed");
    expect(alert.textContent).toContain("disk full");
  });

  it("shows the commands that put one back", async () => {
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Restore…" }));
    const commands = screen.getAllByRole("textbox", { name: "Command" }).map((c) => (c as HTMLInputElement).value);
    expect(commands).toEqual(backup().restore);
    expect(screen.getByText(/what's there is backed up first/)).toBeTruthy();
  });

  it("changes the folder, and says why one can't be used", async () => {
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Change" }));
    const input = screen.getByRole("textbox", { name: "Backups folder" });
    fireEvent.change(input, { target: { value: "/nowhere" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect((await screen.findByRole("alert")).textContent).toBe("There's no folder at /nowhere.");
    fireEvent.change(input, { target: { value: "/Volumes/Backup/Lumiverb" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("/Volumes/Backup/Lumiverb")).toBeTruthy();
    expect(screen.queryByText(/On the same drive/)).toBeNull();
  });

  it("from another device, the folder isn't changed here", async () => {
    state = backups({ changeable_here: false });
    renderSection();
    expect(await screen.findByText("Choose it on the computer Lumiverb is installed on.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Change" })).toBeNull();
  });

  it("isn't shown to someone who isn't an admin", async () => {
    role = "editor";
    renderSection();
    expect(await screen.findByText("Only admins see backups.")).toBeTruthy();
    expect(sent.some((s) => s.url.includes("/system/backups"))).toBe(false);
  });
});

describe("backupsHaveProblem", () => {
  it("flags a failure after the newest, or none for two days", () => {
    const now = Date.now();
    expect(backupsHaveProblem(backups(), now)).toBe(false);
    expect(backupsHaveProblem(backups({ backups: [backup({ made_at: new Date(now - 49 * HOUR).toISOString() })] }), now)).toBe(true);
    expect(backupsHaveProblem(backups({ last_error: "x", last_failed_at: new Date(now).toISOString() }), now)).toBe(true);
    expect(backupsHaveProblem(backups({ last_error: "x", last_failed_at: new Date(now - 6 * HOUR).toISOString() }), now)).toBe(false);
    expect(backupsHaveProblem(backups({ backups: [] }), now)).toBe(false);
  });

  it("sizes", () => {
    expect(size(512)).toBe("512 bytes");
    expect(size(12 * 1024 * 1024)).toBe("12.0 MB");
  });
});
