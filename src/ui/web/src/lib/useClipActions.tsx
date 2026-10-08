import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  archiveClips,
  getTenantSettings,
  restoreClips,
  trashClips,
  unarchiveClips,
  type ProjectUsage,
} from "../api/client";
import { Modal } from "../components/Modal";
import { ProjectUsageList, clipCount } from "../components/ProjectUsageList";

type Notice = { text: string; undo?: () => Promise<unknown>; error?: boolean };

/**
 * Archive and trash, from wherever clips are picked (a selection, the
 * lightbox, a folder). Archive: out of sight, kept forever. Trash:
 * restorable until it's deleted for good after the trash days. Both say
 * what happened with an Undo; trashing clips projects use asks first, as
 * the API requires; archiving a folder says how many clips first.
 *
 * Render `ui` once in the page.
 */
export function useClipActions(onDone?: () => void) {
  const queryClient = useQueryClient();
  const { data: settings } = useQuery({ queryKey: ["tenant-settings"], queryFn: getTenantSettings, staleTime: 60_000 });
  const [projectsAsk, setProjectsAsk] = useState<{ ids: string[]; usage: ProjectUsage } | null>(null);
  const [folderAsk, setFolderAsk] = useState<{ libraryId: string; path: string; name: string; count: number } | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(null), notice.error ? 10_000 : 8_000);
    return () => clearTimeout(t);
  }, [notice]);

  const changed = () => {
    // Grids, folder trees, counts, projects, the trash and archive views.
    void queryClient.invalidateQueries();
    onDone?.();
  };
  const fail = (what: string, err: unknown) =>
    setNotice({ text: `Couldn't ${what}: ${err instanceof Error ? err.message : String(err)}`, error: true });

  const run = async <T,>(what: string, fn: () => Promise<T>, after: (r: T) => void) => {
    setBusy(true);
    try {
      after(await fn());
    } catch (err) {
      fail(what, err);
    } finally {
      setBusy(false);
    }
  };

  const archive = (ids: string[]) =>
    run("archive", () => archiveClips({ asset_ids: ids }), (r) => {
      changed();
      setNotice({
        text: `Archived ${clipCount(r.archived.length)}.`,
        undo: r.archived.length ? () => unarchiveClips({ asset_ids: r.archived }) : undefined,
      });
    });

  // Promise a date only once the setting is known (an older server leaves it out: 30).
  const days = !settings ? null : settings.trash_days === undefined ? 30 : settings.trash_days;
  const trashed = (ids: string[]) => {
    changed();
    setNotice({
      text: `Moved ${clipCount(ids.length)} to the trash.${days ? ` Deleted for good in ${days} days.` : ""}`,
      undo: ids.length ? () => restoreClips(ids) : undefined,
    });
  };

  const trash = (ids: string[], removeFromProjects = false) => {
    setBusy(true);
    trashClips(ids, removeFromProjects)
      .then((r) => {
        setProjectsAsk(null);
        trashed(r.trashed);
      })
      .catch((err) => {
        if (err instanceof ApiError && err.code === "in_projects" && err.details) {
          setProjectsAsk({ ids, usage: err.details as unknown as ProjectUsage });
        } else {
          setProjectsAsk(null);
          fail("move them to the trash", err);
        }
      })
      .finally(() => setBusy(false));
  };

  /** Ask first, saying how many clips, then archive everything under the folder. */
  const archiveFolder = (libraryId: string, path: string, count: number) =>
    setFolderAsk({ libraryId, path, name: path.split("/").pop() || path || "this library", count });

  const confirmFolder = () => {
    if (!folderAsk) return;
    const { libraryId, path, name } = folderAsk;
    setFolderAsk(null);
    void run("archive the folder", () => archiveClips({ library_id: libraryId, path }), (r) => {
      changed();
      setNotice({
        text: `Archived ${clipCount(r.archived.length)} in ${name}.`,
        undo: r.archived.length ? () => unarchiveClips({ asset_ids: r.archived }) : undefined,
      });
    });
  };

  const undo = (n: Notice) => {
    setNotice(null);
    void run("undo", n.undo!, () => {
      changed();
      setNotice({ text: "Undone." });
    });
  };

  // In a portal: wherever the hook lives (a sidebar with a transform, a
  // lightbox), its dialogs and notice cover the page.
  const ui = createPortal(
    <>
      <Modal isOpen={projectsAsk !== null} onClose={() => setProjectsAsk(null)} title="Move to the trash?">
        {projectsAsk && (
          <div className="space-y-4">
            <ProjectUsageList usage={projectsAsk.usage}>
              {clipCount(projectsAsk.usage.assets_in_projects)}{" "}
              {projectsAsk.usage.assets_in_projects === 1 ? "is" : "are"} in projects. In the trash{" "}
              {projectsAsk.usage.assets_in_projects === 1 ? "it's" : "they're"} hidden there, and once deleted for
              good {projectsAsk.usage.assets_in_projects === 1 ? "it leaves" : "they leave"} those projects.
            </ProjectUsageList>
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setProjectsAsk(null)}
                className="rounded-lg border border-gray-600 px-4 py-2 text-sm font-medium text-gray-300 hover:bg-gray-800"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => trash(projectsAsk.ids, true)}
                className="rounded-lg bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-500 disabled:opacity-50"
              >
                Move {clipCount(projectsAsk.ids.length)} to the trash
              </button>
            </div>
          </div>
        )}
      </Modal>
      <Modal isOpen={folderAsk !== null} onClose={() => setFolderAsk(null)} title="Archive this folder?">
        {folderAsk && (
          <div className="space-y-4">
            <p className="text-sm text-gray-300">
              {clipCount(folderAsk.count)} in <span className="font-medium text-gray-100">{folderAsk.name}</span> and
              the folders inside it leave browse and search. They keep everything they have, and you can bring them
              back from Archive. Files added to the folder later show up as usual.
            </p>
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setFolderAsk(null)}
                className="rounded-lg border border-gray-600 px-4 py-2 text-sm font-medium text-gray-300 hover:bg-gray-800"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={confirmFolder}
                className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
              >
                Archive {clipCount(folderAsk.count)}
              </button>
            </div>
          </div>
        )}
      </Modal>
      {notice && (
        <div className="fixed bottom-safe-offset-20 left-1/2 z-50 w-max max-w-[calc(100vw-1rem)] -translate-x-1/2 md:bottom-6">
          <div
            role={notice.error ? "alert" : "status"}
            className={`flex items-center gap-3 rounded-xl border px-4 py-2.5 text-sm shadow-2xl backdrop-blur-sm ${
              notice.error ? "border-red-800/60 bg-red-950/95 text-red-200" : "border-gray-700 bg-gray-900/95 text-gray-200"
            }`}
          >
            <span>{notice.text}</span>
            {notice.undo && (
              <button
                type="button"
                disabled={busy}
                onClick={() => undo(notice)}
                className="rounded-lg px-2 py-1 font-medium text-indigo-300 hover:bg-gray-800 disabled:opacity-50"
              >
                Undo
              </button>
            )}
            <button
              type="button"
              aria-label="Dismiss"
              onClick={() => setNotice(null)}
              className="rounded px-1 text-gray-500 hover:text-gray-300"
            >
              ×
            </button>
          </div>
        </div>
      )}
    </>,
    document.body,
  );

  return { archive, trash: (ids: string[]) => trash(ids), archiveFolder, busy, ui };
}
