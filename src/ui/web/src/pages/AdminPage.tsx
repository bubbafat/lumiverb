import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, useEffect } from "react";
import { Link } from "react-router-dom";
import {
  ApiError,
  getAccountVisits,
  getCurrentUser,
  getSiteVisits,
  getSetupProgress,
  listLibraries,
  setSetupProgress,
  type HealthRow,
  type HealthState,
} from "../api/client";
import type { LibraryListItem } from "../api/types";
import { HealthDot } from "../components/HealthDot";
import { timeAgo } from "../lib/format";
import { useSystemHealth } from "../lib/useSystemHealth";
import { SETUP_QUERY_KEY } from "./WelcomePage";
import { readByDevice, readerLine } from "../lib/readers";

const POLL_INTERVAL = 10_000;

function SectionHeader({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <div className="mb-4">
      <h2 className="text-lg font-semibold text-gray-100">{title}</h2>
      {subtitle && <p className="text-sm text-gray-500">{subtitle}</p>}
    </div>
  );
}

/** Where a row's link goes, in words. */
function linkLabel(link: string): string {
  if (link.startsWith("/settings/processing")) return "Processing settings";
  if (link.startsWith("/settings/ai")) return "AI settings";
  if (link.startsWith("/settings/backups")) return "Backups";
  if (link.startsWith("/settings/storage")) return "Storage";
  if (link.startsWith("/libraries/") && link.endsWith("/settings")) return "Library settings";
  if (link.startsWith("/libraries")) return "Libraries";
  return "Open";
}

function HealthRowItem({ row }: { row: HealthRow }) {
  return (
    <li className="flex items-start gap-3 rounded-lg border border-gray-700/50 bg-gray-900/50 px-4 py-3">
      <HealthDot state={row.state} className="mt-1.5" />
      <div className="min-w-0 flex-1">
        <h3 className="font-medium text-gray-100">{row.title}</h3>
        <p className="mt-0.5 text-sm text-gray-400 wrap-break-word">{row.reason}</p>
        {row.link && (
          <Link to={row.link} className="mt-1 inline-block text-sm text-indigo-400 hover:text-indigo-300 sm:hidden">
            {linkLabel(row.link)} →
          </Link>
        )}
      </div>
      {row.link && (
        <Link to={row.link} className="hidden shrink-0 pt-0.5 text-sm text-indigo-400 hover:text-indigo-300 sm:block">
          {linkLabel(row.link)} →
        </Link>
      )}
    </li>
  );
}

const REACH: Record<string, { state: HealthState | null; label: string }> = {
  true: { state: "green", label: "Reachable" },
  false: { state: "red", label: "Can't be reached" },
  null: { state: null, label: "Not checked lately" },
};

type Reach = { reachable: boolean | null; seen_at: string | null };

function LibraryRow({ lib, reach: seen }: { lib: LibraryListItem; reach: Reach }) {
  const lastScan = lib.last_scan_at ? timeAgo(lib.last_scan_at) : "Never";
  const known = REACH[String(seen.reachable)];
  // A device's: its check-ins say how it is, never this server's reach (ADR-018 decision 1).
  const device = readByDevice(lib) ? readerLine(lib) : null;
  // Not known now: when it was last seen.
  const reach: { state: HealthState | null; label: string } = device
    ? { state: device.attention ? "yellow" : "green", label: device.text }
    : known.state ? known : {
      ...known, label: `${known.label} · ${seen.seen_at ? `last seen ${timeAgo(seen.seen_at)}` : "never seen"}`,
    };
  return (
    <li className="flex items-center gap-3 rounded-lg border border-gray-700/50 bg-gray-900/50 px-4 py-3">
      {reach.state ? (
        <HealthDot state={reach.state} label={reach.label} />
      ) : (
        <span role="img" aria-label={reach.label} title={reach.label}
              className="inline-block h-2 w-2 shrink-0 rounded-full bg-gray-500" />
      )}
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="font-medium text-gray-100">{lib.name}</span>
        </div>
        <p className="mt-0.5 font-mono text-xs text-gray-500 truncate">{lib.root_path}</p>
        {device && (
          <p className={`mt-0.5 text-xs ${device.attention ? "text-amber-300" : "text-gray-400"}`}>{device.text}</p>
        )}
      </div>
      <div className="text-right text-sm text-gray-400 shrink-0">
        <div>Last ingest</div>
        <div className="text-gray-300">{lastScan}</div>
      </div>
    </li>
  );
}

export default function AdminPage() {
  const health = useSystemHealth(POLL_INTERVAL);
  const { data: user } = useQuery({ queryKey: ["settings", "me"], queryFn: getCurrentUser });
  const { data: libraries, isLoading: libsLoading } = useQuery({
    queryKey: ["admin", "libraries"],
    queryFn: () => listLibraries(false),
    refetchInterval: POLL_INTERVAL,
  });

  const activeLibraries = libraries?.filter((l) => l.status !== "trashed") ?? [];
  const reach = new Map<string, Reach>((health.data?.libraries ?? []).map((l) => [l.library_id, l]));

  // The API not answering is the Website row's red; the rest is what it said last.
  let rows = health.data?.rows ?? [];
  if (health.isError) {
    const why = health.error instanceof ApiError ? health.error.message : "it can't be reached";
    const down: HealthRow = {
      key: "website", title: "Website", state: "red", reason: `The API isn't answering: ${why}`,
      link: null, checked_at: null,
    };
    rows = [down, ...rows.filter((r) => r.key !== "website")];
  }

  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  // Someone who isn't an admin, at /admin by its address: the nav doesn't link it for them.
  if (health.error instanceof ApiError && health.error.status === 403) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-6">
        <p className="text-sm text-red-400">Admin access required.</p>
      </div>
    );
  }

  function lastUpdated(ts: number) {
    if (!ts) return null;
    const s = Math.floor((Date.now() - ts) / 1000);
    return <span className="text-xs text-gray-600">Updated {s}s ago</span>;
  }

  return (
    <div className="mx-auto max-w-3xl px-4 py-6 space-y-10 sm:px-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-gray-100">Admin</h1>
          <p className="mt-1 text-sm text-gray-500">
            How the system is doing. Refreshes every 10 seconds.
          </p>
          {health.data?.version && (
            <p className="mt-1 text-xs text-gray-500">
              Lumiverb {health.data.version.replace(/^v(?=\d)/, "")}
            </p>
          )}
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <Link to="/admin/users" className="text-sm text-indigo-400 hover:text-indigo-300">
            Manage users →
          </Link>
          {user?.server_admin && (
            <Link to="/admin/accounts" className="text-sm text-indigo-400 hover:text-indigo-300">
              Accounts →
            </Link>
          )}
          {user?.server_admin && (
            <Link to="/admin/logs" className="text-sm text-indigo-400 hover:text-indigo-300">
              Logs →
            </Link>
          )}
        </div>
      </div>

      <SetupCard />

      <section>
        <div className="flex items-center justify-between mb-4">
          <SectionHeader title="System health" />
          {lastUpdated(health.dataUpdatedAt)}
        </div>
        {rows.length === 0 ? (
          <div className="space-y-3">
            {[1, 2, 3, 4, 5, 6].map((i) => (
              <div key={i} className="h-16 rounded-lg border border-gray-700/50 bg-gray-900/50 animate-pulse" />
            ))}
          </div>
        ) : (
          <ul aria-label="System health" className="space-y-3">
            {rows.map((row) => (
              <HealthRowItem key={row.key} row={row} />
            ))}
          </ul>
        )}
      </section>

      <section>
        <SectionHeader title="Libraries"
          subtitle="Whether each library's files can be reached: this server's looks, or its device's check-ins" />
        {libsLoading ? (
          <div className="space-y-3">
            {[1, 2, 3].map((i) => (
              <div key={i} className="h-16 rounded-lg border border-gray-700/50 bg-gray-900/50 animate-pulse" />
            ))}
          </div>
        ) : activeLibraries.length === 0 ? (
          <p className="text-sm text-gray-500 italic">No libraries.</p>
        ) : (
          <ul aria-label="Libraries" className="space-y-3">
            {activeLibraries.map((lib) => (
              <LibraryRow key={lib.library_id} lib={lib} reach={reach.get(lib.library_id) ?? { reachable: null, seen_at: null }} />
            ))}
          </ul>
        )}
      </section>

      <VisitsSection />

      {user?.server_admin && <SiteVisitsSection />}
    </div>
  );
}

const VISIT_DAYS = 30;

/** Every place's visits in the last 30 days, and the demos started (ADR-025): each a row, its own page linked. */
function VisitsSection() {
  const { data, error } = useQuery({ queryKey: ["account-visits", VISIT_DAYS], queryFn: () => getAccountVisits(VISIT_DAYS) });
  return (
    <section>
      <SectionHeader title="Visits"
        subtitle={`What public pages' visitors did in the last ${VISIT_DAYS} days, counted on this server; nobody's address is kept`} />
      {error ? (
        <p role="alert" className="text-sm text-red-300">
          {error instanceof ApiError ? error.message : "Couldn't reach the server. Try again."}
        </p>
      ) : !data ? (
        <div className="h-16 rounded-lg border border-gray-700/50 bg-gray-900/50 animate-pulse" />
      ) : data.places.length === 0 ? (
        <p className="text-sm text-gray-500 italic">No visits in the last {VISIT_DAYS} days.</p>
      ) : (
        <ul aria-label="Visits" className="space-y-3">
          {data.places.map((p) => {
            const said = [
              p.view.times ? `${p.view.visitors.toLocaleString()} ${p.view.visitors === 1 ? "visitor" : "visitors"}, ${p.view.times.toLocaleString()} ${p.view.times === 1 ? "view" : "views"}` : "",
              p.open.times ? `${p.open.times.toLocaleString()} opened` : "",
              p.play.times ? `${p.play.times.toLocaleString()} played` : "",
              p.export.times ? `${p.export.times.toLocaleString()} ${p.export.times === 1 ? "export" : "exports"}` : "",
              p.demo.times ? `${p.demo.times.toLocaleString()} ${p.demo.times === 1 ? "demo" : "demos"} started` : "",
            ].filter(Boolean).join(" · ");
            const title = p.place === "account" ? "Demo" : p.name ?? "Deleted";
            const link = p.place === "project" && p.name ? `/projects/${encodeURIComponent(p.place_id)}`
              : p.place === "library" && p.name ? `/libraries/${encodeURIComponent(p.place_id)}/settings` : null;
            return (
              <li key={`${p.place}:${p.place_id}`}
                  className="flex items-center gap-3 rounded-lg border border-gray-700/50 bg-gray-900/50 px-4 py-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="truncate font-medium text-gray-100">{title}</span>
                    <span className="shrink-0 text-xs text-gray-500">
                      {p.place === "project" ? "Project" : p.place === "library" ? "Library" : "Try it as a viewer"}
                    </span>
                  </div>
                  <p className="mt-0.5 text-sm text-gray-400">{said}</p>
                </div>
                {link && (
                  <Link to={link} className="shrink-0 text-sm text-indigo-400 hover:text-indigo-300">Open →</Link>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}


/** "Oct 30, 2:16 AM": when a likely person was first seen, in this browser's time. */
function seenAt(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

/** "Oct 30": a day the server added up (its own date, not this browser's). */
function dayOf(day: string): string {
  return new Date(`${day}T00:00:00Z`).toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
}

/** Who other than the people running this server looked at its sites, from nginx's log a day at a time: a server
 *  admin's. Nothing on a server that keeps no log of its sites (a Mac's). */
function SiteVisitsSection() {
  const { data, error } = useQuery({ queryKey: ["site-visits", VISIT_DAYS], queryFn: () => getSiteVisits(VISIT_DAYS) });
  // Shown once it's known there's something to show: nothing flashes by on a server that keeps no log.
  if (!error && (!data || (!data.logged && data.days.length === 0))) return null;
  const people = (data?.days ?? []).flatMap((d) => d.people).sort((a, b) => b.first_at.localeCompare(a.first_at));
  const came = new Map<string, number>();
  for (const day of data?.days ?? []) {
    for (const [host, n] of Object.entries(day.sources)) came.set(host, (came.get(host) ?? 0) + n);
  }
  const sites = [...new Set((data?.days ?? []).flatMap((d) => Object.keys(d.browsers)))];
  return (
    <section>
      <SectionHeader title="Site visits"
        subtitle={`Who other than the people running this server looked at its sites in the last ${VISIT_DAYS} days, from nginx's log a day at a time; nobody's address is kept`} />
      {error ? (
        <p role="alert" className="text-sm text-red-300">
          {error instanceof ApiError ? error.message : "Couldn't reach the server. Try again."}
        </p>
      ) : !data || data.days.length === 0 ? (
        <p className="text-sm text-gray-500 italic">No day added up yet: each is, the night after it ends.</p>
      ) : (
        <div className="space-y-4">
          <p className="text-sm text-gray-300">
            {people.length.toLocaleString()} likely {people.length === 1 ? "person" : "people"}
            {came.size > 0 && (
              <span className="text-gray-400">
                {" · came from "}
                {[...came].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([h, n]) => `${h} ${n}`).join(", ")}
              </span>
            )}
          </p>
          {people.length > 0 && (
            <ul aria-label="Likely people" className="space-y-2">
              {people.map((p, i) => (
                <li key={`${p.first_at}:${i}`} className="rounded-lg border border-gray-700/50 bg-gray-900/50 px-4 py-2">
                  <div className="flex flex-wrap items-baseline gap-x-2 text-sm">
                    <span className="text-gray-100">{seenAt(p.first_at)}</span>
                    <span className="text-gray-400">{p.sites.join(" + ")}</span>
                    {p.device && <span className="text-xs text-gray-500">{p.device}</span>}
                  </div>
                  <p className="mt-0.5 text-sm text-gray-400">{p.why.join("; ")}</p>
                </li>
              ))}
            </ul>
          )}
          <div className="overflow-x-auto">
            <table aria-label="Browser-like addresses by day" className="w-full text-sm">
              <caption className="mb-1 text-left text-xs text-gray-500">
                Browser-like addresses each day: at most, as many scanners run a real browser
              </caption>
              <thead>
                <tr className="text-left text-xs text-gray-500">
                  <th scope="col" className="py-1 pr-4 font-normal">Day</th>
                  {sites.map((site) => <th key={site} scope="col" className="py-1 pr-4 font-normal">{site}</th>)}
                  <th scope="col" className="py-1 font-normal">Likely people</th>
                </tr>
              </thead>
              <tbody>
                {data.days.map((d) => (
                  <tr key={d.day} className="border-t border-gray-800 text-gray-300">
                    <th scope="row" className="py-1 pr-4 text-left font-normal text-gray-400">{dayOf(d.day)}</th>
                    {sites.map((site) => <td key={site} className="py-1 pr-4">{(d.browsers[site] ?? 0).toLocaleString()}</td>)}
                    <td className="py-1">{d.people.length.toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );
}

/** Setting up isn't finished: how far, Continue, or put it away. */
function SetupCard() {
  const queryClient = useQueryClient();
  const { data } = useQuery({ queryKey: SETUP_QUERY_KEY, queryFn: getSetupProgress, retry: false });
  const dismiss = useMutation({
    mutationFn: () => setSetupProgress({ dismissed: true }),
    onSuccess: (next) => queryClient.setQueryData(SETUP_QUERY_KEY, next),
  });
  if (!data || data.finished || data.dismissed) return null;
  return (
    <section aria-label="Setting up" className="rounded-lg border border-indigo-500/40 bg-indigo-950/30 p-5 space-y-3">
      <p className="text-sm text-gray-200">
        Setting up: {data.done.length} of {data.steps.length} done. Where previews live, who can open Lumiverb, AI and
        more.
      </p>
      <div className="flex flex-wrap items-center gap-4">
        <Link to="/welcome" className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500">
          Continue setting up
        </Link>
        <button type="button" className="text-sm text-gray-400 hover:text-gray-200" disabled={dismiss.isPending}
                onClick={() => dismiss.mutate()}>
          Don&apos;t show this again
        </button>
      </div>
    </section>
  );
}
