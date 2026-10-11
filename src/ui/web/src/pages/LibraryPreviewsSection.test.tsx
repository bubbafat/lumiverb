import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import LibraryPreviewsSection from "./LibraryPreviewsSection";
import type { LibraryPreviews } from "../api/client";

const fetchMock = vi.fn();
let role = "admin";
let state: LibraryPreviews;
const sent: { method: string; url: string; body: unknown }[] = [];
const GB = 1024 ** 3;
const SERVER = "/opt/homebrew/var/lumiverb";
const VIDEO = "/Volumes/Video/Lumiverb";
const EMPTY = "/Volumes/Video/Nothing yet";

function previews(over: Partial<LibraryPreviews> = {}): LibraryPreviews {
  return {
    library_id: "lib_v", folder: SERVER, own: false, server_folder: SERVER, available: true, free: 100 * GB,
    total: 500 * GB, same_drive_as_originals: false, moving: null, changeable_here: true, ...over,
  };
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

beforeEach(() => {
  state = previews();
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    sent.push({ method, url, body });
    const path = new URL(url, "http://x").pathname.replace(/^\/v1/, "");
    if (path === "/me") return json({ email: "a@b.c", role });
    if (path === "/libraries/lib_v/previews" && method === "GET") return json(state);
    if (path === "/libraries/lib_v/previews") {
      if (!body.move && body.folder === EMPTY) {
        return json({ error: { code: "move_previews",
                               message: `Video's previews go to ${EMPTY} from now on. That's the drive this library's originals are on: when it's unplugged, its previews go too, and the library can't be browsed.`,
                               details: { from: SERVER, to: EMPTY, files: 0, bytes: 0, free: 900 * GB,
                                          same_drive_as_originals: true } } }, 409);
      }
      if (!body.move) {
        return json({ error: { code: "move_previews",
                               message: `Moves Video's 12 previews (3.0 GB) from ${SERVER} to ${body.folder}. Lumiverb keeps working meanwhile; previews not moved yet show from where they are. That's the drive this library's originals are on: when it's unplugged, its previews go too, and the library can't be browsed.`,
                               details: { from: SERVER, to: body.folder, files: 12, bytes: 3 * GB, free: 900 * GB,
                                          same_drive_as_originals: true } } }, 409);
      }
      state = previews({ folder: body.folder, own: true, same_drive_as_originals: true,
                         moving: { from_folder: SERVER, to_folder: body.folder, done_files: 0, total_files: 12,
                                   done_bytes: 0, total_bytes: 3 * GB, running: true, error: null, started_at: null } });
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
        <LibraryPreviewsSection libraryId="lib_v" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("LibraryPreviewsSection", () => {
  it("says it's in the server's folder, and moves to one of its own after saying yes", async () => {
    renderSection();
    const section = await screen.findByRole("region", { name: "Previews" });
    expect(section.textContent).toContain("In the server's previews folder");
    fireEvent.click(within(section).getByRole("button", { name: "Move them…" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Previews folder" }), { target: { value: VIDEO } });
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    const dialog = await screen.findByRole("dialog", { name: "Move the previews" });
    expect(dialog.textContent).toContain("the library can't be browsed");
    fireEvent.click(within(dialog).getByRole("button", { name: "Move them" }));
    expect((await screen.findByRole("status")).textContent).toContain("Moving its previews from");
    expect(sent.filter((s) => s.method === "PUT").map((s) => s.body)).toEqual([
      { folder: VIDEO, move: false, make_folder: false }, { folder: VIDEO, move: true, make_folder: false }]);
  });

  it("with nothing to move, still says it's the originals' drive, and the yes is Use it", async () => {
    renderSection();
    fireEvent.click(within(await screen.findByRole("region", { name: "Previews" })).getByRole("button", { name: "Move them…" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Previews folder" }), { target: { value: EMPTY } });
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    const dialog = await screen.findByRole("dialog", { name: "Move the previews" });
    expect(dialog.textContent).toContain("the library can't be browsed");
    expect(within(dialog).queryByRole("button", { name: "Move them" })).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: "Use it" }));
    await screen.findByRole("status");
    expect(sent.filter((s) => s.method === "PUT").map((s) => s.body)).toEqual([
      { folder: EMPTY, move: false, make_folder: false }, { folder: EMPTY, move: true, make_folder: false }]);
  });

  it("with a folder of its own: says so, warns about the originals' drive, and offers the way back", async () => {
    state = previews({ folder: VIDEO, own: true, same_drive_as_originals: true });
    renderSection();
    const section = await screen.findByRole("region", { name: "Previews" });
    expect(section.textContent).toContain("A folder of its own:");
    expect(section.textContent).toContain("On the same drive as its originals");
    expect(within(section).getByRole("button", { name: "Back to the server's folder…" })).toBeTruthy();
  });

  it("says when its drive isn't plugged in", async () => {
    state = previews({ folder: VIDEO, own: true, available: false });
    renderSection();
    expect((await screen.findByRole("alert")).textContent).toContain("Is its drive plugged in?");
  });

  it("isn't shown to someone who isn't an admin", async () => {
    role = "editor";
    renderSection();
    await screen.findByText((_, el) => el === document.body);
    expect(screen.queryByRole("region", { name: "Previews" })).toBeNull();
    expect(sent.some((s) => s.url.includes("/previews"))).toBe(false);
  });
});
