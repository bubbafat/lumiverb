import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { ApiError, changePassword, getCurrentUser, logout } from "../../api/client";
import type { KeyThatEnds } from "../../api/client";

const ROLE_COLORS: Record<string, string> = {
  admin: "bg-indigo-600/20 text-indigo-300 border-indigo-600/30",
  editor: "bg-emerald-600/20 text-emerald-300 border-emerald-600/30",
  viewer: "bg-gray-600/20 text-gray-300 border-gray-600/30",
};

function roleBadge(role: string) {
  const cls = ROLE_COLORS[role] ?? ROLE_COLORS.viewer;
  return (
    <span
      className={`inline-block rounded-full border px-2.5 py-0.5 text-xs font-medium ${cls}`}
    >
      {role.charAt(0).toUpperCase() + role.slice(1)}
    </span>
  );
}

/** What a failed "who am I" said, as precisely as it can be told: the HTTP status and the server's code and
 * message, or the browser's own error when no answer came. */
export function whyNot(error: unknown): string {
  if (error instanceof ApiError) {
    return [`HTTP ${error.status}`, error.code, error.message].filter(Boolean).join(" · ");
  }
  if (error instanceof Error) return `${error.name}: ${error.message}`;
  return String(error);
}

export default function AccountSection() {
  const { data: user, error } = useQuery({
    queryKey: ["settings", "me"],
    queryFn: getCurrentUser,
  });
  // The last failure, kept while it's asked again (a new try clears the query's own error).
  const [failed, setFailed] = useState<{ why: string; at: Date } | null>(null);
  useEffect(() => {
    if (user) setFailed(null);
    else if (error) setFailed({ why: whyNot(error), at: new Date() });
  }, [user, error]);

  if (!user && failed) {
    return (
      <div className="rounded-lg border border-red-800/50 bg-gray-900/50 p-6 space-y-4">
        <h2 className="text-lg font-semibold text-gray-100">Account</h2>
        <div role="alert" className="space-y-1 text-sm">
          <p className="text-red-300">Couldn&apos;t tell who&apos;s signed in. Asking again every few seconds.</p>
          <p className="wrap-break-word font-mono text-xs text-gray-400">
            {failed.why} ({failed.at.toLocaleTimeString()})
          </p>
        </div>
        <button
          type="button"
          onClick={() => void logout()}
          className="rounded-lg border border-gray-700 px-4 py-2 text-sm font-medium text-gray-200 transition-colors hover:border-gray-500"
        >
          Sign out
        </button>
      </div>
    );
  }

  if (!user) {
    return (
      <div className="h-32 rounded-lg border border-gray-700/50 bg-gray-900/50 animate-pulse" />
    );
  }

  return (
    <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-6 space-y-5">
      <h2 className="text-lg font-semibold text-gray-100">Account</h2>

      <div className="space-y-4">
        <div>
          <label className="block text-sm font-medium text-gray-400">Email</label>
          <p className="mt-1 text-sm text-gray-200">
            {user.email ?? "None: signed in with an API key"}
          </p>
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-400">Role</label>
          <div className="mt-1">{roleBadge(user.role)}</div>
        </div>

        {/* Trying it as a viewer, the person is everyone's: no password to change. */}
        {user.email && !user.demo && <ChangePassword />}
      </div>

      {/* The one way out on a phone, where there's no sidebar. */}
      <button
        type="button"
        onClick={() => void logout()}
        className="rounded-lg border border-gray-700 px-4 py-2 text-sm font-medium text-gray-200 transition-colors hover:border-gray-500"
      >
        Sign out
      </button>
    </div>
  );
}

const inputClass =
  "w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-gray-100 outline-hidden "
  + "focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500";
const MIN_PASSWORD_LENGTH = 12;

/** Your own password, with the one you have (POST /v1/me/password). A new one ends the API keys you made, so they
 * are named first and ended only once you say so; every other browser signs in again, and this one stays. */
function ChangePassword() {
  const [open, setOpen] = useState(false);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [ending, setEnding] = useState<KeyThatEnds[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [changed, setChanged] = useState<number | null>(null);

  function reset() {
    setCurrent("");
    setNext("");
    setConfirm("");
    setEnding(null);
    setError(null);
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (next.length < MIN_PASSWORD_LENGTH) {
      setError(`Use at least ${MIN_PASSWORD_LENGTH} characters.`);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const done = await changePassword(current, next, ending !== null);
      reset();
      setOpen(false);
      setChanged(done.api_keys_revoked);
    } catch (err) {
      if (err instanceof ApiError && err.code === "keys_end_with_password") {
        setEnding((err.details?.keys as KeyThatEnds[] | undefined) ?? []);
      } else if (err instanceof ApiError && err.status === 429) {
        setError("Too many tries. Wait a minute, then try again.");
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setSaving(false);
    }
  }

  if (!open) {
    return (
      <div className="space-y-2">
        {changed !== null && (
          <p role="status" className="text-sm text-emerald-300">
            Password changed. Other browsers and devices sign in again with it.
            {changed > 0 && ` The ${changed === 1 ? "API key" : `${changed} API keys`} you made ended: make new ones in Settings › API Keys.`}
          </p>
        )}
        <button
          type="button"
          onClick={() => {
            setChanged(null);
            setOpen(true);
          }}
          className="text-sm font-medium text-indigo-400 hover:text-indigo-300 transition-colors"
        >
          Change password
        </button>
      </div>
    );
  }

  const mismatch = confirm !== "" && next !== confirm;
  return (
    <form onSubmit={(e) => void submit(e)} className="max-w-sm space-y-3" aria-label="Change password">
      <label className="block space-y-1 text-sm text-gray-300">
        <span>Current password</span>
        <input type="password" autoComplete="current-password" className={inputClass} value={current}
               onChange={(e) => setCurrent(e.target.value)} />
      </label>
      <div className="space-y-1">
        <label className="block space-y-1 text-sm text-gray-300">
          <span>New password</span>
          <input type="password" autoComplete="new-password" aria-describedby="new-password-hint" className={inputClass}
                 value={next} onChange={(e) => setNext(e.target.value)} />
        </label>
        <p id="new-password-hint" className="text-xs text-gray-500">12 to 72 characters.</p>
      </div>
      <label className="block space-y-1 text-sm text-gray-300">
        <span>New password again</span>
        <input type="password" autoComplete="new-password" className={inputClass} value={confirm}
               onChange={(e) => setConfirm(e.target.value)} />
      </label>
      {mismatch && <p className="text-sm text-red-400">The new passwords don&apos;t match.</p>}
      {ending && (
        <div role="alert" className="space-y-1 rounded-lg border border-amber-600/40 bg-amber-900/20 p-3 text-sm text-amber-200">
          <p>A new password ends the API keys you made. Whatever uses them stops until it&apos;s given a new one:</p>
          <ul className="list-disc pl-5">
            {ending.map((k) => (
              <li key={k.key_id}>{k.label || "Unnamed key"}{k.device ? " (a device: it stops reading its libraries)" : ""}</li>
            ))}
          </ul>
        </div>
      )}
      {error && <p role="alert" className="text-sm text-red-400">{error}</p>}
      <div className="flex items-center gap-3">
        <button type="submit" disabled={saving || !current || !next || next !== confirm}
                className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-500 disabled:opacity-50">
          {saving ? "Saving…" : ending ? "Change it and end them" : "Change password"}
        </button>
        <button type="button" onClick={() => { reset(); setOpen(false); }}
                className="text-sm text-gray-400 hover:text-gray-200">
          Cancel
        </button>
      </div>
    </form>
  );
}
