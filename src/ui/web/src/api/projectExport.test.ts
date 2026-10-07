import { afterEach, describe, expect, it, vi } from "vitest";
import {
  exportProject,
  getDefaultExportFormat,
  getDefaultExportPrefix,
  listExportFormats,
  setDefaultExportFormat,
} from "./client";

/** Send to editor: a project exports as a file the editor imports. */

function stubFetch(body: BodyInit, headers: Record<string, string>) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      calls.push(url);
      return new Response(body, { status: 200, headers });
    }),
  );
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
  try {
    localStorage.clear();
  } catch {
    /* storage unavailable */
  }
});

describe("exportProject", () => {
  it("requests the format and returns the file with its name", async () => {
    const calls = stubFetch("<xmeml/>", {
      "Content-Type": "application/xml",
      "Content-Disposition": `attachment; filename="Customer Video 123.xml"; filename*=UTF-8''Customer%20Video%20123.xml`,
      "X-Lumiverb-Skipped-Stills": "2",
      "X-Lumiverb-Skipped-No-Duration": "1",
      "X-Lumiverb-Unprobed": "4",
    });

    const result = await exportProject("prj_1", "fcp7");

    expect(calls[0]).toBe("/v1/projects/prj_1/export?format=fcp7");
    expect(result.filename).toBe("Customer Video 123.xml");
    expect(result.skippedStills).toBe(2);
    expect(result.skippedNoDuration).toBe(1);
    expect(result.unprobed).toBe(4);
    expect(await result.blob.text()).toBe("<xmeml/>");
  });

  it("passes a media location prefix", async () => {
    const calls = stubFetch("x", { "Content-Disposition": 'attachment; filename="a.fcpxml"' });

    await exportProject("prj_1", "fcpxml", "/Volumes/Travel SSD");

    expect(calls[0]).toBe(
      "/v1/projects/prj_1/export?format=fcpxml&prefix=%2FVolumes%2FTravel+SSD",
    );
  });

  it("falls back to a generic name without a header", async () => {
    stubFetch("x", {});
    const result = await exportProject("prj_1", "fcp7");
    expect(result.filename).toBe("project-export");
    expect(result.skippedStills).toBe(0);
  });
});

describe("listExportFormats", () => {
  it("returns the server's formats", async () => {
    stubFetch(JSON.stringify({ items: [{ id: "fcp7", label: "DaVinci Resolve / Premiere Pro", file_extension: ".xml" }] }), {
      "Content-Type": "application/json",
    });
    const formats = await listExportFormats();
    expect(formats.map((f) => f.id)).toEqual(["fcp7"]);
  });
});

describe("default export format", () => {
  it("starts empty and remembers a choice", () => {
    expect(getDefaultExportFormat()).toBeNull();
    setDefaultExportFormat("fcpxml");
    expect(getDefaultExportFormat()).toBe("fcpxml");
    setDefaultExportFormat(null);
    expect(getDefaultExportFormat()).toBeNull();
  });

  it("remembers the media location with it", () => {
    setDefaultExportFormat("fcp7", "/Volumes/DAS");
    expect(getDefaultExportPrefix()).toBe("/Volumes/DAS");
    setDefaultExportFormat("fcp7");
    expect(getDefaultExportPrefix()).toBeNull();
  });
});
