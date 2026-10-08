import { useState, type ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  listProjects,
  createProject,
  trashProject,
  restoreProject,
  restoreProjectClips,
  emptyProjectTrash,
  setProjectStatus,
  ApiError,
  type ProjectView,
} from "../api/client";
import { useAuthenticatedImage } from "../api/useAuthenticatedImage";
import { Modal } from "../components/Modal";
import { SkeletonRow } from "../components/SkeletonRow";
import type { ProjectItem } from "../api/types";

type Tab = Exclude<ProjectView, "all">;
const TABS: { id: Tab; label: string }[] = [
  { id: "active", label: "Active" },
  { id: "archived", label: "Archived" },
  { id: "trashed", label: "Trash" },
];

function plural(n: number, one: string, many: string) {
  return `${n} ${n === 1 ? one : many}`;
}

const actionClass = "rounded px-2 py-1 text-xs text-gray-500 hover:text-gray-200";

function CoverImage({ project }: { project: ProjectItem }) {
  const { url: coverUrl } = useAuthenticatedImage(
    project.cover_asset_id ?? "",
    "thumbnail",
    { enabled: !!project.cover_asset_id },
  );
  return (
    <div className="aspect-[4/3] bg-gray-800">
      {coverUrl ? (
        <img src={coverUrl} alt={project.name} className="h-full w-full object-cover" />
      ) : (
        <div className="flex h-full w-full items-center justify-center text-gray-600">
          <svg className="h-12 w-12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden>
            <rect x="3" y="3" width="18" height="18" rx="2" />
            <path d="M3 15l5-5 4 4 4-6 5 7" />
          </svg>
        </div>
      )}
    </div>
  );
}

function ProjectCard({ project, actions }: { project: ProjectItem; actions: ReactNode }) {
  // A trashed project can't be opened until it's restored.
  const inTrash = !!project.deleted_at;
  const trashedClips = project.trashed_asset_count ?? 0;
  return (
    <div className="group relative overflow-hidden rounded-lg border border-gray-700/50 bg-gray-900/50 transition-colors duration-150 hover:border-gray-600/50">
      {inTrash ? (
        <div className="opacity-60">
          <CoverImage project={project} />
        </div>
      ) : (
        <Link to={`/projects/${project.project_id}`}>
          <CoverImage project={project} />
        </Link>
      )}
      <div className="p-3">
        <div className="flex flex-col gap-1">
          <div className="min-w-0">
            {inTrash ? (
              <span className="block truncate font-medium text-gray-300">{project.name}</span>
            ) : (
              <Link
                to={`/projects/${project.project_id}`}
                className="block truncate font-medium text-gray-100 hover:text-indigo-300"
              >
                {project.name}
              </Link>
            )}
            <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1">
              <span className="text-xs text-gray-500">
                {project.asset_count} {project.asset_count === 1 ? "item" : "items"}
              </span>
              {trashedClips > 0 && (
                <span className="text-xs text-amber-400/80">{trashedClips} in trash</span>
              )}
              {project.type === "smart" && (
                <span className="rounded bg-indigo-900/60 px-1.5 py-0.5 text-[10px] text-indigo-300">
                  Smart
                </span>
              )}
              {project.ownership === "shared" && (
                <span className="rounded bg-gray-700/60 px-1.5 py-0.5 text-[10px] text-gray-400">
                  Shared
                </span>
              )}
              {project.visibility === "public" && (
                <span className="rounded bg-indigo-900/40 px-1.5 py-0.5 text-[10px] text-indigo-400">
                  Public
                </span>
              )}
            </div>
          </div>
          <div className="-ml-2">{actions}</div>
        </div>
      </div>
    </div>
  );
}

/** Two-step button: the first click asks, the second does it. */
function ConfirmAction({
  label,
  question,
  onConfirm,
  busy,
  confirming,
  setConfirming,
}: {
  label: string;
  question: string;
  onConfirm: () => void;
  busy: boolean;
  confirming: boolean;
  setConfirming: (on: boolean) => void;
}) {
  if (!confirming) {
    return (
      <button type="button" onClick={() => setConfirming(true)} className={`${actionClass} hover:text-red-400`}>
        {label}
      </button>
    );
  }
  return (
    <div className="ml-2 space-y-1">
      <p className="text-xs text-red-300">{question}</p>
      <div className="-ml-2 flex items-center gap-1">
        <button
          type="button"
          onClick={onConfirm}
          disabled={busy}
          className="rounded px-2 py-1 text-xs font-medium text-red-400 hover:bg-red-900/30 disabled:opacity-50"
        >
          Confirm
        </button>
        <button type="button" onClick={() => setConfirming(false)} className={actionClass}>
          Cancel
        </button>
      </div>
    </div>
  );
}

export default function ProjectsPage() {
  const queryClient = useQueryClient();
  const [createOpen, setCreateOpen] = useState(false);
  const [createName, setCreateName] = useState("");
  const [createDesc, setCreateDesc] = useState("");
  const [createError, setCreateError] = useState("");
  const [view, setView] = useState<Tab>("active");
  // Which confirmation is open: a project id for "Delete forever", or "all"
  // for "Empty trash".
  const [confirming, setConfirming] = useState<string | null>(null);
  // A project trashed here, or from its own page on the way here.
  const location = useLocation();
  const [justTrashed, setJustTrashed] = useState<ProjectItem | null>(
    (location.state as { justTrashed?: ProjectItem } | null)?.justTrashed ?? null,
  );
  const [restoring, setRestoring] = useState<ProjectItem | null>(null);

  const { data: projects, isLoading, error } = useQuery({
    queryKey: ["projects", view],
    queryFn: () => listProjects(view),
    refetchInterval: 10_000,
  });

  // Prefix match: refreshes every tab, the sidebar and pickers.
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["projects"] });

  const archiveMutation = useMutation({
    mutationFn: (project: ProjectItem) =>
      setProjectStatus(
        project.project_id,
        project.status === "archived" ? "active" : "archived",
      ),
    onSuccess: refresh,
  });

  const createMutation = useMutation({
    mutationFn: () =>
      createProject(createName.trim(), {
        description: createDesc.trim() || undefined,
      }),
    onSuccess: () => {
      setCreateOpen(false);
      setCreateName("");
      setCreateDesc("");
      setCreateError("");
      refresh();
    },
    onError: (err: ApiError) => setCreateError(err.message),
  });

  const trashMutation = useMutation({
    mutationFn: (project: ProjectItem) => trashProject(project.project_id),
    onSuccess: (_, project) => {
      setJustTrashed(project);
      refresh();
    },
  });

  const restoreMutation = useMutation({
    mutationFn: async ({ project, withClips }: { project: ProjectItem; withClips: boolean }) => {
      await restoreProject(project.project_id);
      if (withClips) await restoreProjectClips(project.project_id);
    },
    onSuccess: () => {
      setRestoring(null);
      setJustTrashed(null);
      refresh();
      queryClient.invalidateQueries({ queryKey: ["project"] });
      queryClient.invalidateQueries({ queryKey: ["project-assets"] });
    },
  });

  const emptyMutation = useMutation({
    mutationFn: (ids?: string[]) => emptyProjectTrash(ids),
    onSuccess: () => {
      setConfirming(null);
      refresh();
    },
  });

  const startRestore = (project: ProjectItem) => {
    // Only ask when there's something more to bring back.
    if ((project.trashed_asset_count ?? 0) > 0) setRestoring(project);
    else restoreMutation.mutate({ project, withClips: false });
  };

  const handleCreateSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setCreateError("");
    if (!createName.trim()) return;
    createMutation.mutate();
  };

  const actionsFor = (project: ProjectItem) => {
    if (view === "trashed") {
      return (
        <div className="flex flex-wrap items-center gap-1">
          <button type="button" onClick={() => startRestore(project)} className={actionClass}>
            Restore
          </button>
          <ConfirmAction
            label="Delete forever"
            question="Delete for good? Its clips stay in your library."
            onConfirm={() => emptyMutation.mutate([project.project_id])}
            busy={emptyMutation.isPending}
            confirming={confirming === project.project_id}
            setConfirming={(on) => setConfirming(on ? project.project_id : null)}
          />
        </div>
      );
    }
    return (
      // Hidden until hover only where hovering exists; a phone can't hover.
      <div className="flex items-center gap-1 transition-opacity focus-within:opacity-100 [@media(hover:hover)]:opacity-0 [@media(hover:hover)]:group-hover:opacity-100">
        <button type="button" onClick={() => archiveMutation.mutate(project)} className={actionClass}>
          {project.status === "archived" ? "Restore" : "Archive"}
        </button>
        <button
          type="button"
          onClick={() => trashMutation.mutate(project)}
          className={`${actionClass} hover:text-red-400`}
        >
          Delete
        </button>
      </div>
    );
  };

  const emptyText = {
    active: "No projects yet. Create one to start curating.",
    archived: "No archived projects.",
    trashed: "The trash is empty.",
  }[view];

  const restoringTrashed = restoring?.trashed_asset_count ?? 0;
  const restoringMissing = restoring?.missing_asset_count ?? 0;

  return (
    <div className="mx-auto max-w-4xl px-6 py-6">
      <div className="space-y-6">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-center gap-4">
            <h1 className="text-2xl font-semibold">Projects</h1>
            <div className="flex rounded-lg border border-gray-700/50 p-0.5 text-sm">
              {TABS.map(({ id, label }) => (
                <button
                  key={id}
                  type="button"
                  onClick={() => {
                    setView(id);
                    setConfirming(null);
                  }}
                  aria-pressed={view === id}
                  className={`rounded-md px-3 py-1 transition-colors duration-150 ${
                    view === id ? "bg-gray-700 text-gray-100" : "text-gray-400 hover:text-gray-200"
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          <button
            type="button"
            onClick={() => setCreateOpen(true)}
            className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors duration-150 hover:bg-indigo-500"
          >
            New project
          </button>
        </div>

        {error && (
          <div className="rounded-lg border border-red-800/50 bg-red-900/20 px-4 py-3 text-red-400">
            {(error as Error).message}
          </div>
        )}

        {justTrashed && (
          <div
            role="status"
            className="flex items-center justify-between gap-3 rounded-lg border border-gray-700/50 bg-gray-900/80 px-4 py-2 text-sm text-gray-300"
          >
            <span className="min-w-0 truncate">Moved &ldquo;{justTrashed.name}&rdquo; to the trash.</span>
            <div className="flex shrink-0 items-center gap-1">
              <button
                type="button"
                onClick={() => restoreMutation.mutate({ project: justTrashed, withClips: false })}
                className="rounded px-2 py-1 font-medium text-indigo-300 hover:bg-gray-800"
              >
                Undo
              </button>
              <button
                type="button"
                onClick={() => setJustTrashed(null)}
                aria-label="Dismiss"
                className="rounded px-2 py-1 text-gray-500 hover:text-gray-300"
              >
                ✕
              </button>
            </div>
          </div>
        )}

        {view === "trashed" && (projects?.length ?? 0) > 0 && (
          <div className="flex flex-wrap items-center justify-between gap-2 text-sm text-gray-400">
            <span>Restore a project, or delete it for good. Its clips stay in your library either way.</span>
            {confirming === "all" ? (
              <div className="flex items-center gap-2">
                <span className="text-red-300">Delete everything in the trash for good?</span>
                <button
                  type="button"
                  onClick={() => emptyMutation.mutate(undefined)}
                  disabled={emptyMutation.isPending}
                  className="rounded px-2 py-1 font-medium text-red-400 hover:bg-red-900/30 disabled:opacity-50"
                >
                  Confirm
                </button>
                <button type="button" onClick={() => setConfirming(null)} className="rounded px-2 py-1 text-gray-500 hover:text-gray-300">
                  Cancel
                </button>
              </div>
            ) : (
              <button
                type="button"
                onClick={() => setConfirming("all")}
                className="rounded-lg border border-red-800/60 px-3 py-1.5 text-red-400 hover:bg-red-900/20"
              >
                Empty trash
              </button>
            )}
          </div>
        )}

        {isLoading ? (
          <div className="space-y-4">
            <SkeletonRow />
            <SkeletonRow />
          </div>
        ) : projects?.length === 0 ? (
          <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-8 text-center text-gray-400">
            {emptyText}
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
            {projects?.map((p) => (
              <ProjectCard key={p.project_id} project={p} actions={actionsFor(p)} />
            ))}
          </div>
        )}
      </div>

      <Modal isOpen={restoring !== null} onClose={() => setRestoring(null)} title="Restore project">
        <div className="space-y-4 text-sm text-gray-300">
          <p>
            &ldquo;{restoring?.name}&rdquo; has {plural(restoringTrashed, "clip", "clips")} in the trash.
            Restore {restoringTrashed === 1 ? "it" : "them"} too? Restored clips also come back to your
            library and to any other projects they&rsquo;re in.
          </p>
          {restoringMissing > 0 && (
            <p className="text-gray-400">
              {plural(restoringMissing, "clip is", "clips are")} missing from disk and will come back when
              {restoringMissing === 1 ? " its file does" : " their files do"}.
            </p>
          )}
          <div className="flex flex-wrap justify-end gap-2">
            <button
              type="button"
              onClick={() => setRestoring(null)}
              className="rounded-lg px-3 py-2 text-gray-400 hover:text-gray-200"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => restoring && restoreMutation.mutate({ project: restoring, withClips: false })}
              disabled={restoreMutation.isPending}
              className="rounded-lg border border-gray-600 px-3 py-2 font-medium text-gray-300 hover:bg-gray-800 disabled:opacity-50"
            >
              Project only
            </button>
            <button
              type="button"
              onClick={() => restoring && restoreMutation.mutate({ project: restoring, withClips: true })}
              disabled={restoreMutation.isPending}
              className="rounded-lg bg-indigo-600 px-3 py-2 font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
            >
              Restore project and clips
            </button>
          </div>
        </div>
      </Modal>

      <Modal
        isOpen={createOpen}
        onClose={() => {
          setCreateOpen(false);
          setCreateError("");
        }}
        title="New project"
      >
        <form onSubmit={handleCreateSubmit} className="space-y-4">
          {createError && (
            <div className="rounded-lg border border-red-800/50 bg-red-900/20 px-3 py-2 text-sm text-red-400">
              {createError}
            </div>
          )}
          <div>
            <label
              htmlFor="col-name"
              className="mb-1 block text-sm text-gray-400"
            >
              Name
            </label>
            <input
              id="col-name"
              type="text"
              value={createName}
              onChange={(e) => setCreateName(e.target.value)}
              placeholder="Best of Europe"
              required
              autoFocus
              className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-gray-100 placeholder-gray-500 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            />
          </div>
          <div>
            <label
              htmlFor="col-desc"
              className="mb-1 block text-sm text-gray-400"
            >
              Description (optional)
            </label>
            <input
              id="col-desc"
              type="text"
              value={createDesc}
              onChange={(e) => setCreateDesc(e.target.value)}
              placeholder="My favorite travel photos"
              className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-gray-100 placeholder-gray-500 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            />
          </div>
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => setCreateOpen(false)}
              className="rounded-lg border border-gray-600 px-4 py-2 text-sm font-medium text-gray-300 transition-colors duration-150 hover:bg-gray-800"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={createMutation.isPending || !createName.trim()}
              className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors duration-150 hover:bg-indigo-500 disabled:opacity-50"
            >
              {createMutation.isPending ? "Creating..." : "Create"}
            </button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
