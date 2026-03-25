import { useMemo } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { listJobs, listLibraries, logout } from "../api/client";
import type { JobListItem, LibraryListItem } from "../api/types";
import { DirectoryTree } from "./DirectoryTree";

const SIDEBAR_COLLAPSED_KEY = "lv_sidebar_collapsed";

type LibraryScanStatus = LibraryListItem["scan_status"];

function scanStatusColor(status: LibraryScanStatus): string {
  if (status === "running" || status === "scanning") return "bg-amber-500";
  if (status === "error") return "bg-red-500";
  return "bg-emerald-500";
}

function photoStackIcon() {
  return (
    <svg
      className="h-5 w-5 text-gray-300"
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden
    >
      <rect
        x="4"
        y="7"
        width="14"
        height="11"
        rx="2"
        className="stroke-gray-400"
        strokeWidth="1.5"
      />
      <rect
        x="7"
        y="4"
        width="13"
        height="11"
        rx="2"
        className="stroke-gray-500"
        strokeWidth="1.5"
      />
    </svg>
  );
}

function gearIcon() {
  return (
    <svg
      className="h-4 w-4 text-gray-400"
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden
    >
      <path
        d="M10.325 4.317a1.5 1.5 0 0 1 3.35 0l.143.955a1.5 1.5 0 0 0 2.104 1.128l.88-.439a1.5 1.5 0 0 1 2.012.683l.75 1.5a1.5 1.5 0 0 1-.683 2.012l-.88.44a1.5 1.5 0 0 0-1.128 2.103l.439.88a1.5 1.5 0 0 1-.683 2.012l-1.5.75a1.5 1.5 0 0 1-2.012-.683l-.44-.88a1.5 1.5 0 0 0-2.103-1.128l-.88.439a1.5 1.5 0 0 1-2.012-.683l-.75-1.5a1.5 1.5 0 0 1 .683-2.012l.88-.44a1.5 1.5 0 0 0 1.128-2.103l-.439-.88a1.5 1.5 0 0 1 .683-2.012z"
        className="stroke-gray-500"
        strokeWidth="1.3"
      />
      <circle
        cx="12"
        cy="12"
        r="2.5"
        className="stroke-gray-300"
        strokeWidth="1.3"
      />
    </svg>
  );
}

function settingsIcon() {
  return (
    <svg
      className="h-4 w-4 text-gray-400"
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden
    >
      <path
        d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"
        className="stroke-gray-500"
        strokeWidth="1.3"
      />
      <circle cx="12" cy="12" r="3" className="stroke-gray-300" strokeWidth="1.3" />
    </svg>
  );
}

function ChevronToggleIcon({ collapsed }: { collapsed: boolean }) {
  return (
    <svg
      className="h-4 w-4 text-gray-400"
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden
    >
      <path
        d={collapsed ? "M9 6l6 6-6 6" : "M15 6l-6 6 6 6"}
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export interface SidebarProps {
  collapsed: boolean;
  onToggleCollapsed: () => void;
  onOpenPalette?: () => void;
}

export function Sidebar({ collapsed, onToggleCollapsed, onOpenPalette }: SidebarProps) {
  const { libraryId } = useParams<{ libraryId: string }>();
  const location = useLocation();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const activePath = searchParams.get("path");

  const onNavigate = (path: string | null) => {
    if (!libraryId) return;
    if (path) {
      navigate(
        `/libraries/${libraryId}/browse?path=${encodeURIComponent(path)}`,
      );
    } else {
      navigate(`/libraries/${libraryId}/browse`);
    }
  };

  const { data: libraries, isLoading: isLibrariesLoading } = useQuery({
    queryKey: ["libraries", false],
    queryFn: () => listLibraries(false),
  });

  const { data: jobs } = useQuery<JobListItem[]>({
    queryKey: ["jobs", "running"],
    queryFn: () => listJobs({ status: "running", limit: 5 }),
    refetchInterval: 10_000,
  });

  const runningCount = jobs?.length ?? 0;

  const isLibrariesRootActive = location.pathname === "/";

  const items = useMemo(
    () => libraries?.filter((lib) => lib.status !== "trashed") ?? [],
    [libraries],
  );

  const showLabels = !collapsed;

  return (
    <aside className="flex h-full flex-col bg-gray-900 border-r border-gray-800 transition-all duration-200">
      <div className="flex items-center gap-2 px-3 py-4">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-indigo-600/20 text-indigo-300">
          <span className="text-lg font-semibold">Lv</span>
        </div>
        {showLabels && (
          <div className="flex flex-col">
            <span className="text-sm font-semibold tracking-tight text-gray-100">
              Lumiverb
            </span>
            <span className="text-xs text-gray-500">Media library</span>
          </div>
        )}
      </div>

      <div className="border-t border-gray-800" />

      {/* Go to library / command palette trigger */}
      {onOpenPalette && (
        <div className="px-2 pt-2">
          <button
            type="button"
            onClick={onOpenPalette}
            title="Go to library (⌘K)"
            className="flex w-full items-center gap-2 rounded-lg border border-gray-700/50 bg-gray-800/50 px-2 py-1.5 text-xs text-gray-500 transition-colors hover:bg-gray-800 hover:text-gray-300"
            aria-label="Open library switcher"
          >
            <svg
              className="h-3.5 w-3.5 shrink-0"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden
            >
              <circle cx="11" cy="11" r="7" />
              <line x1="16.65" y1="16.65" x2="21" y2="21" />
            </svg>
            {showLabels && (
              <>
                <span className="flex-1 text-left">Go to library</span>
                <kbd className="font-mono text-[10px] text-gray-600">⌘K</kbd>
              </>
            )}
          </button>
        </div>
      )}

      <div className="flex-1 overflow-y-auto px-2 py-3">
        {/* Active library context header */}
        {libraryId && showLabels && (() => {
          const activeName = items.find((l) => l.library_id === libraryId)?.name;
          return activeName ? (
            <div className="mb-2 px-2 py-1.5 border-b border-gray-800/60">
              <p className="text-[10px] font-medium uppercase tracking-wider text-gray-600">
                Current library
              </p>
              <p
                className="mt-0.5 truncate text-xs font-medium text-gray-300"
                title={activeName}
              >
                {activeName}
              </p>
            </div>
          ) : null;
        })()}
        <div className="space-y-1">
          {isLibrariesLoading ? (
            Array.from({ length: 3 }).map((_, i) => (
              <div
                key={i}
                className="flex items-center gap-2 rounded-lg px-2 py-2 animate-pulse"
              >
                <div className="h-2 w-2 rounded-full bg-gray-700" />
                <div className="h-5 w-5 rounded bg-gray-800" />
                {showLabels && (
                  <div className="h-4 flex-1 rounded bg-gray-800" />
                )}
              </div>
            ))
          ) : items.length === 0 ? (
            showLabels && (
              <div className="px-2 py-2 text-xs text-gray-500">
                No libraries yet.
              </div>
            )
          ) : (
            items.map((lib) => {
              const active = lib.library_id === libraryId;
              const isBrowseRoute =
                active &&
                libraryId &&
                location.pathname === `/libraries/${libraryId}/browse`;
              return (
                <div key={lib.library_id}>
                  <Link
                    to={`/libraries/${lib.library_id}/browse`}
                    className={`group flex items-center gap-2 rounded-lg px-2 py-3 text-sm transition-colors duration-150 ${
                      active && !activePath
                        ? "bg-indigo-600/30 text-indigo-200"
                        : "text-gray-300 hover:bg-gray-800/80"
                    }`}
                  >
                    <span
                      className={`h-2 w-2 rounded-full ${scanStatusColor(
                        lib.scan_status,
                      )}`}
                    />
                    {photoStackIcon()}
                    {showLabels && (
                      <span className="truncate">{lib.name}</span>
                    )}
                  </Link>
                  {isBrowseRoute && libraryId && (
                    <DirectoryTree
                      libraryId={libraryId}
                      activePath={activePath}
                      onNavigate={onNavigate}
                    />
                  )}
                </div>
              );
            })
          )}
        </div>

        <div className="mt-3">
          <Link
            to="/"
            className={`flex items-center gap-2 rounded-lg px-2 py-3 text-sm transition-colors duration-150 ${
              isLibrariesRootActive
                ? "bg-indigo-600/30 text-indigo-200"
                : "text-gray-400 hover:bg-gray-800/80 hover:text-gray-200"
            }`}
          >
            <span className="h-2 w-2 rounded-full bg-gray-500" />
            {photoStackIcon()}
            {showLabels && <span>Manage libraries</span>}
          </Link>
        </div>
      </div>

      <div className="border-t border-gray-800" />

      <div className="px-2 py-2 space-y-1">
        <Link
          to="/admin"
          className={`flex items-center justify-between gap-2 rounded-lg px-2 py-3 text-xs text-gray-400 transition-colors duration-150 hover:bg-gray-800/80 ${
            location.pathname === "/admin" ? "bg-indigo-600/20 text-indigo-300" : ""
          }`}
        >
          <div className="flex items-center gap-2">
            {showLabels ? (
              <>
                {gearIcon()}
                <span className="font-medium text-gray-300">Workers</span>
              </>
            ) : (
              gearIcon()
            )}
          </div>
          <div className="flex items-center gap-2">
            <span
              className={`h-2 w-2 rounded-full ${
                runningCount > 0
                  ? "bg-emerald-500 animate-pulse"
                  : "bg-gray-500"
              }`}
            />
            {showLabels && (
              <span>
                {runningCount > 0 ? `${runningCount} running` : "Idle"}
              </span>
            )}
          </div>
        </Link>
        <Link
          to="/settings"
          className={`flex items-center gap-2 rounded-lg px-2 py-3 text-xs text-gray-400 transition-colors duration-150 hover:bg-gray-800/80 ${
            location.pathname.startsWith("/settings") ? "bg-indigo-600/20 text-indigo-300" : ""
          }`}
        >
          {settingsIcon()}
          {showLabels && <span className="font-medium text-gray-300">Settings</span>}
        </Link>
      </div>

      <div className="border-t border-gray-800" />

      <div className="flex items-center justify-between px-2 py-2">
        <button
          type="button"
          onClick={() => {
            logout();
          }}
          className={`flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-xs text-gray-500 transition-colors hover:bg-gray-800/80 hover:text-gray-300 ${collapsed ? "hidden" : ""}`}
          title="Sign out"
        >
          <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
            <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
            <polyline points="16 17 21 12 16 7" />
            <line x1="21" y1="12" x2="9" y2="12" />
          </svg>
          Sign out
        </button>
        <button
          type="button"
          onClick={() => {
            const next = !collapsed;
            try {
              window.localStorage.setItem(
                SIDEBAR_COLLAPSED_KEY,
                String(next),
              );
            } catch {
              // ignore
            }
            onToggleCollapsed();
          }}
          className="flex h-8 w-8 items-center justify-center rounded-lg border border-gray-700 bg-gray-900/80 text-gray-400 transition-colors duration-150 hover:border-gray-600 hover:bg-gray-800"
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        >
          <ChevronToggleIcon collapsed={collapsed} />
        </button>
      </div>
    </aside>
  );
}

