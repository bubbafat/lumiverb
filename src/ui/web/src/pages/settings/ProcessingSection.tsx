import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  getCurrentUser,
  getFailures,
  getProducers,
  listLibraries,
  listProjects,
  resumeRedo,
  retryFailures,
  stopRedo,
  type FailingClip,
  type Producer,
} from "../../api/client";

const linkClass = "text-sm text-indigo-300 hover:text-indigo-200 disabled:opacity-50";

export const PRODUCERS_QUERY_KEY = ["producers"];

function message(error: unknown): string {
  return error instanceof ApiError ? error.message : "Couldn't reach the server. Try again.";
}

const n = (value: number) => value.toLocaleString();
const clips = (value: number) => `${n(value)} clip${value === 1 ? "" : "s"}`;

function mediaLabel(media: string[]): string {
  if (media.includes("image") && media.includes("video")) return "Images and videos";
  return media.includes("video") ? "Videos" : "Images";
}

/** Account-wide: what each producer has made for the clips, and stopping or resuming its redo (admins). */
export default function ProcessingSection() {
  const { data: user } = useQuery({ queryKey: ["settings", "me"], queryFn: getCurrentUser });
  const { data: libraries = [] } = useQuery({ queryKey: ["libraries"], queryFn: () => listLibraries() });
  const { data: projects = [] } = useQuery({ queryKey: ["projects", "active"], queryFn: () => listProjects() });
  const [scopeValue, setScopeValue] = useState("all");
  // Which clips the counts are over: everything, one library or one project.
  const [kind, id] = scopeValue.split(":") as ["all" | "library" | "project", string | undefined];
  const { data: producers, isLoading, error } = useQuery({
    queryKey: [...PRODUCERS_QUERY_KEY, scopeValue],
    queryFn: () =>
      getProducers(kind === "library" ? { libraryId: id } : kind === "project" ? { projectId: id } : {}),
    refetchInterval: 30_000,
  });
  const admin = user?.role === "admin";
  const canRetry = user?.role === "admin" || user?.role === "editor";

  return (
    <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-6 space-y-6">
      <div>
        <h2 className="text-lg font-semibold text-gray-100">Processing</h2>
        <p className="mt-1 text-sm text-gray-400">
          What each producer has made for your clips. Stale ones were made with another model or settings than now:
          they're made again after anything missing. Changing a model in Settings → AI is what starts that.
        </p>
      </div>

      <label className="block text-sm text-gray-300">
        <span className="mb-1 block">Counts for</span>
        <select
          className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 sm:w-auto"
          value={scopeValue}
          onChange={(e) => setScopeValue(e.target.value)}
        >
          <option value="all">All libraries</option>
          {libraries.length > 0 && (
            <optgroup label="Libraries">
              {libraries.map((l) => (
                <option key={l.library_id} value={`library:${l.library_id}`}>
                  {l.name}
                </option>
              ))}
            </optgroup>
          )}
          {projects.length > 0 && (
            <optgroup label="Projects">
              {projects.map((p) => (
                <option key={p.project_id} value={`project:${p.project_id}`}>
                  {p.name}
                </option>
              ))}
            </optgroup>
          )}
        </select>
      </label>

      {isLoading && <div className="h-32 rounded-lg border border-gray-700/50 bg-gray-900/50 animate-pulse" />}
      {error && (
        <p role="alert" className="text-sm text-red-300">
          {message(error)}
        </p>
      )}
      {producers && (
        <ul className="space-y-3">
          {producers.map((p) => (
            <ProducerRow
              key={`${p.artifact}|${scopeValue}`}
              producer={p}
              admin={admin}
              canRetry={canRetry}
              libraryId={kind === "library" ? id : undefined}
            />
          ))}
        </ul>
      )}
      {user && !admin && <p className="text-sm text-gray-500">Only admins can stop or resume a redo.</p>}
    </div>
  );
}

function CountsBar({ producer }: { producer: Producer }) {
  const c = producer.counts;
  if (!c || c.applicable === 0) return null;
  const pct = (value: number) => `${(100 * value) / c.applicable}%`;
  return (
    <div aria-hidden="true" className="flex h-1.5 overflow-hidden rounded-full bg-gray-800">
      <div className="bg-emerald-500/80" style={{ width: pct(c.current) }} />
      <div className="bg-amber-400/80" style={{ width: pct(c.stale) }} />
      <div className="bg-gray-600" style={{ width: pct(c.missing) }} />
    </div>
  );
}

function ProducerRow({
  producer,
  admin,
  canRetry,
  libraryId,
}: {
  producer: Producer;
  admin: boolean;
  canRetry: boolean;
  libraryId?: string;
}) {
  const [showFailures, setShowFailures] = useState(false);
  const c = producer.counts;
  const headingId = `producer-${producer.artifact}`;
  return (
    <li className="rounded-md border border-gray-700/60 bg-gray-950/40 px-4 py-3 space-y-2" aria-labelledby={headingId}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h3 id={headingId} className="text-sm font-medium text-gray-100">
          {producer.title}
        </h3>
        <span className="text-xs text-gray-500">
          {mediaLabel(producer.media)}
          {producer.uniform && " · one model for all"}
        </span>
      </div>
      <CountsBar producer={producer} />
      {c && (
        <p className="text-sm text-gray-400">
          {c.applicable === 0 ? (
            "Nothing to make yet."
          ) : (
            <>
              <span className="text-gray-200">{n(c.current)}</span> current · {n(c.missing)} missing ·{" "}
              <span className={c.stale ? "text-amber-300" : undefined}>{n(c.stale)} stale</span>
              {c.failing > 0 && (
                <span className="text-red-300">
                  {" "}
                  · {n(c.failing)} failing
                  {!!c.given_up && ` (${n(c.given_up)} given up)`}
                </span>
              )}
            </>
          )}
        </p>
      )}
      {c && c.failing > 0 && (
        <button
          type="button"
          className={linkClass}
          aria-expanded={showFailures}
          aria-label={`${showFailures ? "Hide" : "Show"} failures: ${producer.title}`}
          onClick={() => setShowFailures((v) => !v)}
        >
          {showFailures ? "Hide failures" : "Show failures"}
        </button>
      )}
      {showFailures && <FailureList producer={producer} canRetry={canRetry} libraryId={libraryId} />}
      {c && c.stale > 0 && <RedoLine producer={producer} stale={c.stale} admin={admin} />}
      <Settings producer={producer} />
    </li>
  );
}

function when(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

/** A producer's failing clips: why, how many tries, when the next one is; Try again (editors and admins). */
function FailureList({ producer, canRetry, libraryId }: { producer: Producer; canRetry: boolean; libraryId?: string }) {
  const queryClient = useQueryClient();
  const key = ["failures", producer.artifact, libraryId ?? "all"];
  const { data, isLoading, error } = useQuery({
    queryKey: key,
    queryFn: () => getFailures({ artifact: producer.artifact, libraryId, limit: 50 }),
  });
  const [notice, setNotice] = useState<string | null>(null);
  const retry = useMutation({
    mutationFn: (assetIds?: string[]) =>
      retryFailures({ artifact: producer.artifact, ...(libraryId ? { library_id: libraryId } : {}),
                      ...(assetIds ? { asset_ids: assetIds } : {}) }),
    onSuccess: (r) => {
      setNotice(`${clips(r.retried)} will be tried again shortly.`);
      queryClient.invalidateQueries({ queryKey: key });
      queryClient.invalidateQueries({ queryKey: PRODUCERS_QUERY_KEY });
    },
  });
  const items: FailingClip[] = data?.items ?? [];
  return (
    <div className="space-y-2 rounded-md border border-red-500/30 bg-red-500/5 px-3 py-2 text-sm" aria-label={`Failures: ${producer.title}`}>
      {isLoading && <p className="text-gray-400">Loading…</p>}
      {error && (
        <p role="alert" className="text-red-300">
          {message(error)}
        </p>
      )}
      {items.length > 0 && canRetry && (
        <button type="button" className={linkClass} disabled={retry.isPending} onClick={() => retry.mutate(undefined)}>
          Try all again
        </button>
      )}
      {notice && (
        <p role="status" className="text-emerald-300">
          {notice}
        </p>
      )}
      {retry.error && (
        <p role="alert" className="text-red-300">
          {message(retry.error)}
        </p>
      )}
      <ul className="space-y-2">
        {items.map((f) => (
          <li key={`${f.asset_id}|${f.artifact}`} className="min-w-0 border-t border-gray-800 pt-2 first:border-0 first:pt-0">
            <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
              <span className="min-w-0 break-all text-gray-200">
                {f.rel_path} <span className="text-gray-500">· {f.library_name}</span>
              </span>
              {canRetry && (
                <button
                  type="button"
                  className={linkClass}
                  disabled={retry.isPending}
                  aria-label={`Try again: ${f.rel_path}`}
                  onClick={() => retry.mutate([f.asset_id])}
                >
                  Try again
                </button>
              )}
            </div>
            <p className="whitespace-pre-wrap break-words text-red-200">{f.error}</p>
            <p className="text-xs text-gray-500">
              {f.given_up
                ? `Gave up after ${f.attempts} tries.`
                : `Tried ${f.attempts} time${f.attempts === 1 ? "" : "s"}; next try ${when(f.retry_at)}.`}
              {f.failed_at && ` Last failed ${when(f.failed_at)}.`}
            </p>
          </li>
        ))}
      </ul>
      {data?.next_cursor && <p className="text-xs text-gray-500">Showing the 50 most recent.</p>}
    </div>
  );
}

/** What happens to a producer's stale clips: redone after anything missing, stopped, or not yet possible. */
function RedoLine({ producer, stale, admin }: { producer: Producer; stale: number; admin: boolean }) {
  const queryClient = useQueryClient();
  const toggle = useMutation({
    mutationFn: () => (producer.paused ? resumeRedo(producer.artifact) : stopRedo(producer.artifact)),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: PRODUCERS_QUERY_KEY }),
  });
  if (!producer.redoable) {
    return <p className="text-sm text-gray-500">Not made again yet. {producer.why_not}</p>;
  }
  const box = producer.paused
    ? "border-gray-600/60 bg-gray-800/40 text-gray-300"
    : "border-indigo-500/30 bg-indigo-500/10 text-indigo-100";
  return (
    <div className={`rounded-md border px-3 py-2 text-sm ${box}`}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span>
          {producer.paused
            ? `Redo stopped: ${clips(stale)} stay as they are until it's resumed.`
            : `Redoing ${clips(stale)}, after anything missing.`}
        </span>
        {admin && (
          <button
            type="button"
            className={linkClass}
            disabled={toggle.isPending}
            aria-label={`${producer.paused ? "Resume" : "Stop"} redoing ${producer.title}`}
            onClick={() => toggle.mutate()}
          >
            {producer.paused ? "Resume" : "Stop"}
          </button>
        )}
      </div>
      {toggle.error && (
        <p role="alert" className="mt-1 text-red-300">
          {message(toggle.error)}
        </p>
      )}
    </div>
  );
}

function Settings({ producer }: { producer: Producer }) {
  const entries = Object.entries(producer.settings);
  if (entries.length === 0) return null;
  return (
    <details className="text-sm text-gray-400">
      <summary className="cursor-pointer select-none text-gray-500 hover:text-gray-300">
        Settings · version {producer.version}
      </summary>
      <dl className="mt-2 grid grid-cols-1 gap-x-4 gap-y-1 sm:grid-cols-[max-content_1fr]">
        {entries.map(([key, value]) => (
          <div key={key} className="contents">
            <dt className="text-gray-500">{key.replace(/_/g, " ")}</dt>
            <dd className="min-w-0 whitespace-pre-wrap break-words text-gray-300">
              {value === "" || value === null ? "—" : String(value)}
            </dd>
          </div>
        ))}
      </dl>
    </details>
  );
}
