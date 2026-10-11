import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  getCurrentUser,
  getStorageInfo,
  goOnMovingPreviews,
  movePreviews,
  type NewFolderFacts,
  type PreviewsMoveFacts,
  type StorageInfo,
  type StorageMove,
} from "../../api/client";

export const STORAGE_QUERY_KEY = ["system", "storage"];

const inputClass =
  "w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 disabled:opacity-50";
const buttonClass = "rounded-md px-4 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50";
const linkClass = "text-sm text-indigo-300 hover:text-indigo-200 disabled:opacity-50";

export function size(bytes: number): string {
  const units = ["bytes", "KB", "MB", "GB", "TB"];
  let n = bytes;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  return i === 0 ? `${n} bytes` : `${n.toFixed(1)} ${units[i]}`;
}

export function free(available: number | null, total: number | null): string {
  return available !== null && total ? `${size(available)} free of ${size(total)}` : "";
}

function message(error: unknown): string {
  return error instanceof ApiError ? error.message : "Couldn't reach the server. Try again.";
}

/** Settings flags it: a previews drive isn't plugged in, or a move stopped. */
export function storageHasProblem(s: StorageInfo | undefined): boolean {
  return !!s && (!s.previews_available || !!s.moving?.error || s.libraries.some((l) => !l.available));
}

/** Where the previews are (off a Mac's system drive, say), and moving them. Admins only. */
export default function StorageSection() {
  const queryClient = useQueryClient();
  const { data: user } = useQuery({ queryKey: ["settings", "me"], queryFn: getCurrentUser });
  const admin = user?.role === "admin";
  const { data, isLoading, error } = useQuery({
    queryKey: STORAGE_QUERY_KEY,
    queryFn: getStorageInfo,
    enabled: admin,
    refetchInterval: (q) => (q.state.data?.moving ? 2_000 : 30_000),
  });

  if (user && !admin) {
    return (
      <Frame>
        <p className="text-sm text-gray-400">Only admins see where Lumiverb keeps its files.</p>
      </Frame>
    );
  }
  if (error) {
    return (
      <Frame>
        <p role="alert" className="text-sm text-red-300">{message(error)}</p>
      </Frame>
    );
  }
  if (isLoading || !data) {
    return <div className="h-32 rounded-lg border border-gray-700/50 bg-gray-900/50 animate-pulse" />;
  }
  const isDefault = data.previews === data.data;

  return (
    <Frame>
      <section className="space-y-2" aria-labelledby="storage-previews">
        <h3 id="storage-previews" className="text-sm font-semibold uppercase tracking-wide text-gray-400">
          Previews
        </h3>
        <p className="break-all font-mono text-sm text-gray-200">{data.previews}</p>
        {!data.previews_available ? (
          <p role="alert" className="text-sm text-red-300">
            This folder isn&apos;t there. Is its drive plugged in? Until it is, previews don&apos;t show and none are made.
          </p>
        ) : (
          <p className="text-xs text-gray-500">
            {isDefault ? "In the data folder. " : data.same_disk ? "On the same drive as the data folder. " : ""}
            {free(data.previews_free, data.previews_total)}
          </p>
        )}
        {data.moving ? (
          <Moving move={data.moving} onTriedAgain={(next) => queryClient.setQueryData(STORAGE_QUERY_KEY, next)} />
        ) : (
          <MoveChoice
            changeableHere={data.changeable_here}
            backLabel={isDefault ? null : "Back to the data folder…"}
            ask={(to, go, make) => movePreviews(to, go, make)}
            onMoving={(next) => queryClient.setQueryData(STORAGE_QUERY_KEY, next)}
          />
        )}
      </section>

      {data.libraries.length > 0 && (
        <section className="space-y-2" aria-labelledby="storage-libraries">
          <h3 id="storage-libraries" className="text-sm font-semibold uppercase tracking-wide text-gray-400">
            Libraries with a folder of their own
          </h3>
          <ul className="space-y-1">
            {data.libraries.map((l) => (
              <li key={l.library_id} className="text-sm text-gray-300">
                <Link to={`/libraries/${l.library_id}/settings`} className="text-indigo-300 hover:text-indigo-200">
                  {l.name}
                </Link>
                : <span className="break-all font-mono">{l.folder}</span>
                {!l.available && <span className="text-red-300"> (its drive isn&apos;t plugged in)</span>}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="space-y-2" aria-labelledby="storage-data">
        <h3 id="storage-data" className="text-sm font-semibold uppercase tracking-wide text-gray-400">
          Data
        </h3>
        <p className="break-all font-mono text-sm text-gray-200">{data.data}</p>
        <p className="text-xs text-gray-500">
          The database, the search index, and the models for search and transcripts. They stay here; Ollama keeps
          its own. {free(data.data_free, data.data_total)}
        </p>
      </section>
    </Frame>
  );
}

function Frame({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-6 space-y-6">
      <div>
        <h2 className="text-lg font-semibold text-gray-100">Storage</h2>
        <p className="mt-1 text-sm text-gray-400">
          Where Lumiverb keeps the previews it makes: thumbnails, video copies for analysis, scene frames and face
          crops. They grow with your libraries; put them on a drive with room. A library can have a folder of its
          own, in its settings.
        </p>
      </div>
      {children}
    </div>
  );
}

/** A move under way: how far, and Try again when it stopped. */
export function Moving<T>({ move, onTriedAgain, what }: {
  move: Omit<StorageMove, "library_id" | "library_name"> & { library_name?: string | null };
  onTriedAgain: (next: T) => void;
  what?: string;
}) {
  const again = useMutation({
    mutationFn: goOnMovingPreviews,
    onSuccess: (next) => onTriedAgain(next as unknown as T),
  });
  const share = move.total_bytes ? Math.min(1, move.done_bytes / move.total_bytes) : 0;
  const whose = what ?? (move.library_name ? `${move.library_name}'s previews` : "the previews");
  return (
    <div className="space-y-2">
      <p role="status" className="text-sm text-indigo-200">
        Moving {whose} from <span className="font-mono">{move.from_folder}</span>:{" "}
        {move.done_files.toLocaleString()} of {move.total_files.toLocaleString()} ({size(move.done_bytes)} of{" "}
        {size(move.total_bytes)}).
      </p>
      <div className="h-2 w-full overflow-hidden rounded bg-gray-800" aria-hidden>
        <div className="h-full bg-indigo-500" style={{ width: `${Math.round(share * 100)}%` }} />
      </div>
      <p className="text-xs text-gray-500">Lumiverb keeps working. Previews not moved yet show from where they are.</p>
      {move.error && !move.running && (
        <div role="alert" className="rounded-md border border-red-500/60 bg-red-500/15 px-4 py-3 text-sm text-red-100">
          <p className="font-semibold text-red-200">The move stopped</p>
          <p className="mt-1">{move.error}</p>
          <p className="mt-1 text-red-200/80">It&apos;s tried again in 15 minutes.</p>
          <button
            type="button"
            className={`${buttonClass} mt-2 bg-gray-700 text-gray-100 hover:bg-gray-600`}
            disabled={again.isPending}
            onClick={() => again.mutate()}
          >
            Try again
          </button>
          {again.isError && <p className="mt-1 text-red-300">{message(again.error)}</p>}
        </div>
      )}
    </div>
  );
}

/** The API's questions: a folder not there yet (make_folder) and what moves (move_previews). */
const ASKED = ["make_folder", "move_previews"];

/** The question a 409 asks, when it's one of these; null otherwise. */
export function asked(error: unknown, codes: string[] = ASKED): ApiError | null {
  return error instanceof ApiError && !!error.code && codes.includes(error.code) && error.details ? error : null;
}

/** A folder not there yet: the API's question (409 make_folder), and Make it. */
export function MakeFolder({ text, facts, pending, onMake, onCancel }: {
  text: string;
  facts: NewFolderFacts;
  pending: boolean;
  onMake: () => void;
  onCancel: () => void;
}) {
  return (
    <div role="dialog" aria-label="Make the folder" className="space-y-3 rounded-md border border-gray-700 px-4 py-3">
      <p className="text-sm text-gray-200">{text}</p>
      <p className="text-xs text-gray-500">{size(facts.free)} free there.</p>
      <div className="flex flex-wrap gap-3">
        <button type="button" className={`${buttonClass} bg-indigo-600 text-white hover:bg-indigo-500`}
                disabled={pending} onClick={onMake}>
          Make it
        </button>
        <button type="button" className={`${buttonClass} text-gray-300 hover:bg-gray-800`} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}

type Question =
  | { kind: "make"; folder: string | null; text: string; facts: NewFolderFacts }
  | { kind: "move"; folder: string | null; text: string; facts: PreviewsMoveFacts; make: boolean };

/** Choosing a folder, making it if it isn't there, seeing what moves, and
 * saying yes: the API's questions (409s), each asked in turn. With nothing to
 * move, it doesn't ask. */
export function MoveChoice<T>({ changeableHere, backLabel, ask, onMoving }: {
  changeableHere: boolean;
  /** "Back to …": null when they're there already. */
  backLabel: string | null;
  ask: (to: string | null, go: boolean, make: boolean) => Promise<T>;
  onMoving: (next: T) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [folder, setFolder] = useState("");
  const [question, setQuestion] = useState<Question | null>(null);
  const mutation = useMutation({
    mutationFn: ({ to, go, make }: { to: string | null; go: boolean; make: boolean }) => ask(to, go, make),
    onSuccess: (next) => {
      onMoving(next);
      setQuestion(null);
      setEditing(false);
    },
    onError: (err, vars) => {
      const q = asked(err);
      if (q?.code === "make_folder") {
        setQuestion({ kind: "make", folder: vars.to, text: q.message, facts: q.details as unknown as NewFolderFacts });
      } else if (q) {
        setQuestion({ kind: "move", folder: vars.to, text: q.message, make: vars.make,
                      facts: q.details as unknown as PreviewsMoveFacts });
      } else {
        setQuestion(null); // said where it's chosen
      }
    },
  });
  const refused = mutation.isError && !asked(mutation.error);

  if (!changeableHere) {
    return <p className="text-xs text-gray-500">Move them on the computer Lumiverb is installed on.</p>;
  }
  if (question?.kind === "make") {
    return (
      <MakeFolder
        text={question.text}
        facts={question.facts}
        pending={mutation.isPending}
        onMake={() => mutation.mutate({ to: question.folder, go: false, make: true })}
        onCancel={() => setQuestion(null)}
      />
    );
  }
  if (question) {
    const facts = question;
    return (
      <div role="dialog" aria-label="Move the previews" className="space-y-3 rounded-md border border-gray-700 px-4 py-3">
        <p className="text-sm text-gray-200">{facts.text}</p>
        <p className="text-xs text-gray-500">{size(facts.facts.free)} free there.</p>
        <div className="flex flex-wrap gap-3">
          <button
            type="button"
            className={`${buttonClass} bg-indigo-600 text-white hover:bg-indigo-500`}
            disabled={mutation.isPending}
            onClick={() => mutation.mutate({ to: facts.folder, go: true, make: facts.make })}
          >
            {facts.facts.files ? "Move them" : "Use it"}
          </button>
          <button type="button" className={`${buttonClass} text-gray-300 hover:bg-gray-800`} onClick={() => setQuestion(null)}>
            Cancel
          </button>
        </div>
      </div>
    );
  }
  if (!editing) {
    return (
      <div className="flex flex-wrap gap-4">
        <button type="button" className={linkClass} onClick={() => { setFolder(""); setEditing(true); }}>
          Move them…
        </button>
        {backLabel && (
          <button type="button" className={linkClass} disabled={mutation.isPending}
                  onClick={() => mutation.mutate({ to: null, go: false, make: false })}>
            {backLabel}
          </button>
        )}
        {refused && <p role="alert" className="text-sm text-red-300">{message(mutation.error)}</p>}
      </div>
    );
  }
  return (
    <form
      className="space-y-2"
      onSubmit={(e) => {
        e.preventDefault();
        mutation.mutate({ to: folder.trim(), go: false, make: false });
      }}
    >
      <input
        aria-label="Previews folder"
        className={inputClass}
        value={folder}
        placeholder="/Volumes/Media/Lumiverb previews"
        onChange={(e) => setFolder(e.target.value)}
      />
      <p className="text-xs text-gray-500">
        An empty folder on the drive you want them on, or one to make. You&apos;ll see what moves first.
      </p>
      {refused && <p role="alert" className="text-sm text-red-300">{message(mutation.error)}</p>}
      <div className="flex flex-wrap items-center gap-3">
        <button type="submit" className={`${buttonClass} bg-indigo-600 text-white hover:bg-indigo-500`}
                disabled={mutation.isPending || !folder.trim()}>
          {mutation.isPending ? "Checking…" : "Next"}
        </button>
        <button type="button" className={`${buttonClass} text-gray-300 hover:bg-gray-800`} onClick={() => setEditing(false)}>
          Cancel
        </button>
      </div>
    </form>
  );
}
