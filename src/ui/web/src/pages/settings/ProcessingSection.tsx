import { useState, type ReactNode } from "react";
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
  pauseScans,
  redoBelow,
  resumeProcessing,
  resumeRedo,
  resumeScans,
  retryFailures,
  setProducerSettings,
  stopRedo,
  type FailingClip,
  type LibraryScans,
  type Producer,
  type ProducerWait,
  type SchedulerStatus,
  type SettingField,
} from "../../api/client";
import { WAIT_SENTENCES, waitIsLoud, waitSentence, type WaitContext } from "../../lib/waits";
import { readerLine } from "../../lib/readers";
import { FACES, LOCATION_GUESS } from "../../lib/names";

const linkClass = "text-sm text-indigo-300 hover:text-indigo-200 disabled:opacity-50";
const inputClass =
  "w-full rounded-md border border-gray-700 bg-gray-950 px-2 py-1 text-sm text-gray-100 focus:border-indigo-500 focus:outline-hidden";

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
  const { data: status, isLoading: statusLoading } = useQuery({
    queryKey: STATUS_QUERY_KEY, queryFn: getSchedulerStatus, refetchInterval: 5_000 });
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
  // Scans are each library's own (ADR-018 phase 1): what scans make is paused in one library when its scans are,
  // and across every library when every one's are.
  const libraryScans = status?.libraries ?? [];
  const scansPaused = kind === "library"
    ? !!libraryScans.find((l) => l.library_id === id)?.paused
    : libraryScans.length > 0 && libraryScans.every((l) => l.paused);
  const canRetry = user?.role === "admin" || user?.role === "editor";

  return (
    <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-6 space-y-6">
      <div>
        <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
          <h2 className="text-lg font-semibold text-gray-100">Processing</h2>
          {status && <Switch name="All processing" target="all" state={status.state} admin={admin} />}
        </div>
        <p className="mt-1 text-sm text-gray-400">
          What each producer has made for your clips. Some may be made another way than now: with another model,
          settings or version. Changing a model in Settings → AI, or a producer's settings below, asks whether to
          make new files only or remake those too, after everything else; you can remake them later here.
        </p>
      </div>

      {/* Only while the first status or counts are on their way: a background refresh keeps what's shown. */}
      {(statusLoading || isLoading) && (
        <div role="status" aria-label="Loading" className="flex items-center gap-2 text-sm text-gray-400">
          <div className="h-4 w-4 animate-spin rounded-full border-2 border-gray-600 border-t-white" />
          Loading…
        </div>
      )}

      {status && (
        <ul className="space-y-2">
          <ScansRow libraries={libraryScans} admin={admin} />
          {status.switches.filter((sw) => sw.target === "upkeep").map((sw) => (
            <li key={sw.target} className="rounded-md border border-gray-700/60 bg-gray-950/40 px-4 py-2 space-y-1">
              <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
                <span className="text-sm text-gray-100">
                  {sw.title}
                  <span className="ml-2 text-xs text-gray-500">{SWITCH_ABOUT[sw.target]}</span>
                </span>
                <Switch name={sw.title} target={sw.target} state={sw.paused ? "paused" : "running"} admin={admin} />
              </div>
              {/* Why its work waits, as a producer's row says its own. */}
              {(sw.waits ?? []).length > 0 && (
                <ul className="space-y-0.5" aria-label={`Why ${sw.title} waits`}>
                  {(sw.waits ?? []).map((w) => (
                    <WaitLine key={waitKey(w)} wait={w} say={{ title: sw.title, media: [], titleOf: (a) => a, when }} />
                  ))}
                </ul>
              )}
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

      {error && (
        <p role="alert" className="text-sm text-red-300">
          {message(error)}
        </p>
      )}
      {producers && (
        <ul className="space-y-3">
          {producers.filter((p) => !p.part_of).map((p) => {
            const rowProps = (q: Producer) => ({
              producer: q,
              admin,
              canRetry,
              paused: held.get(q.artifact) ?? q.paused,
              titleOf: (artifact: string) => producers.find((x) => x.artifact === artifact)?.title ?? artifact,
              scansPaused,
              libraryId: kind === "library" ? id : undefined,
              timeLeft: kind === "all" && status?.live ? status.eta?.producers[q.artifact] : undefined,
              timeNotKnown: kind === "all" && !!status?.live && !!status.eta?.not_counted?.some(
                (n) => n.artifact === q.artifact && n.why === "not_known_yet"),
            });
            // Its parts (scenes, of scene descriptions) show inside its row, not on their own.
            const parts = producers.filter((q) => q.part_of === p.artifact);
            return (
              <ProducerRow key={`${p.artifact}|${scopeValue}`} {...rowProps(p)}
                canRedoBelow={kind !== "project"}
                // Its parts' levels beside its own (faces: Photos, then Videos).
                levels={parts.map((q) => <DetailControl key={`detail-${q.artifact}`} producer={q} admin={admin}
                  libraryId={kind === "library" ? id : undefined} canRedo={kind !== "project"} />)}>
                {parts.map((q) => <ProducerRow key={`${q.artifact}|${scopeValue}`} {...rowProps(q)} nested />)}
              </ProducerRow>
            );
          })}
        </ul>
      )}
      {user && !admin && <p className="text-sm text-gray-500">Only admins can pause processing or choose what's remade.</p>}
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
      <div className="bg-indigo-400/50" style={{ width: pct(c.stale) }} />
      <div className="bg-amber-200/60" style={{ width: pct(c.below ?? 0) }} />
      <div className="bg-sky-500/70" style={{ width: pct(c.rechecking ?? 0) }} />
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
  nested = false,
  canRedoBelow = true,
  levels,
  children,
  titleOf,
}: {
  producer: Producer;
  admin: boolean;
  canRetry: boolean;
  paused: boolean;
  // Another producer's title, by its artifact (what some of its clips wait for).
  titleOf: (artifact: string) => string;
  scansPaused: boolean;
  libraryId?: string;
  // Seconds until it's caught up: the whole account's, so only with its counts.
  timeLeft?: number | null;
  // It has work and no pace yet (one waiting for a machine says so already).
  timeNotKnown?: boolean;
  // Shown inside another producer's row (a part of it).
  nested?: boolean;
  // Its own level control may offer Redo (not over one project's clips).
  canRedoBelow?: boolean;
  // Its parts' level controls, shown after its own.
  levels?: ReactNode;
  children?: ReactNode;
}) {
  const [showFailures, setShowFailures] = useState(false);
  const c = producer.counts;
  const left = timeLeft;
  const headingId = `producer-${producer.artifact}`;
  const Item = nested ? "div" : "li";
  const Heading = nested ? "h4" : "h3";
  const level = (producer.fields ?? []).find((f) => f.kind === "level");
  const hasLevel = !!level;
  // Off (faces): it makes nothing new, so its counts say nothing; what it made stays.
  const off = level?.value === "off";
  const say: WaitContext = { title: producer.title, media: producer.media, titleOf, when };
  // Why its due work waits, each in a sentence (a stopped redo's in the redo's own box).
  const waits = (producer.waits ?? []).filter((w) => w.reason !== "new_version" && w.reason !== "redo_stopped");
  return (
    <Item className={nested ? "space-y-2 border-t border-gray-800 pt-2"
                            : "rounded-md border border-gray-700/60 bg-gray-950/40 px-4 py-3 space-y-2"}
          aria-labelledby={headingId}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <Heading id={headingId} className={nested ? "text-sm text-gray-300" : "text-sm font-medium text-gray-100"}>
          {producer.title}
        </Heading>
        <span className="flex flex-wrap items-baseline gap-x-3 text-xs text-gray-500">
          {!nested && (
            <span>
              {mediaLabel(producer.media)}
              {producer.uniform && " · one model for all"}
            </span>
          )}
          {producer.scheduled && (
            <Switch name={producer.title} target={producer.artifact} state={paused ? "paused" : "running"} admin={admin} />
          )}
        </span>
      </div>
      <CountsBar producer={producer} />
      {producer.plugin && (
        <p className="text-xs text-gray-500">
          Part of the {producer.plugin.title}
          {/plug-in$/i.test(producer.plugin.title) ? "" : " plug-in"}
          {producer.plugin.on === false ? ": off, so nothing is made. Turn it on in Settings › Plug-ins." : "."}
        </p>
      )}
      {producer.artifact === LOCATION_GUESS && (
        <p className="text-xs text-gray-500">Only in the libraries where Infer location is on, in each library&apos;s settings.</p>
      )}
      {!producer.scheduled && scansPaused && <p className="text-sm text-amber-300">Paused with scans</p>}
      {off ? (
        <p className="text-sm text-gray-400">Off: nothing new is found. What's found stays.</p>
      ) : c && !(c.applicable === 0 && waits.length > 0) && (
        <p className="text-sm text-gray-400">
          {c.applicable === 0 ? (
            "Nothing to make yet."
          ) : (
            <>
              <span className="text-gray-200">{n(c.current)}</span> current · {n(c.missing)} missing ·{" "}
              {n(c.stale)} made another way
              {!!c.rechecking && <span> · {n(c.rechecking)} rechecking</span>}
              {!!c.below && <span> · {n(c.below)} below</span>}
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
      {!off && c?.lacking?.some((l) => l.clips > 0) && (
        <p className="text-xs text-gray-500">
          Not made for {c.lacking.filter((l) => l.clips > 0).map((l) => `${n(l.clips)} that have no ${l.label}`).join(", or ")}.
        </p>
      )}
      {!off && waits.length > 0 && (
        <ul className="space-y-0.5" aria-label={`Why ${producer.title} waits`}>
          {waits.map((w) => <WaitLine key={waitKey(w)} wait={w} say={say} />)}
        </ul>
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
      {c && c.stale > 0 && <RedoLine producer={producer} stale={c.stale} admin={admin} paused={paused} say={say} />}
      {!nested && hasLevel && <DetailControl producer={producer} admin={admin} libraryId={libraryId}
        canRedo={canRedoBelow} />}
      {levels}
      <Settings producer={producer} admin={admin} />
      {children}
    </Item>
  );
}

/** A producer's level (scenes' Detail: Low / Normal / High), saved as it's picked (admins). Raising it
 * redoes nothing: what was made lower is counted, with Redo (admins, named: all, or one library's). */
function DetailControl({ producer, admin, libraryId, canRedo }: { producer: Producer; admin: boolean;
                                                                  libraryId?: string; canRedo: boolean }) {
  const queryClient = useQueryClient();
  const field = (producer.fields ?? []).find((f) => f.kind === "level");
  const save = useMutation({
    mutationFn: (value: string) =>
      setProducerSettings(producer.artifact, { [field!.key]: value === field!.default ? null : value }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: PRODUCERS_QUERY_KEY }),
  });
  const redo = useMutation({
    mutationFn: () => redoBelow(producer.artifact, { all: true, ...(libraryId ? { library_id: libraryId } : {}) }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: PRODUCERS_QUERY_KEY }),
  });
  if (!field) return null;
  const choices = field.choices ?? [];
  const label = (v: string) => v.charAt(0).toUpperCase() + v.slice(1);
  const below = producer.counts?.below ?? 0;
  const only = producer.media.length === 1 ? producer.media[0] : null;
  const what = only === "video" ? "video" : only === "image" ? "photo" : "clip";
  return (
    <div className="space-y-1 text-sm">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span id={`${producer.artifact}-level`} className="text-gray-400">{field.label}</span>
        {admin ? (
          <span role="radiogroup" aria-labelledby={`${producer.artifact}-level`}
                className="inline-flex overflow-hidden rounded-md border border-gray-700">
            {choices.map((v) => (
              <button key={v} type="button" role="radio" aria-checked={field.value === v}
                disabled={save.isPending}
                onClick={() => field.value !== v && save.mutate(v)}
                className={`px-2.5 py-0.5 text-sm disabled:opacity-50 ${field.value === v
                  ? "bg-indigo-500/30 text-indigo-100" : "text-gray-300 hover:bg-gray-800"}`}>
                {label(v)}
              </button>
            ))}
          </span>
        ) : (
          <span className="text-gray-200">{label(String(field.value))}</span>
        )}
      </div>
      {below > 0 && (
        <p className="flex flex-wrap items-baseline gap-x-3 text-gray-400">
          <span>{n(below)} {what}{below === 1 ? "" : "s"} below this</span>
          {admin && canRedo && (
            <button type="button" className={linkClass} disabled={redo.isPending}
              aria-label={`Redo ${n(below)} below this: ${producer.title}`} onClick={() => redo.mutate()}>
              Redo
            </button>
          )}
        </p>
      )}
      {(save.error || redo.error) && (
        <p role="alert" className="text-red-300">{message(save.error ?? redo.error)}</p>
      )}
    </div>
  );
}

const waitKey = (w: ProducerWait) => [w.reason, w.artifact, w.library_id, w.job].filter(Boolean).join("|");

/** Why some of a producer's work waits, in one sentence (lib/waits.ts): amber when it waits for someone to
 * act. Settings → AI is a link: leaving a job without a machine never asks, so this is where it shows. */
function WaitLine({ wait, say }: { wait: ProducerWait; say: WaitContext }) {
  const [before, ...rest] = waitSentence(wait, say).split("Settings → AI");
  return (
    <li data-wait={wait.reason} className={`text-sm ${waitIsLoud(wait) ? "text-amber-300" : "text-gray-400"}`}>
      {before}
      {rest.length > 0 && (
        <>
          <Link to="/settings/ai" className="underline hover:text-amber-200">
            Settings → AI
          </Link>
          {rest.join("Settings → AI")}
        </>
      )}
    </li>
  );
}

// Each of the scheduler's kinds (src/server/scheduler/kinds.py), held to them by tests/test_data_names.py: a
// producer's is its name (ADR-024), its remakes' redo:<name>; the scan's is scan.
const KIND_LABELS: Record<string, [string, string]> = {
  scan: ["scan", "scans"],
  "lumiverb.video.format": ["video probe", "video probes"],
  "lumiverb.video.analysis_proxy": ["analysis copy", "analysis copies"],
  "lumiverb.image.likeness": ["CLIP embedding", "CLIP embeddings"],
  "lumiverb.image.description": ["description", "descriptions"],
  "lumiverb.image.text": ["text read", "texts read"],
  "lumiverb.video.scene_descriptions": ["scene description", "scene descriptions"],
  "lumiverb.video.description": ["video description", "video descriptions"],
  "lumiverb.image.faces": ["face batch", "face batches"],
  "lumiverb.video.transcript": ["transcript", "transcripts"],
  "lumiverb.video.scenes": ["scene detection", "scene detections"],
  "lumiverb.capture_time": ["capture time read", "capture times read"],
  "lumiverb.location.guess": ["location guess", "location guesses"],
  "lumiverb.video.faces": ["video's faces", "videos' faces"],
};
const REDO = "redo:";

/** "2 descriptions", "1 redo of transcripts". */
export function kindCount(kind: string, count: number): string {
  if (kind.startsWith(REDO)) {
    const name = kind.slice(REDO.length);
    return `${n(count)} redo${count === 1 ? "" : "s"} of ${KIND_LABELS[name]?.[1] ?? name}`;
  }
  const [one, many] = KIND_LABELS[kind] ?? [kind, kind];
  return `${n(count)} ${count === 1 ? one : many}`;
}

/** One job of a kind: "analysis copy", "redo of transcripts". */
function jobLabel(kind: string): string {
  const name = kind.slice(REDO.length);
  if (kind.startsWith(REDO)) return `redo of ${KIND_LABELS[name]?.[1] ?? name}`;
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

/** A pause switch (Robert, Oct 9): one row's (green running, yellow paused)
 * or "all" (green, yellow when some are paused, red when all are). The global one pauses every row from green or yellow and
 * resumes every row from red. Admins flip it; everyone sees it. A library's scans say how they pause (pause, resume). */
function Switch({ name, target, state, admin, pause, resume }: {
  name: string; target: string; state: "running" | "partly" | "paused"; admin: boolean;
  pause?: () => Promise<void>; resume?: () => Promise<void>;
}) {
  const queryClient = useQueryClient();
  const toggle = useMutation({
    mutationFn: () => (state === "paused"
      ? (resume ?? (() => resumeProcessing(target)))()
      : (pause ?? (() => pauseProcessing(target)))()),
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: STATUS_QUERY_KEY }),
        queryClient.invalidateQueries({ queryKey: PRODUCERS_QUERY_KEY }),
      ]),
  });
  const dot = target !== "all" && state === "paused" ? STATE_DOT.partly : STATE_DOT[state];
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

/** Scans (ADR-018 phase 1): each library's own pause, who reads its files, and why its scans wait. Admins pause or
 * resume one library, or every library at once: that sets each library's own pause (there's no other). */
function ScansRow({ libraries, admin }: { libraries: LibraryScans[]; admin: boolean }) {
  const queryClient = useQueryClient();
  const every = useMutation({
    mutationFn: (pause: boolean) => (pause ? pauseScans("all") : resumeScans("all")),
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: STATUS_QUERY_KEY }),
        queryClient.invalidateQueries({ queryKey: PRODUCERS_QUERY_KEY }),
      ]),
  });
  const paused = libraries.filter((l) => l.paused).length;
  return (
    <li className="rounded-md border border-gray-700/60 bg-gray-950/40 px-4 py-2 space-y-1">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span className="text-sm text-gray-100">
          Scans
          <span className="ml-2 text-xs text-gray-500">{SWITCH_ABOUT.scans}</span>
        </span>
        {admin && libraries.length > 1 && (
          <span className="inline-flex flex-wrap gap-x-3">
            <button type="button" className={linkClass} disabled={every.isPending || paused === libraries.length}
              onClick={() => every.mutate(true)}>
              Pause every library
            </button>
            <button type="button" className={linkClass} disabled={every.isPending || paused === 0}
              onClick={() => every.mutate(false)}>
              Resume every library
            </button>
          </span>
        )}
      </div>
      {every.error && (
        <p role="alert" className="text-sm text-red-300">
          {message(every.error)}
        </p>
      )}
      {libraries.length === 0 ? (
        <p className="text-sm text-gray-500">No libraries yet.</p>
      ) : (
        <ul className="space-y-1" aria-label="Each library's scans">
          {libraries.map((lib) => (
            <li key={lib.library_id} className="space-y-0.5">
              <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
                <span className="text-sm text-gray-200">
                  {lib.name}
                  <ReaderWords lib={lib} />
                </span>
                <Switch name={`Scans of ${lib.name}`} target={lib.library_id} state={lib.paused ? "paused" : "running"}
                  admin={admin} pause={() => pauseScans([lib.library_id])} resume={() => resumeScans([lib.library_id])} />
              </div>
              {/* Why its scans wait: they keep failing (lib/waits.ts scan_failing). */}
              {(lib.waits ?? []).length > 0 && (
                <ul className="space-y-0.5" aria-label={`Why scans of ${lib.name} wait`}>
                  {(lib.waits ?? []).map((w) => (
                    <WaitLine key={waitKey(w)} wait={w}
                      say={{ title: `Scans of ${lib.name}`, media: [], titleOf: (a) => a, when }} />
                  ))}
                </ul>
              )}
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

/** Who reads the library (ADR-018 decision 1): amber when a device isn't heard from, or can't reach its folder. */
function ReaderWords({ lib }: { lib: LibraryScans }) {
  const line = readerLine(lib);
  return <span className={`ml-2 text-xs ${line.attention ? "text-amber-300" : "text-gray-500"}`}>{line.text}</span>;
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
                      // Named, never inferred: these clips, or all of them.
                      ...(assetIds ? { asset_ids: assetIds } : { all: true as const }) }),
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
                <span className="wrap-anywhere">{f.rel_path}</span>{" "}
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
            <p className="whitespace-pre-wrap wrap-break-word text-red-200">{f.error}</p>
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

/** What happens to what a producer made another way than now (ADR-018 decision 6): remade after everything else
 * while its redo runs; kept as it is by a person's choice (new files only), Remake a standing offer, not a warning;
 * held by a new version of it until an admin's OK (amber); or not yet possible. */
function RedoLine({ producer, stale, admin, paused, say }: { producer: Producer; stale: number; admin: boolean;
                                                            paused: boolean; say: WaitContext }) {
  const stopped = producer.new_version ? "new_version" : "redo_stopped";
  const queryClient = useQueryClient();
  const toggle = useMutation({
    mutationFn: () => (producer.redo_stopped ? resumeRedo(producer.artifact) : stopRedo(producer.artifact)),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: PRODUCERS_QUERY_KEY }),
  });
  if (!producer.redoable) {
    return <p className="text-sm text-gray-500">Not made again yet. {producer.why_not}</p>;
  }
  // A new version waits for an admin's OK: amber, as everything someone must act on. Kept by choice: plain.
  const box = producer.redo_stopped && producer.new_version
    ? "border-amber-500/30 bg-amber-500/10 text-amber-100"
    : producer.redo_stopped
      ? "border-gray-600/60 bg-gray-800/40 text-gray-300"
      : "border-indigo-500/30 bg-indigo-500/10 text-indigo-100";
  return (
    <div className={`rounded-md border px-3 py-2 text-sm ${box}`}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span>
          {producer.redo_stopped
            ? WAIT_SENTENCES[stopped]({ reason: stopped, clips: stale }, say)
            : paused
              ? `Remakes ${clips(stale)} once it's resumed, after everything else.`
              : `Remaking ${clips(stale)}, after everything else.`}
        </span>
        {admin && (
          <button
            type="button"
            className={linkClass}
            disabled={toggle.isPending}
            aria-label={producer.redo_stopped ? `Remake ${producer.title}` : `Stop remaking ${producer.title}`}
            onClick={() => toggle.mutate()}
          >
            {producer.redo_stopped ? "Remake" : "Stop"}
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

/** A notch's name as Settings says it: "better" is Better. */
const notchName = (value: string) => value.charAt(0).toUpperCase() + value.slice(1);

function shown(field: SettingField, value: unknown): string {
  if (value === "" || value === null || value === undefined) return "—";
  if (field.kind === "bool") return value ? "On" : "Off";
  if (field.kind === "notch") return notchName(String(value));
  return `${String(value)}${field.unit ? ` ${field.unit}` : ""}`;
}

/** A producer's settings: changeable ones as a form for admins (advanced
 * ones folded away), the rest with why they can't be changed here. */
function Settings({ producer, admin }: { producer: Producer; admin: boolean }) {
  // A level is its own control (DetailControl), not a field here.
  const fields = (producer.fields ?? []).filter((f) => f.kind !== "level");
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
            <dd className="min-w-0 whitespace-pre-wrap wrap-break-word text-gray-300">
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
  if (f.kind === "bool") return raw === "true";
  if (raw.trim() === "") return null;
  return f.kind === "text" || f.kind === "notch" ? raw : Number(raw);
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
  // 409 redo_on_change: the API asks what becomes of what the old settings made (ADR-018 decision 6): new
  // files only, or remade too.
  const [redoing, setRedoing] = useState<{ message: string } | null>(null);
  const changed = fields.filter((f) => !f.fixed && !same(f, values[f.key]));
  const remade = changed.some((f) => f.remakes !== false);
  const done = (producer.counts?.current ?? 0) + (producer.counts?.stale ?? 0);
  const edit = (key: string, value: string) => {
    setValues({ ...values, [key]: value });
    setRedoing(null); // its answer was for the values asked about
  };
  const asSent = (f: SettingField) => {
    const value = asSetting(f, values[f.key]);
    return value === f.default ? null : value;
  };
  const save = useMutation({
    mutationFn: (redo?: boolean) =>
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
      if (e instanceof ApiError && e.code === "redo_on_change") {
        setRedoing({ message: e.message });
      } else setProblem(message(e));
    },
  });
  const input = (f: SettingField) => {
    const id = `${producer.artifact}-${f.key}`;
    if (f.kind === "bool" && !f.fixed) {
      return (
        <div key={f.key} className="flex items-center gap-2">
          <input id={id} type="checkbox" className="h-4 w-4 accent-indigo-500" checked={values[f.key] === "true"}
            onChange={(e) => edit(f.key, String(e.target.checked))} />
          <label htmlFor={id} className="text-gray-400">{f.label}</label>
        </div>
      );
    }
    return (
      <div key={f.key} className="space-y-1">
        <label htmlFor={id} className="block text-gray-400">
          {f.label}
          {f.unit && <span className="text-gray-500"> ({f.unit})</span>}
          {f.fixed && <span className="text-gray-500"> · {shown(f, f.value)}</span>}
        </label>
        {f.fixed ? (
          <p className="text-xs text-gray-500">{f.fixed}</p>
        ) : f.kind === "notch" ? (
          <select id={id} className={`${inputClass} sm:max-w-48`} value={values[f.key]}
            onChange={(e) => edit(f.key, e.target.value)}>
            {(f.choices ?? []).map((c) => <option key={c} value={c}>{notchName(c)}</option>)}
          </select>
        ) : f.kind === "text" ? (
          <textarea id={id} rows={4} className={inputClass} value={values[f.key]}
            onChange={(e) => edit(f.key, e.target.value)} />
        ) : (
          <input id={id} type="number" className={`${inputClass} sm:max-w-48`} value={values[f.key]}
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
        if (changed.length) save.mutate(undefined);
      }}
    >
      {main.map(input)}
      {advanced.length > 0 && (
        <details>
          <summary className="cursor-pointer select-none text-gray-500 hover:text-gray-300">Advanced</summary>
          <div className="mt-2 space-y-3">{advanced.map(input)}</div>
        </details>
      )}
      {remade && !redoing && done > 0 && (
        <p className="text-gray-400">
          {producer.redoable
            ? `Saving asks about the ${done.toLocaleString()} ${done === 1 ? "clip" : "clips"} already made: new files only, or remake them too, after everything else.`
            : `New files get these settings; the ${done.toLocaleString()} ${done === 1 ? "clip" : "clips"} already made stay as ${done === 1 ? "it is" : "they are"}.`}
        </p>
      )}
      {changed.length > 0 && !remade && producer.artifact === FACES && (
        <p className="text-gray-400">Face groups are worked out again; no face is found again.</p>
      )}
      {changed.length > 0 && !remade && producer.artifact === LOCATION_GUESS && (
        <p className="text-gray-400">Guesses are checked again.</p>
      )}
      {problem && (
        <p role="alert" className="text-red-300">
          {problem}
        </p>
      )}
      {redoing && (
        <div role="alert" className="space-y-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2">
          <p className="text-amber-100">{redoing.message}</p>
          <div className="flex flex-wrap gap-4">
            <button type="button" className={linkClass} disabled={save.isPending} onClick={() => save.mutate(false)}>
              New files only
            </button>
            <button type="button" className={linkClass} disabled={save.isPending} onClick={() => save.mutate(true)}>
              Also remake them
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
