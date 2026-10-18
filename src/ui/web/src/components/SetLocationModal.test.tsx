import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("../api/client", async (importOriginal) => {
  const real = await importOriginal<typeof import("../api/client")>();
  return { ...real, setLocations: vi.fn(), listPlugins: vi.fn(), searchPlaces: vi.fn() };
});

import { ApiError, listPlugins, searchPlaces, setLocations } from "../api/client";
import { SetLocationModal, parseLatLon } from "./SetLocationModal";
import { LocationRow } from "./LocationRow";

const set = vi.mocked(setLocations);
const plugins = vi.mocked(listPlugins);
const search = vi.mocked(searchPlaces);

const GEONAMES = {
  plugin_id: "geonames", title: "Place names (GeoNames)", description: "", state: "on" as const,
  acknowledgment: null, acknowledged: null,
  data: { title: "The place list", unit: "places", state: "ready" as const, count: 171329 },
};
const PORT_OF_SPAIN = { place_id: "geonames:3573890", name: "Port of Spain", region: "Port of Spain",
                        country: "Trinidad and Tobago", label: "Port of Spain, Trinidad and Tobago",
                        lat: 10.66668, lon: -61.51889, population: 49031 };
const CREDIT = { text: "Place names from GeoNames, CC BY 4.0", url: "https://www.geonames.org/",
                 license_url: "https://creativecommons.org/licenses/by/4.0/" };

beforeEach(() => {
  set.mockReset();
  plugins.mockReset().mockResolvedValue([]);
  search.mockReset();
});
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

  it("finds a place by name when Place names is on, and sets it by its id", async () => {
    plugins.mockResolvedValue([GEONAMES]);
    search.mockResolvedValue({ places: [PORT_OF_SPAIN], credit: CREDIT });
    set.mockResolvedValueOnce({ updated: ["ast_1", "ast_2"], skipped: [] });
    const onClose = modal();
    fireEvent.change(await screen.findByRole("textbox", { name: "Place" }), { target: { value: "port of" } });
    fireEvent.click(await screen.findByRole("button", { name: "Port of Spain, Trinidad and Tobago" }));
    expect(search).toHaveBeenCalledWith("port of");
    expect((screen.getByRole("textbox", { name: "Place" }) as HTMLInputElement).value)
      .toBe("Port of Spain, Trinidad and Tobago");
    expect((screen.getByRole("link", { name: "CC BY 4.0" }) as HTMLAnchorElement).href)
      .toBe("https://creativecommons.org/licenses/by/4.0/");
    fireEvent.click(screen.getByRole("button", { name: "Set location" }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(set).toHaveBeenCalledWith({ asset_ids: ["ast_1", "ast_2"], replace: "none", place_id: "geonames:3573890" });
  });

  it("offers no place search while Place names is off or its list isn't fetched", async () => {
    plugins.mockResolvedValue([{ ...GEONAMES, data: { ...GEONAMES.data, state: "fetching" as const } }]);
    modal();
    await waitFor(() => expect(plugins).toHaveBeenCalled());
    expect(screen.queryByRole("radio", { name: "Place" })).toBeNull();
    expect(screen.getByLabelText("Latitude, longitude")).toBeTruthy();
    expect(search).not.toHaveBeenCalled();
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

  it("credits a place's name where it shows", () => {
    render(<LocationRow gpsLat={null} gpsLon={null} location={{
      ...person, basis_summary: "Port of Spain, Trinidad and Tobago",
      credit: { text: "GeoNames", url: "https://www.geonames.org/" } }} />);
    expect(screen.getByText(/Port of Spain, Trinidad and Tobago/)).toBeTruthy();
    expect((screen.getByRole("link", { name: "GeoNames" }) as HTMLAnchorElement).href).toBe("https://www.geonames.org/");
  });

  it("says the nearest town to where it was, credited, or that there's none near", () => {
    const place = { label: "Port of Spain, Trinidad and Tobago", place_id: "geonames:3573890", distance_m: 1200,
                    credit: { text: "GeoNames", url: "https://www.geonames.org/" } };
    render(<LocationRow gpsLat={10.66} gpsLon={-61.52} location={null} place={place} />);
    expect(screen.getByText(/Near Port of Spain, Trinidad and Tobago/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "GeoNames" })).toBeTruthy();
    cleanup();
    render(<LocationRow gpsLat={20} gpsLon={-40} location={null} place={{ ...place, label: null, place_id: null }} />);
    expect(screen.getByText(/Not near a town/)).toBeTruthy();
    cleanup();
    // Picked by name: said once.
    render(<LocationRow gpsLat={null} gpsLon={null} place={place} location={{
      ...person, basis_summary: place.label, credit: place.credit }} />);
    expect(screen.queryByText(/Near Port of Spain/)).toBeNull();
    expect(screen.getAllByRole("link", { name: "GeoNames" })).toHaveLength(1);
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

  it("says when a guess's fixes are outside the window", () => {
    render(<LocationRow gpsLat={null} gpsLon={null}
      location={{ lat: 1, lon: 2, radius_m: 8100, source: "time", status: "applied",
        basis_summary: "From iPhone 15 Pro photos at 10:00", outside_window: true }} />);
    fireEvent.click(screen.getByRole("button", { name: "Guess" }));
    expect(screen.getByText("About 8 km · From iPhone 15 Pro photos at 10:00 · outside window")).toBeTruthy();
  });

  it("shows nothing without any location", () => {
    const { container } = render(<LocationRow gpsLat={null} gpsLon={null} location={null} />);
    expect(container.textContent).toBe("");
  });
});
