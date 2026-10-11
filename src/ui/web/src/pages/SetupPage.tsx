import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { FormEvent } from "react";
import { setApiKey } from "../api/client";

/** As for any user (the server checks it too: src/server/setup.py). */
const MIN_PASSWORD_LENGTH = 12;

export type SetupStatus = { needed: boolean; on_this_machine: boolean };

/** Whether this Lumiverb still needs its first admin, and whether this browser may make one. */
export async function fetchSetupStatus(): Promise<SetupStatus | null> {
  try {
    const res = await fetch("/v1/setup");
    return res.ok ? ((await res.json()) as SetupStatus) : null;
  } catch {
    return null;
  }
}

const inputClass =
  "w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-gray-100 placeholder-gray-500 outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500";

/**
 * First-run setup: the install's first admin, before anyone can log in.
 * Only on the computer Lumiverb runs on (the server checks); anywhere else
 * it says so. Once someone can log in, it's the login page.
 */
export default function SetupPage() {
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  useEffect(() => {
    let live = true;
    void fetchSetupStatus().then((found) => {
      if (!live) return;
      if (found && !found.needed) navigate("/login", { replace: true });
      else setStatus(found ?? { needed: true, on_this_machine: true });
    });
    return () => {
      live = false;
    };
  }, [navigate]);

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
      const res = await fetch("/v1/setup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: email.trim(), password }),
      });
      if (res.ok) {
        const data = (await res.json()) as { access_token: string };
        setApiKey(data.access_token);
        navigate("/welcome", { replace: true }); // the rest of setting up: where previews live, AI, …
        return;
      }
      const body = (await res.json().catch(() => null)) as { error?: { message?: string } } | null;
      setError(body?.error?.message ?? "Something went wrong. Try again.");
    } catch {
      setError("Couldn't reach Lumiverb. Is it still running?");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-950">
      <div className="w-full max-w-sm rounded-xl border border-gray-800 bg-gray-900 p-8 shadow-xl">
        <div className="mb-6 flex flex-col items-center gap-2">
          <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-indigo-600/20 text-indigo-300">
            <span className="text-2xl font-semibold">Lv</span>
          </div>
          <h1 className="text-xl font-semibold text-gray-100">Welcome to Lumiverb</h1>
        </div>

        {status === null ? null : !status.on_this_machine ? (
          <p className="text-center text-sm text-gray-300">
            To finish setting up, open Lumiverb on the computer it's installed on.
          </p>
        ) : (
          <form onSubmit={handleSubmit} className="space-y-4">
            <p className="text-sm text-gray-400">
              Create your account. You'll look after this Lumiverb, and can add people later.
            </p>
            <div>
              <label htmlFor="email" className="mb-1.5 block text-sm font-medium text-gray-300">
                Email
              </label>
              <input
                id="email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                autoComplete="email"
                className={inputClass}
              />
            </div>
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
              disabled={loading || !email.trim() || !password || !again}
              className="w-full rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {loading ? "Creating…" : "Create account"}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
