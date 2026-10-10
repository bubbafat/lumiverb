import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("../api/client", async (importOriginal) => {
  const real = await importOriginal<typeof import("../api/client")>();
  return { ...real, setLocations: vi.fn() };
});

import { ApiError, setLocations } from "../api/client";
import { SetLocationModal, parseLatLon } from "./SetLocationModal";
import { LocationRow } from "./LocationRow";

const set = vi.mocked(setLocations);

beforeEach(() => set.mockReset());
afterEach(cleanup);

function modal(ids = ["ast_1", "ast_2"]) {
  const onClose = vi.fn();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <SetLocationModal assetIds={ids} nameOf={(id) => `${id}.jpg`} onClose={onClose} />
    </QueryClientProvider>,
  );
  return onClose;
}

describe("SetLocationModal", () => {
  it("reads coordinates, and refuses 0, 0 and nonsense", () => {
    expect(parseLatLon("48.85, 2.29")).toEqual({ lat: 48.85, lon: 2.29 });
    expect(parseLatLon("-33.9 151.2")).toEqual({ lat: -33.9, lon: 151.2 });
    expect(parseLatLon("0, 0")).toBeNull();
    expect(parseLatLon("91, 0")).toBeNull();
    expect(parseLatLon("paris")).toBeNull();
  });

  it("asks before replacing, then sends the choice", async () => {
    set.mockRejectedValueOnce(new ApiError(409, "exists", "location_exists", { count: 1, person: 0, file: 1 }));
    set.mockResolvedValueOnce({ updated: ["ast_1", "ast_2"], skipped: [] });
    const onClose = modal();
    fireEvent.change(screen.getByLabelText("Latitude, longitude"), { target: { value: "1.5, 2.5" } });
    fireEvent.click(screen.getByRole("button", { name: "Set location" }));
    await screen.findByText(/1 clip already has a location from the file/);
    expect(set).toHaveBeenLastCalledWith({ asset_ids: ["ast_1", "ast_2"], replace: "none", lat: 1.5, lon: 2.5 });
    fireEvent.click(screen.getByRole("button", { name: "Replace 1" }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(set).toHaveBeenLastCalledWith({ asset_ids: ["ast_1", "ast_2"], replace: "all", lat: 1.5, lon: 2.5 });
  });

  it("sets the same place as one of the selected clips", async () => {
    set.mockResolvedValueOnce({ updated: ["ast_1"], skipped: ["ast_2"] });
    modal();
    fireEvent.click(screen.getByRole("radio", { name: "Same place as" }));
    fireEvent.change(screen.getByRole("combobox", { name: "Same place as" }), { target: { value: "ast_2" } });
    fireEvent.click(screen.getByRole("button", { name: "Set location" }));
    await waitFor(() =>
      expect(set).toHaveBeenCalledWith({ asset_ids: ["ast_1", "ast_2"], replace: "none", same_as: "ast_2" }));
  });
});

describe("LocationRow", () => {
  const person = { lat: 1, lon: 2, radius_m: 0, source: "person" as const, status: "applied" as const, set_by: "rob@x.io" };

  it("shows a person's location, who set it, and the file's", () => {
    render(<LocationRow gpsLat={40} gpsLon={-74} location={person} />);
    expect(screen.getByText("Location")).toBeTruthy();
    expect(screen.getByText("1.00000, 2.00000")).toBeTruthy();
    expect(screen.getByText(/Set by rob@x.io/)).toBeTruthy();
    expect(screen.getByText("From the file: 40.00000, -74.00000")).toBeTruthy();
  });

  it("gives viewers no edit controls", () => {
    render(<LocationRow gpsLat={null} gpsLon={null} location={person} onClear={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "Clear" })).toBeNull();
    cleanup();
    render(<LocationRow gpsLat={null} gpsLon={null} location={person} canEdit onClear={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Clear" })).toBeTruthy();
  });

  it("shows a guess's coordinates, Guess opens how it was made, and a suggestion to take", () => {
    render(<LocationRow gpsLat={null} gpsLon={null}
      location={{ lat: 1, lon: 2, radius_m: 20_000, source: "time", status: "applied", basis_summary: "from phone photos" }} />);
    expect(screen.getByText("1.00000, 2.00000")).toBeTruthy();
    expect(screen.queryByText(/About 20 km/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Guess" }));
    expect(screen.getByText("About 20 km · from phone photos")).toBeTruthy();
    cleanup();
    const onAccept = vi.fn();
    render(<LocationRow gpsLat={null} gpsLon={null} canEdit onAccept={onAccept}
      location={{ lat: 1, lon: 2, radius_m: 5000, source: "suggestion", status: "suggested", basis_summary: "Paris" }} />);
    expect(screen.getByText(/Maybe: Paris/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Use this" }));
    expect(onAccept).toHaveBeenCalled();
  });

  it("says when a guess's source was removed", () => {
    render(<LocationRow gpsLat={null} gpsLon={null}
      location={{ lat: 1, lon: 2, radius_m: 8100, source: "time", status: "applied",
        basis_summary: "From iPhone 15 Pro photos at 10:00", basis_gone: true }} />);
    fireEvent.click(screen.getByRole("button", { name: "Guess" }));
    expect(screen.getByText("About 8 km · From iPhone 15 Pro photos at 10:00 · source removed")).toBeTruthy();
    cleanup();
    render(<LocationRow gpsLat={null} gpsLon={null}
      location={{ lat: 1, lon: 2, radius_m: 8100, source: "time", status: "applied",
        basis_summary: "From iPhone 15 Pro photos at 10:00", basis_gone: false }} />);
    fireEvent.click(screen.getByRole("button", { name: "Guess" }));
    expect(screen.queryByText(/source removed/)).toBeNull();
  });

  it("shows nothing without any location", () => {
    const { container } = render(<LocationRow gpsLat={null} gpsLon={null} location={null} />);
    expect(container.textContent).toBe("");
  });
});
