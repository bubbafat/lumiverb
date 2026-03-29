import { useCallback, useEffect, useRef, useState } from "react";
import { listDirectories } from "../api/client";
import type { DirectoryNode } from "../api/types";

export interface DirectoryTreeProps {
  libraryId: string;
  activePath: string | null;
  onNavigate: (path: string | null) => void;
  revision?: number;
  onExcludeFolder?: (path: string) => void;
}

export function DirectoryTree({
  libraryId,
  activePath,
  onNavigate,
  revision,
  onExcludeFolder,
}: DirectoryTreeProps) {
  const [rootNodes, setRootNodes] = useState<DirectoryNode[] | null>(null);
  const [childrenCache, setChildrenCache] = useState<
    Map<string, DirectoryNode[]>
  >(new Map());
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(new Set());
  const [loadingPath, setLoadingPath] = useState<string | null>(null);
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

  // Full reset when library changes
  useEffect(() => {
    let cancelled = false;
    setRootNodes(null);
    setChildrenCache(new Map());
    setExpandedPaths(new Set());
    setLoadingPath(null);

    listDirectories(libraryId)
      .then((nodes) => {
        if (!cancelled) setRootNodes(nodes);
      })
      .catch(() => {
        if (!cancelled) setRootNodes([]);
      });

    return () => {
      cancelled = true;
    };
  }, [libraryId]);

  // Seamless refresh when revision changes (keep existing data visible)
  useEffect(() => {
    if (revision === undefined) return;
    let cancelled = false;

    listDirectories(libraryId)
      .then((nodes) => {
        if (!cancelled) setRootNodes(nodes);
      })
      .catch(() => {
        // keep existing data on error
      });

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revision]);

  const loadChildren = useCallback(
    (path: string) => {
      setLoadingPath(path);
      listDirectories(libraryId, path)
        .then((nodes) => {
          setChildrenCache((prev) => new Map(prev).set(path, nodes));
          setExpandedPaths((prev) => new Set(prev).add(path));
        })
        .catch(() => {
          setChildrenCache((prev) => new Map(prev).set(path, []));
        })
        .finally(() => setLoadingPath(null));
    },
    [libraryId],
  );

  const toggleExpand = useCallback(
    (path: string, e: React.MouseEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const cached = childrenCache.get(path);
      if (cached !== undefined) {
        setExpandedPaths((prev) => {
          const next = new Set(prev);
          if (next.has(path)) next.delete(path);
          else next.add(path);
          return next;
        });
      } else {
        loadChildren(path);
      }
    },
    [childrenCache, loadChildren],
  );

  const handleNodeClick = useCallback(
    (path: string) => {
      onNavigate(path);
    },
    [onNavigate],
  );

  const renderNodes = (nodes: DirectoryNode[], depth: number) => {
      return nodes.map((node) => {
        const isExpanded = expandedPaths.has(node.path);
        const children = childrenCache.get(node.path);
        const hasFetched = children !== undefined;
        const showChevron = !hasFetched || children.length > 0;
        const isLoading = loadingPath === node.path;
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
                {onExcludeFolder ? (
                  <button
                    type="button"
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
                {isDropdownOpen && onExcludeFolder && (
                  <div
                    ref={dropdownRef}
                    className="absolute right-0 top-full z-50 mt-1 w-40 rounded-lg border border-gray-700 bg-gray-800 py-1 shadow-lg"
                  >
                    <button
                      type="button"
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
