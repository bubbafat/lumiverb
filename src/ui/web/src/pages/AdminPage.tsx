import { useQuery } from "@tanstack/react-query";
import { useState, useEffect } from "react";
import { Link } from "react-router-dom";
import { listLibraries } from "../api/client";
import type { LibraryListItem } from "../api/types";

const POLL_INTERVAL = 10_000;


function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function ScanDot({ status }: { status: string }) {
  const cls =
    status === "running" || status === "scanning"
      ? "bg-amber-400 animate-pulse"
      : status === "error"
        ? "bg-red-500"
        : "bg-emerald-500";
  return <span className={`inline-block h-2 w-2 rounded-full ${cls}`} />;
}


function SectionHeader({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <div className="mb-4">
      <h2 className="text-lg font-semibold text-gray-100">{title}</h2>
      {subtitle && <p className="text-sm text-gray-500">{subtitle}</p>}
    </div>
  );
}

function LibraryRow({ lib }: { lib: LibraryListItem }) {
  const lastScan = lib.last_scan_at
    ? relativeTime(lib.last_scan_at)
    : "Never";

  return (
    <div className="flex items-center gap-3 rounded-lg border border-gray-700/50 bg-gray-900/50 px-4 py-3">
      <ScanDot status={lib.scan_status} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="font-medium text-gray-100">{lib.name}</span>
          <span className="rounded px-1.5 py-0.5 text-xs font-medium bg-gray-800 text-gray-400">
            {lib.scan_status}
          </span>
        </div>
        <p className="mt-0.5 font-mono text-xs text-gray-500 truncate">
          {lib.root_path}
        </p>
      </div>
      <div className="text-right text-sm text-gray-400 shrink-0">
        <div>Last ingest</div>
        <div className="text-gray-300">{lastScan}</div>
      </div>
    </div>
  );
}

export default function AdminPage() {
  const { data: libraries, isLoading: libsLoading, dataUpdatedAt: libsUpdated } = useQuery({
    queryKey: ["admin", "libraries"],
    queryFn: () => listLibraries(false),
    refetchInterval: POLL_INTERVAL,
  });

  const activeLibraries = libraries?.filter((l) => l.status !== "trashed") ?? [];

  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  function lastUpdated(ts: number) {
    if (!ts) return null;
    const s = Math.floor((Date.now() - ts) / 1000);
    return <span className="text-xs text-gray-600">Updated {s}s ago</span>;
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-6 space-y-10">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-gray-100">Admin</h1>
          <p className="mt-1 text-sm text-gray-500">
            System overview. Refreshes every 10 seconds.
          </p>
        </div>
        <Link
          to="/admin/users"
          className="text-sm text-indigo-400 hover:text-indigo-300"
        >
          Manage users →
        </Link>
      </div>

      {/* Libraries */}
      <section>
        <div className="flex items-center justify-between mb-4">
          <SectionHeader
            title="Libraries"
            subtitle="Status for all active libraries"
          />
          {lastUpdated(libsUpdated)}
        </div>
        {libsLoading ? (
          <div className="space-y-3">
            {[1, 2, 3].map((i) => (
              <div
                key={i}
                className="h-16 rounded-lg border border-gray-700/50 bg-gray-900/50 animate-pulse"
              />
            ))}
          </div>
        ) : activeLibraries.length === 0 ? (
          <p className="text-sm text-gray-500 italic">No libraries.</p>
        ) : (
          <div className="space-y-3">
            {activeLibraries.map((lib) => (
              <LibraryRow key={lib.library_id} lib={lib} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
