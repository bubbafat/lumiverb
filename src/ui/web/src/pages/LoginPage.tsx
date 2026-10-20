import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import type { FormEvent } from "react";
import { setApiKey } from "../api/client";
import { signInNext } from "../lib/signInNext";
import { fetchSetupStatus } from "./SetupPage";

export default function LoginPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  // A demo server lets anyone try it as a viewer (POST /v1/auth/demo); elsewhere that's a 404.
  const [demoOn, setDemoOn] = useState(false);
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  const next = searchParams.get("next");

  // A new install has nobody to log in yet: its first admin is made on the setup page.
  useEffect(() => {
    let live = true;
    void fetchSetupStatus().then((status) => {
      if (live && status?.needed) navigate("/setup", { replace: true });
    });
    return () => {
      live = false;
    };
  }, [navigate]);

  useEffect(() => {
    let live = true;
    fetch("/v1/auth/demo")
      .then((res) => {
        if (live && res.ok) setDemoOn(true);
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);

  async function tryAsViewer() {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/v1/auth/demo", { method: "POST" });
      if (res.ok) {
        const data = (await res.json()) as { access_token: string };
        setApiKey(data.access_token);
        navigate(signInNext(next));
      } else if (res.status === 429) {
        setError("Too many tries from here just now. Wait a minute and try again.");
      } else {
        setError("Trying it as a viewer isn't open just now.");
      }
    } catch {
      setError("Could not reach server");
    } finally {
      setLoading(false);
    }
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!email.trim() || !password) return;

    setLoading(true);
    setError(null);

    try {
      const res = await fetch("/v1/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: email.trim(), password }),
      });
      if (res.ok) {
        const data = (await res.json()) as { access_token: string };
        setApiKey(data.access_token);
        navigate(signInNext(next));
      } else if (res.status === 401) {
        setError("Invalid email or password");
      } else if (res.status === 404) {
        setError("Signing in with a password isn't open here.");
      } else {
        setError("Could not reach server");
      }
    } catch {
      setError("Could not reach server");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-950">
      <div className="w-full max-w-sm rounded-xl border border-gray-800 bg-gray-900 p-8 shadow-xl">
        <div className="mb-8 flex flex-col items-center gap-2">
          <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-indigo-600/20 text-indigo-300">
            <span className="text-2xl font-semibold">Lv</span>
          </div>
          <h1 className="text-xl font-semibold text-gray-100">Lumiverb</h1>
        </div>

        {demoOn && (
          <div className="mb-6 space-y-2 border-b border-gray-800 pb-6">
            <button
              type="button"
              onClick={() => void tryAsViewer()}
              disabled={loading}
              className="w-full rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Try it as a viewer
            </button>
            <p className="text-center text-xs text-gray-400">
              No account needed: search, browse and open anything. Nothing can be changed.
            </p>
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label
              htmlFor="email"
              className="mb-1.5 block text-sm font-medium text-gray-300"
            >
              Email
            </label>
            <input
              id="email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
              autoComplete="email"
              className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-gray-100 placeholder-gray-500 outline-hidden focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
            />
          </div>

          <div>
            <label
              htmlFor="password"
              className="mb-1.5 block text-sm font-medium text-gray-300"
            >
              Password
            </label>
            <input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••"
              autoComplete="current-password"
              className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-gray-100 placeholder-gray-500 outline-hidden focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
            />
          </div>

          {error && (
            <p className="text-sm text-red-400">{error}</p>
          )}

          <button
            type="submit"
            disabled={loading || !email.trim() || !password}
            className="w-full rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {loading ? "Signing in…" : "Sign in"}
          </button>
        </form>

        {!demoOn && (
          <p className="mt-4 text-center text-sm text-gray-500">
            <Link to="/forgot-password" className="text-indigo-400 hover:text-indigo-300">
              Forgot password?
            </Link>
          </p>
        )}
      </div>
    </div>
  );
}
