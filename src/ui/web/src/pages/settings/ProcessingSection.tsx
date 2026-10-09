import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  getCurrentUser,
  getFailures,
  getProducers,
  getSchedulerStatus,
  listLibraries,
  listProjects,
  pauseProcessing,
  resumeProcessing,
  resumeRedo,
  retryFailures,
  setProducerSettings,
  stopRedo,
  type FailingClip,
  type Producer,
  type SchedulerStatus,
  type SettingField,
} from "../../api/client";

const linkClass = "text-sm text-indigo-300 hover:text-indigo-200 disabled:opacity-50";
const inputClass =
  "w-full rounded-md border border-gray-700 bg-gray-950 px-2 py-1 text-sm text-gray-100 focus:border-indigo-500 focus:outline-none";

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

const STATUS_QUERY_KEY = ["scheduler-status"];

/** Account-wide: what each producer has made for the clips, a pause switch for each processing action and one
 * for all of them (admins), and stopping or resuming a redo (admins). */
export default function ProcessingSection() {
  const { data: user } = useQuery({ queryKey: ["settings", "me"], queryFn: getCurrentUser });
  const { data: status } = useQuery({ queryKey: STATUS_QUERY_KEY, queryFn: getSchedulerStatus, refetchInterval: 5_000 });
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
  // Each switch's state comes from the queue (polled every 5 s), not the scoped counts.
  const held = new Map((status?.switches ?? []).map((sw) => [sw.target, sw.paused]));
  const canRetry = user?.role === "admin" || user?.role === "editor";

  return (
    <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-6 space-y-6">
      <div>
        <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
          <h2 className="text-lg font-semibold text-gray-100">Processing</h2>
          {status && <Switch name="All processing" state={status.state} admin={admin} />}
        </div>
        <p className="mt-1 text-sm text-gray-400">
          What each producer has made for your clips. Stale ones were made with another model or settings than now:
          they're made again after anything missing. Changing a model in Settings → AI, or a producer's settings below, is what starts that.
        </p>
      </div>

      {status && (
        <ul className="space-y-2">
          {status.switches.filter((sw) => sw.target === "scans" || sw.target === "upkeep").map((sw) => (
            <li key={sw.target}
                className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 rounded-md border border-gray-700/60 bg-gray-950/40 px-4 py-2">
              <span className="text-sm text-gray-100">
                {sw.title}
                <span className="ml-2 text-xs text-gray-500">{SWITCH_ABOUT[sw.target]}</span>
              </span>
              <Switch name={sw.title} target={sw.target} state={sw.paused ? "paused" : "running"} admin={admin} />
            </li>
          ))}
        </ul>
      )}

      {status && <NowBox status={status} />}

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
              paused={held.get(p.artifact) ?? p.paused}
              scansPaused={!!held.get("scans")}
              libraryId={kind === "library" ? id : undefined}
              timeLeft={kind === "all" && status?.live ? status.eta?.producers[p.artifact] : undefined}
              timeNotKnown={kind === "all" && !!status?.live && !!status.eta?.not_counted?.some(
                (n) => n.artifact === p.artifact && n.why === "not_known_yet")}
            />
          ))}
        </ul>
      )}
      {user && !admin && <p className="text-sm text-gray-500">Only admins can pause processing or stop a redo.</p>}
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
  paused,
  scansPaused,
  libraryId,
  timeLeft,
  timeNotKnown = false,
}: {
  producer: Producer;
  admin: boolean;
  canRetry: boolean;
  paused: boolean;
  scansPaused: boolean;
  libraryId?: string;
  // Seconds until it's caught up: the whole account's, so only with its counts.
  timeLeft?: number | null;
  // It has work and no pace yet (one waiting for a machine says so already).
  timeNotKnown?: boolean;
}) {
  const [showFailures, setShowFailures] = useState(false);
  const c = producer.counts;
  const left = timeLeft;
  const headingId = `producer-${producer.artifact}`;
  return (
    <li className="rounded-md border border-gray-700/60 bg-gray-950/40 px-4 py-3 space-y-2" aria-labelledby={headingId}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h3 id={headingId} className="text-sm font-medium text-gray-100">
          {producer.title}
        </h3>
        <span className="flex flex-wrap items-baseline gap-x-3 text-xs text-gray-500">
          <span>
            {mediaLabel(producer.media)}
            {producer.uniform && " · one model for all"}
          </span>
          {producer.scheduled && (
            <Switch name={producer.title} target={producer.artifact} state={paused ? "paused" : "running"} admin={admin} />
          )}
        </span>
      </div>
      <CountsBar producer={producer} />
      {!producer.scheduled && scansPaused && <p className="text-sm text-amber-300">Paused with scans</p>}
      {producer.waiting && <WaitingLine why={producer.waiting} />}
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
              {!!left && <span> · {about(left)} left</span>}
              {timeNotKnown && <span> · time left not known yet</span>}
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
      {c && c.stale > 0 && <RedoLine producer={producer} stale={c.stale} admin={admin} paused={paused} />}
      <Settings producer={producer} admin={admin} />
    </li>
  );
}

/** Why a producer's work waits (its AI job is off or has no machine), with
 * Settings → AI as a link: leaving a job without a machine never asks. */
function WaitingLine({ why }: { why: string }) {
  const [before, ...rest] = why.split("Settings → AI");
  return (
    <p role="status" className="text-sm text-amber-300">
      {before}
      {rest.length > 0 && (
        <>
          <Link to="/settings/ai" className="underline hover:text-amber-200">
            Settings → AI
          </Link>
          {rest.join("Settings → AI")}
        </>
      )}
    </p>
  );
}

const KIND_LABELS: Record<string, [string, string]> = {
  scan: ["scan", "scans"],
  probe: ["video probe", "video probes"],
  render: ["analysis copy", "analysis copies"],
  clip: ["CLIP embedding", "CLIP embeddings"],
  vision: ["description", "descriptions"],
  ocr: ["text read", "texts read"],
  scene_vision: ["scene description", "scene descriptions"],
  faces: ["face batch", "face batches"],
  transcript: ["transcript", "transcripts"],
  scenes: ["scene detection", "scene detections"],
};

/** "2 descriptions", "1 redo of transcripts". */
export function kindCount(kind: string, count: number): string {
  if (kind.startsWith("redo_")) {
    const base = KIND_LABELS[kind.slice(5)]?.[1] ?? kind.slice(5);
    return `${n(count)} redo${count === 1 ? "" : "s"} of ${base}`;
  }
  const [one, many] = KIND_LABELS[kind] ?? [kind, kind];
  return `${n(count)} ${count === 1 ? one : many}`;
}

/** One job of a kind: "analysis copy", "redo of transcripts". */
function jobLabel(kind: string): string {
  if (kind.startsWith("redo_")) return `redo of ${KIND_LABELS[kind.slice(5)]?.[1] ?? kind.slice(5)}`;
  return KIND_LABELS[kind]?.[0] ?? kind;
}

/** A span of time, roughly: "under a minute", "4 min", "2 h 5 min", "13 h", "2 days 3 h". */
export function duration(seconds: number): string {
  if (seconds < 60) return "under a minute";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 10) return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`;
  if (hours < 48) return `${Math.round(minutes / 60)} h`;
  const days = Math.floor(hours / 24);
  if (days > 30) return "over a month";
  return hours % 24 ? `${days} days ${hours % 24} h` : `${days} days`;
}

const JOBS_SHOWN = 8;

/** The headline: when everything is made, leaving out (and naming) what has no time. */
function caughtUp(eta: NonNullable<SchedulerStatus["eta"]>): string {
  const why = { no_machine: "no machine doing them now", not_known_yet: "not known yet", paused: "paused" };
  const left = (eta.not_counted ?? []).map((n) => `${n.title.charAt(0).toLowerCase()}${n.title.slice(1)} (${why[n.why]})`);
  if (eta.caught_up === null) {
    const notCounted = eta.not_counted ?? [];
    if (notCounted.length > 0 && notCounted.every((n) => n.why === "paused")) {
      return "Paused.";
    }
    return "How long until everything is made isn't known yet: it's learned from the jobs as they finish.";
  }
  if (eta.caught_up === 0 && left.length === 0) return "Caught up: everything is made.";
  const except = left.length ? `, not counting ${left.slice(0, -1).join(", ")}${left.length > 1 ? " and " : ""}${left[left.length - 1]}` : "";
  return eta.caught_up === 0 ? `Caught up${except}.` : `Caught up in ${about(eta.caught_up)}${except}.`;
}

/** A video's length: "45 s", "24 min", "1 h 10 min". */
function videoLength(seconds: number): string {
  return seconds < 60 ? `${Math.round(seconds)} s` : duration(seconds);
}

/** "about 2 h 5 min", or "under a minute" (no "about" in front of that). */
function about(seconds: number): string {
  const span = duration(seconds);
  return seconds < 60 || span === "over a month" ? span : `about ${span}`;
}

function listKinds(counts: Record<string, number>): string {
  return Object.entries(counts)
    .filter(([, c]) => c > 0)
    .map(([k, c]) => kindCount(k, c))
    .join(", ");
}

const SWITCH_ABOUT: Record<string, string> = {
  scans: "Finding files, thumbnails, video previews",
  upkeep: "Trash purge, file cleanup, face names",
};
const STATE_DOT = { running: "bg-emerald-500", partly: "bg-amber-400", paused: "bg-red-500" };
const STATE_WORDS = { running: "Running", partly: "Partly paused", paused: "Paused" };

/** A pause switch (Robert, Oct 9): one row's (a target: green running, yellow
 * paused) or all of them (no target: green, yellow when some are paused, red
 * when all are). The global one pauses every row from green or yellow and
 * resumes every row from red. Admins flip it; everyone sees it. */
function Switch({ name, target, state, admin }: { name: string; target?: string;
                                                   state: "running" | "partly" | "paused"; admin: boolean }) {
  const queryClient = useQueryClient();
  const toggle = useMutation({
    mutationFn: () => (state === "paused" ? resumeProcessing(target) : pauseProcessing(target)),
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: STATUS_QUERY_KEY }),
        queryClient.invalidateQueries({ queryKey: PRODUCERS_QUERY_KEY }),
      ]),
  });
  const dot = target && state === "paused" ? STATE_DOT.partly : STATE_DOT[state];
  const face = (
    <>
      <span aria-hidden="true" className={`inline-block h-2.5 w-2.5 rounded-full ${dot}`} />
      {STATE_WORDS[state]}
    </>
  );
  const look = "inline-flex items-center gap-1.5 text-sm text-gray-200";
  if (!admin) {
    return <span aria-label={name} data-state={state} className={look}>{face}</span>;
  }
  return (
    <span className="inline-flex flex-wrap items-baseline gap-x-2">
      <button
        type="button"
        role="switch"
        aria-checked={state !== "paused"}
        aria-label={name}
        data-state={state}
        disabled={toggle.isPending}
        onClick={() => toggle.mutate()}
        className={`${look} rounded-full border border-gray-700 px-2.5 py-0.5 hover:border-gray-500 disabled:opacity-50`}
      >
        {face}
      </button>
      {toggle.error && (
        <span role="alert" className="text-sm text-red-300">
          {message(toggle.error)}
        </span>
      )}
    </span>
  );
}

function NowBox({ status }: { status: SchedulerStatus }) {
  if (!status.live) {
    return (
      <p className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-200">
        {status.at
          ? `The scheduler isn't running: last heard from ${when(status.at)}. Nothing is being made.`
          : "The scheduler hasn't said what it's doing yet."}
      </p>
    );
  }
  const running = listKinds(status.running);
  const waiting = listKinds(status.waiting);
  const eta = status.eta;
  return (
    <div className="space-y-1 rounded-md border border-gray-700/60 bg-gray-950/40 px-3 py-2 text-sm" aria-label="Now">
      {eta && <p className="font-medium text-gray-100">{caughtUp(eta)}</p>}
      <p className="text-gray-200">
        <span className="text-gray-400">Now: </span>
        {running || (status.state === "paused" ? "paused" : "nothing to make")}
      </p>
      {eta && eta.jobs.length > 0 && (
        <ul className="space-y-0.5 pl-3 text-gray-400">
          {eta.jobs.slice(0, JOBS_SHOWN).map((job, i) => (
            <li key={i}>
              {jobLabel(job.kind)}
              {job.unit === "second" && job.units > 0 && ` of ${videoLength(job.units)} of video`}
              {" · "}
              {job.late
                ? "taking longer than usual"
                : job.left === null ? "time left not known yet" : job.left < 1 ? "about to finish" : `${about(job.left)} left`}
            </li>
          ))}
          {eta.jobs.length > JOBS_SHOWN && <li>and {eta.jobs.length - JOBS_SHOWN} more</li>}
        </ul>
      )}
      {waiting && (
        <p className="text-gray-400">
          <span>Next: </span>
          {waiting}
        </p>
      )}
      {status.gpu_hold > 0 && (
        <p className="text-gray-400">
          Video work has the GPU: AI machines sharing it take {status.gpu_hold} fewer request
          {status.gpu_hold === 1 ? "" : "s"} meanwhile.
        </p>
      )}
    </div>
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
              <span className="min-w-0 text-gray-200">
                <span className="[overflow-wrap:anywhere]">{f.rel_path}</span>{" "}
                <span className="whitespace-nowrap text-gray-500">· {f.library_name}</span>
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
                : f.retry_at
                  ? `Tried ${f.attempts} time${f.attempts === 1 ? "" : "s"}; next try ${when(f.retry_at)}.`
                  : "Being tried again."}
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
function RedoLine({ producer, stale, admin, paused }: { producer: Producer; stale: number; admin: boolean;
                                                       paused: boolean }) {
  const queryClient = useQueryClient();
  const toggle = useMutation({
    mutationFn: () => (producer.redo_stopped ? resumeRedo(producer.artifact) : stopRedo(producer.artifact)),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: PRODUCERS_QUERY_KEY }),
  });
  if (!producer.redoable) {
    return <p className="text-sm text-gray-500">Not made again yet. {producer.why_not}</p>;
  }
  const box = producer.redo_stopped
    ? "border-gray-600/60 bg-gray-800/40 text-gray-300"
    : "border-indigo-500/30 bg-indigo-500/10 text-indigo-100";
  return (
    <div className={`rounded-md border px-3 py-2 text-sm ${box}`}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span>
          {producer.redo_stopped
            ? `Redo stopped: ${clips(stale)} stay as they are until it's resumed.`
            : paused
              ? `Redoes ${clips(stale)} once it's resumed, after anything missing.`
              : `Redoing ${clips(stale)}, after anything missing.`}
        </span>
        {admin && (
          <button
            type="button"
            className={linkClass}
            disabled={toggle.isPending}
            aria-label={`${producer.redo_stopped ? "Resume" : "Stop"} redoing ${producer.title}`}
            onClick={() => toggle.mutate()}
          >
            {producer.redo_stopped ? "Resume" : "Stop"}
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

function shown(field: SettingField, value: unknown): string {
  if (value === "" || value === null || value === undefined) return "—";
  return `${String(value)}${field.unit ? ` ${field.unit}` : ""}`;
}

/** A producer's settings: changeable ones as a form for admins (advanced
 * ones folded away), the rest with why they can't be changed here. */
function Settings({ producer, admin }: { producer: Producer; admin: boolean }) {
  const fields = producer.fields ?? [];
  if (fields.length === 0) return null;
  const editable = admin && fields.some((f) => !f.fixed);
  return (
    <details className="text-sm text-gray-400">
      <summary className="cursor-pointer select-none text-gray-500 hover:text-gray-300">
        Settings · version {producer.version}
      </summary>
      {editable ? <SettingsForm producer={producer} fields={fields} /> : <SettingsList fields={fields} />}
    </details>
  );
}

/** Why settings can't change, in words (a phone can't hover): once when
 * every one has the same reason, else under each. */
function SettingsList({ fields }: { fields: SettingField[] }) {
  const reasons = new Set(fields.map((f) => f.fixed));
  const one = reasons.size === 1 ? fields[0].fixed : null;
  return (
    <div className="mt-2 space-y-2">
      {one && <p className="text-xs text-gray-500">{one}</p>}
      <dl className="grid grid-cols-1 gap-x-4 gap-y-1 sm:grid-cols-[max-content_1fr]">
        {fields.map((f) => (
          <div key={f.key} className="contents">
            <dt className="text-gray-500">{f.label}</dt>
            <dd className="min-w-0 whitespace-pre-wrap break-words text-gray-300">
              {shown(f, f.value)}
              {f.fixed && !one && <span className="block text-xs text-gray-500">{f.fixed}</span>}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/** What a field holds as its setting: an empty field is the default (null), as the CLI's KEY= is. */
function asSetting(f: SettingField, raw: string): unknown {
  if (raw.trim() === "") return null;
  return f.kind === "text" ? raw : Number(raw);
}

/** Whether a field says what the setting is now. */
function same(f: SettingField, raw: string): boolean {
  const value = asSetting(f, raw);
  return value === null ? f.value === f.default : value === f.value;
}

const fromServer = (fields: SettingField[]) =>
  Object.fromEntries(fields.filter((f) => !f.fixed).map((f) => [f.key, String(f.value ?? "")]));

function SettingsForm({ producer, fields }: { producer: Producer; fields: SettingField[] }) {
  const queryClient = useQueryClient();
  const [values, setValues] = useState<Record<string, string>>(() => fromServer(fields));
  const [problem, setProblem] = useState<string | null>(null);
  // 409 redo_on_change: what the old settings made is made again; the API asks first.
  const [redoing, setRedoing] = useState<string | null>(null);
  const changed = fields.filter((f) => !f.fixed && !same(f, values[f.key]));
  const edit = (key: string, value: string) => {
    setValues({ ...values, [key]: value });
    setRedoing(null); // its answer was for the values asked about
  };
  const asSent = (f: SettingField) => {
    const value = asSetting(f, values[f.key]);
    return value === f.default ? null : value;
  };
  const save = useMutation({
    mutationFn: (redo: boolean) =>
      setProducerSettings(producer.artifact, Object.fromEntries(changed.map((f) => [f.key, asSent(f)])), redo),
    onMutate: () => {
      setProblem(null);
      setRedoing(null);
    },
    onSuccess: (saved) => {
      // What the server saved, as it says it (1.50 is 1.5).
      if (saved?.fields) setValues(fromServer(saved.fields));
      return queryClient.invalidateQueries({ queryKey: PRODUCERS_QUERY_KEY });
    },
    onError: (e) => {
      if (e instanceof ApiError && e.code === "redo_on_change") setRedoing(e.message);
      else setProblem(message(e));
    },
  });
  const input = (f: SettingField) => {
    const id = `${producer.artifact}-${f.key}`;
    return (
      <div key={f.key} className="space-y-1">
        <label htmlFor={id} className="block text-gray-400">
          {f.label}
          {f.unit && <span className="text-gray-500"> ({f.unit})</span>}
          {f.fixed && <span className="text-gray-500"> · {shown(f, f.value)}</span>}
        </label>
        {f.fixed ? (
          <p className="text-xs text-gray-500">{f.fixed}</p>
        ) : f.kind === "text" ? (
          <textarea id={id} rows={4} className={inputClass} value={values[f.key]}
            onChange={(e) => edit(f.key, e.target.value)} />
        ) : (
          <input id={id} type="number" className={`${inputClass} sm:max-w-[12rem]`} value={values[f.key]}
            min={f.minimum ?? undefined} max={f.maximum ?? undefined} step={f.kind === "int" ? 1 : "any"}
            onChange={(e) => edit(f.key, e.target.value)} />
        )}
      </div>
    );
  };
  const main = fields.filter((f) => !f.advanced);
  const advanced = fields.filter((f) => f.advanced);
  return (
    <form
      className="mt-3 space-y-3"
      onSubmit={(e) => {
        e.preventDefault();
        if (changed.length) save.mutate(false);
      }}
    >
      {main.map(input)}
      {advanced.length > 0 && (
        <details>
          <summary className="cursor-pointer select-none text-gray-500 hover:text-gray-300">Advanced</summary>
          <div className="mt-2 space-y-3">{advanced.map(input)}</div>
        </details>
      )}
      {changed.length > 0 && !redoing && (
        <p className="text-amber-300">What these settings made is made again with the new ones, after anything missing.</p>
      )}
      {problem && (
        <p role="alert" className="text-red-300">
          {problem}
        </p>
      )}
      {redoing && (
        <div role="alert" className="space-y-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2">
          <p className="text-amber-100">{redoing}</p>
          <div className="flex flex-wrap gap-4">
            <button type="button" className={linkClass} disabled={save.isPending} onClick={() => save.mutate(true)}>
              Save and make them again
            </button>
            <button type="button" className={linkClass} onClick={() => setRedoing(null)}>
              Cancel
            </button>
          </div>
        </div>
      )}
      <div className="flex flex-wrap gap-4">
        <button type="submit" className={linkClass} disabled={!changed.length || save.isPending}
          aria-label={`Save the settings of ${producer.title}`}>
          {save.isPending ? "Saving…" : "Save"}
        </button>
        <button type="button" className={linkClass}
          disabled={fields.every((f) => f.fixed || asSetting(f, values[f.key]) === null
            || asSetting(f, values[f.key]) === f.default)}
          onClick={() => {
            setValues(Object.fromEntries(fields.filter((f) => !f.fixed).map((f) => [f.key, String(f.default ?? "")])));
            setRedoing(null);
          }}>
          Back to defaults
        </button>
      </div>
    </form>
  );
}
