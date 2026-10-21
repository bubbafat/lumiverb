import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  backUpNow,
  getBackups,
  getCurrentUser,
  setBackupsFolder,
  type BackupItem,
  type Backups,
  type NewFolderFacts,
} from "../../api/client";
import { MakeFolder, asked } from "./StorageSection";
import { copyText } from "../../lib/copyText";

export const BACKUPS_QUERY_KEY = ["system", "backups"];

const inputClass =
  "w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 disabled:opacity-50";
const buttonClass = "rounded-md px-4 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50";
const linkClass = "text-sm text-indigo-300 hover:text-indigo-200 disabled:opacity-50";

/** None for this long is a problem (the Admin page's row turns red too). */
const STALE_MS = 2 * 24 * 3600 * 1000;

const KIND: Record<string, string> = {
  nightly: "Nightly",
  now: "By hand",
  "before-upgrade": "Before an upgrade",
  "before-restore": "Before a restore",
};

/** The last one failed (after the newest made), none for two days, or a folder Lumiverb can't write: Settings flags it. */
export function backupsHaveProblem(b: Backups | undefined, now: number = Date.now()): boolean {
  if (!b) return false;
  if (b.folder_write_error) return true;
  const newest = b.backups[0] ? new Date(b.backups[0].made_at).getTime() : null;
  const failed = b.last_failed_at ? new Date(b.last_failed_at).getTime() : null;
  if (failed !== null && (newest === null || failed > newest)) return true;
  return newest !== null && now - newest > STALE_MS;
}

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

function when(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/** Due now (upkeep runs every 5 minutes), or when. */
function next(iso: string, now: number = Date.now()): string {
  return new Date(iso).getTime() - now < 10 * 60 * 1000 ? "within minutes" : when(iso);
}

function message(error: unknown): string {
  return error instanceof ApiError ? error.message : "Couldn't reach the server. Try again.";
}

/** Backups of every database: when, where they go, and how to put one back. Server admins only. */
export default function BackupsSection() {
  const queryClient = useQueryClient();
  const { data: user } = useQuery({ queryKey: ["settings", "me"], queryFn: getCurrentUser });
  const admin = user?.server_admin === true;
  const { data, isLoading, error } = useQuery({
    queryKey: BACKUPS_QUERY_KEY,
    queryFn: getBackups,
    enabled: admin,
    refetchInterval: (q) => (q.state.data?.running ? 2_000 : 60_000),
  });
  const now = useMutation({
    mutationFn: backUpNow,
    onSuccess: (next) => queryClient.setQueryData(BACKUPS_QUERY_KEY, next),
  });

  if (user && !admin) {
    return (
      <Frame>
        <p className="text-sm text-gray-400">Only a server admin sees backups.</p>
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
  const newest = data.backups[0];
  const failedLast =
    data.last_error && data.last_failed_at && (!newest || new Date(data.last_failed_at) > new Date(newest.made_at));

  return (
    <Frame>
      <section className="space-y-3" aria-label="Status">
        {data.running ? (
          <p role="status" className="text-sm text-indigo-200">
            Backing up now{data.running.of ? ` (${data.running.done + 1} of ${data.running.of})` : ""}…
          </p>
        ) : failedLast ? (
          <div role="alert" className="rounded-md border border-red-500/60 bg-red-500/15 px-4 py-3 text-sm text-red-100">
            <p className="font-semibold text-red-200">The last backup failed</p>
            <p className="mt-1">{data.last_error}</p>
          </div>
        ) : null}
        <p className="text-sm text-gray-300">
          {newest ? `Last: ${when(newest.made_at)}, ${size(newest.bytes)}.` : "None yet."} Next: {next(data.next_at)}.
        </p>
        <button
          type="button"
          className={`${buttonClass} bg-indigo-600 text-white hover:bg-indigo-500`}
          disabled={!!data.running || now.isPending}
          onClick={() => now.mutate()}
        >
          Back up now
        </button>
        {now.isError && <p role="alert" className="text-sm text-red-300">{message(now.error)}</p>}
      </section>

      <FolderChoice backups={data} />

      <section className="space-y-3" aria-labelledby="backups-made">
        <h3 id="backups-made" className="text-sm font-semibold uppercase tracking-wide text-gray-400">
          Made
        </h3>
        {data.backups.length === 0 ? (
          <p className="text-sm text-gray-500">None yet.</p>
        ) : (
          <ul className="space-y-2">
            {data.backups.map((b) => (
              <BackupRow key={b.name} backup={b} />
            ))}
          </ul>
        )}
        <p className="text-xs text-gray-500">
          Kept: every one from the last 7 days, one a week for 4 weeks, and the last 3 before an upgrade.
        </p>
      </section>
    </Frame>
  );
}

function Frame({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-6 space-y-6">
      <div>
        <h2 className="text-lg font-semibold text-gray-100">Backups</h2>
        <p className="mt-1 text-sm text-gray-400">
          Every night, every database: people, names, ratings, notes, projects, settings and search. Previews
          aren&apos;t in them; they&apos;re made again from the originals.
        </p>
        <p className="mt-1 text-sm text-gray-400">
          Keep a copy of this server&apos;s env file with them: it holds the key that opens the AI machines&apos; keys
          and plug-ins&apos; secret settings, sealed in the databases. Restored without it, those are entered again.
        </p>
      </div>
      {children}
    </div>
  );
}

/** Where backups go: shown, and chosen on the machine itself (also in the setup wizard). */
export function FolderChoice({ backups }: { backups: Backups }) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [folder, setFolder] = useState(backups.folder);
  const [toMake, setToMake] = useState<{ folder: string | null; text: string; facts: NewFolderFacts } | null>(null);
  const change = useMutation({
    mutationFn: ({ to, make }: { to: string | null; make: boolean }) => setBackupsFolder(to, make),
    onSuccess: (next) => {
      queryClient.setQueryData(BACKUPS_QUERY_KEY, next);
      setToMake(null);
      setEditing(false);
    },
    onError: (err, vars) => {
      const q = asked(err, ["make_folder"]);
      setToMake(q ? { folder: vars.to, text: q.message, facts: q.details as unknown as NewFolderFacts } : null);
    },
  });
  const refused = change.isError && !asked(change.error, ["make_folder"]);
  const isDefault = backups.folder === backups.default_folder;

  return (
    <section className="space-y-2" aria-labelledby="backups-folder">
      <h3 id="backups-folder" className="text-sm font-semibold uppercase tracking-wide text-gray-400">
        Folder
      </h3>
      {toMake ? (
        <MakeFolder
          text={toMake.text}
          facts={toMake.facts}
          pending={change.isPending}
          onMake={() => change.mutate({ to: toMake.folder, make: true })}
          onCancel={() => setToMake(null)}
        />
      ) : !editing ? (
        <>
          <p className="break-all font-mono text-sm text-gray-200">{backups.folder}</p>
          {backups.folder_write_error && (
            <p role="alert" className="text-sm text-red-300">
              {backups.folder_write_error} The next backup can&apos;t be made.
            </p>
          )}
          {isDefault && (
            <p className="text-xs text-gray-500">
              On the same drive as Lumiverb&apos;s data. A folder on another drive keeps them if this one fails.
            </p>
          )}
          {backups.changeable_here ? (
            <button type="button" className={linkClass} onClick={() => { setFolder(backups.folder); setEditing(true); }}>
              Change
            </button>
          ) : (
            <p className="text-xs text-gray-500">Choose it on the computer Lumiverb is installed on.</p>
          )}
        </>
      ) : (
        <form
          className="space-y-2"
          onSubmit={(e) => {
            e.preventDefault();
            change.mutate({ to: folder.trim(), make: false });
          }}
        >
          <input
            aria-label="Backups folder"
            className={inputClass}
            value={folder}
            placeholder="/Volumes/Backup/Lumiverb"
            onChange={(e) => setFolder(e.target.value)}
          />
          <p className="text-xs text-gray-500">From now on. Those made already stay where they are.</p>
          {refused && <p role="alert" className="text-sm text-red-300">{message(change.error)}</p>}
          <div className="flex flex-wrap items-center gap-3">
            <button type="submit" className={`${buttonClass} bg-indigo-600 text-white hover:bg-indigo-500`}
                    disabled={change.isPending || !folder.trim()}>
              Save
            </button>
            {!isDefault && (
              <button type="button" className={linkClass} disabled={change.isPending}
                      onClick={() => change.mutate({ to: null, make: false })}>
                Use the default
              </button>
            )}
            <button type="button" className={`${buttonClass} text-gray-300 hover:bg-gray-800`} onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </form>
      )}
    </section>
  );
}

function BackupRow({ backup }: { backup: BackupItem }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="rounded-md border border-gray-700/50 px-4 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-sm text-gray-200">
          {when(backup.made_at)} <span className="text-gray-500">· {KIND[backup.kind] ?? backup.kind} · {size(backup.bytes)}</span>
        </span>
        <button type="button" className={linkClass} aria-expanded={open} onClick={() => setOpen(!open)}>
          Restore…
        </button>
      </div>
      {open && (
        <div className="mt-3 space-y-2">
          <p className="text-sm text-gray-300">
            On the computer Lumiverb is installed on, in Terminal. Everything since then is replaced; what&apos;s there
            is backed up first, so it can be undone.
          </p>
          {backup.restore.map((command) => (
            <Command key={command} command={command} />
          ))}
        </div>
      )}
    </li>
  );
}

function Command({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  async function copy() {
    if (await copyText(command)) {
      setCopied(true);
    } else {
      input.current?.select();  // this browser won't copy from here: selected to copy by hand
    }
  }

  return (
    <div className="flex flex-col gap-2 sm:flex-row">
      <input
        ref={input}
        readOnly
        aria-label="Command"
        value={command}
        onFocus={(e) => e.target.select()}
        className="min-w-0 flex-1 rounded-md border border-gray-700 bg-gray-950 px-3 py-2 font-mono text-xs text-gray-100"
      />
      <button type="button" className={`${buttonClass} bg-gray-700 text-gray-100 hover:bg-gray-600`} onClick={() => void copy()}>
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}
