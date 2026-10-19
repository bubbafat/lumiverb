import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { ApiError, setPluginSettings } from "../../api/client";
import type { Plugin, PluginRunning, PluginSetting } from "../../api/types";

const inputClass =
  "w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 disabled:opacity-50";

function firstUp(words: string): string {
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** What Settings › Plug-ins shows of a plug-in the server admin installed (ADR-023), built from its manifest: who
 * made it, what it's doing, its default, what it reads and reaches, and its settings. */
export function InstalledDetails({ plugin, onSaved }: { plugin: Plugin; onSaved: () => void }) {
  const settings = plugin.settings ?? [];
  const account = settings.filter((s) => s.scope === "account");
  const server = settings.filter((s) => s.scope === "server");
  return (
    <div className="space-y-2">
      <MetaLine plugin={plugin} />
      {plugin.running && <RunningLine running={plugin.running} />}
      <DefaultLine plugin={plugin} />
      <WhatItDoes plugin={plugin} />
      {account.length > 0 && (
        <SettingsForm plugin={plugin} settings={account} scope="account" onSaved={onSaved}
                      title="Its settings" />
      )}
      {server.length > 0 && (
        <SettingsForm plugin={plugin} settings={server} scope="server" onSaved={onSaved}
                      title="For the whole server" />
      )}
    </div>
  );
}

function MetaLine({ plugin }: { plugin: Plugin }) {
  return (
    <p className="text-xs text-gray-500">
      Version {plugin.version}
      {plugin.author && <> · by {plugin.author}</>}
      {plugin.homepage && (
        <>
          {" · "}
          <a href={plugin.homepage} target="_blank" rel="noopener noreferrer" className="text-indigo-300 hover:underline">
            About it
          </a>
        </>
      )}
    </p>
  );
}

/** What the plug-in runner last said of its process. */
export function RunningLine({ running }: { running: PluginRunning }) {
  const why = running.why ? `: ${running.why}` : "";
  switch (running.state) {
    case "ready":
      return (
        <p role="status" className="text-sm text-green-300">
          Running{running.memory_mb != null && <span className="text-gray-500"> · {running.memory_mb} MB of memory</span>}
        </p>
      );
    case "starting":
      return <p role="status" className="text-sm text-gray-400">Starting…</p>;
    case "preparing": {
      const total = running.total ?? null;
      const done = running.done ?? 0;
      return (
        <div role="status" className="space-y-1">
          <p className="text-sm text-gray-400">
            Getting ready{running.detail ? `: ${running.detail}` : "…"}
            {total ? ` (${done.toLocaleString()} of ${total.toLocaleString()})` : ""}
          </p>
          {total ? (
            <div className="h-1.5 w-full max-w-xs overflow-hidden rounded-sm bg-gray-800">
              <div className="h-full bg-indigo-500" style={{ width: `${Math.min(100, (100 * done) / total)}%` }} />
            </div>
          ) : null}
        </div>
      );
    }
    case "failing":
      return <p role="alert" className="text-sm text-red-300">Not working{why}</p>;
    case "restarting":
      return <p role="status" className="text-sm text-amber-300">Stopped{why}. Starting it again.</p>;
    case "stopped":
      return <p role="alert" className="text-sm text-red-300">Stopped{why}</p>;
    default:
      return <p role="status" className="text-sm text-gray-500">{running.why ?? "Not running"}</p>;
  }
}

function DefaultLine({ plugin }: { plugin: Plugin }) {
  if (plugin.on_by_default == null) return null;
  const byDefault = plugin.on_by_default ? "on" : "off";
  return (
    <p className="text-xs text-gray-500">
      {plugin.follows_default
        ? `${firstUp(byDefault)} by default, for every account that hasn't turned it on or off.`
        : `Turned ${plugin.state === "off" ? "off" : "on"} here; by default it's ${byDefault}.`}
    </p>
  );
}

function WhatItDoes({ plugin }: { plugin: Plugin }) {
  const lines = [
    ...(plugin.reads ?? []).map((r) => `Reads ${r}`),
    ...(plugin.answers ?? []).map(firstUp),
    ...(plugin.downloads ?? []).map((d) => {
      let host = d.url;
      try {
        host = new URL(d.url).hostname;
      } catch {
        // shown as given
      }
      return `Downloads ${d.title} (about ${d.about_mb.toLocaleString()} MB) from ${host}`;
    }),
    plugin.internet ? "Uses the internet" : "Doesn't use the internet",
  ];
  return (
    <details className="text-sm">
      <summary className="cursor-pointer select-none text-gray-400 hover:text-gray-200">What it reads and reaches</summary>
      <ul className="mt-1 list-disc space-y-0.5 pl-5 text-gray-400">
        {lines.map((line) => <li key={line}>{line}</li>)}
      </ul>
    </details>
  );
}

type Raw = string | boolean;

function rawOf(s: PluginSetting): Raw {
  if (s.type === "bool") return s.value === true;
  if (s.type === "secret") return "";
  return s.value == null ? "" : String(s.value);
}

/** A field's value as the API takes it. */
function sentOf(s: PluginSetting, raw: Raw): number | boolean | string {
  if (s.type === "bool") return raw === true;
  if (s.type === "int" || s.type === "float") return raw === "" ? (raw as string) : Number(raw);
  return raw as string;
}

/** A plug-in's settings of one scope, the form built from its schema: each field its type's control, its unit, its
 * help text, and the server's (or the plug-in's) word on what's wrong with it. */
function SettingsForm({ plugin, settings, scope, title, onSaved }: {
  plugin: Plugin;
  settings: PluginSetting[];
  scope: "account" | "server";
  title: string;
  onSaved: () => void;
}) {
  const [raw, setRaw] = useState<Record<string, Raw>>(() => Object.fromEntries(settings.map((s) => [s.key, rawOf(s)])));
  const [wrong, setWrong] = useState<Record<string, string>>({});
  const [note, setNote] = useState<string | null>(null);
  const editable = settings.some((s) => s.editable);
  const changed = settings.filter((s) => s.editable && raw[s.key] !== rawOf(s));
  const save = useMutation({
    mutationFn: (sent: Record<string, number | boolean | string | null>) =>
      setPluginSettings(plugin.plugin_id, sent, scope),
    onMutate: () => {
      setWrong({});
      setNote(null);
    },
    onSuccess: (saved) => {
      const fresh = (saved.settings ?? []).filter((s) => s.scope === scope);
      setRaw(Object.fromEntries(fresh.map((s) => [s.key, rawOf(s)])));
      setNote(saved.unchecked ? `Saved. ${saved.unchecked}.` : "Saved.");
      onSaved();
    },
    onError: (e) => {
      const errors = e instanceof ApiError ? (e.details?.errors as Record<string, string> | undefined) : undefined;
      if (errors) setWrong(errors);
      else setNote(e instanceof ApiError ? e.message : "Couldn't reach the server. Try again.");
    },
  });
  const field = (s: PluginSetting) => {
    const id = `${plugin.plugin_id}-${s.key}`;
    const error = wrong[s.key];
    const help = (
      <>
        <p className="text-xs text-gray-500">
          {s.help}
          {s.remakes && " Changing it makes what it made again."}
        </p>
        {error && <p role="alert" className="text-xs text-red-300">{error}</p>}
      </>
    );
    if (s.type === "bool") {
      return (
        <div key={s.key} className="space-y-0.5">
          <div className="flex items-center gap-2">
            <input id={id} type="checkbox" className="h-4 w-4 accent-indigo-500" disabled={!s.editable}
                   checked={raw[s.key] === true} onChange={(e) => setRaw({ ...raw, [s.key]: e.target.checked })} />
            <label htmlFor={id} className="text-sm text-gray-300">{s.label}</label>
          </div>
          {help}
        </div>
      );
    }
    const value = raw[s.key] as string;
    const set = (v: string) => setRaw({ ...raw, [s.key]: v });
    return (
      <div key={s.key} className="space-y-1">
        <label htmlFor={id} className="block text-sm text-gray-300">
          {s.label}
          {s.unit && <span className="text-gray-500"> ({s.unit})</span>}
        </label>
        {s.type === "choice" ? (
          <select id={id} className={`${inputClass} sm:max-w-64`} disabled={!s.editable} value={value}
                  onChange={(e) => set(e.target.value)}>
            {s.choices.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
          </select>
        ) : s.type === "int" || s.type === "float" ? (
          <input id={id} type="number" className={`${inputClass} sm:max-w-48`} disabled={!s.editable} value={value}
                 min={s.minimum ?? undefined} max={s.maximum ?? undefined} step={s.type === "int" ? 1 : "any"}
                 onChange={(e) => set(e.target.value)} />
        ) : s.type === "secret" ? (
          <div className="flex items-center gap-2">
            <input id={id} type="password" autoComplete="new-password" className={`${inputClass} sm:max-w-64`}
                   disabled={!s.editable} value={value} placeholder={s.set ? "Set: type to replace it" : "Not set"}
                   maxLength={s.max_length ?? undefined} onChange={(e) => set(e.target.value)} />
            {s.set && s.editable && (
              <button type="button" disabled={save.isPending} className="text-sm text-indigo-300 hover:underline"
                      onClick={() => save.mutate({ [s.key]: null })}>
                Clear
              </button>
            )}
          </div>
        ) : (
          <input id={id} type="text" className={inputClass} disabled={!s.editable} value={value}
                 maxLength={s.max_length ?? undefined} onChange={(e) => set(e.target.value)} />
        )}
        {help}
      </div>
    );
  };
  const main = settings.filter((s) => !s.advanced);
  const advanced = settings.filter((s) => s.advanced);
  return (
    // The server says what's wrong with a value (its schema, or the plug-in), beside its field: not the browser.
    <form
      noValidate
      aria-label={`${plugin.title}: ${title.toLowerCase()}`}
      className="space-y-3 rounded-md border border-gray-800 p-3"
      onSubmit={(e) => {
        e.preventDefault();
        if (changed.length) save.mutate(Object.fromEntries(changed.map((s) => [s.key, sentOf(s, raw[s.key])])));
      }}
    >
      <h4 className="text-sm font-medium text-gray-200">{title}</h4>
      {main.map(field)}
      {advanced.length > 0 && (
        <details>
          <summary className="cursor-pointer select-none text-sm text-gray-500 hover:text-gray-300">Advanced</summary>
          <div className="mt-2 space-y-3">{advanced.map(field)}</div>
        </details>
      )}
      {note && <p role="status" className="text-sm text-gray-400">{note}</p>}
      {editable && (
        <button type="submit" disabled={!changed.length || save.isPending}
                className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50">
          Save
        </button>
      )}
    </form>
  );
}
