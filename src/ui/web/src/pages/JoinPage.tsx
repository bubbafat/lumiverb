import { useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import type { FormEvent } from "react";
import { setApiKey } from "../api/client";

/** As for any user (the server checks it too). */
const MIN_PASSWORD_LENGTH = 12;

const inputClass =
  "w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-gray-100 placeholder-gray-500 outline-hidden focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500";

/**
 * An invite's link (src/server/invites.py): the new person chooses their
 * password, and is signed in. The link works once, for 7 days.
 */
export default function JoinPage() {
  const [params] = useSearchParams();
  // Taken once, then out of the address bar: not left in history or a Referer.
  const token = useRef(params.get("token") ?? "").current;
  if (params.has("token")) {
    window.history.replaceState(null, "", "/join");
  }
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (password.length < MIN_PASSWORD_LENGTH) {
      setError(`Use at least ${MIN_PASSWORD_LENGTH} characters for your password.`);
      return;
    }
    if (password !== again) {
      setError("The passwords don't match.");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/v1/auth/join", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, password }),
      });
      if (res.ok) {
        const data = (await res.json()) as { access_token: string };
        setApiKey(data.access_token);
        navigate("/", { replace: true });
        return;
      }
      const body = (await res.json().catch(() => null)) as { error?: { message?: string } } | null;
      setError(body?.error?.message ?? "Something went wrong. Try again.");
    } catch {
      setError("Couldn't reach Lumiverb. Try again.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-950 px-4">
      <div className="w-full max-w-sm rounded-xl border border-gray-800 bg-gray-900 p-8 shadow-xl">
        <div className="mb-6 flex flex-col items-center gap-2">
          <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-indigo-600/20 text-indigo-300">
            <span className="text-2xl font-semibold">Lv</span>
          </div>
          <h1 className="text-xl font-semibold text-gray-100">Welcome to Lumiverb</h1>
        </div>

        {!token ? (
          <p className="text-center text-sm text-gray-300">
            This link is missing its last part. Copy the whole link, or ask whoever invited you for a new one.
          </p>
        ) : (
          <form onSubmit={handleSubmit} className="space-y-4">
            <p className="text-sm text-gray-400">Choose a password to finish joining.</p>
            <div>
              <label htmlFor="password" className="mb-1.5 block text-sm font-medium text-gray-300">
                Password
              </label>
              <input
                id="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={`At least ${MIN_PASSWORD_LENGTH} characters`}
                autoComplete="new-password"
                className={inputClass}
              />
            </div>
            <div>
              <label htmlFor="again" className="mb-1.5 block text-sm font-medium text-gray-300">
                Password again
              </label>
              <input
                id="again"
                type="password"
                value={again}
                onChange={(e) => setAgain(e.target.value)}
                autoComplete="new-password"
                className={inputClass}
              />
            </div>

            {error && <p className="text-sm text-red-400">{error}</p>}

            <button
              type="submit"
              disabled={loading || !password || !again}
              className="w-full rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {loading ? "One moment…" : "Join"}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
