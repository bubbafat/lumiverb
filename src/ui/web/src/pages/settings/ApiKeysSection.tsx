import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { listApiKeys, createApiKey, revokeApiKey, ApiError } from "../../api/client";
import type { ApiKeyItem } from "../../api/types";

function relativeTime(iso: string | null): string {
  if (!iso) return "never";
  const ms = Date.now() - new Date(iso).getTime();
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function localTimestamp(iso: string | null): string | undefined {
  if (!iso) return undefined;
  return new Date(iso).toLocaleString();
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
  const [newPlaintext, setNewPlaintext] = useState<string | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);
  const [revokeError, setRevokeError] = useState<string | null>(null);

  const { data: keys, isLoading } = useQuery({
    queryKey: ["settings", "apiKeys"],
    queryFn: listApiKeys,
  });

  const createMutation = useMutation({
    mutationFn: (l: string) => createApiKey(l),
    onSuccess: (data) => {
      setNewPlaintext(data.plaintext);
      setLabel("");
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
    createMutation.mutate(label.trim());
  }

  async function handleCopy() {
    if (!newPlaintext) return;
    try {
      await navigator.clipboard.writeText(newPlaintext);
    } catch {
      window.prompt("Copy this key:", newPlaintext);
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

      {newPlaintext && (
        <div className="rounded-lg border border-amber-700/50 bg-amber-900/20 p-4 space-y-3">
          <p className="text-sm font-medium text-amber-300">
            Copy this key now. It won't be shown again.
          </p>
          <div className="flex items-center gap-2">
            <code className="flex-1 break-all rounded bg-gray-800 px-3 py-2 text-sm text-gray-100 font-mono">
              {newPlaintext}
            </code>
            <button
              type="button"
              onClick={() => void handleCopy()}
              className="shrink-0 rounded-lg border border-gray-600 px-3 py-2 text-sm font-medium text-gray-300 transition-colors duration-150 hover:border-gray-500 hover:bg-gray-800/50"
            >
              Copy
            </button>
          </div>
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
