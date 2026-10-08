import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import FilesSection from "./FilesSection";

const fetchMock = vi.fn();
let role = "admin";
let followMoves: boolean | undefined = true;
const patches: unknown[] = [];

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/v1/me")) return new Response(JSON.stringify({ email: "a@b.c", role }), { status: 200 });
    if (url.endsWith("/v1/tenant/settings") && init?.method === "PATCH") {
      const body = JSON.parse(String(init.body));
      patches.push(body);
      if ("follow_moves" in body) followMoves = body.follow_moves;
    }
    if (url.endsWith("/v1/tenant/settings")) {
      const settings: Record<string, unknown> = { video_preview_max_seconds: null, public_video_preview_max_seconds: 10 };
      if (followMoves !== undefined) settings.follow_moves = followMoves;
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
  patches.length = 0;
});

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
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText("Saved");
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
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("people who aren't admins see it but can't change it", async () => {
    role = "editor";
    followMoves = false;
    renderSection();
    await screen.findByText(/Every path is its own file/);
    expect(screen.queryByRole("radio")).toBeNull();
    expect(screen.getByText(/Only admins can change this/)).toBeTruthy();
  });

  it("says when saving failed", async () => {
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/v1/me")) return new Response(JSON.stringify({ email: "a@b.c", role }), { status: 200 });
      if (init?.method === "PATCH") return new Response(JSON.stringify({ error: { code: "x", message: "no" } }), { status: 500 });
      return new Response(JSON.stringify({ follow_moves: true }), { status: 200 });
    });
    renderSection();
    fireEvent.click(await screen.findByRole("radio", { name: /Every path is its own file/ }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.getByText(/Couldn't save/)).toBeTruthy());
  });
});
