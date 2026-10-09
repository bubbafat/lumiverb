import { useQuery } from "@tanstack/react-query";
import { useState, useEffect } from "react";
import { Link } from "react-router-dom";
import { ApiError, listLibraries, type HealthRow, type HealthState } from "../api/client";
import type { LibraryListItem } from "../api/types";
import { HealthDot } from "../components/HealthDot";
import { useSystemHealth } from "../lib/useSystemHealth";

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

function SectionHeader({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <div className="mb-4">
      <h2 className="text-lg font-semibold text-gray-100">{title}</h2>
      {subtitle && <p className="text-sm text-gray-500">{subtitle}</p>}
    </div>
  );
}

/** Where a row's link goes, in words. */
function linkLabel(link: string): string {
  if (link.startsWith("/settings/processing")) return "Processing settings";
  if (link.startsWith("/settings/ai")) return "AI settings";
  if (link.startsWith("/libraries/") && link.endsWith("/settings")) return "Library settings";
  if (link.startsWith("/libraries")) return "Libraries";
  return "Open";
}

function HealthRowItem({ row }: { row: HealthRow }) {
  return (
    <li className="flex items-start gap-3 rounded-lg border border-gray-700/50 bg-gray-900/50 px-4 py-3">
      <HealthDot state={row.state} className="mt-1.5" />
      <div className="min-w-0 flex-1">
        <h3 className="font-medium text-gray-100">{row.title}</h3>
        <p className="mt-0.5 text-sm text-gray-400 break-words">{row.reason}</p>
        {row.link && (
          <Link to={row.link} className="mt-1 inline-block text-sm text-indigo-400 hover:text-indigo-300 sm:hidden">
            {linkLabel(row.link)} →
          </Link>
        )}
      </div>
      {row.link && (
        <Link to={row.link} className="hidden shrink-0 pt-0.5 text-sm text-indigo-400 hover:text-indigo-300 sm:block">
          {linkLabel(row.link)} →
        </Link>
      )}
    </li>
  );
}

const REACH: Record<string, { state: HealthState | null; label: string }> = {
  true: { state: "green", label: "Reachable" },
  false: { state: "red", label: "Can't be reached" },
  null: { state: null, label: "Not checked lately" },
};

type Reach = { reachable: boolean | null; seen_at: string | null };

function LibraryRow({ lib, reach: seen }: { lib: LibraryListItem; reach: Reach }) {
  const lastScan = lib.last_scan_at ? relativeTime(lib.last_scan_at) : "Never";
  const known = REACH[String(seen.reachable)];
  // Not known now: when it was last seen.
  const reach = known.state ? known : {
    ...known, label: `${known.label} · ${seen.seen_at ? `last seen ${relativeTime(seen.seen_at)}` : "never seen"}`,
  };
  return (
    <li className="flex items-center gap-3 rounded-lg border border-gray-700/50 bg-gray-900/50 px-4 py-3">
      {reach.state ? (
        <HealthDot state={reach.state} label={reach.label} />
      ) : (
        <span role="img" aria-label={reach.label} title={reach.label}
              className="inline-block h-2 w-2 shrink-0 rounded-full bg-gray-500" />
      )}
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="font-medium text-gray-100">{lib.name}</span>
        </div>
        <p className="mt-0.5 font-mono text-xs text-gray-500 truncate">{lib.root_path}</p>
      </div>
      <div className="text-right text-sm text-gray-400 shrink-0">
        <div>Last ingest</div>
        <div className="text-gray-300">{lastScan}</div>
      </div>
    </li>
  );
}

export default function AdminPage() {
  const health = useSystemHealth(POLL_INTERVAL);
  const { data: libraries, isLoading: libsLoading } = useQuery({
    queryKey: ["admin", "libraries"],
    queryFn: () => listLibraries(false),
    refetchInterval: POLL_INTERVAL,
  });

  const activeLibraries = libraries?.filter((l) => l.status !== "trashed") ?? [];
  const reach = new Map<string, Reach>((health.data?.libraries ?? []).map((l) => [l.library_id, l]));

  // The API not answering is the Website row's red; the rest is what it said last.
  let rows = health.data?.rows ?? [];
  if (health.isError) {
    const why = health.error instanceof ApiError ? health.error.message : "it can't be reached";
    const down: HealthRow = {
      key: "website", title: "Website", state: "red", reason: `The API isn't answering: ${why}`,
      link: null, checked_at: null,
    };
    rows = [down, ...rows.filter((r) => r.key !== "website")];
  }

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
    <div className="mx-auto max-w-3xl px-4 py-6 space-y-10 sm:px-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-gray-100">Admin</h1>
          <p className="mt-1 text-sm text-gray-500">
            How the system is doing. Refreshes every 10 seconds.
          </p>
        </div>
        <Link to="/admin/users" className="shrink-0 text-sm text-indigo-400 hover:text-indigo-300">
          Manage users →
        </Link>
      </div>

      <section>
        <div className="flex items-center justify-between mb-4">
          <SectionHeader title="System health" />
          {lastUpdated(health.dataUpdatedAt)}
        </div>
        {rows.length === 0 ? (
          <div className="space-y-3">
            {[1, 2, 3, 4, 5, 6].map((i) => (
              <div key={i} className="h-16 rounded-lg border border-gray-700/50 bg-gray-900/50 animate-pulse" />
            ))}
          </div>
        ) : (
          <ul aria-label="System health" className="space-y-3">
            {rows.map((row) => (
              <HealthRowItem key={row.key} row={row} />
            ))}
          </ul>
        )}
      </section>

      <section>
        <SectionHeader title="Libraries" subtitle="Whether the scheduler can reach each library's storage" />
        {libsLoading ? (
          <div className="space-y-3">
            {[1, 2, 3].map((i) => (
              <div key={i} className="h-16 rounded-lg border border-gray-700/50 bg-gray-900/50 animate-pulse" />
            ))}
          </div>
        ) : activeLibraries.length === 0 ? (
          <p className="text-sm text-gray-500 italic">No libraries.</p>
        ) : (
          <ul aria-label="Libraries" className="space-y-3">
            {activeLibraries.map((lib) => (
              <LibraryRow key={lib.library_id} lib={lib} reach={reach.get(lib.library_id) ?? { reachable: null, seen_at: null }} />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
