import { useQuery } from "@tanstack/react-query";
import { useState, useEffect } from "react";
import { listLibraries, listJobs, getJobStats } from "../api/client";
import type { JobListItem, JobStatsResponse, LibraryListItem } from "../api/types";

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

function duration(startIso: string | null): string {
  if (!startIso) return "—";
  const ms = Date.now() - new Date(startIso).getTime();
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
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
        <div>Last scan</div>
        <div className="text-gray-300">{lastScan}</div>
      </div>
    </div>
  );
}

function WorkerSummary({ stats }: { stats: JobStatsResponse }) {
  const { total_pending, total_claimed, total_failed, rows } = stats;
  const activeRows = rows.filter((r) => r.pending + r.claimed + r.failed > 0);

  return (
    <div className="space-y-4">
      {/* Top-line counts */}
      <div className="grid grid-cols-3 gap-3">
        {[
          { label: "Pending", value: total_pending, cls: "text-gray-200" },
          {
            label: "Running",
            value: total_claimed,
            cls: total_claimed > 0 ? "text-amber-300" : "text-gray-200",
          },
          {
            label: "Failed",
            value: total_failed,
            cls: total_failed > 0 ? "text-red-400" : "text-gray-200",
          },
        ].map(({ label, value, cls }) => (
          <div
            key={label}
            className="rounded-lg border border-gray-700/50 bg-gray-900/50 px-4 py-3 text-center"
          >
            <div className={`text-2xl font-semibold ${cls}`}>{value.toLocaleString()}</div>
            <div className="mt-0.5 text-xs text-gray-500">{label}</div>
          </div>
        ))}
      </div>

      {/* Breakdown by type */}
      {activeRows.length > 0 && (
        <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-700/50">
                <th className="px-4 py-2 text-left text-xs font-medium text-gray-500">Job type</th>
                <th className="px-4 py-2 text-right text-xs font-medium text-gray-500">Pending</th>
                <th className="px-4 py-2 text-right text-xs font-medium text-gray-500">Running</th>
                <th className="px-4 py-2 text-right text-xs font-medium text-gray-500">Failed</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800/50">
              {activeRows.map((r) => (
                <tr key={r.job_type}>
                  <td className="px-4 py-2 font-mono text-xs text-gray-300">{r.job_type}</td>
                  <td className="px-4 py-2 text-right text-gray-400">
                    {r.pending ? r.pending.toLocaleString() : "—"}
                  </td>
                  <td className={`px-4 py-2 text-right ${r.claimed > 0 ? "text-amber-300" : "text-gray-400"}`}>
                    {r.claimed ? r.claimed.toLocaleString() : "—"}
                  </td>
                  <td className={`px-4 py-2 text-right ${r.failed > 0 ? "text-red-400" : "text-gray-400"}`}>
                    {r.failed ? r.failed.toLocaleString() : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function RunningJobs({ jobs }: { jobs: JobListItem[] }) {
  const running = jobs.filter((j) => j.status === "claimed");
  if (running.length === 0) {
    return (
      <p className="text-sm text-gray-500 italic">No jobs currently running.</p>
    );
  }
  return (
    <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 overflow-hidden">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-gray-700/50">
            <th className="px-4 py-2 text-left text-xs font-medium text-gray-500">Type</th>
            <th className="px-4 py-2 text-left text-xs font-medium text-gray-500">Worker</th>
            <th className="px-4 py-2 text-left text-xs font-medium text-gray-500">Asset</th>
            <th className="px-4 py-2 text-right text-xs font-medium text-gray-500">Running for</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-800/50">
          {running.map((j) => (
            <tr key={j.job_id}>
              <td className="px-4 py-2 font-mono text-xs text-gray-300">{j.job_type}</td>
              <td className="px-4 py-2 font-mono text-xs text-gray-500 truncate max-w-[12rem]">
                {j.worker_id ?? "—"}
              </td>
              <td className="px-4 py-2 font-mono text-xs text-gray-500 truncate max-w-[14rem]">
                {j.asset_id ? j.asset_id.slice(0, 8) + "…" : "—"}
              </td>
              <td className="px-4 py-2 text-right text-xs text-amber-300">
                {duration(j.claimed_at)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function FailedJobs({ jobs }: { jobs: JobListItem[] }) {
  const failed = jobs
    .filter((j) => j.status === "failed")
    .slice(0, 20);
  if (failed.length === 0) return null;
  return (
    <div>
      <h3 className="mb-2 text-sm font-medium text-gray-400">
        Recent failures ({failed.length})
      </h3>
      <div className="rounded-lg border border-red-900/40 bg-gray-900/50 overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-700/50">
              <th className="px-4 py-2 text-left text-xs font-medium text-gray-500">Type</th>
              <th className="px-4 py-2 text-left text-xs font-medium text-gray-500">Error</th>
              <th className="px-4 py-2 text-right text-xs font-medium text-gray-500">Age</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800/50">
            {failed.map((j) => (
              <tr key={j.job_id}>
                <td className="px-4 py-2 font-mono text-xs text-gray-300">{j.job_type}</td>
                <td className="px-4 py-2 text-xs text-red-400 truncate max-w-[28rem]">
                  {j.error_message ?? "Unknown error"}
                </td>
                <td className="px-4 py-2 text-right text-xs text-gray-500">
                  {relativeTime(j.created_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
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

  const { data: stats, isLoading: statsLoading, dataUpdatedAt: statsUpdated } = useQuery<JobStatsResponse>({
    queryKey: ["admin", "job-stats"],
    queryFn: () => getJobStats(),
    refetchInterval: POLL_INTERVAL,
  });

  const { data: jobs, isLoading: jobsLoading } = useQuery<JobListItem[]>({
    queryKey: ["admin", "jobs"],
    queryFn: () => listJobs({ limit: 200 }),
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
      <div>
        <h1 className="text-2xl font-semibold text-gray-100">System status</h1>
        <p className="mt-1 text-sm text-gray-500">
          Read-only dashboard. Refreshes every 10 seconds.
        </p>
      </div>

      {/* Libraries */}
      <section>
        <div className="flex items-center justify-between mb-4">
          <SectionHeader
            title="Libraries"
            subtitle="Scan status for all active libraries"
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

      {/* Workers */}
      <section className="space-y-6">
        <div className="flex items-center justify-between">
          <SectionHeader
            title="Workers"
            subtitle="Job queue status across all job types"
          />
          {lastUpdated(statsUpdated)}
        </div>
        {statsLoading ? (
          <div className="h-32 rounded-lg border border-gray-700/50 bg-gray-900/50 animate-pulse" />
        ) : stats ? (
          <>
            <WorkerSummary stats={stats} />
            <div>
              <h3 className="mb-2 text-sm font-medium text-gray-400">
                Currently running
              </h3>
              {jobsLoading ? (
                <div className="h-16 rounded-lg border border-gray-700/50 bg-gray-900/50 animate-pulse" />
              ) : (
                <RunningJobs jobs={jobs ?? []} />
              )}
            </div>
            <FailedJobs jobs={jobs ?? []} />
          </>
        ) : null}
      </section>
    </div>
  );
}
