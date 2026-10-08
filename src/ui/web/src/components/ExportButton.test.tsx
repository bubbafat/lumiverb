import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ExportButton } from "./ExportButton";

const api = vi.hoisted(() => ({
  listExportFormats: vi.fn(),
  exportProject: vi.fn(),
  getDefaultExportFormat: vi.fn(),
  getDefaultExportPrefix: vi.fn(),
  setDefaultExportFormat: vi.fn(),
}));
vi.mock("../api/client", () => api);

function renderButton() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ExportButton projectId="prj_1" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  api.listExportFormats.mockResolvedValue([
    { id: "fcp7", label: "DaVinci Resolve / Premiere Pro", file_extension: ".xml" },
    { id: "fcpxml", label: "Final Cut Pro", file_extension: ".fcpxml" },
  ]);
  api.exportProject.mockResolvedValue({
    blob: new Blob(["<x/>"]),
    filename: "Job.fcpxml",
    skippedStills: 0,
    skippedNoDuration: 0,
    unprobed: 0,
  });
  api.getDefaultExportPrefix.mockReturnValue(null);
  URL.createObjectURL = vi.fn(() => "blob:fake");
  URL.revokeObjectURL = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ExportButton", () => {
  it("asks for a format the first time and can make it the default", async () => {
    api.getDefaultExportFormat.mockReturnValue(null);
    renderButton();

    fireEvent.click(screen.getByRole("button", { name: "Export" }));
    fireEvent.click(await screen.findByLabelText("Final Cut Pro"));
    fireEvent.click(screen.getByLabelText("Make default"));
    fireEvent.click(screen.getByRole("button", { name: "Export file" }));

    await waitFor(() => expect(api.exportProject).toHaveBeenCalledWith("prj_1", "fcpxml", undefined));
    expect(api.setDefaultExportFormat).toHaveBeenCalledWith("fcpxml", undefined);
  });

  it("exports straight away with a default format", async () => {
    api.getDefaultExportFormat.mockReturnValue("fcp7");
    renderButton();

    fireEvent.click(screen.getByRole("button", { name: "Export" }));

    await waitFor(() => expect(api.exportProject).toHaveBeenCalledWith("prj_1", "fcp7", undefined));
    expect(screen.queryByLabelText("Make default")).toBeNull();
  });

  it("the menu arrow always opens the chooser, with a media location", async () => {
    api.getDefaultExportFormat.mockReturnValue("fcp7");
    renderButton();

    fireEvent.click(screen.getByRole("button", { name: "Export options" }));
    fireEvent.change(await screen.findByLabelText("Media location"), {
      target: { value: "/Volumes/Travel SSD" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Export file" }));

    await waitFor(() =>
      expect(api.exportProject).toHaveBeenCalledWith("prj_1", "fcp7", "/Volumes/Travel SSD"),
    );
    expect(api.setDefaultExportFormat).not.toHaveBeenCalled();
  });

  it("says when photos were left out", async () => {
    api.getDefaultExportFormat.mockReturnValue("fcp7");
    api.exportProject.mockResolvedValue({
      blob: new Blob(["x"]), filename: "a.xml", skippedStills: 3, skippedNoDuration: 0, unprobed: 0,
    });
    renderButton();

    fireEvent.click(screen.getByRole("button", { name: "Export" }));

    expect(await screen.findByText(/3 photos weren't included/)).toBeTruthy();
  });
});


describe("ExportButton remembered location and reports", () => {
  it("one click uses the remembered media location", async () => {
    api.getDefaultExportFormat.mockReturnValue("fcpxml");
    api.getDefaultExportPrefix.mockReturnValue("/Volumes/DAS");
    renderButton();

    fireEvent.click(screen.getByRole("button", { name: "Export" }));

    await waitFor(() =>
      expect(api.exportProject).toHaveBeenCalledWith("prj_1", "fcpxml", "/Volumes/DAS"),
    );
  });

  it("make default saves the location too", async () => {
    api.getDefaultExportFormat.mockReturnValue(null);
    renderButton();

    fireEvent.click(screen.getByRole("button", { name: "Export" }));
    fireEvent.click(await screen.findByLabelText("Final Cut Pro"));
    fireEvent.change(screen.getByLabelText("Media location"), { target: { value: "/Volumes/DAS" } });
    fireEvent.click(screen.getByLabelText("Make default"));
    fireEvent.click(screen.getByRole("button", { name: "Export file" }));

    await waitFor(() => expect(api.setDefaultExportFormat).toHaveBeenCalledWith("fcpxml", "/Volumes/DAS"));
  });

  it("says when clips were left out or exported at a fallback rate", async () => {
    api.getDefaultExportFormat.mockReturnValue("fcp7");
    api.exportProject.mockResolvedValue({
      blob: new Blob(["x"]), filename: "a.xml", skippedStills: 0, skippedNoDuration: 2, unprobed: 5,
    });
    renderButton();

    fireEvent.click(screen.getByRole("button", { name: "Export" }));

    expect(await screen.findByText(/2 videos with no known length weren't included/)).toBeTruthy();
    expect(screen.getByText(/5 videos haven't been probed/)).toBeTruthy();
  });

  it("says when trashed clips or clips with missing files were left out", async () => {
    api.getDefaultExportFormat.mockReturnValue("fcp7");
    api.exportProject.mockResolvedValue({
      blob: new Blob(["x"]), filename: "a.xml", skippedStills: 0, skippedNoDuration: 0, unprobed: 0,
      skippedTrashed: 2, skippedMissing: 1,
    });
    renderButton();

    fireEvent.click(screen.getByRole("button", { name: "Export" }));

    expect(await screen.findByText(/2 clips in the trash weren't included/)).toBeTruthy();
    expect(screen.getByText(/1 clip missing from disk wasn't included/)).toBeTruthy();
  });

  it("frees the download link only after the browser has it", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    api.getDefaultExportFormat.mockReturnValue("fcp7");
    renderButton();

    fireEvent.click(screen.getByRole("button", { name: "Export" }));
    await waitFor(() => expect(URL.createObjectURL).toHaveBeenCalled());
    expect(URL.revokeObjectURL).not.toHaveBeenCalled();
    vi.advanceTimersByTime(60_000);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:fake");
    vi.useRealTimers();
  });
});
