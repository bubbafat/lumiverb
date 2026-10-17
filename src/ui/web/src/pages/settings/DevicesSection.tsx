import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  endDeviceKey,
  getCurrentUser,
  getNetworkAccess,
  listDevices,
  listLibraries,
  newDeviceKey,
  registerDevice,
  removeDevice,
  renameDevice,
  type Device,
  type DeviceWithKey,
} from "../../api/client";
import { copyText, selectText } from "../../lib/copyText";
import { timeAgo } from "../../lib/format";

export const DEVICES_QUERY_KEY = ["devices"];

const inputClass =
  "rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-gray-100 placeholder-gray-500 outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500";
const smallButton =
  "rounded-lg border border-gray-700 px-3 py-1 text-sm text-gray-200 hover:border-gray-500 disabled:opacity-50";

function said(error: unknown): string {
  return error instanceof ApiError ? error.message : "Couldn't reach the server. Try again.";
}

/** The account's devices (ADR-018 phase 6): computers that read libraries for this server, scanning them and making
 * what needs their files, while this server does everything else. An account admin registers one and gives the key to
 * `lumiverb device run` on it; a library chooses it as its reader in its settings. */
export default function DevicesSection() {
  const queryClient = useQueryClient();
  const { data: user } = useQuery({ queryKey: ["settings", "me"], queryFn: getCurrentUser });
  const isAdmin = user?.role === "admin";
  const { data: devices, isLoading, error } = useQuery({ queryKey: DEVICES_QUERY_KEY, queryFn: listDevices,
                                                         enabled: isAdmin });
  const { data: libraries } = useQuery({ queryKey: ["libraries", true], queryFn: () => listLibraries(true),
                                         enabled: isAdmin });
  const [name, setName] = useState("");
  const [shown, setShown] = useState<DeviceWithKey | null>(null);
  const refresh = () => queryClient.invalidateQueries({ queryKey: DEVICES_QUERY_KEY });
  const register = useMutation({
    mutationFn: () => registerDevice(name.trim()),
    onSuccess: (device) => {
      setShown(device);
      setName("");
      void refresh();
    },
  });

  if (user && !isAdmin) {
    return (
      <Frame>
        <p className="text-sm text-gray-400">Only admins manage devices.</p>
      </Frame>
    );
  }
  const names = new Map((libraries ?? []).map((l) => [l.library_id, l.name]));

  return (
    <div className="space-y-6">
      <Frame>
        {error && <p role="alert" className="text-sm text-red-300">{said(error)}</p>}
        {isLoading ? (
          <div className="h-16 rounded-lg bg-gray-900/50 animate-pulse" />
        ) : devices && devices.length > 0 ? (
          <ul aria-label="Devices" className="divide-y divide-gray-800">
            {devices.map((d) => (
              <DeviceRow key={d.device_id} device={d} libraryNames={names} onKey={setShown} onChanged={refresh} />
            ))}
          </ul>
        ) : (
          <p className="text-sm text-gray-500 italic">No devices yet.</p>
        )}
      </Frame>

      {shown && <KeyPanel device={shown} onDone={() => setShown(null)} />}

      <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-6">
        <h3 className="mb-1 text-sm font-semibold text-gray-100">Register a device</h3>
        <p className="mb-4 text-sm text-gray-400">
          A computer with libraries on its drives. It gets a key of its own, which only scans the libraries that
          choose it, and makes what needs their files.
        </p>
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (name.trim()) register.mutate();
          }}
        >
          <label className="flex-1 text-sm text-gray-300">
            <span className="mb-1 block">Name</span>
            <input
              type="text"
              value={name}
              maxLength={100}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Studio Mac"
              className={`w-full ${inputClass}`}
            />
          </label>
          <button
            type="submit"
            disabled={register.isPending || !name.trim()}
            className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {register.isPending ? "Registering…" : "Register"}
          </button>
        </form>
        {register.isError && <p role="alert" className="mt-3 text-sm text-red-300">{said(register.error)}</p>}
      </div>
    </div>
  );
}

function Frame({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-6 space-y-4">
      <div>
        <h2 className="text-lg font-semibold text-gray-100">Devices</h2>
        <p className="mt-1 text-sm text-gray-400">
          Computers that read libraries for this server: each scans the libraries that choose it and sends what
          needs their files. This server does everything else.
        </p>
      </div>
      {children}
    </div>
  );
}

/** One device: what it reads, its key, and what an admin can do with it. */
function DeviceRow({ device, libraryNames, onKey, onChanged }: {
  device: Device;
  libraryNames: Map<string, string>;
  onKey: (d: DeviceWithKey) => void;
  onChanged: () => void;
}) {
  const [renaming, setRenaming] = useState(false);
  const [newName, setNewName] = useState(device.name);
  const [confirming, setConfirming] = useState<"key" | "end" | "remove" | null>(null);
  const rename = useMutation({
    mutationFn: () => renameDevice(device.device_id, newName.trim()),
    onSuccess: () => {
      setRenaming(false);
      onChanged();
    },
  });
  const rekey = useMutation({
    mutationFn: () => newDeviceKey(device.device_id),
    onSuccess: (d) => {
      setConfirming(null);
      onKey(d);
      onChanged();
    },
  });
  const end = useMutation({
    mutationFn: () => endDeviceKey(device.device_id),
    onSuccess: () => {
      setConfirming(null);
      onChanged();
    },
  });
  const remove = useMutation({
    mutationFn: () => removeDevice(device.device_id),
    onSuccess: () => {
      setConfirming(null);
      onChanged();
    },
    onError: () => setConfirming(null),
  });
  const reads = device.libraries.map((id) => libraryNames.get(id) ?? id);
  const key = !device.has_key
    ? "No key: it does nothing until it gets a new one."
    : device.key_last_used_at
      ? `Its key was last used ${timeAgo(device.key_last_used_at)}.`
      : "Its key hasn't been used yet.";
  const error = rename.error ?? rekey.error ?? end.error ?? remove.error;
  const blocked = remove.error instanceof ApiError && remove.error.code === "device_reads_libraries";

  return (
    <li className="space-y-2 py-4 first:pt-0 last:pb-0" aria-label={device.name}>
      {renaming ? (
        <form
          className="flex flex-wrap gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (newName.trim()) rename.mutate();
          }}
        >
          <input aria-label="New name" value={newName} maxLength={100} onChange={(e) => setNewName(e.target.value)}
                 className={inputClass} />
          <button type="submit" disabled={rename.isPending || !newName.trim()} className={smallButton}>Save</button>
          <button type="button" onClick={() => setRenaming(false)} className="text-sm text-gray-400">Cancel</button>
        </form>
      ) : (
        <p className="font-medium text-gray-100">{device.name}</p>
      )}
      <p className="text-sm text-gray-400">
        {reads.length ? `Reads ${reads.join(", ")}.` : "Reads no library yet: choose it in a library's settings."}{" "}
        {key}
      </p>
      {confirming ? (
        <div role="alertdialog" aria-label="Are you sure?" className="flex flex-wrap items-center gap-2 text-sm text-amber-100/90">
          <span>
            {confirming === "key" && "A new key: its old one stops working at once."}
            {confirming === "end" && "End its key: it does nothing until it gets a new one."}
            {confirming === "remove" && "Remove it, and end its key."}
          </span>
          <button
            type="button"
            className="rounded-md bg-red-600 px-3 py-1 text-sm font-medium text-white hover:bg-red-500 disabled:opacity-50"
            disabled={rekey.isPending || end.isPending || remove.isPending}
            onClick={() => (confirming === "key" ? rekey.mutate() : confirming === "end" ? end.mutate() : remove.mutate())}
          >
            {confirming === "key" ? "New key" : confirming === "end" ? "End key" : "Remove"}
          </button>
          <button type="button" onClick={() => setConfirming(null)} className="text-sm text-gray-400">Cancel</button>
        </div>
      ) : (
        <div className="flex flex-wrap gap-2">
          {!renaming && <button type="button" className={smallButton} onClick={() => setRenaming(true)}>Rename</button>}
          <button type="button" className={smallButton} onClick={() => setConfirming("key")}>New key</button>
          {device.has_key && (
            <button type="button" className={smallButton} onClick={() => setConfirming("end")}>End key</button>
          )}
          <button type="button" className={smallButton} onClick={() => setConfirming("remove")}>Remove</button>
        </div>
      )}
      {error && (
        <p role="alert" className="text-sm text-red-300">
          {blocked ? `It reads ${reads.join(", ")}: choose another reader for ${reads.length === 1 ? "it" : "them"} first.` : said(error)}
        </p>
      )}
    </li>
  );
}

/** A device's key, shown this once, and what to run on it. */
function KeyPanel({ device, onDone }: { device: DeviceWithKey; onDone: () => void }) {
  const keyText = useRef<HTMLElement>(null);
  const [copied, setCopied] = useState<"copied" | "select" | null>(null);
  const { data: network } = useQuery({ queryKey: ["system", "network"], queryFn: getNetworkAccess });
  // Where the device reaches this server: its address on the network when other devices may use it.
  const server = network?.other_devices && network.addresses.length ? network.addresses[0] : window.location.origin;

  async function copy() {
    if (await copyText(device.key)) {
      setCopied("copied");
    } else {
      selectText(keyText.current);
      setCopied("select");
    }
  }

  return (
    <div role="region" aria-label="Its key" className="rounded-lg border border-amber-700/50 bg-amber-900/20 p-4 space-y-3">
      <p className="text-sm font-medium text-amber-300">{device.name}&apos;s key. Copy it now: it won&apos;t be shown again.</p>
      <div className="flex items-center gap-2">
        <code ref={keyText} className="flex-1 break-all rounded bg-gray-800 px-3 py-2 font-mono text-sm text-gray-100 select-all">
          {device.key}
        </code>
        <button type="button" onClick={() => void copy()} className={smallButton}>
          {copied === "copied" ? "Copied" : "Copy"}
        </button>
      </div>
      {copied === "select" && (
        <p className="text-xs text-gray-400">This browser won&apos;t copy from here: the key is selected, so press ⌘C (Ctrl+C).</p>
      )}
      <div className="space-y-1 text-sm text-gray-300">
        <p>On the device, with Lumiverb installed, set where this server is and the key (paste it when asked):</p>
        <pre className="overflow-x-auto rounded bg-gray-950 px-3 py-2 font-mono text-xs text-gray-200">
          {`lumiverb config set --api-url ${server} --api-key -`}
        </pre>
        <p>Then leave it reading:</p>
        <pre className="overflow-x-auto rounded bg-gray-950 px-3 py-2 font-mono text-xs text-gray-200">lumiverb device run</pre>
        <p className="text-xs text-gray-500">
          It reads the libraries whose settings choose it. If the device can&apos;t reach {server}, Settings › Network
          lets other devices use Lumiverb.
        </p>
      </div>
      <button type="button" onClick={onDone} className="text-sm text-gray-400 hover:text-gray-200">Done</button>
    </div>
  );
}
