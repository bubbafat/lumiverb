import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  listLibraries,
  listLibraryHealth,
  createLibrary,
  deleteLibrary,
  emptyTrash,
  getTenantSettings,
  listArchive,
  restoreLibrary,
  ApiError,
  type ProjectUsage,
  updateLibraryVisibility,
} from "../api/client";
import { Badge } from "../components/Badge";
import { Modal } from "../components/Modal";
import { ProjectUsageList, clipCount } from "../components/ProjectUsageList";
import { SkeletonRow } from "../components/SkeletonRow";
import { deletedForGoodOn, shortDate } from "../lib/format";
import { useCanEdit, useRole } from "../lib/useCanEdit";

function formatLastIngest(lastScanAt: string | null): string {
  if (!lastScanAt) return "Never ingested";
  const d = new Date(lastScanAt);
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));
  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  if (diffDays < 7) return `${diffDays} days ago`;
  if (diffDays < 30) return `${Math.floor(diffDays / 7)} weeks ago`;
  return `${Math.floor(diffDays / 30)} months ago`;
}

export default function LibrariesPage() {
  const queryClient = useQueryClient();
  const [addOpen, setAddOpen] = useState(false);
  const [addName, setAddName] = useState("");
  const [addPath, setAddPath] = useState("");
  const [addError, setAddError] = useState("");
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);
  // The API asked about the library's clips that projects use: hidden there in
  // the trash, gone from them once the library is deleted for good.
  const [projectsAsk, setProjectsAsk] = useState<{ id: string; usage: ProjectUsage } | null>(null);
  // Delete for good: every library in the trash (true), or one (its id).
  const [emptyTrashConfirm, setEmptyTrashConfirm] = useState<true | string | false>(false);
  const [restoreError, setRestoreError] = useState<string | null>(null);
  // Set when the server says the trashed libraries' clips are in projects.
  const [trashUsage, setTrashUsage] = useState<ProjectUsage | null>(null);
  const [visibilityUpdatingId, setVisibilityUpdatingId] = useState<
    string | null
  >(null);
  const [visibilityError, setVisibilityError] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [emptyTrashError, setEmptyTrashError] = useState<string | null>(null);

  const { data: libraries, isLoading, error } = useQuery({
    queryKey: ["libraries", true],
    queryFn: () => listLibraries(true),
    refetchInterval: 10_000,
  });

  // Health is a separate query because it's a bulk endpoint (one SQL
  // call for every library). Polled at the same 10s cadence so the
  // green/orange dot stays in sync with the rest of the page without
  // dragging the libraries fetch into a more expensive query.
  const { data: health } = useQuery({
    queryKey: ["library-health"],
    queryFn: listLibraryHealth,
    refetchInterval: 10_000,
  });

  const healthByLibraryId = new Map(
    (health ?? []).map((row) => [row.library_id, row]),
  );

  const createMutation = useMutation({
    mutationFn: () => createLibrary(addName.trim(), addPath.trim()),
    onSuccess: () => {
      setAddOpen(false);
      setAddName("");
      setAddPath("");
      setAddError("");
      queryClient.invalidateQueries({ queryKey: ["libraries"] });
    },
    onError: (err: ApiError) => {
      setAddError(err.message);
    },
  });

  const { data: settings } = useQuery({ queryKey: ["tenant-settings"], queryFn: getTenantSettings, staleTime: 60_000 });
  const trashDays = settings?.trash_days === undefined ? 30 : settings.trash_days;
  const canEdit = useCanEdit();
  // Deleting media for good right away is an admin's; editors trash and restore.
  const isAdmin = useRole() === "admin";
  // Archived clips aren't deleted on their own, but they go with their library: say how many.
  const { data: archivedInDeleting } = useQuery({
    queryKey: ["archive", deleteConfirmId, null, "all", "count"],
    queryFn: () => listArchive({ libraryId: deleteConfirmId!, limit: 1 }),
    enabled: deleteConfirmId !== null,
  });
  const archivedCount = archivedInDeleting?.total ?? 0;

  type DeleteVars = { id: string; removeFromProjects?: boolean };
  const deleteMutation = useMutation({
    mutationFn: ({ id, removeFromProjects }: DeleteVars) => deleteLibrary(id, removeFromProjects ?? false),
    onMutate: () => setDeleteError(null),
    onSuccess: () => {
      setDeleteConfirmId(null);
      setProjectsAsk(null);
      queryClient.invalidateQueries({ queryKey: ["libraries"] });
      queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
    onError: (err: ApiError, vars) => {
      if (err.code === "in_projects" && err.details) {
        setProjectsAsk({ id: vars.id, usage: err.details as unknown as ProjectUsage });
      } else {
        setDeleteConfirmId(null);
        setProjectsAsk(null);
        setDeleteError(err.message);
      }
    },
  });

  const restoreMutation = useMutation({
    mutationFn: (id: string) => restoreLibrary(id),
    onMutate: () => setRestoreError(null),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["libraries"] });
      queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
    onError: (err: ApiError) => setRestoreError(err.message),
  });

  const emptyTrashMutation = useMutation({
    // The libraries shown: one, or every one in the trash on this page (never one trashed since).
    mutationFn: (removeFromProjects: boolean) =>
      emptyTrash(
        removeFromProjects,
        typeof emptyTrashConfirm === "string"
          ? [emptyTrashConfirm]
          : (libraries ?? []).filter((l) => l.status === "trashed").map((l) => l.library_id),
      ),
    onMutate: () => setEmptyTrashError(null),
    onSuccess: () => {
      setEmptyTrashConfirm(false);
      setTrashUsage(null);
      queryClient.invalidateQueries({ queryKey: ["libraries"] });
      queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
    onError: (err: ApiError) => {
      if (err.code === "in_projects" && err.details) setTrashUsage(err.details as unknown as ProjectUsage);
      else setEmptyTrashError(err.message);
    },
  });
  const closeEmptyTrash = () => {
    setEmptyTrashConfirm(false);
    setTrashUsage(null);
    setEmptyTrashError(null);
  };
  const usageTotal = trashUsage ? trashUsage.projects.length + trashUsage.other_projects : 0;

  const visibilityMutation = useMutation({
    mutationFn: (vars: {
      libraryId: string;
      is_public: boolean;
    }) => updateLibraryVisibility(vars.libraryId, vars.is_public),
    onMutate: (vars) => {
      setVisibilityUpdatingId(vars.libraryId);
      setVisibilityError(null);
    },
    onSuccess: () => {
      setVisibilityUpdatingId(null);
      queryClient.invalidateQueries({ queryKey: ["libraries", true] });
    },
    onError: (err: ApiError) => {
      setVisibilityUpdatingId(null);
      setVisibilityError(err.message);
    },
  });

  async function copyShareLink(libraryId: string): Promise<void> {
    const url = `${window.location.origin}/libraries/${libraryId}/browse`;
    try {
      await navigator.clipboard.writeText(url);
    } catch {
      window.prompt("Copy link", url);
    }
  }

  const trashedCount = libraries?.filter((l) => l.status === "trashed").length ?? 0;
  // What the delete-for-good dialog is about: one library, or all of them in the trash.
  const emptyOne = typeof emptyTrashConfirm === "string"
    ? libraries?.find((l) => l.library_id === emptyTrashConfirm)
    : undefined;
  const emptyCount = emptyOne ? 1 : trashedCount;

  const handleAddSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setAddError("");
    if (!addName.trim() || !addPath.trim()) return;
    createMutation.mutate();
  };

  return (
    <div className="mx-auto max-w-3xl px-6 py-6">
      <div className="space-y-6">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <h1 className="text-2xl font-semibold">Libraries</h1>
          <div className="flex items-center gap-3">
            {isAdmin && trashedCount > 0 && (
              <button
                type="button"
                onClick={() => setEmptyTrashConfirm(true)}
                className="rounded-lg border border-amber-700/50 bg-amber-900/20 px-4 py-2 text-sm font-medium text-amber-400 transition-colors duration-150 hover:bg-amber-900/40"
              >
                Empty trash ({trashedCount})
              </button>
            )}
            <button
              type="button"
              onClick={() => setAddOpen(true)}
              className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors duration-150 hover:bg-indigo-500"
            >
              Add library
            </button>
          </div>
        </div>

        {error && (
          <div className="flex items-center justify-between rounded-lg border border-red-800/50 bg-red-900/20 px-4 py-3 text-red-400">
            <span>{(error as Error).message}</span>
          </div>
        )}
        {visibilityError && (
          <div className="flex items-center justify-between rounded-lg border border-red-800/50 bg-red-900/20 px-4 py-3 text-red-400">
            <span>{visibilityError}</span>
          </div>
        )}
        {deleteError && (
          <div role="alert" className="flex items-center justify-between rounded-lg border border-red-800/50 bg-red-900/20 px-4 py-3 text-red-400">
            <span>Couldn&apos;t delete the library: {deleteError}</span>
          </div>
        )}
        {restoreError && (
          <div role="alert" className="flex items-center justify-between rounded-lg border border-red-800/50 bg-red-900/20 px-4 py-3 text-red-400">
            <span>Couldn&apos;t restore the library: {restoreError}</span>
          </div>
        )}
        <p className="text-sm text-gray-500">
          Clips you archived or moved to the trash are in{" "}
          <Link to="/archive" className="text-indigo-300 hover:underline">Archive</Link> and{" "}
          <Link to="/trash" className="text-indigo-300 hover:underline">Trash</Link>.
        </p>

        {isLoading ? (
          <div className="space-y-4">
            <SkeletonRow />
            <SkeletonRow />
            <SkeletonRow />
          </div>
        ) : (
          <div className="space-y-4">
            {libraries?.length === 0 ? (
              <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-8 text-center text-gray-400">
                No libraries yet. Add one to get started.
              </div>
            ) : (
              libraries?.map((lib) => (
                <div
                  key={lib.library_id}
                  className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-4 transition-colors duration-150 hover:border-gray-600/50"
                >
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        {lib.status !== "trashed" && (() => {
                          const h = healthByLibraryId.get(lib.library_id);
                          // Default to neutral grey if health hasn't loaded
                          // yet — avoids a misleading green flash on first
                          // paint.
                          const color = !h
                            ? "bg-gray-500"
                            : h.healthy
                              ? "bg-green-500"
                              : "bg-amber-500";
                          const title = !h
                            ? "Health: loading…"
                            : h.healthy
                              ? "All enrichment complete"
                              : `${h.pending} asset${h.pending === 1 ? "" : "s"} need repair`;
                          return (
                            <span
                              className={`inline-block h-2 w-2 rounded-full ${color}`}
                              title={title}
                              aria-label={title}
                            />
                          );
                        })()}
                        <span className="font-semibold text-gray-100">
                          {lib.name}
                        </span>
                        {lib.status === "trashed" && (
                          <Badge variant="trashed">Trashed</Badge>
                        )}
                      </div>
                      <p className="mt-0.5 font-mono text-sm text-gray-400">
                        {lib.root_path}
                      </p>
                      {lib.status === "trashed" && (() => {
                        const on = deletedForGoodOn(lib.trashed_at, trashDays);
                        return (
                          <p className="mt-0.5 text-sm text-amber-300/80">
                            {on ? `Deleted for good on ${shortDate(on)} unless restored.` : "In the trash until you delete it for good."}
                          </p>
                        );
                      })()}
                    </div>
                    <div className="flex flex-wrap items-center gap-3">
                      {lib.status !== "trashed" && (
                        <>
                          <Link
                            to={`/libraries/${lib.library_id}/browse`}
                            className="rounded-lg border border-gray-600 px-3 py-1.5 text-sm font-medium text-gray-300 transition-colors duration-150 hover:border-gray-500 hover:bg-gray-800/50"
                          >
                            Browse
                          </Link>
                          <Link
                            to={`/libraries/${lib.library_id}/settings`}
                            className="rounded-lg border border-gray-600 px-3 py-1.5 text-sm font-medium text-gray-300 transition-colors duration-150 hover:border-gray-500 hover:bg-gray-800/50"
                          >
                            Settings
                          </Link>
                        </>
                      )}
                      {lib.status !== "trashed" && (
                        <>
                          <button
                            type="button"
                            disabled={
                              visibilityMutation.isPending &&
                              visibilityUpdatingId === lib.library_id
                            }
                            onClick={() => {
                              const currentIsPublic = lib.is_public ?? false;
                              const nextIsPublic = !currentIsPublic;
                              if (nextIsPublic) {
                                const ok = window.confirm(
                                  "Anyone with this link will be able to view this library's photos. Continue?",
                                );
                                if (!ok) return;
                              }
                              visibilityMutation.mutate({
                                libraryId: lib.library_id,
                                is_public: nextIsPublic,
                              });
                            }}
                            className={`rounded-lg px-3 py-1.5 text-sm font-medium transition-colors duration-150 ${
                              (lib.is_public ?? false)
                                ? "bg-indigo-600 text-white hover:bg-indigo-500"
                                : "border border-gray-600 text-gray-300 hover:bg-gray-800/50 hover:border-gray-500"
                            }`}
                          >
                            {lib.is_public ? "Public" : "Private"}
                          </button>
                          {lib.is_public && (
                            <button
                              type="button"
                              onClick={() => void copyShareLink(lib.library_id)}
                              className="rounded-lg border border-gray-600 bg-gray-900/30 px-3 py-1.5 text-sm font-medium text-gray-300 transition-colors duration-150 hover:border-gray-500 hover:bg-gray-800/50 disabled:opacity-50 disabled:cursor-not-allowed"
                              disabled={
                                visibilityMutation.isPending &&
                                visibilityUpdatingId === lib.library_id
                              }
                            >
                              Copy link
                            </button>
                          )}
                        </>
                      )}
                      <span className="text-sm text-gray-500">
                        {formatLastIngest(lib.last_scan_at)}
                      </span>
                      {canEdit && lib.status === "trashed" && (
                        <div className="flex items-center gap-2">
                          <button
                            type="button"
                            onClick={() => restoreMutation.mutate(lib.library_id)}
                            disabled={restoreMutation.isPending}
                            className="rounded-lg border border-gray-600 px-3 py-1.5 text-sm font-medium text-gray-200 transition-colors duration-150 hover:border-gray-500 hover:bg-gray-800/50 disabled:opacity-50"
                          >
                            Restore
                          </button>
                          {isAdmin && (
                            <button
                              type="button"
                              onClick={() => setEmptyTrashConfirm(lib.library_id)}
                              className="rounded px-3 py-1.5 text-sm font-medium text-red-400 transition-colors duration-150 hover:bg-red-900/30"
                            >
                              Delete for good
                            </button>
                          )}
                        </div>
                      )}
                      {canEdit && lib.status !== "trashed" && (
                        <div>
                          {projectsAsk?.id === lib.library_id ? (
                            <div className="flex max-w-md flex-col gap-2" role="alertdialog" aria-label={`Delete ${lib.name}`}>
                              <ProjectUsageList usage={projectsAsk.usage}>
                                {clipCount(projectsAsk.usage.assets_in_projects)} in {lib.name}{" "}
                                {projectsAsk.usage.assets_in_projects === 1 ? "is" : "are"} in projects. In the trash{" "}
                                {projectsAsk.usage.assets_in_projects === 1 ? "it's" : "they're"} hidden there; deleted
                                for good with the library, {projectsAsk.usage.assets_in_projects === 1 ? "it leaves" : "they leave"} those
                                projects.
                              </ProjectUsageList>
                              <div className="flex items-center gap-2">
                                <button
                                  type="button"
                                  onClick={() => deleteMutation.mutate({ id: lib.library_id, removeFromProjects: true })}
                                  disabled={deleteMutation.isPending}
                                  className="rounded px-2 py-1 text-sm font-medium text-red-400 transition-colors duration-150 hover:bg-red-900/30"
                                >
                                  Move to trash anyway
                                </button>
                                <button
                                  type="button"
                                  onClick={() => {
                                    setProjectsAsk(null);
                                    setDeleteConfirmId(null);
                                  }}
                                  className="rounded px-2 py-1 text-sm text-gray-400 transition-colors duration-150 hover:text-gray-300"
                                >
                                  Cancel
                                </button>
                              </div>
                            </div>
                          ) : deleteConfirmId === lib.library_id ? (
                            <div className="flex items-center gap-2">
                              <span className="text-sm text-gray-400">
                                Delete {lib.name}? It moves to the trash with everything in it
                                {archivedCount > 0
                                  ? `, its ${archivedCount === 1 ? "archived clip" : `${archivedCount.toLocaleString()} archived clips`} too,`
                                  : ""}
                                {trashDays ? ` and is deleted for good after ${trashDays} days` : ""}. You can
                                restore it until then.
                              </span>
                              <button
                                type="button"
                                onClick={() =>
                                  deleteMutation.mutate({ id: lib.library_id })
                                }
                                disabled={deleteMutation.isPending}
                                className="rounded px-2 py-1 text-sm font-medium text-red-400 transition-colors duration-150 hover:bg-red-900/30"
                              >
                                Confirm
                              </button>
                              <button
                                type="button"
                                onClick={() => setDeleteConfirmId(null)}
                                className="rounded px-2 py-1 text-sm text-gray-400 transition-colors duration-150 hover:text-gray-300"
                              >
                                Cancel
                              </button>
                            </div>
                          ) : (
                            <button
                              type="button"
                              onClick={() => setDeleteConfirmId(lib.library_id)}
                              className="rounded px-3 py-1.5 text-sm font-medium text-red-400 transition-colors duration-150 hover:bg-red-900/30"
                            >
                              Delete
                            </button>
                          )}
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              ))
            )}
          </div>
        )}
      </div>

      <Modal
        isOpen={addOpen}
        onClose={() => {
          setAddOpen(false);
          setAddError("");
        }}
        title="Add library"
      >
        <form onSubmit={handleAddSubmit} className="space-y-4">
          {addError && (
            <div className="rounded-lg border border-red-800/50 bg-red-900/20 px-3 py-2 text-sm text-red-400">
              {addError}
            </div>
          )}
          <div>
            <label
              htmlFor="lib-name"
              className="mb-1 block text-sm text-gray-400"
            >
              Name
            </label>
            <input
              id="lib-name"
              type="text"
              value={addName}
              onChange={(e) => setAddName(e.target.value)}
              placeholder="My Photos"
              required
              className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-gray-100 placeholder-gray-500 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            />
          </div>
          <div>
            <label
              htmlFor="lib-path"
              className="mb-1 block text-sm text-gray-400"
            >
              Root path
            </label>
            <input
              id="lib-path"
              type="text"
              value={addPath}
              onChange={(e) => setAddPath(e.target.value)}
              placeholder="/Volumes/Photos/2024"
              required
              className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 font-mono text-sm text-gray-100 placeholder-gray-500 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            />
          </div>
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => setAddOpen(false)}
              className="rounded-lg border border-gray-600 px-4 py-2 text-sm font-medium text-gray-300 transition-colors duration-150 hover:bg-gray-800"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={createMutation.isPending || !addName.trim() || !addPath.trim()}
              className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors duration-150 hover:bg-indigo-500 disabled:opacity-50"
            >
              {createMutation.isPending ? "Creating…" : "Create library"}
            </button>
          </div>
        </form>
      </Modal>

      {emptyTrashConfirm !== false && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          onClick={closeEmptyTrash}
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-label={emptyOne ? `Delete ${emptyOne.name} for good` : "Empty trash"}
            className="w-full max-w-md rounded-xl bg-gray-900 p-6 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 className="mb-2 text-lg font-semibold text-gray-100">
              {emptyOne ? `Delete ${emptyOne.name} for good` : "Empty trash"}
            </h2>
            <p className="mb-4 text-sm text-gray-400">
              {emptyOne
                ? `This will permanently delete ${emptyOne.name} and all its assets.`
                : `This will permanently delete ${trashedCount} trashed${
                    trashedCount === 1 ? " library and all its" : " libraries and all their"
                  } assets.`}{" "}
              This cannot be undone.
            </p>
            {trashUsage && (
              <div className="mb-4">
                <ProjectUsageList usage={trashUsage}>
                  {clipCount(trashUsage.assets_in_projects)} from{" "}
                  {emptyCount === 1 ? "this library" : "these libraries"}{" "}
                  {trashUsage.assets_in_projects === 1 ? "is" : "are"} in{" "}
                  {usageTotal === 1 ? "1 project" : `${usageTotal} projects`}. Deleting{" "}
                  {trashUsage.assets_in_projects === 1 ? "it" : "them"} for good removes{" "}
                  {trashUsage.assets_in_projects === 1 ? "it" : "them"} from{" "}
                  {usageTotal === 1 ? "that project" : "those projects"}.
                </ProjectUsageList>
              </div>
            )}
            {emptyTrashError && (
              <div role="alert" className="mb-4 rounded-lg border border-red-800/50 bg-red-900/20 px-3 py-2 text-sm text-red-400">
                Couldn&apos;t empty the trash: {emptyTrashError}
              </div>
            )}
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={closeEmptyTrash}
                className="rounded-lg border border-gray-600 px-4 py-2 text-sm font-medium text-gray-300 transition-colors duration-150 hover:bg-gray-800"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => emptyTrashMutation.mutate(trashUsage !== null)}
                disabled={emptyTrashMutation.isPending}
                className="rounded-lg bg-red-600 px-4 py-2 text-sm font-medium text-white transition-colors duration-150 hover:bg-red-500 disabled:opacity-50"
              >
                {emptyTrashMutation.isPending
                  ? "Deleting…"
                  : trashUsage
                    ? "Delete and remove from projects"
                    : emptyOne
                      ? "Delete for good"
                      : "Empty trash"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
