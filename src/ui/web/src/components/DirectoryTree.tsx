import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { listDirectories } from "../api/client";
import type { DirectoryNode } from "../api/types";
import { directoriesKey } from "../lib/directoriesKey";

export interface DirectoryTreeProps {
  libraryId: string;
  activePath: string | null;
  onNavigate: (path: string | null) => void;
  onExcludeFolder?: (path: string) => void;
  /** Archive every clip under the folder; count is how many (subfolders included). */
  onArchiveFolder?: (path: string, count: number) => void;
}

export function DirectoryTree({
  libraryId,
  activePath,
  onNavigate,
  onExcludeFolder,
  onArchiveFolder,
}: DirectoryTreeProps) {
  const hasMenu = Boolean(onExcludeFolder || onArchiveFolder);
  const queryClient = useQueryClient();
  // Open folders belong to one library.
  const [expanded, setExpanded] = useState<{ libraryId: string; paths: Set<string> }>({
    libraryId,
    paths: new Set(),
  });
  const expandedPaths = useMemo(
    () => (expanded.libraryId === libraryId ? expanded.paths : new Set<string>()),
    [expanded, libraryId],
  );
  const [dropdownPath, setDropdownPath] = useState<string | null>(null);
  const dropdownRef = useRef<HTMLDivElement>(null);

  // Close dropdown on click-outside or Escape
  useEffect(() => {
    if (dropdownPath === null) return;
    const handleClick = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setDropdownPath(null);
      }
    };
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setDropdownPath(null);
    };
    document.addEventListener("mousedown", handleClick);
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("mousedown", handleClick);
      document.removeEventListener("keydown", handleKey);
    };
  }, [dropdownPath]);

  // Another library starts with every folder closed (the guard above covers
  // the render before this runs).
  useEffect(() => {
    setExpanded({ libraryId, paths: new Set() });
    setDropdownPath(null);
  }, [libraryId]);

  // Each listing is a query, ["directories", libraryId, parent]: the page
  // invalidates ["directories", libraryId] when the library changes (at most
  // every 30 seconds while it keeps changing, see useRevisionRefresh), so the
  // top level and every open folder refetch together, and a closed one
  // refetches when it's opened again.
  const rootQuery = useQuery({
    queryKey: directoriesKey(libraryId, null),
    queryFn: () => listDirectories(libraryId),
  });
  const rootNodes = rootQuery.data ?? (rootQuery.isError ? [] : null);

  // The listings of open folders that are on screen: open, under open
  // parents. One under a closed parent stays open for when the parent opens
  // again, but isn't fetched (or refreshed) until then.
  const openPaths = useMemo(
    () =>
      [...expandedPaths].filter((path) => {
        const parts = path.split("/");
        for (let i = 1; i < parts.length; i++) {
          if (!expandedPaths.has(parts.slice(0, i).join("/"))) return false;
        }
        return true;
      }),
    [expandedPaths],
  );
  const childQueries = useQueries({
    queries: openPaths.map((path) => ({
      queryKey: directoriesKey(libraryId, path),
      queryFn: () => listDirectories(libraryId, path),
    })),
  });
  const childrenOf = (path: string) => {
    const i = openPaths.indexOf(path);
    if (i >= 0) {
      const q = childQueries[i];
      return { children: q.data ?? (q.isError ? [] : undefined), isLoading: q.isPending && !q.isError };
    }
    // A closed folder: what it held when last open, for its chevron.
    return {
      children: queryClient.getQueryData<DirectoryNode[]>(directoriesKey(libraryId, path)),
      isLoading: false,
    };
  };

  const toggleExpand = useCallback((path: string, e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setExpanded((prev) => {
      const next = new Set(prev.libraryId === libraryId ? prev.paths : []);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return { libraryId, paths: next };
    });
  }, [libraryId]);

  const handleNodeClick = useCallback(
    (path: string) => {
      onNavigate(path);
    },
    [onNavigate],
  );

  const renderNodes = (nodes: DirectoryNode[], depth: number) => {
      return nodes.map((node) => {
        const { children, isLoading } = childrenOf(node.path);
        const hasFetched = children !== undefined;
        const isExpanded = expandedPaths.has(node.path) && hasFetched;
        const showChevron = !hasFetched || children.length > 0;
        const isActive = activePath === node.path;
        const isDropdownOpen = dropdownPath === node.path;

        return (
          <div key={node.path}>
            <div
              className={`flex items-center rounded-lg text-sm transition-colors duration-150 ${
                isActive
                  ? "bg-indigo-600/30 text-indigo-200"
                  : "text-gray-300 hover:bg-gray-800/80"
              }`}
              style={{ paddingLeft: 8 + depth * 8 }}
            >
              <button
                type="button"
                onClick={(e) => toggleExpand(node.path, e)}
                aria-label={isExpanded ? "Collapse" : "Expand"}
                className="flex h-10 w-10 shrink-0 items-center justify-center p-2"
              >
                {showChevron ? (
                  <svg
                    className={`h-4 w-4 text-gray-400 transition-transform duration-150 motion-reduce:transition-none ${isExpanded ? "rotate-90" : ""}`}
                    viewBox="0 0 24 24"
                    fill="none"
                    aria-hidden
                  >
                    <path
                      d="M9 6l6 6-6 6"
                      stroke="currentColor"
                      strokeWidth="1.7"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                ) : (
                  <span className="h-4 w-4" />
                )}
              </button>
              <button
                type="button"
                onClick={() => handleNodeClick(node.path)}
                className="flex min-w-0 flex-1 items-center gap-2 py-1.5 pr-2 text-left"
              >
                <span className="h-2 w-2 shrink-0 rounded-full bg-gray-500" />
                <span className="min-w-0 flex-1 truncate">{node.name}</span>
              </button>
              {/* Asset count: plain text or clickable with dropdown */}
              <div className="relative shrink-0">
                {hasMenu ? (
                  <button
                    type="button"
                    aria-label={`${node.name}: ${node.asset_count === 1 ? "1 clip" : `${node.asset_count} clips`}, folder actions`}
                    aria-haspopup="menu"
                    aria-expanded={isDropdownOpen}
                    onClick={(e) => {
                      e.stopPropagation();
                      setDropdownPath(isDropdownOpen ? null : node.path);
                    }}
                    className="rounded px-1.5 py-0.5 text-xs text-gray-500 hover:bg-gray-700/50 hover:text-gray-300"
                  >
                    {node.asset_count}
                  </button>
                ) : (
                  <span className="px-1.5 py-0.5 text-xs text-gray-500">
                    {node.asset_count}
                  </span>
                )}
                {isDropdownOpen && hasMenu && (
                  <div
                    ref={dropdownRef}
                    role="menu"
                    className="absolute right-0 top-full z-50 mt-1 w-44 rounded-lg border border-gray-700 bg-gray-800 py-1 shadow-lg"
                  >
                    {onArchiveFolder && (
                      <button
                        type="button"
                        role="menuitem"
                        onClick={(e) => {
                          e.stopPropagation();
                          setDropdownPath(null);
                          onArchiveFolder(node.path, node.asset_count);
                        }}
                        className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm text-gray-200 hover:bg-gray-700/50"
                      >
                        <svg className="h-4 w-4 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden>
                          <path strokeLinecap="round" strokeLinejoin="round" d="M4 7h16M5 7v11a2 2 0 002 2h10a2 2 0 002-2V7M9 11h6M3 4h18v3H3z" />
                        </svg>
                        Archive folder
                      </button>
                    )}
                    {onExcludeFolder && (
                      <button
                        type="button"
                        role="menuitem"
                        onClick={(e) => {
                          e.stopPropagation();
                          setDropdownPath(null);
                          onExcludeFolder(node.path);
                        }}
                        className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm text-red-400 hover:bg-gray-700/50"
                      >
                        <svg className="h-4 w-4 shrink-0" viewBox="0 0 24 24" fill="none" aria-hidden>
                          <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.7" />
                          <path d="M5.5 18.5l13-13" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
                        </svg>
                        Exclude folder
                      </button>
                    )}
                  </div>
                )}
              </div>
            </div>
            {isLoading && (
              <div
                className="flex items-center gap-2 rounded-lg px-2 py-1.5"
                style={{ paddingLeft: 8 + (depth + 1) * 8 }}
              >
                <div className="h-4 w-4 shrink-0" />
                <div className="h-2 w-2 shrink-0 rounded-full bg-gray-600" />
                <div className="h-4 flex-1 animate-pulse rounded bg-gray-800" />
              </div>
            )}
            {isExpanded && hasFetched && children && children.length > 0 && (
              <div>{renderNodes(children, depth + 1)}</div>
            )}
          </div>
        );
      });
  };

  if (rootNodes === null) {
    return (
      <div className="mt-1 space-y-1">
        <div
          className="flex items-center gap-2 rounded-lg px-2 py-1.5"
          style={{ paddingLeft: 8 }}
        >
          <div className="h-4 w-4 shrink-0" />
          <div className="h-2 w-2 shrink-0 rounded-full bg-gray-600" />
          <div className="h-4 flex-1 animate-pulse rounded bg-gray-800" />
        </div>
      </div>
    );
  }

  if (rootNodes.length === 0) {
    return null;
  }

  return <div className="mt-1 space-y-0.5">{renderNodes(rootNodes, 0)}</div>;
}
