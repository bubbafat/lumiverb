import { useInfiniteQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, getServiceLog, type LogEntry, type ServiceLog } from "../api/client";

const PAGE = 200;
const FOLLOW_EVERY_MS = 5_000;

/** Each part of Lumiverb, in words; which a log has depends on where it runs. */
const PART_NAMES: Record<string, string> = {
  api: "API",
  scheduler: "Processing",
  postgres: "Database",
  quickwit: "Quickwit (removed)", // an older install's lines (ADR-019: search is in the database)
  nginx: "Web server",
  upkeep: "Upkeep",
  "lumiverb-server": "Service",
};
const PARTS: Record<NonNullable<ServiceLog["source"]>, string[]> = {
  file: ["api", "scheduler", "postgres", "nginx", "lumiverb-server"],
  journal: ["api", "scheduler", "upkeep"],
};

const LEVEL_STYLE: Record<LogEntry["level"], string> = {
  error: "border-red-500/60 text-red-200",
  warning: "border-amber-500/60 text-amber-100",
  info: "border-gray-700 text-gray-300",
};

function partName(part: string | null): string {
  return part ? PART_NAMES[part] ?? part : "—";
}

function asText(entries: LogEntry[]): string {
  return entries.map((e) => `${e.part ?? "-"} | ${e.at ? `${e.at} ` : ""}${e.text}`).join("\n") + "\n";
}

function download(entries: LogEntry[]) {
  const url = URL.createObjectURL(new Blob([asText(entries)], { type: "text/plain" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = "lumiverb-log.txt";
  a.click();
  URL.revokeObjectURL(url);
}

export default function AdminLogsPage() {
  const [part, setPart] = useState<string | null>(null);
  const [problems, setProblems] = useState(false);
  const [typed, setTyped] = useState("");
  const [q, setQ] = useState("");
  const [follow, setFollow] = useState(false);

  useEffect(() => {
    const id = setTimeout(() => setQ(typed.trim()), 300);
    return () => clearTimeout(id);
  }, [typed]);

  const log = useInfiniteQuery({
    queryKey: ["admin", "logs", part, problems, q],
    queryFn: ({ pageParam }) => getServiceLog({ after: pageParam, part, problems, q, limit: PAGE }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    refetchInterval: follow ? FOLLOW_EVERY_MS : false,
    retry: false,
  });
  const first = log.data?.pages[0];
  const entries = useMemo(() => log.data?.pages.flatMap((p) => p.entries) ?? [], [log.data]);
  const lastPage = log.data?.pages[log.data.pages.length - 1];
  const filtered = part !== null || problems || q !== "";

  if (log.error instanceof ApiError && log.error.status === 403) {
    return (
      <div className="mx-auto max-w-5xl px-4 py-6 sm:px-6">
        <p className="text-sm text-red-400">
          {log.error.code === "logs_elsewhere" || log.error.code === "server_admin_required"
            ? log.error.message
            : "Admin access required."}
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-5xl px-4 py-6 space-y-6 sm:px-6">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-semibold text-gray-100">Logs</h1>
          {first?.where && <p className="mt-1 text-sm text-gray-500 break-all">{first.where}. Newest first.</p>}
        </div>
        <Link to="/admin" className="shrink-0 text-sm text-indigo-400 hover:text-indigo-300">
          ← Admin
        </Link>
      </div>

      {first?.unavailable ? (
        <p className="text-sm text-gray-400">{first.unavailable}</p>
      ) : (
        <>
          <div className="space-y-3">
            <div role="group" aria-label="Part" className="flex flex-wrap gap-2">
              {[null, ...(first?.source ? PARTS[first.source] : [])].map((p) => (
                <button
                  key={p ?? "all"}
                  type="button"
                  aria-pressed={part === p}
                  onClick={() => setPart(p)}
                  className={`rounded-full border px-3 py-1 text-sm ${part === p
                    ? "border-indigo-500 bg-indigo-600/30 text-indigo-100"
                    : "border-gray-700 text-gray-300 hover:border-gray-500"}`}
                >
                  {p === null ? "All" : partName(p)}
                </button>
              ))}
            </div>
            <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
              <input
                type="search"
                aria-label="Search the log"
                placeholder="Search"
                value={typed}
                onChange={(e) => setTyped(e.target.value)}
                className="w-full rounded border border-gray-700 bg-gray-800 px-3 py-1.5 text-sm text-gray-200 sm:w-64"
              />
              <label className="flex items-center gap-2 text-sm text-gray-300">
                <input type="checkbox" checked={problems} onChange={(e) => setProblems(e.target.checked)} />
                Problems only
              </label>
              <label className="flex items-center gap-2 text-sm text-gray-300">
                <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />
                Follow
              </label>
              <button
                type="button"
                disabled={entries.length === 0}
                onClick={() => download(entries)}
                className="text-sm text-indigo-400 hover:text-indigo-300 disabled:opacity-40"
              >
                Download these lines
              </button>
            </div>
          </div>

          {log.isError && !(log.error instanceof ApiError && log.error.status === 403) && (
            <p className="text-sm text-red-400">
              The log can&apos;t be read: {log.error instanceof ApiError ? log.error.message : "the API isn't answering"}
            </p>
          )}

          {log.isLoading ? (
            <div className="h-40 rounded-lg border border-gray-700/50 bg-gray-900/50 animate-pulse" />
          ) : entries.length === 0 && !log.hasNextPage ? (
            <p className="text-sm text-gray-500 italic">{filtered ? "No lines match." : "Nothing here yet."}</p>
          ) : (
            <ol aria-label="Log" className="space-y-1 font-mono text-xs">
              {entries.map((e) => (
                <li key={e.id} className={`border-l-2 pl-3 py-0.5 ${LEVEL_STYLE[e.level]}`}>
                  <span className="mr-2 text-gray-500">{partName(e.part)}</span>
                  {e.at && <span className="mr-2 text-gray-500">{new Date(e.at).toLocaleString()}</span>}
                  <span className="whitespace-pre-wrap break-words">{e.text}</span>
                </li>
              ))}
            </ol>
          )}

          {log.hasNextPage && (
            <div className="flex flex-wrap items-center gap-3">
              {lastPage && lastPage.entries.length === 0 && (
                <span className="text-sm text-gray-500">Nothing found in the stretch looked through.</span>
              )}
              <button
                type="button"
                disabled={log.isFetchingNextPage}
                onClick={() => void log.fetchNextPage()}
                className="rounded-md border border-gray-700 px-3 py-1.5 text-sm text-gray-200 hover:border-gray-500 disabled:opacity-50"
              >
                {log.isFetchingNextPage ? "Looking…" : "Look further back"}
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
