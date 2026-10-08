import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { listArchive, listLibraries, unarchiveClips, type ArchivedClip } from "../api/client";
import { HiddenClipGrid } from "../components/HiddenClipGrid";
import { clipCount } from "../components/ProjectUsageList";
import { useCanEdit } from "../lib/useCanEdit";
import { useClipActions } from "../lib/useClipActions";
import { shortDate } from "../lib/format";

type Kind = "all" | "by_hand" | "missing";
const KINDS: { value: Kind; label: string }[] = [
  { value: "all", label: "All" },
  { value: "by_hand", label: "Archived by hand" },
  { value: "missing", label: "File missing" },
];

/** Clips out of sight, kept forever: archived by a person, or their file went
 * missing (those come back by themselves when the file does). */
export default function ArchivePage() {
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const libraryId = params.get("library") ?? undefined;
  const path = params.get("path") ?? undefined;
  const kind = (KINDS.find((k) => k.value === params.get("kind"))?.value ?? "all") as Kind;
  const canEdit = useCanEdit();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [notice, setNotice] = useState<{ text: string; error?: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const clipActions = useClipActions(() => setSelected(new Set()));

  const { data: libraries } = useQuery({ queryKey: ["libraries", false], queryFn: () => listLibraries(false) });
  const archive = useInfiniteQuery({
    queryKey: ["archive", libraryId ?? null, path ?? null, kind],
    queryFn: ({ pageParam }) => listArchive({ libraryId, path, kind, after: pageParam, limit: 100 }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const clips = useMemo(() => archive.data?.pages.flatMap((p) => p.items) ?? [], [archive.data]);
  const total = archive.data?.pages[0]?.total ?? 0;

  const setParam = (key: string, value: string | undefined) => {
    setSelected(new Set());
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    if (key === "library") next.delete("path");
    setParams(next, { replace: true });
  };
  const toggle = (id: string) =>
    setSelected((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const unarchive = async (pick: { asset_ids: string[] } | { library_id: string; path: string }) => {
    setBusy(true);
    try {
      const r = await unarchiveClips(pick);
      setSelected(new Set());
      void queryClient.invalidateQueries();
      const kept = r.skipped.length;
      setNotice({
        text:
          `Unarchived ${clipCount(r.unarchived.length)}.` +
          (kept
            ? ` ${clipCount(kept)} ${kept === 1 ? "wasn't" : "weren't"} archived by hand: a missing file comes back` +
              " when the file does."
            : ""),
      });
    } catch (err) {
      setNotice({ text: `Couldn't unarchive: ${err instanceof Error ? err.message : String(err)}`, error: true });
    } finally {
      setBusy(false);
    }
  };

  const meta = (clip: ArchivedClip) => (
    <span>
      {clip.file_missing ? <span className="text-amber-300/80">File missing</span> : "Archived"}{" "}
      {shortDate(new Date(clip.archived_at))}
    </span>
  );
  const allShown = clips.length > 0 && clips.every((c) => selected.has(c.asset_id));
  const folderName = path ? path.split("/").pop() : undefined;

  return (
    <div className="mx-auto max-w-6xl space-y-5 px-4 py-6 sm:px-6">
      <div>
        <h1 className="text-2xl font-semibold">Archive</h1>
        <p className="mt-1 max-w-2xl text-sm text-gray-400">
          Clips out of sight, kept forever with everything they have. Clips whose file went missing come back by
          themselves when the file does.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <div role="tablist" aria-label="Which archived clips" className="flex rounded-lg border border-gray-700 p-0.5">
          {KINDS.map((k) => (
            <button
              key={k.value}
              type="button"
              role="tab"
              aria-selected={kind === k.value}
              onClick={() => setParam("kind", k.value === "all" ? undefined : k.value)}
              className={`rounded-md px-3 py-1 text-sm ${
                kind === k.value ? "bg-indigo-600/40 text-indigo-100" : "text-gray-400 hover:text-gray-200"
              }`}
            >
              {k.label}
            </button>
          ))}
        </div>
        <label className="text-sm text-gray-400" htmlFor="archive-library">Library</label>
        <select
          id="archive-library"
          value={libraryId ?? ""}
          onChange={(e) => setParam("library", e.target.value || undefined)}
          className="rounded-lg border border-gray-700 bg-gray-800 px-2 py-1.5 text-sm text-gray-200"
        >
          <option value="">All libraries</option>
          {libraries?.map((l) => (
            <option key={l.library_id} value={l.library_id}>{l.name}</option>
          ))}
        </select>
        {path && (
          <span className="flex items-center gap-1 rounded-lg border border-gray-700 px-2 py-1 text-sm text-gray-300">
            In {path}
            <button
              type="button"
              aria-label="Show every folder"
              onClick={() => setParam("path", undefined)}
              className="px-1 text-gray-500 hover:text-gray-200"
            >
              ×
            </button>
          </span>
        )}
        <span className="text-sm text-gray-500">{clipCount(total)}</span>
        {canEdit && clips.length > 0 && (
          <button
            type="button"
            onClick={() => setSelected(allShown ? new Set() : new Set(clips.map((c) => c.asset_id)))}
            className="rounded-lg px-2 py-1 text-sm text-indigo-300 hover:bg-gray-800"
          >
            {allShown ? "Select none" : "Select all shown"}
          </button>
        )}
        {canEdit && libraryId && path && kind !== "missing" && total > 0 && (
          <button
            type="button"
            disabled={busy}
            onClick={() => void unarchive({ library_id: libraryId, path })}
            className="rounded-lg border border-gray-600 px-3 py-1.5 text-sm text-gray-200 hover:bg-gray-800 disabled:opacity-50"
          >
            Unarchive everything in {folderName}
          </button>
        )}
      </div>

      {notice && (
        <div
          role={notice.error ? "alert" : "status"}
          className={`rounded-lg border px-4 py-2 text-sm ${
            notice.error ? "border-red-800/50 bg-red-900/20 text-red-300" : "border-gray-700 bg-gray-900/60 text-gray-200"
          }`}
        >
          {notice.text}
        </div>
      )}

      {archive.isLoading ? (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
          {Array.from({ length: 8 }, (_, i) => (
            <div key={i} className="aspect-[4/3] animate-pulse rounded-lg bg-gray-800" />
          ))}
        </div>
      ) : archive.isError ? (
        <div role="alert" className="rounded-lg border border-red-800/50 bg-red-900/20 px-4 py-3 text-red-300">
          Couldn&apos;t load the archive: {(archive.error as Error).message}
        </div>
      ) : clips.length === 0 ? (
        <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-8 text-center text-gray-400">
          Nothing archived here.
        </div>
      ) : (
        <HiddenClipGrid clips={clips} selected={selected} selectable={canEdit} onToggle={toggle} meta={meta} />
      )}
      {archive.hasNextPage && (
        <div className="flex justify-center">
          <button
            type="button"
            disabled={archive.isFetchingNextPage}
            onClick={() => void archive.fetchNextPage()}
            className="rounded-lg border border-gray-600 px-4 py-2 text-sm text-gray-300 hover:bg-gray-800 disabled:opacity-50"
          >
            {archive.isFetchingNextPage ? "Loading…" : "Show more"}
          </button>
        </div>
      )}

      {selected.size > 0 && (
        <div className="fixed bottom-safe-offset-20 left-1/2 z-40 w-max max-w-[calc(100vw-1rem)] -translate-x-1/2 md:bottom-6">
          <div className="flex flex-wrap items-center justify-center gap-x-3 gap-y-2 whitespace-nowrap rounded-xl border border-gray-700 bg-gray-900/95 px-4 py-2.5 shadow-2xl backdrop-blur-sm">
            <span className="text-sm font-medium text-gray-300">{selected.size} selected</span>
            <button
              type="button"
              disabled={busy}
              onClick={() => void unarchive({ asset_ids: [...selected] })}
              className="rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
            >
              Unarchive
            </button>
            <button
              type="button"
              disabled={busy || clipActions.busy}
              onClick={() => clipActions.trash([...selected])}
              className="rounded-lg border border-red-900/60 px-3 py-1.5 text-sm font-medium text-red-300 hover:bg-red-950/50 disabled:opacity-50"
            >
              Move to trash
            </button>
            <button
              type="button"
              onClick={() => setSelected(new Set())}
              className="rounded-lg px-2 py-1 text-xs text-gray-500 hover:bg-gray-800 hover:text-gray-300"
            >
              Clear
            </button>
          </div>
        </div>
      )}
      {clipActions.ui}
    </div>
  );
}
