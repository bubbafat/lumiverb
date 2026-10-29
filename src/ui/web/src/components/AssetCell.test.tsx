import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { AssetCell } from "./AssetCell";
import type { AssetPageItem, SearchContext } from "../api/types";

vi.mock("../api/useAuthenticatedImage", () => ({
  useAuthenticatedImage: () => ({ url: "blob:x", isLoading: false, error: null, generating: false }),
}));

afterEach(cleanup);

function clip(search_context: SearchContext | null): AssetPageItem {
  return {
    asset_id: "ast_1", rel_path: "Edinburgh/street.jpg", file_size: 1, file_mtime: null, content_hash: null,
    media_type: "image", width: 4, height: 3, taken_at: null, status: "indexed", duration_sec: null,
    camera_make: null, camera_model: null, iso: null, aperture: null, focal_length: null, focal_length_35mm: null,
    lens_model: null, flash_fired: null, gps_lat: null, gps_lon: null, created_at: null, search_context,
  };
}

const read = { score: 1, hit_type: "asset", snippet: "Dundee Diner Kitchen", start_ms: null, end_ms: null,
               matched_in: "text" };

describe("a search result's thumbnail", () => {
  it("is tagged with what found it, and says the words on hover", () => {
    render(<AssetCell asset={clip(read)} onClick={() => {}} />);
    expect(screen.getByText("Text in photo")).toBeTruthy();
    expect(screen.getByText("Text in photo: Dundee Diner Kitchen")).toBeTruthy();
  });

  it("isn't tagged when its description found it, or outside a search", () => {
    const { rerender } = render(<AssetCell asset={clip({ ...read, matched_in: "description" })} onClick={() => {}} />);
    expect(screen.queryByText(/Description/)).toBeNull();
    rerender(<AssetCell asset={clip(null)} onClick={() => {}} />);
    expect(screen.queryByText(/Text in photo/)).toBeNull();
  });
});
