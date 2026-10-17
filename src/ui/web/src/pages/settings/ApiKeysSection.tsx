import { useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  listApiKeys,
  createApiKey,
  revokeApiKey,
  getCurrentUser,
  getTenantSettings,
  updateTenantSettings,
  ApiError,
  type TenantSettings,
} from "../../api/client";
import type { ApiKeyItem } from "../../api/types";
import { copyText, selectText } from "../../lib/copyText";
import { timeAgo } from "../../lib/format";

function relativeTime(iso: string | null): string {
  return iso ? timeAgo(iso) : "never";
}

function localTimestamp(iso: string | null): string | undefined {
  if (!iso) return undefined;
  return new Date(iso).toLocaleString();
}

/** When a key ends unless it's used first; an admin key past it is the account's last, which is kept. */
function Ends({ apiKey }: { apiKey: ApiKeyItem }) {
  if (!apiKey.ends_at) return null;
  const at = new Date(apiKey.ends_at);
  const text = at.getTime() > Date.now()
    ? `Ends ${at.toLocaleDateString()} unless used`
    : apiKey.role === "admin"
      ? "Kept: the last admin key"
      : "Ends within minutes";
  return <span className="mt-0.5 block text-xs text-gray-500" title={at.toLocaleString()}>{text}</span>;
}

function KeyRow({
  apiKey,
  onRevoke,
}: {
  apiKey: ApiKeyItem;
  onRevoke: (keyId: string) => void;
}) {
  const [confirming, setConfirming] = useState(false);

  return (
    <tr className="border-b border-gray-800">
      <td className="px-4 py-3 text-sm text-gray-200">
        {apiKey.label || <span className="italic text-gray-500">no label</span>}
      </td>
      <td className="px-4 py-3 text-sm text-gray-400">
        {apiKey.role}
      </td>
      <td className="px-4 py-3 text-sm text-gray-400" title={localTimestamp(apiKey.created_at)}>
        {relativeTime(apiKey.created_at)}
      </td>
      <td className="px-4 py-3 text-sm text-gray-400" title={localTimestamp(apiKey.last_used_at)}>
        {relativeTime(apiKey.last_used_at)}
        <Ends apiKey={apiKey} />
      </td>
      <td className="px-4 py-3 text-right">
        {confirming ? (
          <div className="flex items-center justify-end gap-2">
            <button
              onClick={() => {
                onRevoke(apiKey.key_id);
                setConfirming(false);
              }}
              className="text-sm font-medium text-red-400 hover:text-red-300"
            >
              Confirm
            </button>
            <button
              onClick={() => setConfirming(false)}
              className="text-sm text-gray-400 hover:text-gray-300"
            >
              Cancel
            </button>
          </div>
        ) : (
          <button
            onClick={() => setConfirming(true)}
            className="text-sm text-red-400 hover:text-red-300"
          >
            Revoke
          </button>
        )}
      </td>
    </tr>
  );
}

export default function ApiKeysSection() {
  const qc = useQueryClient();
  const [label, setLabel] = useState("");
  const [role, setRole] = useState("");
  const [newPlaintext, setNewPlaintext] = useState<string | null>(null);
  // Whether the key was copied: "copied", "select" (it couldn't be: selected to copy by hand), or not yet.
  const [copied, setCopied] = useState<"copied" | "select" | null>(null);
  const keyText = useRef<HTMLElement>(null);
  const [createError, setCreateError] = useState<string | null>(null);
  const [revokeError, setRevokeError] = useState<string | null>(null);

  const { data: me } = useQuery({
    queryKey: ["settings", "me"],
    queryFn: getCurrentUser,
  });
  const callerRole = me?.role ?? "viewer";

  const { data: keys, isLoading } = useQuery({
    queryKey: ["settings", "apiKeys"],
    queryFn: listApiKeys,
  });

  const createMutation = useMutation({
    mutationFn: ({ l, r }: { l: string; r: string }) => createApiKey(l, r || undefined),
    onSuccess: (data) => {
      setNewPlaintext(data.plaintext);
      setLabel("");
      setRole("");
      setCreateError(null);
      qc.invalidateQueries({ queryKey: ["settings", "apiKeys"] });
    },
    onError: (err) => {
      if (err instanceof ApiError) {
        setCreateError(err.message);
      } else {
        setCreateError("Failed to create key.");
      }
    },
  });

  const revokeMutation = useMutation({
    mutationFn: (keyId: string) => revokeApiKey(keyId),
    onSuccess: () => {
      setRevokeError(null);
      qc.invalidateQueries({ queryKey: ["settings", "apiKeys"] });
    },
    onError: (err) => {
      if (err instanceof ApiError) {
        setRevokeError(err.message);
      } else {
        setRevokeError("Failed to revoke key.");
      }
    },
  });

  function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setCreateError(null);
    setNewPlaintext(null);
    setCopied(null);
    createMutation.mutate({ l: label.trim(), r: role });
  }

  async function handleCopy() {
    if (!newPlaintext) return;
    if (await copyText(newPlaintext)) {
      setCopied("copied");
    } else {
      selectText(keyText.current);
      setCopied("select");
    }
  }

  return (
    <div className="space-y-6">
      <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-6 space-y-5">
        <h2 className="text-lg font-semibold text-gray-100">API Keys</h2>

        {revokeError && (
          <p className="text-sm text-red-400">{revokeError}</p>
        )}

        {isLoading ? (
          <div className="h-24 rounded-lg border border-gray-700/50 bg-gray-900/50 animate-pulse" />
        ) : keys && keys.length > 0 ? (
          <div className="rounded-lg border border-gray-700/50 overflow-hidden">
            <table className="w-full">
              <thead>
                <tr className="border-b border-gray-700/50">
                  <th className="px-4 py-2 text-left text-xs font-medium text-gray-500">Label</th>
                  <th className="px-4 py-2 text-left text-xs font-medium text-gray-500">Role</th>
                  <th className="px-4 py-2 text-left text-xs font-medium text-gray-500">Created</th>
                  <th className="px-4 py-2 text-left text-xs font-medium text-gray-500">Last used</th>
                  <th className="px-4 py-2" />
                </tr>
              </thead>
              <tbody>
                {keys.map((k) => (
                  <KeyRow
                    key={k.key_id}
                    apiKey={k}
                    onRevoke={(keyId) => revokeMutation.mutate(keyId)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-gray-500 italic">No API keys yet.</p>
        )}
      </div>

      <UnusedKeys isAdmin={callerRole === "admin"} />

      {newPlaintext && (
        <div className="rounded-lg border border-amber-700/50 bg-amber-900/20 p-4 space-y-3">
          <p className="text-sm font-medium text-amber-300">
            Copy this key now. It won't be shown again.
          </p>
          <div className="flex items-center gap-2">
            <code ref={keyText} className="flex-1 break-all rounded bg-gray-800 px-3 py-2 text-sm text-gray-100 font-mono select-all">
              {newPlaintext}
            </code>
            <button
              type="button"
              onClick={() => void handleCopy()}
              className="shrink-0 rounded-lg border border-gray-600 px-3 py-2 text-sm font-medium text-gray-300 transition-colors duration-150 hover:border-gray-500 hover:bg-gray-800/50"
            >
              {copied === "copied" ? "Copied" : "Copy"}
            </button>
          </div>
          {copied === "select" && (
            <p className="text-xs text-gray-400">This browser won't copy from here: the key is selected, so press ⌘C (Ctrl+C).</p>
          )}
        </div>
      )}

      <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-6">
        <h3 className="mb-4 text-sm font-semibold text-gray-100">Create key</h3>
        <form onSubmit={handleCreate} className="flex items-end gap-3">
          <div className="flex-1">
            <label className="mb-1 block text-sm font-medium text-gray-300">Label</label>
            <input
              type="text"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="e.g. CI pipeline"
              className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-gray-100 placeholder-gray-500 outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
            />
          </div>
          <div className="w-36">
            <label className="mb-1 block text-sm font-medium text-gray-300">Role</label>
            <select
              value={role}
              onChange={(e) => setRole(e.target.value)}
              className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-gray-100 outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
            >
              <option value="">Same as mine ({callerRole})</option>
              {callerRole === "admin" && <option value="admin">Admin</option>}
              {callerRole !== "viewer" && <option value="editor">Editor</option>}
              {callerRole !== "viewer" && <option value="viewer">Viewer</option>}
            </select>
          </div>
          <button
            type="submit"
            disabled={createMutation.isPending || !label.trim()}
            className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {createMutation.isPending ? "Creating..." : "Create key"}
          </button>
        </form>
        {createError && (
          <p className="mt-3 text-sm text-red-400">{createError}</p>
        )}
      </div>
    </div>
  );
}

const MAX_IDLE_DAYS = 3650;
const SUGGESTED_IDLE_DAYS = 90;

/** Whether keys people make stop working once nobody uses them for a while
 * (the account's key_idle_days). Lumiverb's own keys and devices' aren't
 * affected. Admins change it; fewer days asks first when keys would end at once. */
function UnusedKeys({ isAdmin }: { isAdmin: boolean }) {
  const { data: settings } = useQuery({ queryKey: ["tenant-settings"], queryFn: getTenantSettings });
  if (!settings) return null;
  // Built once the settings are here, so the choice starts from them.
  return <UnusedKeysForm settings={settings} isAdmin={isAdmin} />;
}

function UnusedKeysForm({ settings, isAdmin }: { settings: TenantSettings; isAdmin: boolean }) {
  const qc = useQueryClient();
  const savedDays = settings.key_idle_days ?? null;
  const [on, setOn] = useState(savedDays !== null);
  const [days, setDays] = useState(String(savedDays ?? SUGGESTED_IDLE_DAYS));
  const [saved, setSaved] = useState(false);
  // Fewer days would end these at once: the server asked first.
  const [ask, setAsk] = useState<string[] | null>(null);

  const save = useMutation({
    mutationFn: ({ value, confirm }: { value: number | null; confirm: boolean }) =>
      updateTenantSettings({ key_idle_days: value, ...(confirm ? { confirm_end_keys: true } : {}) }),
    onSuccess: (next) => {
      qc.setQueryData(["tenant-settings"], next);
      qc.invalidateQueries({ queryKey: ["settings", "apiKeys"] });
      setAsk(null);
      setSaved(true);
    },
    onError: (err) => {
      if (err instanceof ApiError && err.code === "keys_would_end" && err.details) {
        const keys = (err.details as { keys?: { label: string | null }[] }).keys ?? [];
        setAsk(keys.map((k) => k.label || "no label"));
      }
    },
  });

  const parsed = /^[0-9]+$/.test(days.trim()) ? Number(days.trim()) : NaN;
  const valid = !on || (parsed >= 1 && parsed <= MAX_IDLE_DAYS);
  const value = on ? parsed : null;
  const canSave = isAdmin && valid && value !== savedDays && !save.isPending;
  const changed = () => {
    setSaved(false);
    setAsk(null);
  };

  return (
    <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-6 space-y-4">
      <div>
        <h3 className="text-sm font-semibold text-gray-100">Unused keys</h3>
        <p className="mt-1 text-sm text-gray-400">
          A key someone made can stop working once nobody has used it for a while: one left in an old script or on
          a lost laptop. Lumiverb&apos;s own keys and devices&apos; aren&apos;t affected, and the last admin key is
          kept.
        </p>
      </div>
      {isAdmin ? (
        <form
          aria-label="Unused keys"
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (canSave) save.mutate({ value, confirm: false });
          }}
        >
          <label className="flex items-center gap-3 text-sm text-gray-200">
            <input
              type="checkbox"
              checked={on}
              onChange={(e) => {
                setOn(e.target.checked);
                changed();
              }}
            />
            <span>
              End keys not used for{" "}
              <input
                type="text"
                inputMode="numeric"
                aria-label="Days unused"
                value={days}
                disabled={!on}
                onChange={(e) => {
                  setDays(e.target.value);
                  changed();
                }}
                className="mx-1 w-16 rounded border border-gray-700 bg-gray-800 px-2 py-0.5 text-gray-100 disabled:opacity-50"
              />{" "}
              days
            </span>
          </label>
          {on && !valid && (
            <p role="alert" className="text-sm text-red-300">
              Give a whole number of days from 1 to {MAX_IDLE_DAYS.toLocaleString()}.
            </p>
          )}
          {ask && (
            <div role="alertdialog" aria-label="End them now?" className="rounded-lg border border-amber-800/50 bg-amber-950/30 p-3 text-sm text-amber-100/90">
              <p>
                {ask.length === 1 ? "1 key hasn't" : `${ask.length} keys haven't`} been used for longer than {parsed}{" "}
                {parsed === 1 ? "day" : "days"}, and would stop working within minutes: {ask.join(", ")}.
              </p>
              <div className="mt-3 flex gap-2">
                <button
                  type="button"
                  disabled={save.isPending}
                  onClick={() => save.mutate({ value, confirm: true })}
                  className="rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-500 disabled:opacity-50"
                >
                  Save and end them
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setAsk(null);
                    save.reset();
                  }}
                  className="rounded-md px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-800"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
          <div className="flex items-center gap-3">
            <button
              type="submit"
              disabled={!canSave}
              className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Save
            </button>
            <span role="status" className="text-sm">
              {saved && !save.isPending && <span className="text-emerald-300">Saved</span>}
              {save.isError && !ask && <span className="text-red-300">Couldn&apos;t save. Try again.</span>}
            </span>
          </div>
        </form>
      ) : (
        <div className="space-y-1">
          <p className="text-sm text-gray-200">
            {savedDays === null
              ? "Keys work until someone revokes them."
              : `A key ends once nobody has used it for ${savedDays} ${savedDays === 1 ? "day" : "days"}.`}
          </p>
          <p className="text-sm text-gray-500">Only admins can change this.</p>
        </div>
      )}
    </div>
  );
}
