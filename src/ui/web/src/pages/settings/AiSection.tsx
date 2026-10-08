import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  connectVision,
  getCurrentUser,
  getVisionSettings,
  saveVisionSettings,
  type VisionSettings,
} from "../../api/client";

const inputClass =
  "w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 disabled:opacity-50";
const buttonClass =
  "rounded-md px-4 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50";

/** "3 minutes ago", for when the worker last checked. */
export function ago(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "";
  const seconds = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  return new Date(iso).toLocaleString();
}

/** Whether Settings should flag the AI page: the worker couldn't use the model. */
export function visionHasProblem(settings: VisionSettings | undefined): boolean {
  return !!settings?.status && !settings.status.ok;
}

function message(error: unknown): string {
  return error instanceof ApiError ? error.message : "Couldn't reach the server. Try again.";
}

/** What the worker found last time it checked these settings. */
function Status({ settings }: { settings: VisionSettings }) {
  const { status } = settings;
  if (!settings.api_url || !settings.model) {
    return (
      <div role="status" className="rounded-md border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm text-amber-200">
        Vision AI is off. Descriptions, OCR and scene descriptions wait until a model is chosen.
      </div>
    );
  }
  if (!status) {
    return <p role="status" className="text-sm text-gray-400">Saved. The worker hasn't checked it yet.</p>;
  }
  if (status.ok) {
    return (
      <p role="status" className="text-sm text-emerald-300">
        Working: {settings.model} answered {ago(status.checked_at)}.
      </p>
    );
  }
  return (
    <div role="alert" className="rounded-md border border-red-500/60 bg-red-500/15 px-4 py-3 text-sm text-red-100">
      <p className="font-semibold text-red-200">Vision AI is paused</p>
      <p className="mt-1">{status.error}</p>
      <p className="mt-1 text-red-200/80">
        Checked {ago(status.checked_at)}. Descriptions, OCR and scene descriptions wait until this is fixed; no
        clip is marked as failed for it.
      </p>
    </div>
  );
}

/** Account-wide: the vision AI endpoint, its key and the model. Admins change it. */
export default function AiSection() {
  const { data: user } = useQuery({ queryKey: ["settings", "me"], queryFn: getCurrentUser });
  const { data: settings, isLoading } = useQuery({ queryKey: ["vision-settings"], queryFn: getVisionSettings });
  if (isLoading || !settings) {
    return <div className="h-32 rounded-lg border border-gray-700/50 bg-gray-900/50 animate-pulse" />;
  }
  return (
    <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-6 space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-gray-100">AI</h2>
        <p className="mt-1 text-sm text-gray-400">
          The vision model that writes descriptions, reads text (OCR) and describes video scenes. Any
          OpenAI-compatible endpoint, such as Ollama.
        </p>
      </div>
      <Status settings={settings} />
      {user?.role === "admin" ? (
        // Rebuilt when the saved settings change, so the form starts from them.
        <AiForm key={`${settings.api_url}|${settings.model}|${settings.has_key}`} settings={settings} />
      ) : (
        <div className="space-y-1 text-sm text-gray-200">
          <p>Endpoint: {settings.api_url || "none"}</p>
          <p>Model: {settings.model || "none"}</p>
          <p className="text-gray-500">Only admins can change this.</p>
        </div>
      )}
    </div>
  );
}

function AiForm({ settings }: { settings: VisionSettings }) {
  const queryClient = useQueryClient();
  const [url, setUrl] = useState(settings.api_url);
  const [key, setKey] = useState("");
  const [forgetKey, setForgetKey] = useState(false);
  // What the endpoint offered at the last Connect; null until connected (again).
  const [models, setModels] = useState<string[] | null>(null);
  const [model, setModel] = useState(settings.model);
  const [problem, setProblem] = useState<string | null>(null);

  // A key typed now; "" to save none; undefined keeps the saved one.
  const keyToSend = key ? key : forgetKey ? "" : undefined;
  const sameUrl = url.trim().replace(/\/+$/, "") === settings.api_url;

  const connect = useMutation({
    mutationFn: () => connectVision({ api_url: url.trim(), api_key: keyToSend }),
    onMutate: () => setProblem(null),
    onSuccess: (offered) => {
      setModels(offered);
      if (!offered.includes(model)) setModel("");
    },
    onError: (e) => {
      setModels(null);
      setProblem(message(e));
    },
  });

  const save = useMutation({
    mutationFn: (body: { api_url: string; api_key?: string; model: string }) => saveVisionSettings(body),
    onMutate: () => setProblem(null),
    onSuccess: (next) => {
      queryClient.setQueryData(["vision-settings"], next);
    },
    onError: (e) => {
      if (e instanceof ApiError && e.code === "vision_model_unavailable") {
        const offered = (e.details?.models as string[] | undefined) ?? [];
        setModels(offered);
        setModel("");
      }
      setProblem(message(e));
    },
  });

  const edited = () => {
    setModels(null);
    setProblem(null);
  };
  const changed = !sameUrl || model !== settings.model || keyToSend !== undefined;
  const canSave = !!models && models.includes(model) && changed && !save.isPending;
  const modelChange = !!settings.model && !!model && model !== settings.model;
  const busy = connect.isPending || save.isPending;

  return (
    <form
      className="space-y-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (canSave) save.mutate({ api_url: url.trim(), api_key: keyToSend, model });
      }}
    >
      <label className="block space-y-1 text-sm text-gray-300">
        <span>Endpoint URL</span>
        <input
          type="url"
          aria-label="Endpoint URL"
          placeholder="http://localhost:11434/v1"
          value={url}
          className={inputClass}
          onChange={(e) => {
            setUrl(e.target.value);
            edited();
          }}
        />
      </label>
      <label className="block space-y-1 text-sm text-gray-300">
        <span>API key (optional)</span>
        <input
          type="password"
          aria-label="API key"
          autoComplete="off"
          placeholder={settings.has_key && sameUrl && !forgetKey ? "Saved — leave blank to keep it" : "None"}
          value={key}
          className={inputClass}
          onChange={(e) => {
            setKey(e.target.value);
            edited();
          }}
        />
      </label>
      {settings.has_key && sameUrl && !key && (
        <label className="flex items-center gap-2 text-sm text-gray-300">
          <input
            type="checkbox"
            checked={forgetKey}
            onChange={(e) => {
              setForgetKey(e.target.checked);
              edited();
            }}
          />
          Remove the saved key
        </label>
      )}

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          disabled={!url.trim() || busy}
          className={`${buttonClass} bg-gray-700 text-gray-100 hover:bg-gray-600`}
          onClick={() => connect.mutate()}
        >
          {connect.isPending ? "Connecting…" : "Connect"}
        </button>
        {models && <span className="text-sm text-emerald-300">Connected: {models.length} model{models.length === 1 ? "" : "s"}</span>}
      </div>

      {models && (
        <label className="block space-y-1 text-sm text-gray-300">
          <span>Model</span>
          <select
            aria-label="Model"
            value={model}
            className={inputClass}
            onChange={(e) => {
              setModel(e.target.value);
              setProblem(null);
            }}
          >
            <option value="" disabled>
              Pick a model…
            </option>
            {models.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </label>
      )}
      {!models && settings.model && <p className="text-sm text-gray-400">Model: {settings.model}. Connect to change it.</p>}

      {modelChange && (
        <p className="text-sm text-amber-300">
          Changing the model marks existing descriptions, OCR and scene descriptions as made with the old one.
        </p>
      )}
      {problem && (
        <p role="alert" className="text-sm text-red-300">
          {problem}
        </p>
      )}

      <div className="flex flex-wrap items-center gap-3">
        <button type="submit" disabled={!canSave} className={`${buttonClass} bg-indigo-600 text-white hover:bg-indigo-500`}>
          {save.isPending ? "Saving…" : "Save"}
        </button>
        {settings.api_url && (
          <button
            type="button"
            disabled={busy}
            className={`${buttonClass} text-gray-300 hover:bg-gray-800`}
            onClick={() => save.mutate({ api_url: "", model: "" })}
          >
            Turn off vision AI
          </button>
        )}
      </div>
    </form>
  );
}
