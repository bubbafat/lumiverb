import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, chooseLibraryReader, getCurrentUser, getLibrary, getTenantSettings, listDevices } from "../api/client";
import { AccountFoldersNote } from "../components/AccountFoldersNote";
import { roleOn, runsIt } from "../lib/libraryRoles";
import { DEVICES_QUERY_KEY } from "./settings/DevicesSection";

const SERVER = "server";

/** Who reads the library's files (ADR-018 decision 1): this server, or a device (Settings › Devices), never both at
 * once. The reader scans the library and makes what needs its files; this server does everything else either way.
 * An account admin's choice, with the folder as the new reader sees it, as adding a library is: the library's own
 * admin sees who reads it and where, and that an account admin chooses them. A folder on this server is in the
 * account's folders when a server admin gave it any: said under the folder. */
export default function LibraryReaderSection({ libraryId }: { libraryId: string }) {
  const { data: user } = useQuery({ queryKey: ["settings", "me"], queryFn: getCurrentUser });
  const { data: library } = useQuery({ queryKey: ["library", libraryId], queryFn: () => getLibrary(libraryId) });
  const accountAdmin = user?.role === "admin";
  const { data: devices } = useQuery({ queryKey: DEVICES_QUERY_KEY, queryFn: listDevices, enabled: accountAdmin });
  if (!library || !user || !runsIt(roleOn(library, user.role))) return null;
  const reader = library.reader ?? SERVER;
  const readerName = library.reader_name ?? null;
  if (!accountAdmin) return <ReaderShown reader={reader} readerName={readerName} root={library.root_path} />;
  // Built once the library is here, so the choice starts from it.
  return <ReaderForm key={`${library.reader}:${library.root_path}`} libraryId={libraryId} reader={reader}
                     readerName={readerName} root={library.root_path} devices={devices ?? []} />;
}

function Intro({ reader, readerName }: { reader: string; readerName: string | null }) {
  const now = reader === SERVER ? "This server reads its files." : `${readerName ?? "A device"} reads its files.`;
  return (
    <>
      <h2 className="text-lg font-semibold text-gray-100">Who reads it</h2>
      <p className="text-sm text-gray-400">
        {now} Its reader scans it and makes what needs its files (previews, video facts, analysis copies); this server
        does everything else (search, faces, descriptions, scenes, transcripts).
      </p>
    </>
  );
}

/** For the library's admin who isn't an account admin: who reads it and its folder, as they are. */
function ReaderShown({ reader, readerName, root }: { reader: string; readerName: string | null; root: string }) {
  return (
    <div className="space-y-3 rounded-lg border border-gray-700/50 bg-gray-900/50 px-4 py-3" aria-label="Who reads it">
      <Intro reader={reader} readerName={readerName} />
      <p className="text-sm text-gray-300">
        Its folder {reader === SERVER ? "on this server" : `as ${readerName ?? "the device"} sees it`}:{" "}
        <span className="break-all font-mono">{root}</span>
      </p>
      <p className="text-xs text-gray-500">An account admin chooses its folder and who reads it.</p>
    </div>
  );
}

function ReaderForm({ libraryId, reader, readerName, root, devices }: {
  libraryId: string;
  reader: string;
  readerName: string | null;
  root: string;
  devices: { device_id: string; name: string }[];
}) {
  const queryClient = useQueryClient();
  const [choice, setChoice] = useState(reader);
  const [folder, setFolder] = useState(root);
  const { data: settings } = useQuery({ queryKey: ["tenant-settings"], queryFn: getTenantSettings, staleTime: 60_000 });
  const save = useMutation({
    mutationFn: () => chooseLibraryReader(libraryId, choice, folder.trim() !== root ? folder.trim() : undefined),
    onSuccess: (next) => {
      queryClient.setQueryData(["library", libraryId], next);
      void queryClient.invalidateQueries({ queryKey: ["libraries"] });
      void queryClient.invalidateQueries({ queryKey: DEVICES_QUERY_KEY });
      void queryClient.invalidateQueries({ queryKey: ["scheduler-status"] });
    },
  });
  const chosen = devices.find((d) => d.device_id === choice);
  const changed = choice !== reader || folder.trim() !== root;

  return (
    <div className="space-y-3 rounded-lg border border-gray-700/50 bg-gray-900/50 px-4 py-3" aria-label="Who reads it">
      <Intro reader={reader} readerName={readerName} />
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          if (changed && folder.trim()) save.mutate();
        }}
      >
        <label className="block text-sm text-gray-300">
          <span className="mb-1 block">Read by</span>
          <select
            value={choice}
            onChange={(e) => {
              setChoice(e.target.value);
              save.reset();
            }}
            className="rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-gray-100"
          >
            <option value={SERVER}>This server</option>
            {devices.map((d) => (
              <option key={d.device_id} value={d.device_id}>{d.name}</option>
            ))}
            {reader !== SERVER && !devices.some((d) => d.device_id === reader) && (
              <option value={reader}>{readerName ?? "Its device"}</option>
            )}
          </select>
        </label>
        {devices.length === 0 && (
          <p className="text-xs text-gray-500">
            To have another computer read it, register it in <Link to="/settings/devices" className="text-indigo-400 hover:text-indigo-300">Settings › Devices</Link>.
          </p>
        )}
        <label className="block text-sm text-gray-300">
          <span className="mb-1 block">
            Its folder {choice === SERVER ? "on this server" : `as ${chosen?.name ?? readerName ?? "the device"} sees it`}
          </span>
          <input
            type="text"
            value={folder}
            onChange={(e) => {
              setFolder(e.target.value);
              save.reset();
            }}
            className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 font-mono text-sm text-gray-100"
          />
        </label>
        {choice === SERVER && <AccountFoldersNote folders={settings?.folders} />}
        {choice !== reader && (
          <p className="text-sm text-amber-100/90">
            {choice === SERVER
              ? "This server reads it from now, at that folder; the device stops."
              : `${chosen?.name ?? "The device"} reads it from its next check-in, at that folder; ${reader === SERVER ? "this server" : readerName ?? "its device"} stops.`}
          </p>
        )}
        <div className="flex items-center gap-3">
          <button
            type="submit"
            disabled={!changed || !folder.trim() || save.isPending}
            className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Save
          </button>
          <span role="status" className="text-sm">
            {save.isSuccess && <span className="text-emerald-300">Saved</span>}
          </span>
        </div>
        {save.isError && (
          <p role="alert" className="text-sm text-red-300">
            {save.error instanceof ApiError ? save.error.message : "Couldn't reach the server. Try again."}
          </p>
        )}
      </form>
    </div>
  );
}
