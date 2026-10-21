import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import { useClipActions } from "./useClipActions";
import { useRatings } from "./useRatings";

const api = vi.hoisted(() => ({
  lookupRatings: vi.fn(),
  rateAsset: vi.fn(),
  batchRateAssets: vi.fn(),
  getTenantSettings: vi.fn(),
}));
vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  ...api,
}));

const IDS = ["a1", "a2", "a3"];

/** A page's use: useClipActions' notice says what failed. */
function Harness() {
  const actions = useClipActions();
  const { ratingsMap, rate, rateSelection } = useRatings(IDS, true, actions.fail);
  return (
    <div>
      <p data-testid="stars">{String(ratingsMap.a1?.stars ?? "none")}</p>
      <button type="button" onClick={() => void rate("a1", { stars: 2 })}>two</button>
      <button type="button" onClick={() => void rate("a1", { stars: 4 })}>four</button>
      <button type="button" onClick={() => void rateSelection(IDS, { favorite: true })}>favorite all</button>
      {actions.ui}
    </div>
  );
}

function renderHarness() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Harness />
    </QueryClientProvider>,
  );
}

const stars = () => screen.getByTestId("stars").textContent;

beforeEach(() => {
  api.getTenantSettings.mockResolvedValue({ trash_days: 30 });
  api.lookupRatings.mockResolvedValue({ ratings: { a1: { favorite: false, stars: 1, color: null } } });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("useRatings", () => {
  it("shows a rating at once and keeps it once saved, saying nothing", async () => {
    api.rateAsset.mockResolvedValue({});
    renderHarness();
    await waitFor(() => expect(stars()).toBe("1"));
    fireEvent.click(screen.getByRole("button", { name: "two" }));
    await waitFor(() => expect(stars()).toBe("2"));
    expect(api.rateAsset).toHaveBeenCalledWith("a1", { stars: 2 });
    await new Promise((r) => setTimeout(r, 10));
    expect(stars()).toBe("2");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("puts a rating that wasn't saved back and says so, even when the server can't be reached at all", async () => {
    // A phone's browser drops the request; asking the server again would fail too.
    api.rateAsset.mockRejectedValue(new TypeError("Load failed"));
    renderHarness();
    await waitFor(() => expect(stars()).toBe("1"));
    api.lookupRatings.mockRejectedValue(new TypeError("Load failed"));
    fireEvent.click(screen.getByRole("button", { name: "two" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Couldn't save the rating: Load failed");
    await waitFor(() => expect(stars()).toBe("1"));
  });

  it("never undoes a later rating of the clip when an earlier one fails", async () => {
    let refuse!: (err: unknown) => void;
    api.rateAsset
      .mockReturnValueOnce(new Promise((_, reject) => { refuse = reject; }))
      .mockResolvedValueOnce({});
    renderHarness();
    await waitFor(() => expect(stars()).toBe("1"));
    fireEvent.click(screen.getByRole("button", { name: "two" }));
    fireEvent.click(screen.getByRole("button", { name: "four" }));
    await waitFor(() => expect(stars()).toBe("4"));
    refuse(new ApiError(503, "The server is busy."));
    expect((await screen.findByRole("alert")).textContent).toContain("Couldn't save the rating: The server is busy.");
    await new Promise((r) => setTimeout(r, 10));
    expect(stars()).toBe("4");
  });

  it("says when rating a selection fails, and how many clips it was", async () => {
    api.batchRateAssets.mockRejectedValue(new TypeError("Load failed"));
    renderHarness();
    fireEvent.click(screen.getByRole("button", { name: "favorite all" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Couldn't rate 3 clips: Load failed");
    expect(api.batchRateAssets).toHaveBeenCalledWith(IDS, { favorite: true });
  });

  it("asks for the ratings again once a selection is rated", async () => {
    api.batchRateAssets.mockResolvedValue({ updated: 3 });
    renderHarness();
    await waitFor(() => expect(api.lookupRatings).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "favorite all" }));
    await waitFor(() => expect(api.lookupRatings).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
