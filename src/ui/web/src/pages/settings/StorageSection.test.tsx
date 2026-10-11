import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import StorageSection, { storageHasProblem } from "./StorageSection";
import type { StorageInfo } from "../../api/client";

const fetchMock = vi.fn();
let role = "admin";
let state: StorageInfo;
const sent: { method: string; url: string; body: unknown }[] = [];
const GB = 1024 ** 3;
const DATA = "/opt/homebrew/var/lumiverb";
const DRIVE = "/Volumes/Media/Lumiverb previews";

function info(over: Partial<StorageInfo> = {}): StorageInfo {
  return {
    previews: DATA, data: DATA, previews_available: true, previews_free: 100 * GB, previews_total: 500 * GB,
    data_free: 100 * GB, data_total: 500 * GB, same_disk: true, moving: null, changeable_here: true, libraries: [],
    ...over,
  };
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

beforeEach(() => {
  state = info();
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    sent.push({ method, url, body });
    const path = new URL(url, "http://x").pathname.replace(/^\/v1/, "");
    if (path === "/me") return json({ email: "a@b.c", role });
    if (path === "/system/storage") return json(state);
    if (path === "/system/storage/previews") {
      if (body.folder === "/nowhere") {
        return json({ error: { code: "folder_not_usable", message: "There's no folder at /nowhere.", details: {} } }, 400);
      }
      if (!body.move) {
        return json({ error: { code: "move_previews",
                               message: `Moves 1,234 previews (2.0 GB) from ${DATA} to ${body.folder ?? DATA}. Lumiverb keeps working meanwhile; previews not moved yet show from where they are.`,
                               details: { from: DATA, to: body.folder, files: 1234, bytes: 2 * GB, free: 3000 * GB } } }, 409);
      }
      state = info({ previews: body.folder, same_disk: false,
                     moving: { from_folder: DATA, to_folder: body.folder, done_files: 0, total_files: 1234,
                               done_bytes: 0, total_bytes: 2 * GB, running: true, error: null, started_at: null } });
      return json(state, 202);
    }
    if (path === "/system/storage/move") {
      state = { ...state, moving: { ...state.moving!, running: true, error: null } };
      return json(state, 202);
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
        <StorageSection />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("StorageSection", () => {
  it("says where the previews and the data are, and how much room there is", async () => {
    renderSection();
    const previews = await screen.findByRole("region", { name: "Previews" });
    expect(within(previews).getByText(DATA)).toBeTruthy();
    expect(within(previews).getByText(/In the data folder\. 100\.0 GB free of 500\.0 GB/)).toBeTruthy();
    expect(within(screen.getByRole("region", { name: "Data" })).getByText(/They stay here/)).toBeTruthy();
  });

  it("shows what moves, and moves only once the user says so", async () => {
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Move them…" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Previews folder" }), { target: { value: DRIVE } });
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    const dialog = await screen.findByRole("dialog", { name: "Move the previews" });
    expect(dialog.textContent).toContain("Moves 1,234 previews (2.0 GB)");
    expect(sent.filter((s) => s.method === "PUT").map((s) => s.body)).toEqual([{ folder: DRIVE, move: false }]);
    fireEvent.click(within(dialog).getByRole("button", { name: "Move them" }));
    expect(await screen.findByText(/Moving the previews from/)).toBeTruthy();
    expect(screen.getByText(/0 of 1,234/)).toBeTruthy();
    expect(sent.filter((s) => s.method === "PUT").map((s) => s.body)).toEqual([
      { folder: DRIVE, move: false }, { folder: DRIVE, move: true }]);
  });

  it("says why a folder can't be used", async () => {
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Move them…" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Previews folder" }), { target: { value: "/nowhere" } });
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect((await screen.findByRole("alert")).textContent).toBe("There's no folder at /nowhere.");
  });

  it("says when a move stopped, and tries again", async () => {
    state = info({ previews: DRIVE, same_disk: false,
                   moving: { from_folder: DATA, to_folder: DRIVE, done_files: 600, total_files: 1234,
                             done_bytes: GB, total_bytes: 2 * GB, running: false,
                             error: "The new drive is full (/Volumes/Media).", started_at: null } });
    renderSection();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("The move stopped");
    expect(alert.textContent).toContain("The new drive is full");
    fireEvent.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByText(/600 of 1,234 \(1\.0 GB of 2\.0 GB\)/)).toBeTruthy();
    expect(sent.some((s) => s.method === "POST" && s.url.endsWith("/system/storage/move"))).toBe(true);
  });

  it("says when the previews' drive isn't plugged in", async () => {
    state = info({ previews: DRIVE, previews_available: false, same_disk: false });
    renderSection();
    expect((await screen.findByRole("alert")).textContent).toContain("Is its drive plugged in?");
  });

  it("from another device, they aren't moved here", async () => {
    state = info({ changeable_here: false });
    renderSection();
    expect(await screen.findByText("Move them on the computer Lumiverb is installed on.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Move them…" })).toBeNull();
  });

  it("isn't shown to someone who isn't an admin", async () => {
    role = "viewer";
    renderSection();
    expect(await screen.findByText("Only admins see where Lumiverb keeps its files.")).toBeTruthy();
    expect(sent.some((s) => s.url.includes("/system/storage"))).toBe(false);
  });
});

describe("libraries with a folder of their own", () => {
  it("are listed with a link to their settings, and an unplugged one is flagged", async () => {
    state = info({ libraries: [{ library_id: "lib_v", name: "Video", folder: "/Volumes/Video", available: false }] });
    renderSection();
    const list = await screen.findByRole("region", { name: "Libraries with a folder of their own" });
    expect(within(list).getByRole("link", { name: "Video" }).getAttribute("href")).toBe("/libraries/lib_v/settings");
    expect(list.textContent).toContain("its drive isn't plugged in");
    expect(storageHasProblem(state)).toBe(true);
  });

  it("a library's move says whose", async () => {
    state = info({ moving: { library_id: "lib_v", library_name: "Video", from_folder: DATA, to_folder: "/Volumes/Video",
                             done_files: 1, total_files: 2, done_bytes: GB, total_bytes: 2 * GB, running: true,
                             error: null, started_at: null } });
    renderSection();
    expect((await screen.findByRole("status")).textContent).toContain("Moving Video's previews from");
  });
});

describe("storageHasProblem", () => {
  it("flags an unplugged drive or a stopped move", () => {
    expect(storageHasProblem(info())).toBe(false);
    expect(storageHasProblem(info({ previews_available: false }))).toBe(true);
    expect(storageHasProblem(info({ moving: { from_folder: DATA, to_folder: DRIVE, done_files: 0, total_files: 1,
                                               done_bytes: 0, total_bytes: 1, running: false, error: "x",
                                               started_at: null } }))).toBe(true);
  });
});
