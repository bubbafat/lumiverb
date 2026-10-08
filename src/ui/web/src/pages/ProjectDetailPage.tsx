import { useMemo, useRef, useEffect, useState, useCallback, useLayoutEffect } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useInfiniteQuery, useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  getProject,
  listProjectAssets,
  updateProject,
  trashProject,
  restoreProjectClips,
  removeAssetsFromProject,
  ApiError,
} from "../api/client";
import { AssetCell } from "../components/AssetCell";
import { ProjectPicker } from "../components/ProjectPicker";
import { ExportButton } from "../components/ExportButton";
import { Lightbox } from "../components/Lightbox";
import { SelectionToolbar } from "../components/SelectionToolbar";
import { ZoomControl } from "../components/ZoomControl";
import type { AssetPageItem } from "../api/types";
import { savedQueryLabels } from "../lib/queryFilter";
import type { SavedQueryV2 } from "../lib/queryFilter";
import { useScrollContainer } from "../context/ScrollContainerContext";
import { groupAssetsByDate } from "../lib/groupByDate";
import { useSelection } from "../lib/useSelection";
import { buildVirtualRows, buildFixedGridRows } from "../lib/virtualRows";
import { useLocalStorage } from "../lib/useLocalStorage";
import type { VirtualRowKind } from "../lib/virtualRows";

const PAGE_SIZE = 200;
const ROW_GAP = 4;
const FIXED_GRID_BREAKPOINT = 700;

const ZOOM_LEVELS = [
  { justifiedHeight: 120, fixedCellWidth: 100 },
  { justifiedHeight: 170, fixedCellWidth: 130 },
  { justifiedHeight: 220, fixedCellWidth: 150 },
  { justifiedHeight: 300, fixedCellWidth: 200 },
  { justifiedHeight: 420, fixedCellWidth: 280 },
] as const;

const CELL_ASPECT_RATIO = 1.0;

export default function ProjectDetailPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const parentEl = useScrollContainer();

  // Project metadata
  const { data: project, isLoading: isProjectLoading } = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => getProject(projectId!),
    enabled: !!projectId,
  });

  // Settings state
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [editName, setEditName] = useState("");
  const [editDesc, setEditDesc] = useState("");
  const [editSort, setEditSort] = useState("manual");
  const [editVisibility, setEditVisibility] = useState("private");
  const [settingsError, setSettingsError] = useState("");


  // Grid state
  const [zoomLevel, setZoomLevel] = useLocalStorage("lv_grid_zoom", 2);
  const [containerWidth, setContainerWidth] = useState(0);
  // State, not a ref: the grid mounts only after the project loads, and
  // measuring has to start then.
  const [gridEl, setGridEl] = useState<HTMLDivElement | null>(null);
  const [lightboxAsset, setLightboxAsset] = useState<AssetPageItem | null>(null);

  // Fetch project assets with infinite query
  const {
    data,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
    isLoading: isAssetsLoading,
  } = useInfiniteQuery({
    queryKey: ["project-assets", projectId],
    queryFn: ({ pageParam }) =>
      listProjectAssets(projectId!, pageParam, PAGE_SIZE),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage?.next_cursor ?? undefined,
    enabled: !!projectId,
  });

  // Flatten pages into ordered asset list, adapted to AssetPageItem shape
  const orderedAssets: AssetPageItem[] = useMemo(() => {
    if (!data?.pages) return [];
    return data.pages.flatMap((page) =>
      page.items.map((item) => ({
        ...item,
        file_mtime: null,
        sha256: null,
        iso: null,
        aperture: null,
        focal_length: null,
        focal_length_35mm: null,
        lens_model: null,
        flash_fired: null,
        gps_lat: null,
        gps_lon: null,
        created_at: null,
      })),
    );
  }, [data]);

  // Group by date
  const groups = useMemo(
    () => groupAssetsByDate(orderedAssets),
    [orderedAssets],
  );

  const orderedAssetIds = useMemo(
    () => orderedAssets.map((a) => a.asset_id),
    [orderedAssets],
  );
  const selection = useSelection(orderedAssetIds);
  const [pickerAssetIds, setPickerAssetIds] = useState<string[] | null>(null);

  // Measure container width
  useLayoutEffect(() => {
    if (!gridEl) return;
    const ro = new ResizeObserver((entries) => {
      setContainerWidth(entries[0]?.contentRect.width ?? 0);
    });
    ro.observe(gridEl);
    return () => ro.disconnect();
  }, [gridEl]);

  const zoom = ZOOM_LEVELS[zoomLevel] ?? ZOOM_LEVELS[2];

  const virtualRows: VirtualRowKind[] = useMemo(() => {
    if (containerWidth <= 0) return [];
    if (containerWidth <= FIXED_GRID_BREAKPOINT) {
      const columns = Math.max(2, Math.floor(containerWidth / zoom.fixedCellWidth));
      const cellWidth = Math.floor(
        (containerWidth - ROW_GAP * (columns - 1)) / columns,
      );
      const rowHeight = Math.round(cellWidth * CELL_ASPECT_RATIO);
      return buildFixedGridRows(groups, containerWidth, columns, rowHeight, ROW_GAP);
    }
    return buildVirtualRows(groups, containerWidth, zoom.justifiedHeight, ROW_GAP);
  }, [groups, containerWidth, zoom]);

  const rowVirtualizer = useVirtualizer({
    count: virtualRows.length,
    getScrollElement: () => parentEl,
    estimateSize: (i) => virtualRows[i]?.height ?? zoom.justifiedHeight,
    overscan: 3,
    gap: ROW_GAP,
  });

  // Infinite scroll trigger
  const hasNextPageRef = useRef(hasNextPage);
  const isFetchingNextPageRef = useRef(isFetchingNextPage);
  hasNextPageRef.current = hasNextPage;
  isFetchingNextPageRef.current = isFetchingNextPage;

  useEffect(() => {
    if (!parentEl) return;
    const handleScroll = () => {
      const { scrollHeight, scrollTop, clientHeight } = parentEl;
      if (
        scrollHeight - scrollTop - clientHeight < 400 &&
        hasNextPageRef.current &&
        !isFetchingNextPageRef.current
      ) {
        fetchNextPage();
      }
    };
    parentEl.addEventListener("scroll", handleScroll, { passive: true });
    return () => parentEl.removeEventListener("scroll", handleScroll);
  }, [parentEl, fetchNextPage]);

  // Lightbox handlers
  const handleAssetClick = useCallback((asset: AssetPageItem) => {
    setLightboxAsset(asset);
  }, []);

  const handleLightboxClose = useCallback(() => {
    setLightboxAsset(null);
  }, []);

  const handleLightboxNavigate = useCallback(
    (index: number) => {
      const asset = orderedAssets[index];
      if (asset) setLightboxAsset(asset);
      if (hasNextPage && !isFetchingNextPage && index >= orderedAssets.length - 20) {
        fetchNextPage();
      }
    },
    [orderedAssets, hasNextPage, isFetchingNextPage, fetchNextPage],
  );

  // Settings mutations
  const updateMutation = useMutation({
    mutationFn: (body: Parameters<typeof updateProject>[1]) =>
      updateProject(projectId!, body),
    onSuccess: () => {
      setSettingsOpen(false);
      setSettingsError("");
      queryClient.invalidateQueries({ queryKey: ["project", projectId] });
      queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
    onError: (err: ApiError) => setSettingsError(err.message),
  });

  // To the trash, then to the list, which offers an undo.
  const trashMutation = useMutation({
    mutationFn: () => trashProject(projectId!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["projects"] });
      navigate("/projects", { state: { justTrashed: project } });
    },
  });

  const restoreClipsMutation = useMutation({
    mutationFn: () => restoreProjectClips(projectId!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["project", projectId] });
      queryClient.invalidateQueries({ queryKey: ["project-assets", projectId] });
      queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
  });

  const removeMutation = useMutation({
    mutationFn: () =>
      removeAssetsFromProject(projectId!, selection.toArray()),
    onSuccess: () => {
      selection.clear();
      queryClient.invalidateQueries({ queryKey: ["project-assets", projectId] });
      queryClient.invalidateQueries({ queryKey: ["project", projectId] });
      queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
  });

  const openSettings = () => {
    if (project) {
      setEditName(project.name);
      setEditDesc(project.description ?? "");
      setEditSort(project.sort_order);
      setEditVisibility(project.visibility);
      setSettingsError("");
    }
    setSettingsOpen(true);
  };

  const handleSettingsSave = (e: React.FormEvent) => {
    e.preventDefault();
    updateMutation.mutate({
      name: editName.trim(),
      description: editDesc.trim() || null,
      sort_order: editSort,
      visibility: editVisibility,
    });
  };

  const trashedClips = project?.trashed_asset_count ?? 0;
  const missingClips = project?.missing_asset_count ?? 0;

  if (isProjectLoading) {
    return (
      <div className="flex h-full items-center justify-center text-gray-500">
        Loading...
      </div>
    );
  }

  if (!project) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-4 text-gray-500">
        <p>Project not found</p>
        <Link to="/projects" className="text-indigo-400 hover:text-indigo-300">
          Back to projects
        </Link>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b border-gray-800 px-4 py-3">
        <div className="flex w-full min-w-0 items-center gap-3 sm:w-auto">
          <Link
            to="/projects"
            className="text-sm text-gray-500 hover:text-gray-300"
          >
            Projects
          </Link>
          <span className="text-gray-600">/</span>
          <h1 className="truncate text-lg font-semibold text-gray-100">
            {project.name}
          </h1>
          {project.type === "smart" && (
            <span className="shrink-0 rounded bg-indigo-900/60 px-1.5 py-0.5 text-[10px] text-indigo-300">
              Smart
            </span>
          )}
          <span className="shrink-0 text-sm text-gray-500">
            {project.asset_count} {project.asset_count === 1 ? "item" : "items"}
          </span>
        </div>
        {project.type === "smart" && project.saved_query && (
          <div className="flex flex-wrap gap-1.5">
            {savedQueryLabels(project.saved_query as SavedQueryV2).map((label: string) => (
              <span
                key={label}
                className="rounded-full bg-indigo-900/40 px-2.5 py-0.5 text-xs text-indigo-300"
              >
                {label}
              </span>
            ))}
          </div>
        )}
        <div className="flex items-center gap-2">
          <ZoomControl value={zoomLevel} onChange={setZoomLevel} />
          <ExportButton projectId={project.project_id} />
          <button
            type="button"
            onClick={openSettings}
            className="rounded-lg border border-gray-600 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-800/50"
          >
            Settings
          </button>
        </div>
      </div>

      {/* Description */}
      {project.description && (
        <div className="shrink-0 border-b border-gray-800/50 px-4 py-2">
          <p className="text-sm text-gray-400">{project.description}</p>
        </div>
      )}

      {/* Clips still in the project but not shown: trashed, or files missing */}
      {(trashedClips > 0 || missingClips > 0) && (
        <div
          role="status"
          className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b border-amber-900/40 bg-amber-950/30 px-4 py-2 text-sm text-amber-200/90"
        >
          {trashedClips > 0 && (
            <span>
              {trashedClips === 1 ? "1 clip is" : `${trashedClips} clips are`} in the trash, so
              {trashedClips === 1 ? " it isn't" : " they aren't"} shown or exported.
            </span>
          )}
          {missingClips > 0 && (
            <span>
              {missingClips === 1 ? "1 clip is" : `${missingClips} clips are`} missing from disk and will
              come back when {missingClips === 1 ? "its file does" : "their files do"}.
            </span>
          )}
          {trashedClips > 0 && (
            <button
              type="button"
              onClick={() => restoreClipsMutation.mutate()}
              disabled={restoreClipsMutation.isPending}
              className="rounded-md border border-amber-700/60 px-2.5 py-1 text-xs font-medium text-amber-100 hover:bg-amber-900/40 disabled:opacity-50"
            >
              Restore them
            </button>
          )}
        </div>
      )}

      {/* Grid */}
      <div ref={setGridEl} className="flex-1 min-h-0 px-2 py-2">
        {isAssetsLoading ? (
          <div className="flex h-32 items-center justify-center text-gray-500">
            Loading assets...
          </div>
        ) : orderedAssets.length === 0 ? (
          <div className="flex h-32 items-center justify-center text-gray-500">
            This project is empty.
          </div>
        ) : (
          <div
            style={{
              height: rowVirtualizer.getTotalSize(),
              width: "100%",
              position: "relative",
            }}
          >
            {rowVirtualizer.getVirtualItems().map((virtualItem) => {
              const vr = virtualRows[virtualItem.index];
              if (!vr) return null;

              const commonStyle: React.CSSProperties = {
                position: "absolute",
                top: 0,
                left: 0,
                width: "100%",
                height: `${virtualItem.size}px`,
                transform: `translateY(${virtualItem.start}px)`,
              };

              if (vr.type === "header") {
                return (
                  <div
                    key={virtualItem.key}
                    style={commonStyle}
                    className="flex items-end"
                  >
                    <div className="px-1 py-2 text-sm font-semibold text-gray-400">
                      {vr.label}
                    </div>
                  </div>
                );
              }

              const group = groups[vr.groupIndex];
              if (!group) return null;
              const { justifiedRow } = vr;

              let x = 0;

              return (
                <div key={virtualItem.key} style={commonStyle}>
                  <div
                    className="relative"
                    style={{
                      height: `${justifiedRow.height}px`,
                      marginTop: `${ROW_GAP}px`,
                    }}
                  >
                    {justifiedRow.items.map((itemIndex, idx) => {
                      const asset = group.assets[itemIndex];
                      if (!asset) return null;
                      const width = justifiedRow.widths[idx];
                      const left = x;
                      x += width + ROW_GAP;

                      const aspectRatio =
                        justifiedRow.widths[idx] / justifiedRow.height;

                      return (
                        <div
                          key={asset.asset_id}
                          className="absolute"
                          style={{
                            left,
                            top: 0,
                            width,
                            height: "100%",
                          }}
                        >
                          <AssetCell
                            asset={asset}
                            onClick={() => handleAssetClick(asset)}
                            aspectRatio={aspectRatio}
                            selected={selection.has(asset.asset_id)}
                            selectionActive={selection.isActive}
                            onSelect={(e) => selection.toggle(asset.asset_id, { shiftKey: e.shiftKey })}
                          />
                        </div>
                      );
                    })}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Lightbox */}
      {lightboxAsset && (
        <Lightbox
          asset={lightboxAsset}
          assets={orderedAssets}
          hasMore={hasNextPage}
          onClose={handleLightboxClose}
          onNavigate={handleLightboxNavigate}
          onAddToProject={(assetId) => setPickerAssetIds([assetId])}
          onSimilarClick={(similarAsset) => setLightboxAsset(similarAsset)}
        />
      )}

      {/* Selection toolbar */}
      <SelectionToolbar count={selection.count} onClear={selection.clear}>
        <button
          type="button"
          onClick={() => setPickerAssetIds(selection.toArray())}
          className="rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white transition-colors hover:bg-indigo-500"
        >
          Add to project
        </button>
        {project?.type !== "smart" && (
          <button
            type="button"
            onClick={() => removeMutation.mutate()}
            disabled={removeMutation.isPending}
            className="rounded-lg border border-red-700/50 px-3 py-1.5 text-sm font-medium text-red-400 transition-colors hover:bg-red-900/30 disabled:opacity-50"
          >
            {removeMutation.isPending ? "Removing..." : "Remove from project"}
          </button>
        )}
      </SelectionToolbar>

      {/* Project picker */}
      {pickerAssetIds && (
        <ProjectPicker
          assetIds={pickerAssetIds}
          onClose={() => setPickerAssetIds(null)}
          onDone={selection.clear}
        />
      )}

      {/* Settings modal */}
      {settingsOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          onClick={() => setSettingsOpen(false)}
        >
          <div
            className="w-full max-w-md rounded-xl bg-gray-900 p-6 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 className="mb-4 text-lg font-semibold text-gray-100">
              Project settings
            </h2>
            <form onSubmit={handleSettingsSave} className="space-y-4">
              {settingsError && (
                <div className="rounded-lg border border-red-800/50 bg-red-900/20 px-3 py-2 text-sm text-red-400">
                  {settingsError}
                </div>
              )}
              <div>
                <label htmlFor="edit-name" className="mb-1 block text-sm text-gray-400">
                  Name
                </label>
                <input
                  id="edit-name"
                  type="text"
                  value={editName}
                  onChange={(e) => setEditName(e.target.value)}
                  required
                  className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-gray-100 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
                />
              </div>
              <div>
                <label htmlFor="edit-desc" className="mb-1 block text-sm text-gray-400">
                  Description
                </label>
                <input
                  id="edit-desc"
                  type="text"
                  value={editDesc}
                  onChange={(e) => setEditDesc(e.target.value)}
                  className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-gray-100 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
                />
              </div>
              <div>
                <label htmlFor="edit-sort" className="mb-1 block text-sm text-gray-400">
                  Sort order
                </label>
                <select
                  id="edit-sort"
                  value={editSort}
                  onChange={(e) => setEditSort(e.target.value)}
                  className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-gray-100 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
                >
                  <option value="manual">Manual</option>
                  <option value="added_at">Date added</option>
                  <option value="taken_at">Date taken</option>
                </select>
              </div>
              <div>
                <label htmlFor="edit-visibility" className="mb-1 block text-sm text-gray-400">
                  Visibility
                </label>
                <select
                  id="edit-visibility"
                  value={editVisibility}
                  onChange={(e) => setEditVisibility(e.target.value)}
                  className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-gray-100 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
                >
                  <option value="private">Private (only you)</option>
                  <option value="shared">Shared (all team members)</option>
                  <option value="public">Public (anyone with link)</option>
                </select>
                {editVisibility === "public" && (
                  <p className="mt-1 text-xs text-amber-400">
                    Anyone with the link can view this project without signing in.
                  </p>
                )}
              </div>
              <div className="flex items-center justify-between pt-2">
                <button
                  type="button"
                  onClick={() => trashMutation.mutate()}
                  disabled={trashMutation.isPending}
                  className="text-sm text-red-400 hover:text-red-300 disabled:opacity-50"
                >
                  Move to trash
                </button>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => setSettingsOpen(false)}
                    className="rounded-lg border border-gray-600 px-4 py-2 text-sm font-medium text-gray-300 hover:bg-gray-800"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={updateMutation.isPending || !editName.trim()}
                    className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
                  >
                    {updateMutation.isPending ? "Saving..." : "Save"}
                  </button>
                </div>
              </div>
            </form>

          </div>
        </div>
      )}
    </div>
  );
}
