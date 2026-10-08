import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import FilesSection from "./FilesSection";

const fetchMock = vi.fn();
let role = "admin";
let followMoves: boolean | undefined = true;
let trashDays: number | null | undefined = 30;
const patches: unknown[] = [];

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/v1/me")) return new Response(JSON.stringify({ email: "a@b.c", role }), { status: 200 });
    if (url.endsWith("/v1/tenant/settings") && init?.method === "PATCH") {
      const body = JSON.parse(String(init.body));
      patches.push(body);
      if ("follow_moves" in body) followMoves = body.follow_moves;
      if ("trash_days" in body) trashDays = body.trash_days;
    }
    if (url.endsWith("/v1/tenant/settings")) {
      const settings: Record<string, unknown> = { video_preview_max_seconds: null, public_video_preview_max_seconds: 10 };
      if (followMoves !== undefined) settings.follow_moves = followMoves;
      if (trashDays !== undefined) settings.trash_days = trashDays;
      return new Response(JSON.stringify(settings), { status: 200 });
    }
    return new Response("{}", { status: 404 });
  });
});

afterEach(() => {
  cleanup();
  fetchMock.mockReset();
  vi.unstubAllGlobals();
  role = "admin";
  followMoves = true;
  trashDays = 30;
  patches.length = 0;
});

const moves = () => screen.getByRole("form", { name: "Moves and renames" });
const trash = () => screen.getByRole("form", { name: "Trash" });

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <FilesSection />
    </QueryClientProvider>,
  );
}

describe("FilesSection: follow moves and renames", () => {
  it("is on by default and says what that means", async () => {
    renderSection();
    const on = await screen.findByRole("radio", { name: /Follow moves and renames/ });
    expect((on as HTMLInputElement).checked).toBe(true);
    expect(screen.getByText(/keeps its notes, ratings and projects/)).toBeTruthy();
  });

  it("an older server that doesn't say counts as on", async () => {
    followMoves = undefined;
    renderSection();
    const on = await screen.findByRole("radio", { name: /Follow moves and renames/ });
    expect((on as HTMLInputElement).checked).toBe(true);
  });

  it("an admin turns it off, and only the setting is sent", async () => {
    renderSection();
    fireEvent.click(await screen.findByRole("radio", { name: /Every path is its own file/ }));
    expect(screen.getByText(/starts fresh/)).toBeTruthy();
    fireEvent.click(within(moves()).getByRole("button", { name: "Save" }));
    await within(moves()).findByText("Saved");
    expect(patches).toEqual([{ follow_moves: false }]);
  });

  it("names each choice by its title and describes it with its explanation", async () => {
    renderSection();
    const off = await screen.findByRole("radio", { name: "Every path is its own file" });
    expect(off.getAttribute("aria-describedby")).toBe("follow-moves-false");
    expect(document.getElementById("follow-moves-false")?.textContent).toMatch(/starts fresh/);
  });

  it("Save waits for a change", async () => {
    renderSection();
    await screen.findByRole("radio", { name: /Follow moves and renames/ });
    expect((within(moves()).getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("people who aren't admins see it but can't change it", async () => {
    role = "editor";
    followMoves = false;
    renderSection();
    await screen.findByText(/Every path is its own file/);
    expect(screen.queryByRole("radio")).toBeNull();
    expect(screen.getAllByText(/Only admins can change this/)).toHaveLength(2);
  });

  it("says when saving failed", async () => {
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/v1/me")) return new Response(JSON.stringify({ email: "a@b.c", role }), { status: 200 });
      if (init?.method === "PATCH") return new Response(JSON.stringify({ error: { code: "x", message: "no" } }), { status: 500 });
      return new Response(JSON.stringify({ follow_moves: true }), { status: 200 });
    });
    renderSection();
    fireEvent.click(await screen.findByRole("radio", { name: /Every path is its own file/ }));
    fireEvent.click(within(moves()).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(within(moves()).getByText(/Couldn't save/)).toBeTruthy());
  });
});

describe("FilesSection: the trash", () => {
  it("deletes for good after 30 days unless changed", async () => {
    renderSection();
    const auto = await screen.findByRole("radio", { name: /Delete for good after/ });
    expect((auto as HTMLInputElement).checked).toBe(true);
    expect((within(trash()).getByRole("textbox", { name: "Days in the trash" }) as HTMLInputElement).value).toBe("30");
    expect((within(trash()).getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("an older server that doesn't say keeps 30 days", async () => {
    trashDays = undefined;
    renderSection();
    await screen.findByRole("radio", { name: /Delete for good after/ });
    expect((within(trash()).getByRole("textbox", { name: "Days in the trash" }) as HTMLInputElement).value).toBe("30");
  });

  it("an admin sets the days, and only that is sent", async () => {
    renderSection();
    await screen.findByRole("radio", { name: /Delete for good after/ });
    fireEvent.change(within(trash()).getByRole("textbox", { name: "Days in the trash" }), { target: { value: "7" } });
    fireEvent.click(within(trash()).getByRole("button", { name: "Save" }));
    await within(trash()).findByText("Saved");
    expect(patches).toEqual([{ trash_days: 7 }]);
  });

  it("an admin turns it off: the trash is emptied by hand only", async () => {
    renderSection();
    fireEvent.click(await screen.findByRole("radio", { name: /Only when emptied by hand/ }));
    expect((within(trash()).getByRole("textbox", { name: "Days in the trash" }) as HTMLInputElement).disabled).toBe(true);
    fireEvent.click(within(trash()).getByRole("button", { name: "Save" }));
    await within(trash()).findByText("Saved");
    expect(patches).toEqual([{ trash_days: null }]);
  });

  it.each(["0", "3651", "2.5", "ten", "-1", ""])("refuses %j days before asking the server", async (bad) => {
    renderSection();
    await screen.findByRole("radio", { name: /Delete for good after/ });
    fireEvent.change(within(trash()).getByRole("textbox", { name: "Days in the trash" }), { target: { value: bad } });
    expect(within(trash()).getByRole("alert").textContent).toMatch(/whole number of days from 1 to 3,650/);
    expect((within(trash()).getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
    expect(patches).toEqual([]);
  });

  it("asks before fewer days delete things at once, and saves only when told to", async () => {
    let asked = false;
    const base = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "PATCH") {
        const body = JSON.parse(String(init.body));
        if (!body.confirm_purge) {
          asked = true;
          patches.push(body);
          return new Response(JSON.stringify({ error: { code: "trash_days_shortened", message: "m",
            details: { trash_days: 7, clips: 12, libraries: 1, projects: 0 } } }), { status: 409 });
        }
      }
      return base(url, init);
    });
    renderSection();
    await screen.findByRole("radio", { name: /Delete for good after/ });
    fireEvent.change(within(trash()).getByRole("textbox", { name: "Days in the trash" }), { target: { value: "7" } });
    fireEvent.click(within(trash()).getByRole("button", { name: "Save" }));
    const ask = await within(trash()).findByRole("alertdialog", { name: "Delete them now?" });
    expect(asked).toBe(true);
    expect(ask.textContent).toMatch(/12 clips, 1 library have been in the trash longer than 7 days, and would be deleted for good within minutes\./);
    expect(within(trash()).queryByText(/Couldn't save/)).toBeNull();
    fireEvent.click(within(ask).getByRole("button", { name: "Save and delete them" }));
    await within(trash()).findByText("Saved");
    expect(patches).toEqual([{ trash_days: 7 }, { trash_days: 7, confirm_purge: true }]);
  });

  it("people who aren't admins see how long, but can't change it", async () => {
    role = "editor";
    trashDays = null;
    renderSection();
    expect(await screen.findByText("Kept until someone deletes it for good.")).toBeTruthy();
    expect(screen.queryByRole("textbox")).toBeNull();
  });
});
