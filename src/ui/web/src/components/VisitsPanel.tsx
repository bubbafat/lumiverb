import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ApiError, type PlaceVisits, type VisitCounts } from "../api/client";
import { useAuthenticatedImage } from "../api/useAuthenticatedImage";
import { THUMBNAIL } from "../lib/names";

/** The periods a report can cover, in days. */
export const PERIODS = [7, 30, 90] as const;

const CHART_HEIGHT = 96;
const MOST_BAR = 24; // a bar is never thicker, whatever the room
const GAP = 2; // the surface between touching bars

/** "Oct 29" in this browser's language. */
function shortDay(iso: string): string {
  return new Date(`${iso}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function plural(n: number, one: string, many: string): string {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}

/** Every day of the period, oldest first, with what was counted that day (nothing on a day with no visits). */
export function everyDay(report: PlaceVisits, today: Date = new Date()): (VisitCounts & { day: string })[] {
  const byDay = new Map(report.days.map((d) => [d.day, d]));
  const none = { visitors: 0, times: 0 };
  const out: (VisitCounts & { day: string })[] = [];
  const day = new Date(`${report.since}T12:00:00`);
  const last = new Date(today.getFullYear(), today.getMonth(), today.getDate(), 12);
  while (day <= last) {
    const iso = `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`;
    out.push(byDay.get(iso) ?? { day: iso, view: none, open: none, play: none, export: none, demo: none });
    day.setDate(day.getDate() + 1);
  }
  return out;
}

/** What a day's tooltip and row say. */
export function daySaid(d: VisitCounts): string {
  const said = [plural(d.view.visitors, "visitor", "visitors")];
  if (d.view.times) said.push(plural(d.view.times, "view", "views"));
  if (d.open.times) said.push(`${d.open.times.toLocaleString()} opened`);
  if (d.play.times) said.push(`${d.play.times.toLocaleString()} played`);
  if (d.export.times) said.push(plural(d.export.times, "export", "exports"));
  return said.join(" · ");
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 px-3 py-2">
      <div className="text-xl font-semibold tabular-nums text-gray-100">{value.toLocaleString()}</div>
      <div className="text-xs text-gray-400">{label}</div>
    </div>
  );
}

/** Visitors a day: one bar a day, hover or focus a bar for its day. */
function VisitorsChart({ days }: { days: (VisitCounts & { day: string })[] }) {
  const [shown, setShown] = useState<number | null>(null);
  const most = Math.max(1, ...days.map((d) => d.view.visitors));
  const width = 600;
  const slot = width / days.length;
  const bar = Math.max(2, Math.min(MOST_BAR, slot - GAP));
  const at = shown === null ? null : days[shown];
  return (
    <figure className="relative" aria-label="Visitors a day">
      <figcaption className="mb-1 flex justify-between text-xs text-gray-400">
        <span>Visitors a day</span>
        <span className="tabular-nums">most {most.toLocaleString()}</span>
      </figcaption>
      <svg viewBox={`0 0 ${width} ${CHART_HEIGHT}`} preserveAspectRatio="none" className="h-24 w-full"
           onPointerLeave={() => setShown(null)} role="img" aria-label="Visitors a day, as bars">
        {days.map((d, i) => {
          const h = (d.view.visitors / most) * (CHART_HEIGHT - 4);
          const x = i * slot + (slot - bar) / 2;
          const r = Math.min(4, bar / 2, h);
          const y = CHART_HEIGHT - h;
          // Rounded at the top, square at the baseline.
          const path = h <= 0 ? "" : `M${x},${CHART_HEIGHT} V${y + r} Q${x},${y} ${x + r},${y} H${x + bar - r} `
            + `Q${x + bar},${y} ${x + bar},${y + r} V${CHART_HEIGHT} Z`;
          return (
            <g key={d.day} tabIndex={0} aria-label={`${shortDay(d.day)}: ${daySaid(d)}`}
               onPointerEnter={() => setShown(i)} onFocus={() => setShown(i)} onBlur={() => setShown(null)}
               className="outline-hidden">
              <rect x={i * slot} y={0} width={slot} height={CHART_HEIGHT} fill="transparent" />
              {path && <path d={path} className={shown === i ? "fill-indigo-400" : "fill-indigo-500"} />}
            </g>
          );
        })}
        <line x1={0} x2={width} y1={CHART_HEIGHT - 0.5} y2={CHART_HEIGHT - 0.5} className="stroke-gray-700"
              strokeWidth={1} vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="mt-1 flex justify-between text-xs text-gray-500">
        <span>{shortDay(days[0].day)}</span>
        <span>{shortDay(days[days.length - 1].day)}</span>
      </div>
      {at && (
        <div role="tooltip"
             className="pointer-events-none absolute top-0 z-10 -translate-x-1/2 whitespace-nowrap rounded-md border border-gray-700 bg-gray-900 px-2 py-1 text-xs text-gray-200 shadow-lg"
             style={{ left: `${Math.min(85, Math.max(15, ((shown! + 0.5) / days.length) * 100))}%` }}>
          <span className="font-medium">{shortDay(at.day)}</span> · {daySaid(at)}
        </div>
      )}
    </figure>
  );
}

function ClipRow({ assetId, opened, played }: { assetId: string; opened: number; played: number }) {
  const { url } = useAuthenticatedImage(assetId, THUMBNAIL, {});
  return (
    <li className="flex items-center gap-3">
      {url ? <img src={url} alt="" className="h-10 w-10 shrink-0 rounded-sm object-cover" />
        : <div className="h-10 w-10 shrink-0 rounded-sm bg-gray-800" />}
      <span className="text-sm text-gray-300">
        {[opened ? `${opened.toLocaleString()} opened` : "", played ? `${played.toLocaleString()} played` : ""]
          .filter(Boolean).join(" · ")}
      </span>
    </li>
  );
}

/**
 * A place's visits (ADR-025): its visitors, views, clips opened and videos played over a period, a day at a time,
 * its clips most opened, where visitors came from. `exports`: a project's, which are counted too.
 */
export function VisitsPanel({ queryKey, load, exports = false }: {
  queryKey: readonly unknown[];
  load: (days: number) => Promise<PlaceVisits>;
  exports?: boolean;
}) {
  const [period, setPeriod] = useState<number>(30);
  const { data, error, isLoading } = useQuery({ queryKey: [...queryKey, period], queryFn: () => load(period) });
  const days = data ? everyDay(data) : [];
  const t = data?.totals;
  const nothing = t && !t.view.times && !t.open.times && !t.play.times && !t.export.times;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-gray-500">
          Counted on this server, with no cookies; nobody's address is kept. A visitor is one each day, and opening
          the same thing again within half an hour counts once.
        </p>
        <div role="group" aria-label="Period" className="flex shrink-0 rounded-lg border border-gray-700 text-xs">
          {PERIODS.map((p) => (
            <button key={p} type="button" aria-pressed={period === p} onClick={() => setPeriod(p)}
                    className={`px-2.5 py-1 first:rounded-l-lg last:rounded-r-lg ${period === p
                      ? "bg-indigo-600/40 text-indigo-100" : "text-gray-400 hover:text-gray-200"}`}>
              {p} days
            </button>
          ))}
        </div>
      </div>
      {isLoading && <div className="h-40 animate-pulse rounded-lg bg-gray-900/50" />}
      {error && (
        <p role="alert" className="text-sm text-red-300">
          {error instanceof ApiError ? error.message : "Couldn't reach the server. Try again."}
        </p>
      )}
      {data && t && (nothing ? (
        <p className="text-sm text-gray-400">No visits in the last {period} days.</p>
      ) : (
        <>
          <div className={`grid grid-cols-2 gap-2 ${exports ? "sm:grid-cols-5" : "sm:grid-cols-4"}`}>
            <Stat label="Visitors" value={t.view.visitors} />
            <Stat label="Page views" value={t.view.times} />
            <Stat label="Clips opened" value={t.open.times} />
            <Stat label="Videos played" value={t.play.times} />
            {exports && <Stat label="Exports" value={t.export.times} />}
          </div>
          <VisitorsChart days={days} />
          <details className="text-sm text-gray-300">
            <summary className="cursor-pointer text-gray-400 hover:text-gray-200">Day by day</summary>
            <table className="mt-2 w-full text-left text-xs">
              <thead className="text-gray-500">
                <tr><th className="py-1 font-normal">Day</th><th className="py-1 font-normal">What was counted</th></tr>
              </thead>
              <tbody>
                {data.days.slice().reverse().map((d) => (
                  <tr key={d.day} className="border-t border-gray-800">
                    <td className="py-1 pr-3 whitespace-nowrap">{shortDay(d.day)}</td>
                    <td className="py-1">{daySaid(d)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
          {data.clips.length > 0 && (
            <section aria-label="Clips most opened">
              <h3 className="mb-2 text-sm font-medium text-gray-200">Clips most opened and played</h3>
              <ul className="grid gap-2 sm:grid-cols-2">
                {data.clips.slice(0, 10).map((c) => (
                  <ClipRow key={c.asset_id} assetId={c.asset_id} opened={c.opened} played={c.played} />
                ))}
              </ul>
            </section>
          )}
          {data.sources.length > 0 && (
            <section aria-label="Where visitors came from">
              <h3 className="mb-1 text-sm font-medium text-gray-200">Where visitors came from</h3>
              <ul className="text-sm text-gray-300">
                {data.sources.map((s) => (
                  <li key={s.site}>{s.site} · {plural(s.visitors, "visitor", "visitors")}</li>
                ))}
              </ul>
            </section>
          )}
          {data.bots > 0 && (
            <p className="text-xs text-gray-500">
              {plural(data.bots, "request", "requests")} from search engines' crawlers and other bots, not counted as
              visitors.
            </p>
          )}
        </>
      ))}
    </div>
  );
}
