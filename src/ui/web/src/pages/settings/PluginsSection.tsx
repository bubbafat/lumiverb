import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, deleteKeptFacts, getCurrentUser, listKeptPlugins, listPlugins, setPlugin } from "../../api/client";
import type { KeptPlugin, Plugin, PluginAcknowledgment } from "../../api/types";
import { InstalledDetails } from "./InstalledPlugin";

export const PLUGINS_QUERY_KEY = ["plugins"];

function said(error: unknown): string {
  return error instanceof ApiError ? error.message : "Couldn't reach the server. Try again.";
}

function day(at: string): string {
  return new Date(at).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

/** Optional parts of Lumiverb an account turns on: the plug-ins installed on this server, Lumiverb's own and the
 * server admin's (ADR-023), each shown from its manifest with its settings. Turning on one that asks an acknowledgment (a license,
 * say) shows it in full: OK accepts it and turns the plug-in on, Cancel turns the switch back off. Account admins
 * turn them on; everyone else sees which are on. */
export default function PluginsSection() {
  const queryClient = useQueryClient();
  const { data: user } = useQuery({ queryKey: ["settings", "me"], queryFn: getCurrentUser });
  const admin = user?.role === "admin";
  const { data: plugins, isLoading, error } = useQuery({
    queryKey: PLUGINS_QUERY_KEY,
    queryFn: listPlugins,
    // While one starts, gets ready or restarts: how far, every two seconds.
    refetchInterval: (query) => {
      const plugins = query.state.data ?? [];
      return plugins.some((p) => ["starting", "preparing", "restarting"].includes(p.running?.state ?? "")) ? 2000 : false;
    },
  });
  // The plug-in being turned on and what it asks, while that's shown.
  const [asking, setAsking] = useState<{ plugin: Plugin; acknowledgment: PluginAcknowledgment } | null>(null);
  const change = useMutation({
    mutationFn: ({ plugin, on, acknowledged }: { plugin: Plugin; on: boolean; acknowledged?: string }) =>
      setPlugin(plugin.plugin_id, on, acknowledged),
    onSuccess: () => {
      setAsking(null);
      return queryClient.invalidateQueries({ queryKey: PLUGINS_QUERY_KEY });
    },
    onError: (e, { plugin }) => {
      // Its text changed since the page read it: the newer one, to read and accept.
      if (e instanceof ApiError && e.code === "acknowledgment_required" && e.details) {
        setAsking({ plugin, acknowledgment: e.details as unknown as PluginAcknowledgment });
      }
    },
  });
  const failed = change.error instanceof ApiError && change.error.code === "acknowledgment_required"
    ? null : change.error;

  function toggle(plugin: Plugin, on: boolean) {
    change.reset();
    if (on && plugin.acknowledgment) setAsking({ plugin, acknowledgment: plugin.acknowledgment });
    else change.mutate({ plugin, on });
  }

  return (
    <div className="space-y-4 rounded-lg border border-gray-700/50 bg-gray-900/50 p-6">
      <div>
        <h2 className="text-lg font-semibold text-gray-100">Plug-ins</h2>
        <p className="mt-1 text-sm text-gray-400">
          Optional parts of Lumiverb, installed on this server: each is on or off as installed (Lumiverb&apos;s own,
          off), until turned on or off here.
          {!admin && user && " An admin turns them on."}
        </p>
      </div>
      {error && <p role="alert" className="text-sm text-red-300">{said(error)}</p>}
      {failed && <p role="alert" className="text-sm text-red-300">{said(failed)}</p>}
      {isLoading ? (
        <div className="h-16 rounded-lg bg-gray-900/50 animate-pulse" />
      ) : (
        <ul aria-label="Plug-ins" className="divide-y divide-gray-800">
          {(plugins ?? []).map((p) => (
            <PluginRow key={p.plugin_id} plugin={p} admin={admin} busy={change.isPending}
                       turningOn={asking?.plugin.plugin_id === p.plugin_id} onToggle={(on) => toggle(p, on)}
                       onSaved={() => queryClient.invalidateQueries({ queryKey: PLUGINS_QUERY_KEY })} />
          ))}
        </ul>
      )}
      <KeptPlugins admin={admin} />
      {asking && (
        <AcknowledgmentDialog
          acknowledgment={asking.acknowledgment}
          busy={change.isPending}
          onOk={() => change.mutate({ plugin: asking.plugin, on: true, acknowledged: asking.acknowledgment.version })}
          onCancel={() => {
            setAsking(null);
            change.reset();
          }}
        />
      )}
    </div>
  );
}

/** Plug-ins the server admin uninstalled with what they made kept: their facts are shown and searched as they
 * were, made no more. An account admin can delete one's facts from this account, after seeing how many. */
function KeptPlugins({ admin }: { admin: boolean }) {
  const queryClient = useQueryClient();
  const { data: kept } = useQuery({ queryKey: [...PLUGINS_QUERY_KEY, "kept"], queryFn: listKeptPlugins });
  const [confirming, setConfirming] = useState<KeptPlugin | null>(null);
  const remove = useMutation({
    mutationFn: (p: KeptPlugin) => deleteKeptFacts(p.plugin_id, p.clips),
    onSuccess: () => {
      setConfirming(null);
      return queryClient.invalidateQueries({ queryKey: PLUGINS_QUERY_KEY });
    },
  });
  if (!kept?.length) return null;
  return (
    <div className="space-y-2">
      <h3 className="text-sm font-medium text-gray-300">Uninstalled, facts kept</h3>
      <ul aria-label="Uninstalled, facts kept" className="divide-y divide-gray-800">
        {kept.map((p) => (
          <li key={p.plugin_id} aria-label={p.title} className="space-y-2 py-3">
            <p className="text-sm text-gray-400">
              <span className="text-gray-200">{p.title}</span> {p.version}: uninstalled {day(p.removed_at)}. Its
              facts for {p.clips.toLocaleString()} clip{p.clips === 1 ? "" : "s"} are kept here, shown and searched
              as they were.
            </p>
            {admin && (confirming?.plugin_id === p.plugin_id ? (
              <div role="alertdialog" aria-label="Delete its facts?" className="flex flex-wrap items-center gap-2 text-sm">
                <span className="text-amber-100/90">
                  Delete its facts for {p.clips.toLocaleString()} clip{p.clips === 1 ? "" : "s"} from this account, for good?
                </span>
                <button type="button" disabled={remove.isPending} onClick={() => remove.mutate(p)}
                        className="rounded-md bg-red-600 px-3 py-1 text-sm font-medium text-white hover:bg-red-500 disabled:opacity-50">
                  Delete
                </button>
                <button type="button" onClick={() => setConfirming(null)} className="text-sm text-gray-400">Cancel</button>
              </div>
            ) : (
              <button type="button" onClick={() => { remove.reset(); setConfirming(p); }}
                      className="text-sm text-red-300 hover:text-red-200">
                Delete its facts here
              </button>
            ))}
            {remove.error && confirming?.plugin_id === p.plugin_id && (
              <p role="alert" className="text-sm text-red-300">{said(remove.error)}</p>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

function PluginRow({ plugin, admin, busy, turningOn, onToggle, onSaved }: {
  plugin: Plugin;
  admin: boolean;
  busy: boolean;
  // Its acknowledgment is shown: the switch is on until it's accepted or cancelled.
  turningOn: boolean;
  onToggle: (on: boolean) => void;
  // Its settings were saved.
  onSaved: () => void;
}) {
  const on = plugin.state === "on" || turningOn;
  const accepted = plugin.acknowledged;
  return (
    <li className="space-y-1 py-3 first:pt-0 last:pb-0">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h3 className="text-sm font-medium text-gray-100">{plugin.title}</h3>
          <p className="text-sm text-gray-400">{plugin.description}</p>
        </div>
        {admin ? (
          <label className="flex shrink-0 items-center gap-2 text-sm text-gray-300">
            <input type="checkbox" checked={on} disabled={busy || turningOn}
                   aria-label={`${plugin.title}: on`} onChange={(e) => onToggle(e.target.checked)} />
            {on ? "On" : "Off"}
          </label>
        ) : (
          <span className="shrink-0 text-sm text-gray-300">{plugin.state === "on" ? "On" : "Off"}</span>
        )}
      </div>
      {plugin.state === "waiting" && (
        <p className="text-sm text-amber-300">
          Off: {plugin.acknowledgment?.title ?? "what it asks"} changed since it was accepted.
          {admin && " Turn it on to read and accept the new text."}
        </p>
      )}
      <InstalledDetails plugin={plugin} onSaved={onSaved} />
      {plugin.state === "on" && accepted && (
        <p className="text-xs text-gray-500">
          {plugin.acknowledgment?.title ?? "Its terms"} accepted {day(accepted.at)}
          {accepted.by && ` by ${accepted.by}`}.
        </p>
      )}
    </li>
  );
}

/** What turning a plug-in on asks, in full: OK accepts it, Cancel leaves the plug-in off. */
function AcknowledgmentDialog({ acknowledgment, busy, onOk, onCancel }: {
  acknowledgment: PluginAcknowledgment;
  busy: boolean;
  onOk: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onCancel}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label={acknowledgment.title}
        className="flex max-h-[85vh] w-[calc(100vw-2rem)] max-w-lg flex-col rounded-xl bg-gray-900 p-4 shadow-2xl sm:p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="mb-3 text-lg font-semibold text-gray-100">{acknowledgment.title}</h2>
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto text-sm text-gray-300">
          {acknowledgment.text.split("\n\n").map((paragraph, i) => <p key={i}>{paragraph}</p>)}
          {acknowledgment.links.length > 0 && (
            <ul className="space-y-1">
              {acknowledgment.links.map((link) => (
                <li key={link.url}>
                  <a href={link.url} target="_blank" rel="noopener noreferrer" className="text-indigo-300 hover:underline">
                    {link.label}
                  </a>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="mt-4 flex justify-end gap-2">
          <button type="button" onClick={onCancel}
                  className="rounded-lg border border-gray-600 px-4 py-2 text-sm font-medium text-gray-300 hover:bg-gray-800">
            Cancel
          </button>
          <button type="button" onClick={onOk} disabled={busy}
                  className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50">
            OK
          </button>
        </div>
      </div>
    </div>
  );
}
