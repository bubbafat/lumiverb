import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  getCurrentUser,
  getStorageInfo,
  goOnMovingPreviews,
  movePreviews,
  type PreviewsMoveFacts,
  type StorageInfo,
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

function free(available: number | null, total: number | null): string {
  return available !== null && total ? `${size(available)} free of ${size(total)}` : "";
}

function message(error: unknown): string {
  return error instanceof ApiError ? error.message : "Couldn't reach the server. Try again.";
}

/** Settings flags it: the previews' drive isn't plugged in, or a move stopped. */
export function storageHasProblem(s: StorageInfo | undefined): boolean {
  return !!s && (!s.previews_available || !!s.moving?.error);
}

/** Where the previews are (off a Mac's system drive, say), and moving them. Admins only. */
export default function StorageSection() {
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
            {data.previews === data.data ? "In the data folder. " : data.same_disk ? "On the same drive as the data folder. " : ""}
            {free(data.previews_free, data.previews_total)}
          </p>
        )}
        {data.moving ? <Moving info={data} /> : <MoveChoice info={data} />}
      </section>

      <section className="space-y-2" aria-labelledby="storage-data">
        <h3 id="storage-data" className="text-sm font-semibold uppercase tracking-wide text-gray-400">
          Data
        </h3>
        <p className="break-all font-mono text-sm text-gray-200">{data.data}</p>
        <p className="text-xs text-gray-500">
          The database, the search index and AI models. They stay here. {free(data.data_free, data.data_total)}
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
          crops. They grow with your libraries; put them on a drive with room.
        </p>
      </div>
      {children}
    </div>
  );
}

function Moving({ info }: { info: StorageInfo }) {
  const queryClient = useQueryClient();
  const move = info.moving!;
  const again = useMutation({
    mutationFn: goOnMovingPreviews,
    onSuccess: (next) => queryClient.setQueryData(STORAGE_QUERY_KEY, next),
  });
  const share = move.total_bytes ? Math.min(1, move.done_bytes / move.total_bytes) : 0;
  return (
    <div className="space-y-2">
      <p role="status" className="text-sm text-indigo-200">
        Moving the previews from <span className="font-mono">{move.from_folder}</span>:{" "}
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

function MoveChoice({ info }: { info: StorageInfo }) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [folder, setFolder] = useState("");
  const [facts, setFacts] = useState<{ folder: string | null; text: string; move: PreviewsMoveFacts } | null>(null);
  const ask = useMutation({
    mutationFn: ({ to, go }: { to: string | null; go: boolean }) => movePreviews(to, go),
    onSuccess: (next) => {
      queryClient.setQueryData(STORAGE_QUERY_KEY, next);
      setFacts(null);
      setEditing(false);
    },
    onError: (err, vars) => {
      if (err instanceof ApiError && err.code === "move_previews" && err.details) {
        setFacts({ folder: vars.to, text: err.message, move: err.details as unknown as PreviewsMoveFacts });
      }
    },
  });
  const isDefault = info.previews === info.data;

  if (!info.changeable_here) {
    return <p className="text-xs text-gray-500">Move them on the computer Lumiverb is installed on.</p>;
  }
  if (facts) {
    return (
      <div role="dialog" aria-label="Move the previews" className="space-y-3 rounded-md border border-gray-700 px-4 py-3">
        <p className="text-sm text-gray-200">{facts.text}</p>
        <p className="text-xs text-gray-500">{size(facts.move.free)} free there.</p>
        <div className="flex flex-wrap gap-3">
          <button
            type="button"
            className={`${buttonClass} bg-indigo-600 text-white hover:bg-indigo-500`}
            disabled={ask.isPending}
            onClick={() => ask.mutate({ to: facts.folder, go: true })}
          >
            Move them
          </button>
          <button type="button" className={`${buttonClass} text-gray-300 hover:bg-gray-800`} onClick={() => setFacts(null)}>
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
        {!isDefault && (
          <button type="button" className={linkClass} disabled={ask.isPending} onClick={() => ask.mutate({ to: null, go: false })}>
            Back to the data folder…
          </button>
        )}
        {ask.isError && !(ask.error instanceof ApiError && ask.error.code === "move_previews") && (
          <p role="alert" className="text-sm text-red-300">{message(ask.error)}</p>
        )}
      </div>
    );
  }
  const refused = ask.isError && !(ask.error instanceof ApiError && ask.error.code === "move_previews");
  return (
    <form
      className="space-y-2"
      onSubmit={(e) => {
        e.preventDefault();
        ask.mutate({ to: folder.trim(), go: false });
      }}
    >
      <input
        aria-label="Previews folder"
        className={inputClass}
        value={folder}
        placeholder="/Volumes/Media/Lumiverb previews"
        onChange={(e) => setFolder(e.target.value)}
      />
      <p className="text-xs text-gray-500">An empty folder on the drive you want them on. You&apos;ll see what moves first.</p>
      {refused && <p role="alert" className="text-sm text-red-300">{message(ask.error)}</p>}
      <div className="flex flex-wrap items-center gap-3">
        <button type="submit" className={`${buttonClass} bg-indigo-600 text-white hover:bg-indigo-500`}
                disabled={ask.isPending || !folder.trim()}>
          {ask.isPending ? "Checking…" : "Next"}
        </button>
        <button type="button" className={`${buttonClass} text-gray-300 hover:bg-gray-800`} onClick={() => setEditing(false)}>
          Cancel
        </button>
      </div>
    </form>
  );
}
