import { useState } from "react";
import { useParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  listLibraries,
  getLibraryFilters,
  addLibraryFilter,
  deleteLibraryFilter,
  ApiError,
} from "../api/client";
import type { PathFilterItem } from "../api/client";

interface FilterSectionProps {
  title: string;
  subtitle: string;
  filters: PathFilterItem[];
  onAdd: (pattern: string) => Promise<void>;
  onDelete: (filterId: string) => void;
  isAdding: boolean;
  addError: string | null;
  emptyMessage: string;
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
}: FilterSectionProps) {
  const [pattern, setPattern] = useState("");
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);

  const handleAdd = async () => {
    const trimmed = pattern.trim();
    if (!trimmed) return;
    await onAdd(trimmed);
    setPattern("");
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
            disabled={isAdding || !pattern.trim()}
            className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors duration-150 hover:bg-indigo-500 disabled:opacity-50"
          >
            {isAdding ? "Adding…" : "Add"}
          </button>
        </div>
        {addError && (
          <p className="text-sm text-red-400">{addError}</p>
        )}
      </div>
    </div>
  );
}

export default function LibrarySettingsPage() {
  const { libraryId } = useParams<{ libraryId: string }>();
  const queryClient = useQueryClient();

  const [includeAddError, setIncludeAddError] = useState<string | null>(null);
  const [excludeAddError, setExcludeAddError] = useState<string | null>(null);

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

  const invalidateFilters = () =>
    queryClient.invalidateQueries({ queryKey: ["library-filters", libraryId] });

  const addIncludeMutation = useMutation({
    mutationFn: (pattern: string) =>
      addLibraryFilter(libraryId!, "include", pattern),
    onSuccess: () => {
      setIncludeAddError(null);
      void invalidateFilters();
    },
    onError: (err: ApiError) => setIncludeAddError(err.message),
  });

  const addExcludeMutation = useMutation({
    mutationFn: (pattern: string) =>
      addLibraryFilter(libraryId!, "exclude", pattern),
    onSuccess: () => {
      setExcludeAddError(null);
      void invalidateFilters();
    },
    onError: (err: ApiError) => setExcludeAddError(err.message),
  });

  const deleteMutation = useMutation({
    mutationFn: (filterId: string) =>
      deleteLibraryFilter(libraryId!, filterId),
    onSuccess: () => void invalidateFilters(),
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
                  return addExcludeMutation.mutateAsync(pattern);
                }}
                onDelete={(filterId) => deleteMutation.mutate(filterId)}
                isAdding={addExcludeMutation.isPending}
                addError={excludeAddError}
                emptyMessage="No exclude patterns."
              />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
