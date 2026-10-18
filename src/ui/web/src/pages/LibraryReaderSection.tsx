import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, chooseLibraryReader, getCurrentUser, getLibrary, listDevices } from "../api/client";
import { roleOn, runsIt } from "../lib/libraryRoles";
import { DEVICES_QUERY_KEY } from "./settings/DevicesSection";

const SERVER = "server";

/** Who reads the library's files (ADR-018 decision 1): this server, or a device (Settings › Devices), never both at
 * once. The reader scans the library and makes what needs its files; this server does everything else either way.
 * Its admin's choice, with the folder as the new reader sees it. The account's devices are listed for an account
 * admin (Settings › Devices); another of its admins chooses this server or the device it has. */
export default function LibraryReaderSection({ libraryId }: { libraryId: string }) {
  const { data: user } = useQuery({ queryKey: ["settings", "me"], queryFn: getCurrentUser });
  const { data: library } = useQuery({ queryKey: ["library", libraryId], queryFn: () => getLibrary(libraryId) });
  const accountAdmin = user?.role === "admin";
  const { data: devices } = useQuery({ queryKey: DEVICES_QUERY_KEY, queryFn: listDevices, enabled: accountAdmin });
  if (!library || !runsIt(roleOn(library, user?.role))) return null;
  // Built once the library is here, so the choice starts from it.
  return <ReaderForm key={`${library.reader}:${library.root_path}`} libraryId={libraryId} reader={library.reader ?? SERVER}
                     readerName={library.reader_name ?? null} root={library.root_path} devices={devices ?? []}
                     accountAdmin={accountAdmin} />;
}

function ReaderForm({ libraryId, reader, readerName, root, devices, accountAdmin }: {
  libraryId: string;
  reader: string;
  readerName: string | null;
  root: string;
  devices: { device_id: string; name: string }[];
  accountAdmin: boolean;
}) {
  const queryClient = useQueryClient();
  const [choice, setChoice] = useState(reader);
  const [folder, setFolder] = useState(root);
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
  const now = reader === SERVER ? "This server reads its files." : `${readerName ?? "A device"} reads its files.`;
  const changed = choice !== reader || folder.trim() !== root;

  return (
    <div className="space-y-3 rounded-lg border border-gray-700/50 bg-gray-900/50 px-4 py-3" aria-label="Who reads it">
      <h2 className="text-lg font-semibold text-gray-100">Who reads it</h2>
      <p className="text-sm text-gray-400">
        {now} Its reader scans it and makes what needs its files (previews, video facts, analysis copies); this server
        does everything else (search, faces, descriptions, scenes, transcripts).
      </p>
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
        {accountAdmin && devices.length === 0 && (
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
