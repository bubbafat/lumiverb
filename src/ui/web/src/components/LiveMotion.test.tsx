import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { LiveMotion } from "./LiveMotion";

const api = vi.hoisted(() => ({ getPlayback: vi.fn() }));
vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  ...api,
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderIt(props: Partial<Parameters<typeof LiveMotion>[0]> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <LiveMotion assetId="ast_photo" {...props} />
    </QueryClientProvider>,
  );
}

const LINK = {
  url: "/v1/stream/tok", expires_at: "2026-10-21T12:00:00Z", source: "lumiverb.video.preview",
  max_seconds: null, ready: true, whole: true,
};

describe("LiveMotion", () => {
  it("plays the photo's own playback over it on LIVE, and the still is back when it ends", async () => {
    api.getPlayback.mockResolvedValue(LINK);
    renderIt();
    const live = screen.getByRole("button", { name: "LIVE" }) as HTMLButtonElement;
    await waitFor(() => expect(live.disabled).toBe(false));
    expect(api.getPlayback).toHaveBeenCalledWith("ast_photo", undefined, undefined);
    expect(screen.queryByLabelText("Live Photo")).toBeNull();

    fireEvent.click(live);
    const video = screen.getByLabelText("Live Photo") as HTMLVideoElement;
    expect(video.getAttribute("src")).toBe("/v1/stream/tok");
    expect(live.getAttribute("aria-pressed")).toBe("true");
    fireEvent.ended(video);
    expect(screen.queryByLabelText("Live Photo")).toBeNull();
  });

  it("asks as a public page's visitor, for the page they're on", async () => {
    api.getPlayback.mockResolvedValue(LINK);
    renderIt({ isPublic: true, publicProjectId: "prj_1" });
    await waitFor(() => expect(api.getPlayback).toHaveBeenCalledWith("ast_photo", undefined, "prj_1"));
  });

  it("offers nothing to play while there's no link", async () => {
    api.getPlayback.mockReturnValue(new Promise(() => {}));
    renderIt();
    expect((screen.getByRole("button", { name: "LIVE" }) as HTMLButtonElement).disabled).toBe(true);
  });
});
