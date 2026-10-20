import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ApiError, getCurrentUser, logout } from "../../api/client";

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
        {user.email && !user.demo && (
          <div>
            <Link
              to="/reset-password"
              className="text-sm font-medium text-indigo-400 hover:text-indigo-300 transition-colors"
            >
              Change password
            </Link>
          </div>
        )}
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
