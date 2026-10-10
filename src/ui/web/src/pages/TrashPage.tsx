import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  emptyClipTrash,
  listLibraries,
  listTrash,
  restoreClips,
  type ProjectUsage,
  type TrashedClip,
} from "../api/client";
import { HiddenClipGrid } from "../components/HiddenClipGrid";
import { Modal } from "../components/Modal";
import { ProjectUsageList, clipCount } from "../components/ProjectUsageList";
import { useCanEdit, useRole } from "../lib/useCanEdit";
import { deletedForGoodOn, shortDate } from "../lib/format";

/** Clips a person moved to the trash: restorable until they're deleted for good
 * after the trash days. Libraries and projects have their own trash. */
export default function TrashPage() {
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const libraryId = params.get("library") ?? undefined;
  const canEdit = useCanEdit();
  const isAdmin = useRole() === "admin";
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [notice, setNotice] = useState<{ text: string; error?: boolean } | null>(null);
  // Delete for good: the chosen clips, or (null ids) everything in the trash.
  const [deleting, setDeleting] = useState<{ ids: string[] | null; usage?: ProjectUsage } | null>(null);
  const [busy, setBusy] = useState(false);

  const { data: libraries } = useQuery({ queryKey: ["libraries", false], queryFn: () => listLibraries(false) });
  const trash = useInfiniteQuery({
    queryKey: ["trash", libraryId ?? null],
    queryFn: ({ pageParam }) => listTrash({ libraryId, after: pageParam, limit: 100 }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const clips = useMemo(() => trash.data?.pages.flatMap((p) => p.items) ?? [], [trash.data]);
  const first = trash.data?.pages[0];
  const total = first?.total ?? 0;
  const days = first?.trash_days;

  const toggle = (id: string) =>
    setSelected((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const refresh = () => {
    setSelected(new Set());
    void queryClient.invalidateQueries();
  };
  const fail = (what: string, err: unknown) =>
    setNotice({ text: `Couldn't ${what}: ${err instanceof Error ? err.message : String(err)}`, error: true });

  const restore = async () => {
    setBusy(true);
    try {
      const r = await restoreClips([...selected]);
      refresh();
      const back = r.to_archive?.length ?? 0;
      setNotice({
        text: `Restored ${clipCount(r.restored.length)}.` +
          (back ? ` ${clipCount(back)} went back to the archive, where ${back === 1 ? "it was" : "they were"}.` : ""),
      });
    } catch (err) {
      fail("restore them", err);
    } finally {
      setBusy(false);
    }
  };

  const deleteForGood = async (removeFromProjects: boolean) => {
    if (!deleting) return;
    setBusy(true);
    try {
      // Everything shown: this library's trash when filtered, never more.
      const r = await emptyClipTrash(
        deleting.ids ?? "all",
        removeFromProjects,
        deleting.ids ? {} : { libraryId, trashedBefore: first?.listed_at },
      );
      setDeleting(null);
      refresh();
      setNotice({ text: `Deleted ${clipCount(r.deleted)} for good.` });
    } catch (err) {
      if (err instanceof ApiError && err.code === "in_projects" && err.details) {
        setDeleting({ ...deleting, usage: err.details as unknown as ProjectUsage });
      } else {
        setDeleting(null);
        fail("delete them", err);
      }
    } finally {
      setBusy(false);
    }
  };

  const meta = (clip: TrashedClip) => {
    const on = deletedForGoodOn(clip.trashed_at, days);
    return on ? <span className="text-amber-300/80">Deleted for good {shortDate(on)}</span> : <span>Kept until emptied</span>;
  };
  const allShown = clips.length > 0 && clips.every((c) => selected.has(c.asset_id));
  const deleteCount = deleting?.ids ? deleting.ids.length : total;
  const libraryName = libraries?.find((l) => l.library_id === libraryId)?.name;

  return (
    <div className="mx-auto max-w-6xl space-y-5 px-4 py-6 sm:px-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Trash</h1>
          <p className="mt-1 max-w-2xl text-sm text-gray-400">
            Clips you moved to the trash.{" "}
            {days
              ? `They're deleted for good ${days} days after they went in; restore them until then.`
              : "They stay until someone deletes them for good."}{" "}
            Libraries and projects in the trash are on the{" "}
            <Link to="/" className="text-indigo-300 hover:underline">Libraries</Link> and{" "}
            <Link to="/projects" className="text-indigo-300 hover:underline">Projects</Link> pages.
          </p>
        </div>
        {isAdmin && total > 0 && (
          <button
            type="button"
            onClick={() => setDeleting({ ids: null })}
            className="shrink-0 rounded-lg border border-red-800/60 bg-red-950/30 px-4 py-2 text-sm font-medium text-red-300 hover:bg-red-900/40"
          >
            {libraryName ? `Empty ${libraryName}'s trash` : "Empty trash"} ({total.toLocaleString()})
          </button>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <label className="text-sm text-gray-400" htmlFor="trash-library">Library</label>
        <select
          id="trash-library"
          value={libraryId ?? ""}
          onChange={(e) => {
            setSelected(new Set());
            const next = new URLSearchParams(params);
            if (e.target.value) next.set("library", e.target.value);
            else next.delete("library");
            setParams(next, { replace: true });
          }}
          className="rounded-lg border border-gray-700 bg-gray-800 px-2 py-1.5 text-sm text-gray-200"
        >
          <option value="">All libraries</option>
          {libraries?.map((l) => (
            <option key={l.library_id} value={l.library_id}>{l.name}</option>
          ))}
        </select>
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

      {trash.isLoading ? (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
          {Array.from({ length: 8 }, (_, i) => (
            <div key={i} className="aspect-[4/3] animate-pulse rounded-lg bg-gray-800" />
          ))}
        </div>
      ) : trash.isError ? (
        <div role="alert" className="rounded-lg border border-red-800/50 bg-red-900/20 px-4 py-3 text-red-300">
          Couldn&apos;t load the trash: {(trash.error as Error).message}
        </div>
      ) : clips.length === 0 ? (
        <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-8 text-center text-gray-400">
          The trash is empty.
        </div>
      ) : (
        <HiddenClipGrid clips={clips} selected={selected} selectable={canEdit} onToggle={toggle} meta={meta} />
      )}
      {trash.hasNextPage && (
        <div className="flex justify-center">
          <button
            type="button"
            disabled={trash.isFetchingNextPage}
            onClick={() => void trash.fetchNextPage()}
            className="rounded-lg border border-gray-600 px-4 py-2 text-sm text-gray-300 hover:bg-gray-800 disabled:opacity-50"
          >
            {trash.isFetchingNextPage ? "Loading…" : "Show more"}
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
              onClick={() => void restore()}
              className="rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
            >
              Restore
            </button>
            {isAdmin && (
              <button
                type="button"
                disabled={busy}
                onClick={() => setDeleting({ ids: [...selected] })}
                className="rounded-lg border border-red-900/60 px-3 py-1.5 text-sm font-medium text-red-300 hover:bg-red-950/50 disabled:opacity-50"
              >
                Delete for good
              </button>
            )}
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

      <Modal
        isOpen={deleting !== null}
        onClose={() => setDeleting(null)}
        title={deleting?.ids ? "Delete for good?" : libraryName ? `Empty ${libraryName}'s trash?` : "Empty the trash?"}
      >
        {deleting && (
          <div className="space-y-4">
            <p className="text-sm text-gray-300">
              {deleting.ids
                ? clipCount(deleteCount)
                : `Every clip in the trash${libraryName ? ` of ${libraryName}` : ""} (${clipCount(deleteCount)})`} will be
              deleted for good, with their previews and analysis. This can&apos;t be undone. The original files on
              your drives aren&apos;t touched.
            </p>
            {deleting.usage && (
              <ProjectUsageList usage={deleting.usage}>
                {clipCount(deleting.usage.assets_in_projects)}{" "}
                {deleting.usage.assets_in_projects === 1 ? "is" : "are"} in projects; deleting{" "}
                {deleting.usage.assets_in_projects === 1 ? "it" : "them"} for good removes{" "}
                {deleting.usage.assets_in_projects === 1 ? "it" : "them"} from those projects.
              </ProjectUsageList>
            )}
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setDeleting(null)}
                className="rounded-lg border border-gray-600 px-4 py-2 text-sm font-medium text-gray-300 hover:bg-gray-800"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => void deleteForGood(Boolean(deleting.usage))}
                className="rounded-lg bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-500 disabled:opacity-50"
              >
                {deleting.usage ? "Delete and remove from projects" : `Delete ${clipCount(deleteCount)} for good`}
              </button>
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}
