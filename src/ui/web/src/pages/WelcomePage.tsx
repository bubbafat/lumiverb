import { useState, type ReactNode } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  createLibrary,
  getBackups,
  getCurrentUser,
  getNetworkAccess,
  getProducers,
  getSetupProgress,
  setProducerSettings,
  setSetupProgress,
  type Producer,
} from "../api/client";
import AiSection from "./settings/AiSection";
import { FolderChoice, BACKUPS_QUERY_KEY } from "./settings/BackupsSection";
import NetworkSection from "./settings/NetworkSection";
import StorageSection from "./settings/StorageSection";
import { InviteForm, InviteLinkPanel, type MadeLink } from "./AdminUsersPage";

export const SETUP_QUERY_KEY = ["tenant", "setup"];

const buttonClass = "rounded-md px-4 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50";
const inputClass =
  "w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 disabled:opacity-50";

interface Step {
  id: string;
  title: string;
  /** Why now: what's hard to change later, or what it makes work. */
  why: string;
  /** What skipping it costs. */
  skip: ReactNode;
  body: () => ReactNode;
}

const STEPS: Step[] = [
  {
    id: "storage",
    title: "Where previews live",
    why: "Previews grow with your libraries: about 15 GB per 100,000 photos and up to 1 GB per hour of video. "
      + "Moving them later copies them all, so choose a drive with room now. Backups of the database go to a folder "
      + "of their own; one on another drive keeps them if this one fails.",
    skip: "They stay on this computer's drive, beside the database.",
    body: () => (
      <>
        <StorageSection />
        <BackupsFolder />
      </>
    ),
  },
  {
    id: "network",
    title: "Who can open it",
    why: "To begin with, only this computer can open Lumiverb. Let the devices on your network use it so your phone, "
      + "other computers and the people you invite can reach it.",
    skip: <NetworkSkip />,
    body: () => <NetworkSection />,
  },
  {
    id: "find",
    title: "What to find",
    why: "Faces are found and grouped so you can name people; places are guessed for photos without a location. "
      + "Set them before your first library: what's found is found as it's scanned.",
    skip: "Faces are found at Normal in photos and Low in videos; places aren't guessed.",
    body: () => <FindStep />,
  },
  {
    id: "ai",
    title: "AI machines",
    why: "Most of Lumiverb needs nothing more: previews, search by what's in a photo, similar photos, faces, video "
      + "scenes and transcripts all run here. Descriptions, tags and text in photos need an AI machine: a computer "
      + "running Ollama with a vision model.",
    skip: "No descriptions, tags or text in photos. They wait, and once a machine is added they catch up on everything.",
    body: () => <AiSection />,
  },
  {
    id: "people",
    title: "People",
    why: "Invite the people you share with: each gets a link and chooses their own password. Viewers browse; editors "
      + "can also rate, name and organize; admins can change everything.",
    skip: "Only you can sign in. Invite people later from Admin › Manage users.",
    body: () => <PeopleStep />,
  },
  {
    id: "library",
    title: "Your first library",
    why: "A library is a folder of photos and videos. Lumiverb reads it and never changes it. Once it's added, "
      + "Lumiverb scans it and starts making previews.",
    skip: "Nothing to browse yet. Add a library later from Libraries.",
    body: () => <LibraryStep />,
  },
];

/** First-run setup, after the first admin: each step a Settings section, in
 * the order that matters (what's hard to change later first), each saying
 * what skipping it costs. It can be left and picked up again (the Admin page). */
export default function WelcomePage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const { data: user } = useQuery({ queryKey: ["settings", "me"], queryFn: getCurrentUser });
  const { data: progress } = useQuery({ queryKey: SETUP_QUERY_KEY, queryFn: getSetupProgress, enabled: user?.role === "admin" });
  const save = useMutation({
    mutationFn: setSetupProgress,
    onSuccess: (next) => queryClient.setQueryData(SETUP_QUERY_KEY, next),
  });

  if (user && user.role !== "admin") {
    return <div className="mx-auto max-w-3xl px-6 py-6 text-sm text-gray-400">Only an admin sets Lumiverb up.</div>;
  }
  if (!progress) {
    return <div className="mx-auto max-w-3xl px-6 py-6"><div className="h-32 rounded-lg bg-gray-900/50 animate-pulse" /></div>;
  }
  const done = new Set(progress.done);
  // The caller's: where previews live and who can open it only for a server admin (the install's, not the account's).
  const steps = STEPS.filter((s) => progress.steps.includes(s.id));
  const asked = steps.findIndex((s) => s.id === params.get("step"));
  const index = asked >= 0 ? asked : Math.max(0, steps.findIndex((s) => !done.has(s.id)));
  const step = steps[index];
  const last = index === steps.length - 1;

  function go(to: number) {
    setParams({ step: steps[to].id });
    window.scrollTo?.(0, 0);
  }

  async function next() {
    await save.mutateAsync({ done: [...progress!.done.filter((d) => d !== step.id), step.id] });
    if (last) navigate("/", { replace: true });
    else go(index + 1);
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-6 space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-gray-100">Set up Lumiverb</h1>
        <p className="mt-1 text-sm text-gray-400">
          A few choices, the ones that are hard to change later first. Each can be changed again in Settings.
        </p>
      </div>

      <ol aria-label="Steps" className="flex flex-wrap gap-2">
        {steps.map((s, i) => (
          <li key={s.id}>
            <button
              type="button"
              aria-current={i === index ? "step" : undefined}
              onClick={() => go(i)}
              className={`rounded-full px-3 py-1 text-sm ${
                i === index ? "bg-indigo-600/40 text-indigo-100" : done.has(s.id) ? "text-emerald-300" : "text-gray-400"
              } hover:bg-gray-800`}
            >
              {done.has(s.id) ? "✓ " : `${i + 1}. `}
              {s.title}
            </button>
          </li>
        ))}
      </ol>

      <section aria-labelledby="step-title" className="space-y-4">
        <h2 id="step-title" className="text-xl font-semibold text-gray-100">{step.title}</h2>
        <p className="text-sm text-gray-300">{step.why}</p>
        {step.body()}
        <p className="text-sm text-gray-500"><span className="text-gray-400">If you skip this: </span>{step.skip}</p>
      </section>

      {save.isError && <p role="alert" className="text-sm text-red-300">Couldn&apos;t save that. Try again.</p>}
      <div className="flex flex-wrap items-center gap-3 border-t border-gray-800 pt-4">
        {index > 0 && (
          <button type="button" className={`${buttonClass} text-gray-300 hover:bg-gray-800`} onClick={() => go(index - 1)}>
            Back
          </button>
        )}
        <button
          type="button"
          className={`${buttonClass} bg-indigo-600 text-white hover:bg-indigo-500`}
          disabled={save.isPending}
          onClick={() => void next()}
        >
          {last ? "Finish" : "Continue"}
        </button>
        <button type="button" className="ml-auto text-sm text-gray-400 hover:text-gray-200" onClick={() => navigate("/")}>
          I&apos;ll finish later
        </button>
      </div>
    </div>
  );
}

/** Where Lumiverb doesn't run the web server (a Linux install's), who can reach it was set up there. */
function NetworkSkip() {
  const { data } = useQuery({ queryKey: ["system", "network"], queryFn: getNetworkAccess });
  if (data && !data.available) return <>Nothing changes: who can reach it is set up where it&apos;s installed.</>;
  return <>Only this computer can open it, and invite links won&apos;t open anywhere else.</>;
}

function BackupsFolder() {
  const { data } = useQuery({ queryKey: BACKUPS_QUERY_KEY, queryFn: getBackups });
  if (!data) return null;
  return (
    <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-6">
      <h3 className="mb-2 text-lg font-semibold text-gray-100">Backups</h3>
      <FolderChoice backups={data} />
    </div>
  );
}

const LEVELS: Record<string, string> = { off: "Off", low: "Low", normal: "Normal", high: "High" };

function level(p: Producer | undefined): { value: string; choices: string[] } | null {
  const field = p?.fields?.find((f) => f.kind === "level");
  return field ? { value: String(field.value), choices: field.choices ?? [] } : null;
}

/** Faces in photos and videos (Off, Low, Normal, High) and place guesses. */
function FindStep() {
  const queryClient = useQueryClient();
  const { data: producers } = useQuery({ queryKey: ["producers", "setup"], queryFn: () => getProducers() });
  const change = useMutation({
    mutationFn: ({ artifact, settings }: { artifact: string; settings: Record<string, unknown> }) =>
      setProducerSettings(artifact, settings),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["producers"] }),
  });
  if (!producers) return <div className="h-24 rounded-lg bg-gray-900/50 animate-pulse" />;
  const by = Object.fromEntries(producers.map((p) => [p.artifact, p]));
  const photos = level(by.faces);
  const videos = level(by.video_faces);
  const places = by.location?.fields?.find((f) => f.key === "infer_location");

  function Level({ label, artifact, value, hint }: { label: string; artifact: string; value: { value: string; choices: string[] }; hint: string }) {
    return (
      <label className="block space-y-1 text-sm text-gray-200">
        <span className="block">{label}</span>
        <select
          aria-label={label}
          className={`${inputClass} max-w-[12rem]`}
          value={value.value}
          disabled={change.isPending}
          onChange={(e) => change.mutate({ artifact, settings: { detail: e.target.value } })}
        >
          {value.choices.map((c) => <option key={c} value={c}>{LEVELS[c] ?? c}</option>)}
        </select>
        <span className="block text-xs text-gray-500">{hint}</span>
      </label>
    );
  }

  return (
    <div className="space-y-4 rounded-lg border border-gray-700/50 bg-gray-900/50 p-6">
      {photos && (
        <Level label="Faces in photos" artifact="faces" value={photos}
               hint="Low: clear faces near the camera. High: small faces in crowds too, and more that aren't faces. Off: none found; what's found stays." />
      )}
      {videos && (
        <Level label="Faces in videos" artifact="video_faces" value={videos}
               hint="Low: a frame from each scene. Normal: a few. High: every 3 seconds, slower." />
      )}
      {places && (
        <label className="flex items-start gap-3 text-sm text-gray-200">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={!!places.value}
            disabled={change.isPending}
            onChange={(e) => change.mutate({ artifact: "location", settings: { infer_location: e.target.checked } })}
          />
          <span>
            Guess places
            <span className="block text-xs text-gray-500">
              Photos and videos without a location are placed from those taken around the same time.
            </span>
          </span>
        </label>
      )}
      {change.isError && (
        <p role="alert" className="text-sm text-red-300">
          {change.error instanceof ApiError ? change.error.message : "Couldn't change it. Try again."}
        </p>
      )}
    </div>
  );
}

function PeopleStep() {
  const [made, setMade] = useState<MadeLink | null>(null);
  const { data: network } = useQuery({ queryKey: ["system", "network"], queryFn: getNetworkAccess });
  const thisMacOnly = !!network?.available && !network.other_devices;
  return (
    <div className="space-y-4 rounded-lg border border-gray-700/50 bg-gray-900/50 p-6">
      {made && <InviteLinkPanel link={made} thisMacOnly={thisMacOnly} onDone={() => setMade(null)} />}
      <InviteForm onMade={setMade} />
    </div>
  );
}

function LibraryStep() {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [folder, setFolder] = useState("");
  const add = useMutation({
    mutationFn: () => createLibrary(name.trim(), folder.trim()),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["libraries"] }),
  });
  if (add.isSuccess) {
    return (
      <p role="status" className="rounded-lg border border-emerald-600/40 bg-emerald-900/20 p-4 text-sm text-emerald-200">
        {add.data.name} is added. Lumiverb is scanning it; previews appear as they&apos;re made.
      </p>
    );
  }
  return (
    <form
      className="space-y-3 rounded-lg border border-gray-700/50 bg-gray-900/50 p-6"
      onSubmit={(e) => {
        e.preventDefault();
        add.mutate();
      }}
    >
      <label className="block space-y-1 text-sm text-gray-300">
        <span>Name</span>
        <input aria-label="Library name" className={inputClass} value={name} placeholder="Family photos"
               onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="block space-y-1 text-sm text-gray-300">
        <span>Folder</span>
        <input aria-label="Library folder" className={inputClass} value={folder} placeholder="/Volumes/Media/Photos"
               onChange={(e) => setFolder(e.target.value)} />
      </label>
      {add.isError && (
        <p role="alert" className="text-sm text-red-300">
          {add.error instanceof ApiError ? add.error.message : "Couldn't add it. Try again."}
        </p>
      )}
      <button type="submit" className={`${buttonClass} bg-indigo-600 text-white hover:bg-indigo-500`}
              disabled={add.isPending || !name.trim() || !folder.trim()}>
        Add library
      </button>
    </form>
  );
}
