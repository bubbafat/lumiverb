import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  listProjects,
  createProject,
  deleteProject,
  setProjectStatus,
  ApiError,
  type ProjectStatus,
} from "../api/client";
import { useAuthenticatedImage } from "../api/useAuthenticatedImage";
import { Modal } from "../components/Modal";
import { SkeletonRow } from "../components/SkeletonRow";
import type { ProjectItem } from "../api/types";

function ProjectCard({
  project,
  onDelete,
  onToggleArchive,
  deleteConfirmId,
  setDeleteConfirmId,
  isDeleting,
}: {
  project: ProjectItem;
  onDelete: (id: string) => void;
  onToggleArchive: (project: ProjectItem) => void;
  deleteConfirmId: string | null;
  setDeleteConfirmId: (id: string | null) => void;
  isDeleting: boolean;
}) {
  const archived = project.status === "archived";
  const { url: coverUrl } = useAuthenticatedImage(
    project.cover_asset_id ?? "",
    "thumbnail",
    { enabled: !!project.cover_asset_id },
  );

  return (
    <div className="group relative overflow-hidden rounded-lg border border-gray-700/50 bg-gray-900/50 transition-colors duration-150 hover:border-gray-600/50">
      <Link to={`/projects/${project.project_id}`}>
        <div className="aspect-[4/3] bg-gray-800">
          {coverUrl ? (
            <img
              src={coverUrl}
              alt={project.name}
              className="h-full w-full object-cover"
            />
          ) : (
            <div className="flex h-full w-full items-center justify-center text-gray-600">
              <svg
                className="h-12 w-12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                aria-hidden
              >
                <rect x="3" y="3" width="18" height="18" rx="2" />
                <path d="M3 15l5-5 4 4 4-6 5 7" />
              </svg>
            </div>
          )}
        </div>
      </Link>
      <div className="p-3">
        <div className="flex flex-col gap-1 sm:flex-row sm:items-start sm:justify-between sm:gap-2">
          <div className="min-w-0">
            <Link
              to={`/projects/${project.project_id}`}
              className="block truncate font-medium text-gray-100 hover:text-indigo-300"
            >
              {project.name}
            </Link>
            <div className="mt-0.5 flex items-center gap-2">
              <span className="text-xs text-gray-500">
                {project.asset_count}{" "}
                {project.asset_count === 1 ? "item" : "items"}
              </span>
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
          <div className="-ml-2 shrink-0 sm:ml-0">
            {deleteConfirmId === project.project_id ? (
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => onDelete(project.project_id)}
                  disabled={isDeleting}
                  className="rounded px-2 py-1 text-xs font-medium text-red-400 hover:bg-red-900/30"
                >
                  Confirm
                </button>
                <button
                  type="button"
                  onClick={() => setDeleteConfirmId(null)}
                  className="rounded px-2 py-1 text-xs text-gray-400 hover:text-gray-300"
                >
                  Cancel
                </button>
              </div>
            ) : (
              <div className="flex items-center gap-1 transition-opacity focus-within:opacity-100 [@media(hover:hover)]:opacity-0 [@media(hover:hover)]:group-hover:opacity-100">
                <button
                  type="button"
                  onClick={() => onToggleArchive(project)}
                  className="rounded px-2 py-1 text-xs text-gray-500 hover:text-gray-200"
                >
                  {archived ? "Restore" : "Archive"}
                </button>
                <button
                  type="button"
                  onClick={() => setDeleteConfirmId(project.project_id)}
                  className="rounded px-2 py-1 text-xs text-gray-500 hover:text-red-400"
                >
                  Delete
                </button>
              </div>
            )}
          </div>
        </div>
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
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);
  const [view, setView] = useState<ProjectStatus>("active");

  const { data: projects, isLoading, error } = useQuery({
    queryKey: ["projects", view],
    queryFn: () => listProjects(view),
    refetchInterval: 10_000,
  });

  const archiveMutation = useMutation({
    mutationFn: (project: ProjectItem) =>
      setProjectStatus(
        project.project_id,
        project.status === "archived" ? "active" : "archived",
      ),
    // Prefix match: refreshes this page, the sidebar and pickers.
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["projects"] }),
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
      queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
    onError: (err: ApiError) => setCreateError(err.message),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteProject(id),
    onSuccess: () => {
      setDeleteConfirmId(null);
      queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
  });

  const handleCreateSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setCreateError("");
    if (!createName.trim()) return;
    createMutation.mutate();
  };

  return (
    <div className="mx-auto max-w-4xl px-6 py-6">
      <div className="space-y-6">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-center gap-4">
            <h1 className="text-2xl font-semibold">Projects</h1>
            <div className="flex rounded-lg border border-gray-700/50 p-0.5 text-sm">
              {(["active", "archived"] as const).map((v) => (
                <button
                  key={v}
                  type="button"
                  onClick={() => setView(v)}
                  aria-pressed={view === v}
                  className={`rounded-md px-3 py-1 capitalize transition-colors duration-150 ${
                    view === v ? "bg-gray-700 text-gray-100" : "text-gray-400 hover:text-gray-200"
                  }`}
                >
                  {v}
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

        {isLoading ? (
          <div className="space-y-4">
            <SkeletonRow />
            <SkeletonRow />
          </div>
        ) : projects?.length === 0 ? (
          <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-8 text-center text-gray-400">
            {view === "archived"
              ? "No archived projects."
              : "No projects yet. Create one to start curating."}
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
            {projects?.map((col) => (
              <ProjectCard
                key={col.project_id}
                project={col}
                onDelete={(id) => deleteMutation.mutate(id)}
                onToggleArchive={(p) => archiveMutation.mutate(p)}
                deleteConfirmId={deleteConfirmId}
                setDeleteConfirmId={setDeleteConfirmId}
                isDeleting={deleteMutation.isPending}
              />
            ))}
          </div>
        )}
      </div>

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
