import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  addMachine,
  connectMachine,
  getAiSettings,
  getCurrentUser,
  removeMachine,
  setJobModel,
  updateMachine,
  type AiJob,
  type AiMachine,
  type AiSettings,
  type MachineFields,
} from "../../api/client";

const inputClass =
  "w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 disabled:opacity-50";
const buttonClass =
  "rounded-md px-4 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50";
const linkClass = "text-sm text-indigo-300 hover:text-indigo-200 disabled:opacity-50";

export const AI_QUERY_KEY = ["ai-settings"];

/** "3 minutes ago", for when a machine was last checked. */
export function ago(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "";
  const seconds = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  return new Date(iso).toLocaleString();
}

/** A job that has a model but can't run: no machine does it, or none doing it is online. */
function jobDown(ai: AiSettings, job: AiJob): boolean {
  if (!job.model) return false;
  if (job.machines === 0) return true;
  const doing = ai.machines.filter((m) => m.enabled && m.jobs.includes(job.job));
  return job.offering === 0 && doing.some((m) => m.status);
}

/** Whether Settings should flag the AI page: a job can't run, or a machine doing one is offline. */
export function aiHasProblem(ai: AiSettings | undefined): boolean {
  if (!ai) return false;
  return (
    ai.jobs.some((j) => jobDown(ai, j)) ||
    ai.machines.some((m) => m.enabled && m.jobs.length > 0 && m.status !== null && !m.status.online)
  );
}

function message(error: unknown): string {
  return error instanceof ApiError ? error.message : "Couldn't reach the server. Try again.";
}

/** Account-wide: the GPU machines AI work runs on, and each job's model. Admins change them. */
export default function AiSection() {
  const { data: user } = useQuery({ queryKey: ["settings", "me"], queryFn: getCurrentUser });
  const { data: ai, isLoading } = useQuery({ queryKey: AI_QUERY_KEY, queryFn: getAiSettings, refetchInterval: 30_000 });
  const [adding, setAdding] = useState(false);
  // The model a job had before an admin changed it here: what it made is now stale.
  const [replaced, setReplaced] = useState<{ label: string; model: string } | null>(null);
  if (isLoading || !ai) {
    return <div className="h-32 rounded-lg border border-gray-700/50 bg-gray-900/50 animate-pulse" />;
  }
  const admin = user?.role === "admin";
  return (
    <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-6 space-y-6">
      <div>
        <h2 className="text-lg font-semibold text-gray-100">AI</h2>
        <p className="mt-1 text-sm text-gray-400">
          The GPU machines that write descriptions, read text in images, describe video scenes and transcribe
          speech: any OpenAI-compatible endpoint, such as Ollama for descriptions or speaches for transcripts,
          and the worker&apos;s own Whisper. Work is spread across the machines that are online.
        </p>
      </div>

      {ai.jobs.map((job) => (
        <JobBanner key={job.job} ai={ai} job={job} />
      ))}

      <section className="space-y-3" aria-labelledby="ai-machines">
        <div className="flex items-center justify-between gap-3">
          <h3 id="ai-machines" className="text-sm font-semibold uppercase tracking-wide text-gray-400">
            Machines
          </h3>
          {admin && !adding && (
            <button type="button" className={linkClass} onClick={() => setAdding(true)}>
              Add machine
            </button>
          )}
        </div>
        {ai.machines.length === 0 && !adding && <p className="text-sm text-gray-500">No machines yet.</p>}
        <ul className="space-y-3">
          {ai.machines.map((m) => (
            <MachineRow key={m.machine_id} ai={ai} machine={m} admin={admin} />
          ))}
        </ul>
        {adding && <MachineForm ai={ai} onDone={() => setAdding(false)} />}
      </section>

      <section className="space-y-3" aria-labelledby="ai-models">
        <h3 id="ai-models" className="text-sm font-semibold uppercase tracking-wide text-gray-400">
          Models
        </h3>
        <p className="text-sm text-gray-500">One per job. Every machine doing a job must offer its model.</p>
        {ai.jobs.map((job) => (
          <JobModel
            key={`${job.job}|${job.model}`}
            job={job}
            admin={admin}
            onChanged={(old) => setReplaced(old ? { label: job.label, model: old } : null)}
          />
        ))}
        {replaced && (
          <p role="status" className="text-sm text-amber-200">
            What {replaced.label.toLowerCase()} made with {replaced.model} is being made again, after anything missing.
            Follow it, or stop it, in{" "}
            <Link to="/settings/processing" className="text-indigo-300 underline hover:text-indigo-200">
              Processing
            </Link>
            .
          </p>
        )}
      </section>
      {!admin && <p className="text-sm text-gray-500">Only admins can change these.</p>}
    </div>
  );
}

function JobBanner({ ai, job }: { ai: AiSettings; job: AiJob }) {
  if (!job.model) {
    return (
      <div role="status" className="rounded-md border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm text-amber-200">
        {job.label} {job.label.endsWith("s") || job.label.includes("&") ? "are" : "is"} off: no model is chosen.
      </div>
    );
  }
  if (!jobDown(ai, job)) return null;
  return (
    <div role="alert" className="rounded-md border border-red-500/60 bg-red-500/15 px-4 py-3 text-sm text-red-100">
      <p className="font-semibold text-red-200">{job.label}: paused</p>
      <p className="mt-1">
        {job.machines === 0
          ? `No machine does ${job.label.toLowerCase()}. Add one, or give the job to a machine.`
          : `No machine doing ${job.label.toLowerCase()} is online. Each machine below says why.`}
      </p>
      <p className="mt-1 text-red-200/80">The work waits until it's fixed; no clip is marked as failed for it.</p>
    </div>
  );
}

function MachineStatusLine({ machine }: { machine: AiMachine }) {
  const { status } = machine;
  if (!machine.enabled) return <p className="text-sm text-gray-500">Turned off: gets no work.</p>;
  if (!status) return <p role="status" className="text-sm text-gray-400">Not checked yet.</p>;
  if (status.online) {
    return (
      <p role="status" className="text-sm text-emerald-300">
        Online · checked {ago(status.checked_at)}
      </p>
    );
  }
  return (
    <p role="alert" className="text-sm text-red-300">
      Offline: {status.error} · checked {ago(status.checked_at)}
    </p>
  );
}

function MachineRow({ ai, machine, admin }: { ai: AiSettings; machine: AiMachine; admin: boolean }) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const labels = ai.jobs.filter((j) => machine.jobs.includes(j.job)).map((j) => j.label);
  const remove = useMutation({
    mutationFn: () => removeMachine(machine.machine_id),
    onMutate: () => setProblem(null),
    onSuccess: (next) => queryClient.setQueryData(AI_QUERY_KEY, next),
    onError: (e) => setProblem(message(e)),
  });
  const offline = machine.enabled && machine.jobs.length > 0 && machine.status && !machine.status.online;

  if (editing) {
    return (
      <li>
        <MachineForm ai={ai} machine={machine} onDone={() => setEditing(false)} />
      </li>
    );
  }
  return (
    <li
      className={`rounded-md border px-4 py-3 space-y-1 ${
        offline ? "border-red-500/60 bg-red-500/10" : "border-gray-700/60 bg-gray-950/40"
      }`}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <p className="font-medium text-gray-100">{machine.name}</p>
        {admin && (
          <span className="flex gap-3">
            <button type="button" className={linkClass} aria-label={`Edit ${machine.name}`} onClick={() => setEditing(true)}>
              Edit
            </button>
            {!machine.built_in && (
              <button
                type="button"
                className="text-sm text-red-300 hover:text-red-200 disabled:opacity-50"
                aria-label={`Remove ${machine.name}`}
                disabled={remove.isPending}
                onClick={() => remove.mutate()}
              >
                Remove
              </button>
            )}
          </span>
        )}
      </div>
      {machine.built_in ? (
        <p className="text-xs text-gray-400">Whisper on the worker&apos;s own computer</p>
      ) : (
        <p className="break-all font-mono text-xs text-gray-400">{machine.api_url}</p>
      )}
      <p className="text-sm text-gray-300">
        {labels.length ? `Does: ${labels.join(", ")}` : "Does nothing yet"} · {machine.at_once} at once
        {machine.shares_gpu && " · video work comes first on its GPU"}
      </p>
      <MachineStatusLine machine={machine} />
      {problem && (
        <p role="alert" className="text-sm text-red-300">
          {problem}
        </p>
      )}
    </li>
  );
}

/** Add a machine, or change one. Connect asks it which models it offers and,
 * for a new machine, gives it the jobs whose model it offers. The built-in
 * machine has no URL or key, and does only the jobs it can. */
function MachineForm({ ai, machine, onDone }: { ai: AiSettings; machine?: AiMachine; onDone: () => void }) {
  const queryClient = useQueryClient();
  const builtIn = !!machine?.built_in;
  const [name, setName] = useState(machine?.name ?? "");
  const [url, setUrl] = useState(machine?.api_url ?? "");
  const [key, setKey] = useState("");
  const [forgetKey, setForgetKey] = useState(false);
  const [jobs, setJobs] = useState<string[]>(machine?.jobs ?? []);
  // Jobs picked by hand: Connect leaves them as they are.
  const [picked, setPicked] = useState(!!machine);
  const [atOnce, setAtOnce] = useState(machine?.at_once ?? 2);
  const [enabled, setEnabled] = useState(machine?.enabled ?? true);
  const [sharesGpu, setSharesGpu] = useState(machine?.shares_gpu ?? false);
  // What it offered at the last Connect; null until connected (again).
  const [models, setModels] = useState<string[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  const sameUrl = !!machine && url.trim().replace(/\/+$/, "") === machine.api_url;
  // A key typed now; "" to save none; undefined keeps the saved one.
  const keyToSend = key ? key : forgetKey ? "" : machine ? undefined : "";
  const moved = !builtIn && (!machine || !sameUrl || keyToSend !== undefined);
  // The jobs it can be given: the built-in machine's own, or any.
  const doable = ai.jobs.filter((j) => !builtIn || j.built_in);
  const edited = () => {
    setModels(null);
    setProblem(null);
  };

  const connect = useMutation({
    mutationFn: () =>
      connectMachine({ api_url: url.trim(), api_key: keyToSend, machine_id: sameUrl ? machine?.machine_id : undefined }),
    onMutate: () => setProblem(null),
    onSuccess: (offered) => {
      setModels(offered);
      // A new machine does the jobs whose model it offers, until jobs are picked by
      // hand. One that offers none of them may be the first for a job only servers
      // do, with no model or machine yet: it's given those. (Nothing doing
      // transcripts means the built-in was turned off on purpose.)
      const matched = ai.jobs.filter((j) => j.model && offered.includes(j.model));
      const first = (j: AiJob) =>
        !j.model && !j.built_in && !ai.machines.some((m) => m.enabled && m.jobs.includes(j.job));
      if (!picked) setJobs((matched.length ? matched : ai.jobs.filter(first)).map((j) => j.job));
    },
    onError: (e) => {
      setModels(null);
      setProblem(message(e));
    },
  });

  const save = useMutation({
    mutationFn: () => {
      if (builtIn && machine) {
        return updateMachine(machine.machine_id, { name: name.trim(), jobs, at_once: atOnce, enabled });
      }
      const fields: MachineFields = { name: name.trim(), api_url: url.trim(), jobs, at_once: atOnce, enabled,
                                      shares_gpu: sharesGpu };
      if (keyToSend !== undefined) fields.api_key = keyToSend;
      return machine ? updateMachine(machine.machine_id, fields) : addMachine(fields);
    },
    onMutate: () => setProblem(null),
    onSuccess: (next) => {
      queryClient.setQueryData(AI_QUERY_KEY, next);
      onDone();
    },
    onError: (e) => setProblem(message(e)),
  });

  // A job whose model this machine doesn't offer can't be given to it.
  const missing = (job: AiJob) => !!models && !!job.model && !models.includes(job.model);
  const canSave = !!name.trim() && (builtIn || !!url.trim()) && (!moved || !!models) && !jobs.some((j) => {
    const job = ai.jobs.find((x) => x.job === j);
    return job ? missing(job) : false;
  }) && !save.isPending;
  const title = machine ? `Edit ${machine.name}` : "Add a machine";

  return (
    <form
      aria-label={title}
      className="space-y-4 rounded-md border border-indigo-500/40 bg-gray-950/60 p-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (canSave) save.mutate();
      }}
    >
      <p className="font-medium text-gray-100">{title}</p>
      <label className="block space-y-1 text-sm text-gray-300">
        <span>Name</span>
        <input aria-label="Name" placeholder="Brain 3080" value={name} className={inputClass} onChange={(e) => setName(e.target.value)} />
      </label>
      {!builtIn && (
        <>
          <label className="block space-y-1 text-sm text-gray-300">
            <span>Endpoint URL</span>
            <input
              type="url"
              aria-label="Endpoint URL"
              placeholder="http://localhost:11434/v1"
              value={url}
              className={inputClass}
              onChange={(e) => {
                setUrl(e.target.value);
                edited();
              }}
            />
          </label>
          <label className="block space-y-1 text-sm text-gray-300">
            <span>API key (optional)</span>
            <input
              type="password"
              aria-label="API key"
              autoComplete="off"
              placeholder={machine?.has_key && sameUrl && !forgetKey ? "Saved — leave blank to keep it" : "None"}
              value={key}
              className={inputClass}
              onChange={(e) => {
                setKey(e.target.value);
                edited();
              }}
            />
          </label>
          {machine?.has_key && sameUrl && !key && (
            <label className="flex items-center gap-2 text-sm text-gray-300">
              <input
                type="checkbox"
                checked={forgetKey}
                onChange={(e) => {
                  setForgetKey(e.target.checked);
                  edited();
                }}
              />
              Remove the saved key
            </label>
          )}
          <div className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              disabled={!url.trim() || connect.isPending}
              className={`${buttonClass} bg-gray-700 text-gray-100 hover:bg-gray-600`}
              onClick={() => connect.mutate()}
            >
              {connect.isPending ? "Connecting…" : "Connect"}
            </button>
            {models && (
              <span role="status" className="text-sm text-emerald-300">
                Connected: offers {models.join(", ")}
              </span>
            )}
            {!models && moved && <span className="text-sm text-gray-500">Connect to check it before saving.</span>}
          </div>
        </>
      )}

      <fieldset className="space-y-2">
        <legend className="text-sm text-gray-300">Does</legend>
        {doable.map((job) => (
          <label key={job.job} className="flex items-center gap-2 text-sm text-gray-200">
            <input
              type="checkbox"
              checked={jobs.includes(job.job)}
              onChange={(e) => {
                setPicked(true);
                setJobs(e.target.checked ? [...jobs, job.job] : jobs.filter((j) => j !== job.job));
              }}
            />
            {job.label}
            {job.model && <span className="text-gray-500">({job.model})</span>}
            {missing(job) && jobs.includes(job.job) && (
              <span role="alert" className="text-red-300">
                It doesn't offer {job.model}.
              </span>
            )}
          </label>
        ))}
      </fieldset>
      <label className="block space-y-1 text-sm text-gray-300">
        <span>Requests at once</span>
        <input
          type="number"
          aria-label="Requests at once"
          min={1}
          max={32}
          value={atOnce}
          className={`${inputClass} max-w-[8rem]`}
          onChange={(e) => setAtOnce(Math.max(1, Math.min(32, Number(e.target.value) || 1)))}
        />
        <span className="block text-xs text-gray-500">
          How many it works on together (images to describe, clips to transcribe); more needs more GPU memory.
        </span>
      </label>
      {!builtIn && (
        <label className="flex items-start gap-2 text-sm text-gray-300">
          <input type="checkbox" className="mt-1" checked={sharesGpu} onChange={(e) => setSharesGpu(e.target.checked)} />
          <span>
            Shares the GPU with video work here
            <span className="block text-xs text-gray-500">
              It runs on the server Lumiverb decodes video on. While video is decoded there, it gets fewer requests,
              so previews and analysis copies come first.
            </span>
          </span>
        </label>
      )}
      {machine && (
        <label className="flex items-center gap-2 text-sm text-gray-300">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          Use this machine
        </label>
      )}
      {problem && (
        <p role="alert" className="text-sm text-red-300">
          {problem}
        </p>
      )}
      <div className="flex flex-wrap gap-3">
        <button type="submit" disabled={!canSave} className={`${buttonClass} bg-indigo-600 text-white hover:bg-indigo-500`}>
          {save.isPending ? "Saving…" : machine ? "Save" : "Add"}
        </button>
        <button type="button" className={`${buttonClass} text-gray-300 hover:bg-gray-800`} onClick={onDone}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function JobModel({
  job,
  admin,
  onChanged,
}: {
  job: AiJob;
  admin: boolean;
  /** The model it had, when an admin changed it to another one here. */
  onChanged: (old: string | null) => void;
}) {
  const queryClient = useQueryClient();
  const [changing, setChanging] = useState(false);
  const [model, setModel] = useState(job.model);
  const [problem, setProblem] = useState<string | null>(null);
  // 409 redo_on_change: the new model makes what the job made again; the API asks first.
  const [redoing, setRedoing] = useState<{ message: string; next: string; clips: number } | null>(null);
  // What the machines doing it offer (the server says).
  const offered = job.choices;
  const save = useMutation({
    mutationFn: ({ next, redo }: { next: string; redo?: boolean }) => setJobModel(job.job, next, redo),
    onMutate: () => {
      setProblem(null);
      setRedoing(null);
    },
    onSuccess: (next, { next: chosen }) => {
      onChanged(job.model && chosen && chosen !== job.model ? job.model : null);
      queryClient.setQueryData(AI_QUERY_KEY, next);
      setChanging(false);
    },
    onError: (e, { next }) => {
      if (e instanceof ApiError && e.code === "redo_on_change") {
        setRedoing({ message: e.message, next, clips: Number(e.details?.clips ?? 0) });
      } else if (e instanceof ApiError && e.code === "model_not_offered") {
        const machines = (e.details?.machines as { name: string; models: string[]; error: string }[] | undefined) ?? [];
        setProblem(
          `${e.message} ` +
            machines.map((m) => `${m.name}: ${m.error || (m.models.length ? m.models.join(", ") : "no models")}`).join("; "),
        );
      } else setProblem(message(e));
    },
  });
  return (
    <div className="rounded-md border border-gray-700/60 bg-gray-950/40 px-4 py-3 space-y-2">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <p className="text-sm text-gray-200">
          <span className="font-medium">{job.label}:</span> {job.model || "off"}
          {job.model && (
            <span className="ml-2 text-gray-400">
              offered by {job.offering} of {job.machines} machine{job.machines === 1 ? "" : "s"}
            </span>
          )}
        </p>
        {admin && !changing && (
          <button type="button" className={linkClass} aria-label={`Change the model for ${job.label}`} onClick={() => setChanging(true)}>
            Change
          </button>
        )}
      </div>
      {changing && (
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (model && model !== job.model) save.mutate({ next: model });
          }}
        >
          <select aria-label={`Model for ${job.label}`} value={model} className={inputClass} onChange={(e) => {
              setModel(e.target.value);
              setRedoing(null);  // the question was about another choice
            }}>
            <option value="" disabled>
              {offered.length ? "Pick a model…" : "No machine doing it has been checked yet"}
            </option>
            {offered.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
          {job.model && model && model !== job.model && !redoing && (
            <p className="text-sm text-amber-300">
              What was made with {job.model} is made again with {model}, after anything missing.
            </p>
          )}
          {problem && (
            <p role="alert" className="text-sm text-red-300">
              {problem}
            </p>
          )}
          {redoing && (
            <div role="alert" className="space-y-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2">
              <p className="text-sm text-amber-100">{redoing.message}</p>
              <button
                type="button"
                disabled={save.isPending}
                className={`${buttonClass} bg-amber-600 text-white hover:bg-amber-500`}
                onClick={() => save.mutate({ next: redoing.next, redo: true })}
              >
                {`Change it and redo ${redoing.clips.toLocaleString()} clip${redoing.clips === 1 ? "" : "s"}`}
              </button>
            </div>
          )}
          <div className="flex flex-wrap gap-3">
            <button
              type="submit"
              disabled={!model || model === job.model || save.isPending}
              className={`${buttonClass} bg-indigo-600 text-white hover:bg-indigo-500`}
            >
              {save.isPending ? "Saving…" : "Save"}
            </button>
            {job.model && (
              <button type="button" disabled={save.isPending} className={`${buttonClass} text-gray-300 hover:bg-gray-800`} onClick={() => save.mutate({ next: "" })}>
                Turn off {job.label.toLowerCase()}
              </button>
            )}
            <button
              type="button"
              className={`${buttonClass} text-gray-300 hover:bg-gray-800`}
              onClick={() => {
                setChanging(false);
                setModel(job.model);
                setProblem(null);
                setRedoing(null);
              }}
            >
              Cancel
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
