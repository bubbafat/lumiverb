import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import PlaybackSection from "./PlaybackSection";

const fetchMock = vi.fn();
let role = "admin";
let cap: number | null = null;
let publicCap: number | null = 10;

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/v1/me")) return new Response(JSON.stringify({ email: "a@b.c", role }), { status: 200 });
    if (url.endsWith("/v1/tenant/settings") && init?.method === "PATCH") {
      const body = JSON.parse(String(init.body));
      if ("video_preview_max_seconds" in body) cap = body.video_preview_max_seconds;
      if ("public_video_preview_max_seconds" in body) publicCap = body.public_video_preview_max_seconds;
    }
    if (url.endsWith("/v1/tenant/settings")) {
      return new Response(
        JSON.stringify({ video_preview_max_seconds: cap, public_video_preview_max_seconds: publicCap }),
        { status: 200 },
      );
    }
    return new Response("{}", { status: 404 });
  });
});

afterEach(() => {
  cleanup();
  fetchMock.mockReset();
  vi.unstubAllGlobals();
  role = "admin";
  cap = null;
  publicCap = 10;
});

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <PlaybackSection />
    </QueryClientProvider>,
  );
}

function patches() {
  return fetchMock.mock.calls
    .filter(([, init]) => (init as RequestInit | undefined)?.method === "PATCH")
    .map(([, init]) => JSON.parse(String((init as RequestInit).body)));
}

describe("PlaybackSection", () => {
  it("shows full length signed in and 10 seconds on public pages by default", async () => {
    renderSection();
    const full = await screen.findByLabelText("Whole video");
    expect((full as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText(/Public pages: first/) as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText("Public seconds") as HTMLInputElement).value).toBe("10");
  });

  it("lets an admin raise or lift the public cap", async () => {
    renderSection();
    await screen.findByLabelText("Whole video");
    fireEvent.change(screen.getByLabelText("Public seconds"), { target: { value: "60" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(patches()).toEqual([{ video_preview_max_seconds: null, public_video_preview_max_seconds: 60 }]));
    fireEvent.click(screen.getByLabelText("Public pages: whole video"));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(patches()[1]).toEqual({ video_preview_max_seconds: null, public_video_preview_max_seconds: null }));
  });

  it("lets an admin cap playback and go back to full length", async () => {
    renderSection();
    fireEvent.click(await screen.findByLabelText(/First/));
    fireEvent.change(screen.getByLabelText("Seconds"), { target: { value: "30" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(patches()).toEqual([{ video_preview_max_seconds: 30, public_video_preview_max_seconds: 10 }]));
    await screen.findByText("Saved");

    fireEvent.click(screen.getByLabelText("Whole video"));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(patches()[1]).toEqual({ video_preview_max_seconds: null, public_video_preview_max_seconds: 10 }));
  });

  it("won't save a cap that isn't a whole number of seconds", async () => {
    renderSection();
    fireEvent.click(await screen.findByLabelText(/First/));
    for (const bad of ["", "0", "-5", "2.5", "90000"]) {
      fireEvent.change(screen.getByLabelText("Seconds"), { target: { value: bad } });
      expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
    }
    expect(patches()).toEqual([]);
  });

  it("shows an existing cap", async () => {
    cap = 45;
    renderSection();
    // Right from the first frame: no flash of the defaults.
    expect(((await screen.findByLabelText(/First/)) as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText("Seconds") as HTMLInputElement).value).toBe("45");
  });

  it("shows non-admins what public pages actually get", async () => {
    role = "viewer";
    cap = 5;
    publicCap = 10;
    renderSection();
    await screen.findByText("Public pages: first 5 seconds");
  });

  it("is read-only for anyone but admins", async () => {
    role = "editor";
    cap = 20;
    renderSection();
    await screen.findByText("Signed in: first 20 seconds");
    screen.getByText("Public pages: first 10 seconds");
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
    expect(screen.queryByLabelText("Seconds")).toBeNull();
    screen.getByText(/Only admins can change this/);
  });
});
