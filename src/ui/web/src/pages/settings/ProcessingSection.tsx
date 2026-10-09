import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  cancelUpgrade,
  getCurrentUser,
  getProducers,
  listLibraries,
  listProjects,
  upgradeProducer,
  type EditsChoice,
  type Producer,
  type ProducerUpgrade,
  type UpgradeRequest,
  type UpgradeResult,
} from "../../api/client";

const buttonClass =
  "rounded-md px-4 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50";
const linkClass = "text-sm text-indigo-300 hover:text-indigo-200 disabled:opacity-50";

export const PRODUCERS_QUERY_KEY = ["producers"];

/** Which clips the counts are over: everything, one library or one project. */
interface Scope {
  kind: "all" | "library" | "project";
  id?: string;
  name?: string;
}

function message(error: unknown): string {
  return error instanceof ApiError ? error.message : "Couldn't reach the server. Try again.";
}

const n = (value: number) => value.toLocaleString();
const clips = (value: number) => `${n(value)} clip${value === 1 ? "" : "s"}`;

function mediaLabel(media: string[]): string {
  if (media.includes("image") && media.includes("video")) return "Images and videos";
  return media.includes("video") ? "Videos" : "Images";
}

/** The button that confirms a producer upgraded all at once names what happens. */
function everythingLabel(artifact: string, count: number): string {
  if (artifact === "clip") return `Re-embed all ${clips(count)}`;
  if (artifact === "faces") return `Find faces again in all ${clips(count)}`;
  return `Redo all ${clips(count)}`;
}

const EDIT_LABELS: Record<EditsChoice, { label: string; hint: string }> = {
  keep: { label: "Keep my edits", hint: "The new one is made underneath; your edits stay on top." },
  replace: { label: "Replace my edits", hint: "As each is made again, your edits go to history and the new one shows." },
  skip: { label: "Skip edited clips", hint: "Clips you edited stay as they are." },
};

/** Account-wide: what each producer has made for the clips, and upgrading what's stale (admins). */
export default function ProcessingSection() {
  const { data: user } = useQuery({ queryKey: ["settings", "me"], queryFn: getCurrentUser });
  const { data: libraries = [] } = useQuery({ queryKey: ["libraries"], queryFn: () => listLibraries() });
  const { data: projects = [] } = useQuery({ queryKey: ["projects", "active"], queryFn: () => listProjects() });
  const [scopeValue, setScopeValue] = useState("all");
  const [kind, id] = scopeValue.split(":") as ["all" | "library" | "project", string | undefined];
  const scope: Scope =
    kind === "library"
      ? { kind, id, name: libraries.find((l) => l.library_id === id)?.name }
      : kind === "project"
        ? { kind, id, name: projects.find((p) => p.project_id === id)?.name }
        : { kind: "all" };
  const { data: producers, isLoading, error } = useQuery({
    queryKey: [...PRODUCERS_QUERY_KEY, scopeValue],
    queryFn: () =>
      getProducers(kind === "library" ? { libraryId: id } : kind === "project" ? { projectId: id } : {}),
    refetchInterval: 30_000,
  });
  const admin = user?.role === "admin";

  return (
    <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-6 space-y-6">
      <div>
        <h2 className="text-lg font-semibold text-gray-100">Processing</h2>
        <p className="mt-1 text-sm text-gray-400">
          What each producer has made for your clips. Stale ones were made with an older model or settings: they're
          made again only when an admin upgrades them, after anything missing.
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
            <ProducerRow key={`${p.artifact}|${scopeValue}`} producer={p} scope={scope} admin={admin} />
          ))}
        </ul>
      )}
      {user && !admin && <p className="text-sm text-gray-500">Only admins can upgrade.</p>}
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

function ProducerRow({ producer, scope, admin }: { producer: Producer; scope: Scope; admin: boolean }) {
  const [open, setOpen] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
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
              {c.failing > 0 && <span className="text-red-300"> · {n(c.failing)} failing</span>}
            </>
          )}
        </p>
      )}
      {producer.upgrades.map((u) => (
        <UpgradeLine key={u.upgrade_id} producer={producer} upgrade={u} admin={admin} />
      ))}
      {notice && (
        <p role="status" className="text-sm text-emerald-300">
          {notice}
        </p>
      )}
      {c && c.stale > 0 && !producer.upgradable && producer.why_not && (
        <p className="text-sm text-gray-500">Can't be upgraded yet. {producer.why_not}</p>
      )}
      {c && c.stale > 0 && producer.upgradable && admin && !open && (
        <button
          type="button"
          className={linkClass}
          aria-label={`${upgradeLabel(producer, scope, c.stale)}: ${producer.title}`}
          onClick={() => {
            setNotice(null);
            setOpen(true);
          }}
        >
          {upgradeLabel(producer, scope, c.stale)}
        </button>
      )}
      {open && c && (
        <UpgradePanel
          producer={producer}
          scope={scope}
          stale={c.stale}
          onClose={(result) => {
            setOpen(false);
            if (result) setNotice(upgradedNotice(result));
          }}
        />
      )}
      <Settings producer={producer} />
    </li>
  );
}

/** A producer upgraded all at once is upgraded everywhere, whatever the counts are for. */
function upgradeLabel(producer: Producer, scope: Scope, stale: number): string {
  return producer.uniform && scope.kind !== "all" ? "Upgrade everywhere" : `Upgrade ${n(stale)} stale`;
}

function upgradedNotice(r: UpgradeResult): string {
  const parts = [r.upgrading ? `Upgrading ${clips(r.upgrading)}.` : "Nothing to upgrade: every stale clip has your edits."];
  if (r.skipped_edited) parts.push(`Skipped ${clips(r.skipped_edited)} with your edits.`);
  if (r.edits_to_replace) parts.push(`Your edits on ${clips(r.edits_to_replace)} go to history as each is made again.`);
  return parts.join(" ");
}

function UpgradeLine({ producer, upgrade, admin }: { producer: Producer; upgrade: ProducerUpgrade; admin: boolean }) {
  const queryClient = useQueryClient();
  const cancel = useMutation({
    mutationFn: () => cancelUpgrade(producer.artifact, upgrade.upgrade_id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: PRODUCERS_QUERY_KEY }),
  });
  const where = upgrade.scope.kind === "all" ? "everywhere" : `in ${upgrade.scope.name ?? "a deleted scope"}`;
  const done = upgrade.total - upgrade.remaining;
  return (
    <div className="rounded-md border border-indigo-500/30 bg-indigo-500/10 px-3 py-2 text-sm text-indigo-100">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span>
          Upgrading {where}: {n(done)} of {n(upgrade.total)} done
          {upgrade.edits !== "keep" &&
            (upgrade.edits === "skip" ? " · edited clips skipped" : " · edits replaced as each is made")}
          {!!upgrade.still_stale && ` · ${n(upgrade.still_stale)} made again but still stale`}
        </span>
        {admin && (
          <button
            type="button"
            className={linkClass}
            disabled={cancel.isPending}
            aria-label={`Stop upgrading ${producer.title} ${where}`}
            onClick={() => cancel.mutate()}
          >
            Stop
          </button>
        )}
      </div>
      {cancel.error && (
        <p role="alert" className="mt-1 text-red-300">
          {message(cancel.error)}
        </p>
      )}
    </div>
  );
}

type Step =
  | { kind: "ask" }
  | { kind: "edits"; stale: number; edited: number }
  | { kind: "everything"; stale: number; message: string };

/** The questions an upgrade needs, in the order the API asks them. */
function UpgradePanel({
  producer,
  scope,
  stale,
  onClose,
}: {
  producer: Producer;
  scope: Scope;
  stale: number;
  onClose: (result: UpgradeResult | null) => void;
}) {
  const queryClient = useQueryClient();
  const [step, setStep] = useState<Step>({ kind: "ask" });
  const [edits, setEdits] = useState<EditsChoice>("keep");
  // Answers already given, sent again with each try: the API may ask more than one question.
  const [answered, setAnswered] = useState<{ edits?: boolean; confirm?: boolean }>({});
  const [problem, setProblem] = useState<string | null>(null);
  // A producer upgraded all at once ignores the scope the counts are for.
  const narrowed = !producer.uniform && scope.kind !== "all";
  const where = narrowed ? ` in ${scope.name ?? "this scope"}` : "";
  const thing = producer.title.toLowerCase();

  const upgrade = useMutation({
    mutationFn: (body: UpgradeRequest) => upgradeProducer(producer.artifact, body),
    onMutate: () => setProblem(null),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: PRODUCERS_QUERY_KEY });
      onClose(result);
    },
    onError: (e) => {
      if (e instanceof ApiError && e.code === "edited_clips") {
        setStep({ kind: "edits", stale: Number(e.details?.stale ?? stale), edited: Number(e.details?.edited ?? 0) });
      } else if (e instanceof ApiError && e.code === "redo_everything") {
        setStep({ kind: "everything", stale: Number(e.details?.stale ?? stale), message: e.message });
      } else setProblem(message(e));
    },
  });

  const body = (now: { edits?: boolean; confirm?: boolean }): UpgradeRequest => ({
    ...(narrowed && scope.kind === "library" ? { library_id: scope.id } : {}),
    ...(narrowed && scope.kind === "project" ? { project_id: scope.id } : {}),
    ...(now.edits ? { edits } : {}),
    ...(now.confirm ? { confirm: true } : {}),
  });

  return (
    <form
      aria-label={`Upgrade ${producer.title}`}
      className="space-y-3 rounded-md border border-gray-700 bg-gray-900/70 p-3"
      onSubmit={(e) => {
        e.preventDefault();
        const now = {
          ...answered,
          ...(step.kind === "edits" ? { edits: true } : {}),
          ...(step.kind === "everything" ? { confirm: true } : {}),
        };
        setAnswered(now);
        upgrade.mutate(body(now));
      }}
    >
      {step.kind === "ask" && (
        <p className="text-sm text-gray-200">
          {producer.uniform
            ? `${producer.title} must come from one model across the library, so it's upgraded everywhere at once.`
            : `Make ${clips(stale)}' ${thing} again${where}? The worker does them after anything missing.`}
        </p>
      )}
      {step.kind === "edits" && (
        <fieldset className="space-y-2">
          <legend className="text-sm text-gray-200">
            {n(step.edited)} of the {clips(step.stale)} have your edits.
          </legend>
          {(Object.keys(EDIT_LABELS) as EditsChoice[]).map((choice) => (
            <label key={choice} className="flex items-start gap-2 text-sm text-gray-300">
              <input
                type="radio"
                name={`edits-${producer.artifact}`}
                value={choice}
                checked={edits === choice}
                onChange={() => setEdits(choice)}
                className="mt-1"
              />
              <span>
                <span className="text-gray-100">{EDIT_LABELS[choice].label}</span>
                <span className="block text-gray-400">{EDIT_LABELS[choice].hint}</span>
              </span>
            </label>
          ))}
        </fieldset>
      )}
      {step.kind === "everything" && <p className="text-sm text-amber-200">{step.message}</p>}
      {problem && (
        <p role="alert" className="text-sm text-red-300">
          {problem}
        </p>
      )}
      <div className="flex flex-wrap gap-3">
        <button
          type="submit"
          disabled={upgrade.isPending}
          className={`${buttonClass} bg-indigo-600 text-white hover:bg-indigo-500`}
        >
          {upgrade.isPending
            ? "Upgrading…"
            : step.kind === "everything"
              ? everythingLabel(producer.artifact, step.stale)
              : "Upgrade"}
        </button>
        <button type="button" className={`${buttonClass} text-gray-300 hover:bg-gray-800`} onClick={() => onClose(null)}>
          Not now
        </button>
      </div>
    </form>
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
