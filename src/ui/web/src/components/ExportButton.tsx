import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  exportProject,
  getDefaultExportFormat,
  listExportFormats,
  setDefaultExportFormat,
} from "../api/client";

// Long enough for any browser (Safari included) to start the download.
const REVOKE_DOWNLOAD_URL_MS = 60_000;

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/**
 * Split Export button for a project (send to editor).
 *
 * "Export" uses the remembered default format, or opens the chooser the
 * first time; the arrow always opens the chooser, which can make the chosen
 * format the default. Clips point at the originals as the libraries know
 * them; the editor relinks them where its paths differ.
 */
export function ExportButton({ projectId }: { projectId: string }) {
  const [chooserOpen, setChooserOpen] = useState(false);
  const [format, setFormat] = useState<string | null>(null);
  const [makeDefault, setMakeDefault] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const { data: formats } = useQuery({
    queryKey: ["export-formats"],
    queryFn: listExportFormats,
    staleTime: Infinity,
  });

  async function run(formatId: string) {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const file = await exportProject(projectId, formatId);
      const url = URL.createObjectURL(file.blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = file.filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), REVOKE_DOWNLOAD_URL_MS);
      const notes: string[] = [];
      if (file.stills > 0) {
        notes.push(`${plural(file.stills, "photo is", "photos are")} in it as stills.`);
      }
      if (file.skippedNoDuration > 0) {
        notes.push(
          `${plural(file.skippedNoDuration, "video with no known length wasn't", "videos with no known length weren't")} included.`,
        );
      }
      if (file.skippedTrashed > 0) {
        notes.push(
          `${plural(file.skippedTrashed, "clip in the trash wasn't", "clips in the trash weren't")} included.`,
        );
      }
      if (file.skippedMissing > 0) {
        notes.push(
          `${plural(file.skippedMissing, "clip missing from disk wasn't", "clips missing from disk weren't")} included.`,
        );
      }
      if (file.skippedLibraryTrashed > 0) {
        notes.push(
          `${plural(file.skippedLibraryTrashed, "clip in a deleted library wasn't", "clips in a deleted library weren't")} included.`,
        );
      }
      if (file.skippedArchived > 0) {
        notes.push(`${plural(file.skippedArchived, "archived clip wasn't", "archived clips weren't")} included.`);
      }
      if (file.unprobed > 0) {
        notes.push(
          `${plural(file.unprobed, "video hasn't", "videos haven't")} been probed and use a default frame rate; run lumiverb enrich --job-type probe.`,
        );
      }
      if (notes.length) setMessage(notes.join(" "));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Export failed");
    } finally {
      setBusy(false);
    }
  }

  function openChooser() {
    setFormat(getDefaultExportFormat() ?? formats?.[0]?.id ?? null);
    setMakeDefault(false);
    setChooserOpen(true);
  }

  function onExportClick() {
    const remembered = getDefaultExportFormat();
    if (remembered) void run(remembered);
    else openChooser();
  }

  async function onChooserSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!format) return;
    if (makeDefault) setDefaultExportFormat(format);
    setChooserOpen(false);
    await run(format);
  }

  return (
    <div className="relative">
      <div className="flex">
        <button
          type="button"
          onClick={onExportClick}
          disabled={busy}
          className="rounded-l-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-60"
        >
          {busy ? "Exporting…" : "Export"}
        </button>
        <button
          type="button"
          onClick={openChooser}
          disabled={busy}
          aria-label="Export options"
          className="rounded-r-lg border-l border-indigo-500 bg-indigo-600 px-2 py-1.5 text-sm text-white hover:bg-indigo-500 disabled:opacity-60"
        >
          ▾
        </button>
      </div>

      {(message || error) && (
        <div
          role="status"
          className={`absolute right-0 top-full z-20 mt-2 w-72 rounded-lg border px-3 py-2 text-xs ${
            error
              ? "border-red-800/50 bg-red-950 text-red-300"
              : "border-gray-700 bg-gray-900 text-gray-300"
          }`}
        >
          {error ?? message}
          <button
            type="button"
            onClick={() => {
              setError(null);
              setMessage(null);
            }}
            className="ml-2 text-gray-500 hover:text-gray-300"
          >
            Dismiss
          </button>
        </div>
      )}

      {chooserOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <form
            onSubmit={onChooserSubmit}
            role="dialog"
            aria-label="Export project"
            className="w-full max-w-md space-y-4 rounded-xl border border-gray-700 bg-gray-900 p-5 text-gray-200"
          >
            <h2 className="text-lg font-semibold">Send to editor</h2>
            <fieldset className="space-y-2">
              <legend className="mb-1 text-sm text-gray-400">Format</legend>
              {(formats ?? []).map((f) => (
                <label key={f.id} className="flex items-center gap-2 text-sm">
                  <input
                    type="radio"
                    name="export-format"
                    value={f.id}
                    checked={format === f.id}
                    onChange={() => setFormat(f.id)}
                    aria-label={f.label}
                  />
                  <span>{f.label}</span>
                  <span className="text-xs text-gray-500">{f.file_extension}</span>
                </label>
              ))}
            </fieldset>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={makeDefault}
                onChange={(e) => setMakeDefault(e.target.checked)}
                aria-label="Make default"
              />
              Make default
            </label>
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setChooserOpen(false)}
                className="rounded-lg px-3 py-1.5 text-sm text-gray-400 hover:text-gray-200"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={!format}
                className="rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-60"
              >
                Export file
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
