import { useEffect, useRef, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  listLibraries,
  getLibraryFilters,
  addLibraryFilter,
  deleteLibraryFilter,
  previewLibraryFilter,
  getTenantSettings,
  ApiError,
} from "../api/client";
import type { PathFilterItem, ProjectUsage } from "../api/client";
import { ProjectUsageList, clipCount } from "../components/ProjectUsageList";

/* ---------- pattern warning helper ---------- */

function detectDoubleStarExt(pattern: string): string | null {
  const segs = pattern.replace(/\\/g, "/").split("/");
  for (const seg of segs) {
    if (seg.startsWith("**") && seg.length > 2) {
      const ext = seg.slice(2);
      return `**/*${ext}`;
    }
  }
  return null;
}

/* ---------- FilterSection ---------- */

interface FilterSectionProps {
  title: string;
  subtitle: string;
  filters: PathFilterItem[];
  onAdd: (pattern: string) => Promise<unknown>;
  onDelete: (filterId: string) => void;
  isAdding: boolean;
  addError: string | null;
  emptyMessage: string;
  initialPattern?: string;
  isExclude?: boolean;
  libraryId?: string;
}

function FilterSection({
  title,
  subtitle,
  filters,
  onAdd,
  onDelete,
  isAdding,
  addError,
  emptyMessage,
  initialPattern,
  isExclude,
  libraryId,
}: FilterSectionProps) {
  const [pattern, setPattern] = useState(initialPattern ?? "");
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);
  const [confirmState, setConfirmState] = useState<{
    count: number;
    pattern: string;
  } | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [warningExpanded, setWarningExpanded] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // Focus and pre-populate when initialPattern is set
  useEffect(() => {
    if (initialPattern) {
      setPattern(initialPattern);
      inputRef.current?.focus();
    }
  }, [initialPattern]);

  const suggestedPattern = detectDoubleStarExt(pattern);
  const { data: settings } = useQuery({
    queryKey: ["tenant-settings"],
    queryFn: getTenantSettings,
    enabled: Boolean(isExclude),
    staleTime: 60_000,
  });
  const trashDays = settings?.trash_days === undefined ? 30 : settings.trash_days;

  const handleAdd = async () => {
    const trimmed = pattern.trim();
    if (!trimmed) return;

    // For exclude filters, preview first to check if assets will be trashed
    if (isExclude && libraryId) {
      setPreviewing(true);
      try {
        const preview = await previewLibraryFilter(libraryId, "exclude", trimmed);
        if (preview.matching_asset_count > 0) {
          setConfirmState({ count: preview.matching_asset_count, pattern: trimmed });
          return;
        }
      } catch {
        // If preview fails, proceed without confirmation
      } finally {
        setPreviewing(false);
      }
    }

    await add(trimmed);
  };

  // A failed add keeps what was typed; the page shows why.
  const add = async (value: string) => {
    try {
      await onAdd(value);
      setPattern("");
    } catch {
      // shown by the page (addError)
    }
  };

  const handleConfirm = async () => {
    if (!confirmState) return;
    setConfirmState(null);
    await add(confirmState.pattern);
  };

  return (
    <div className="space-y-3">
      <div>
        <h3 className="text-sm font-semibold text-gray-200">{title}</h3>
        <p className="text-xs text-gray-500">{subtitle}</p>
      </div>

      <div className="space-y-1">
        {filters.length === 0 ? (
          <p className="text-sm italic text-gray-500">{emptyMessage}</p>
        ) : (
          filters.map((f) => (
            <div
              key={f.filter_id}
              className="flex items-center justify-between rounded-lg border border-gray-700/50 bg-gray-900/50 px-4 py-3"
            >
              <div className="flex items-center gap-4 min-w-0">
                <span className="font-mono text-sm text-gray-400 truncate">
                  {f.pattern}
                </span>
                <span className="shrink-0 text-xs text-gray-600">
                  added{" "}
                  {new Date(f.created_at).toLocaleDateString(undefined, {
                    month: "short",
                    day: "numeric",
                  })}
                </span>
              </div>
              <div className="ml-4 shrink-0">
                {deleteConfirmId === f.filter_id ? (
                  <div className="flex items-center gap-2">
                    <span className="text-sm text-gray-400">Remove?</span>
                    <button
                      type="button"
                      onClick={() => {
                        onDelete(f.filter_id);
                        setDeleteConfirmId(null);
                      }}
                      className="rounded px-3 py-1.5 text-sm font-medium text-red-400 transition-colors duration-150 hover:bg-red-900/30"
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
                    onClick={() => setDeleteConfirmId(f.filter_id)}
                    className="rounded px-3 py-1.5 text-sm font-medium text-red-400 transition-colors duration-150 hover:bg-red-900/30"
                  >
                    Delete
                  </button>
                )}
              </div>
            </div>
          ))
        )}
      </div>

      <div className="space-y-2">
        <div className="flex gap-2">
          <input
            ref={inputRef}
            type="text"
            value={pattern}
            onChange={(e) => setPattern(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void handleAdd();
            }}
            placeholder="e.g. **/Proxy/**"
            className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 font-mono text-sm text-gray-100 placeholder-gray-500 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
          />
          <button
            type="button"
            onClick={() => void handleAdd()}
            disabled={isAdding || previewing || !pattern.trim()}
            className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors duration-150 hover:bg-indigo-500 disabled:opacity-50"
          >
            {previewing ? "Checking…" : isAdding ? "Adding…" : "Add"}
          </button>
        </div>

        {/* **.ext pattern warning */}
        {suggestedPattern && (
          <div className="flex items-start gap-2 rounded-lg border border-amber-700/50 bg-amber-900/20 px-3 py-2">
            <button
              type="button"
              onClick={() => setWarningExpanded(!warningExpanded)}
              className="mt-0.5 shrink-0 text-amber-400"
              title={`Did you mean ${suggestedPattern}?`}
            >
              <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" aria-hidden>
                <path d="M12 2L1 21h22L12 2z" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" />
                <path d="M12 10v4M12 17v.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
              </svg>
            </button>
            <p className="text-sm text-amber-300">
              Did you mean <button
                type="button"
                onClick={() => setPattern(pattern.replace(/\*\*([^/])/g, "**/*$1"))}
                className="font-mono underline hover:text-amber-200"
              >{suggestedPattern}</button>?
              {warningExpanded && (
                <span className="text-amber-400/80">
                  {" "}<code className="font-mono">**.ext</code> only matches files directly in the folder, not in subfolders.
                </span>
              )}
            </p>
          </div>
        )}

        {addError && (
          <p className="text-sm text-red-400">{addError}</p>
        )}

        {/* Trash confirmation modal */}
        {confirmState && (
          <div className="rounded-lg border border-red-700/50 bg-red-900/20 px-4 py-3">
            <p className="text-sm text-red-300">
              This will move <strong>{confirmState.count.toLocaleString()}</strong> existing
              {confirmState.count === 1 ? " clip" : " clips"} to the trash
              {trashDays ? `, deleted for good after ${trashDays} days unless restored,` : ""} and prevent future
              ingestion.
            </p>
            <div className="mt-3 flex gap-2">
              <button
                type="button"
                onClick={() => void handleConfirm()}
                disabled={isAdding}
                className="rounded-lg bg-red-600 px-4 py-1.5 text-sm font-medium text-white transition-colors duration-150 hover:bg-red-500 disabled:opacity-50"
              >
                {isAdding ? "Applying…" : "Confirm"}
              </button>
              <button
                type="button"
                onClick={() => setConfirmState(null)}
                className="rounded-lg px-4 py-1.5 text-sm font-medium text-gray-400 transition-colors duration-150 hover:text-gray-200"
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/* ---------- Page ---------- */

export default function LibrarySettingsPage() {
  const { libraryId } = useParams<{ libraryId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();

  const excludeParam = searchParams.get("exclude");

  const [includeAddError, setIncludeAddError] = useState<string | null>(null);
  const [excludeAddError, setExcludeAddError] = useState<string | null>(null);
  // Projects use clips the exclude filter would trash: the API asked first.
  const [projectsAsk, setProjectsAsk] = useState<{ pattern: string; usage: ProjectUsage } | null>(null);

  const { data: libraries } = useQuery({
    queryKey: ["libraries", true],
    queryFn: () => listLibraries(true),
  });

  const library = libraries?.find((l) => l.library_id === libraryId);

  const {
    data: filters,
    isLoading: filtersLoading,
    error: filtersError,
  } = useQuery({
    queryKey: ["library-filters", libraryId],
    queryFn: () => getLibraryFilters(libraryId!),
    enabled: !!libraryId,
  });

  const invalidateAll = () => {
    void queryClient.invalidateQueries({ queryKey: ["library-filters", libraryId] });
    void queryClient.invalidateQueries({ queryKey: ["assets"] });
    void queryClient.invalidateQueries({ queryKey: ["directories"] });
    void queryClient.invalidateQueries({ queryKey: ["revision"] });
  };

  const clearExcludeParam = () => {
    if (excludeParam) {
      setSearchParams((prev) => {
        const next = new URLSearchParams(prev);
        next.delete("exclude");
        next.delete("tab");
        return next;
      }, { replace: true });
    }
  };

  const addIncludeMutation = useMutation({
    mutationFn: (pattern: string) =>
      addLibraryFilter(libraryId!, "include", pattern),
    onSuccess: () => {
      setIncludeAddError(null);
      invalidateAll();
    },
    onError: (err: ApiError) => setIncludeAddError(err.message),
  });

  type ExcludeVars = { pattern: string; trashMatching: boolean; removeFromProjects?: boolean };
  const addExcludeMutation = useMutation({
    mutationFn: ({ pattern, trashMatching, removeFromProjects }: ExcludeVars) =>
      addLibraryFilter(libraryId!, "exclude", pattern, trashMatching, removeFromProjects ?? false),
    onSuccess: () => {
      setExcludeAddError(null);
      setProjectsAsk(null);
      clearExcludeParam();
      invalidateAll();
    },
    onError: (err: ApiError, vars) => {
      if (err.code === "in_projects" && err.details) {
        setProjectsAsk({ pattern: vars.pattern, usage: err.details as unknown as ProjectUsage });
      } else {
        setExcludeAddError(err.message);
      }
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (filterId: string) =>
      deleteLibraryFilter(libraryId!, filterId),
    onSuccess: () => invalidateAll(),
  });

  return (
    <div className="mx-auto max-w-3xl px-6 py-6">
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-semibold">Library Settings</h1>
          {library && (
            <p className="mt-1 text-sm text-gray-500">{library.name}</p>
          )}
        </div>

        <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 px-4 py-3">
          <div className="mb-4">
            <h2 className="text-lg font-semibold text-gray-100">
              Path Filters
            </h2>
            <p className="mt-1 text-sm text-gray-400">
              Control which paths are indexed from this library's root (e.g.
              include only <span className="font-mono">Photos/**</span>, exclude
              all Proxy folders). Includes are applied first; excludes prune the
              result.
            </p>
          </div>

          {filtersError && (
            <div className="mb-4 rounded-lg border border-red-800/50 bg-red-900/20 px-3 py-2 text-sm text-red-400">
              {(filtersError as Error).message}
            </div>
          )}

          {filtersLoading ? (
            <p className="text-sm text-gray-500">Loading…</p>
          ) : (
            <div className="space-y-6">
              <FilterSection
                title="Include patterns"
                subtitle="Only paths matching these patterns are indexed. If empty, all paths are included."
                filters={filters?.includes ?? []}
                onAdd={(pattern) => {
                  setIncludeAddError(null);
                  return addIncludeMutation.mutateAsync(pattern);
                }}
                onDelete={(filterId) => deleteMutation.mutate(filterId)}
                isAdding={addIncludeMutation.isPending}
                addError={includeAddError}
                emptyMessage="No include patterns. All paths are included by default."
              />

              <div className="border-t border-gray-700/50" />

              <FilterSection
                title="Exclude patterns"
                subtitle="Paths matching these patterns are excluded after includes are applied."
                filters={filters?.excludes ?? []}
                onAdd={(pattern) => {
                  setExcludeAddError(null);
                  setProjectsAsk(null);
                  // The projects question is shown below, not thrown; any other error keeps the typed pattern.
                  return addExcludeMutation.mutateAsync({ pattern, trashMatching: true }).catch((err) => {
                    if (!(err instanceof ApiError && err.code === "in_projects")) throw err;
                  });
                }}
                onDelete={(filterId) => deleteMutation.mutate(filterId)}
                isAdding={addExcludeMutation.isPending}
                addError={excludeAddError}
                emptyMessage="No exclude patterns."
                initialPattern={excludeParam ?? undefined}
                isExclude
                libraryId={libraryId}
              />
              {projectsAsk && (
                <div role="alertdialog" aria-label="Clips in projects" className="space-y-3">
                  <ProjectUsageList usage={projectsAsk.usage}>
                    {clipCount(projectsAsk.usage.assets_in_projects)} that{" "}
                    <span className="font-mono">{projectsAsk.pattern}</span> matches{" "}
                    {projectsAsk.usage.assets_in_projects === 1 ? "is" : "are"} in projects. In the trash{" "}
                    {projectsAsk.usage.assets_in_projects === 1 ? "it's" : "they're"} hidden there; deleted for good,{" "}
                    {projectsAsk.usage.assets_in_projects === 1 ? "it leaves" : "they leave"} those projects.
                  </ProjectUsageList>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      disabled={addExcludeMutation.isPending}
                      onClick={() =>
                        addExcludeMutation.mutate({ pattern: projectsAsk.pattern, trashMatching: true, removeFromProjects: true })
                      }
                      className="rounded-lg bg-red-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-red-500 disabled:opacity-50"
                    >
                      Exclude and move them to the trash
                    </button>
                    <button
                      type="button"
                      onClick={() => setProjectsAsk(null)}
                      className="rounded-lg px-4 py-1.5 text-sm font-medium text-gray-400 hover:text-gray-200"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
