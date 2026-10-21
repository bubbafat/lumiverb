import type { AssetPageItem } from "../api/types";

export interface DateGroup {
  label: string; // e.g. "Tuesday, June 4, 2024" or "Unknown date"; "" for a sort that isn't by a date
  dateIso: string | null; // YYYY-MM-DD of a taken date (its header filters by it), else null
  assets: AssetPageItem[]; // ordered assets in this group
}

/** The date a clip is grouped under for a sort by date: its taken date (else its file's), when it was added, or
 * when it last changed. None for a sort that isn't by a date. */
const DATE_OF: Record<string, (asset: AssetPageItem) => string | null | undefined> = {
  taken_at: (asset) => asset.taken_at ?? asset.file_mtime,
  created_at: (asset) => asset.created_at,
  changed_at: (asset) => asset.changed_at,
};

/** The clips under a header for each day.
 *
 * sortedBy: what the server sorted them by (a browse page's sort). They're grouped by that date, in the order
 * they came (ascending or descending, as asked), or kept as one group with no header's words for a sort that
 * isn't by a date (size, name). Without it (a project's own order), by taken date, newest day first. */
export function groupAssetsByDate(assets: AssetPageItem[], sortedBy?: string): DateGroup[] {
  if (!assets.length) return [];
  const sort = sortedBy ?? "taken_at";
  const dateOf = DATE_OF[sort];
  if (!dateOf) return [{ label: "", dateIso: null, assets }];

  type Group = DateGroup & { latest: number | null };
  const groupsByLabel = new Map<string, Group>();
  for (const asset of assets) {
    const dateStr = dateOf(asset);
    const d = dateStr ? new Date(dateStr) : null;
    const time = d !== null && !Number.isNaN(d.getTime()) ? d.getTime() : null;
    const label = time !== null
      ? d!.toLocaleDateString("en-US", { weekday: "long", year: "numeric", month: "long", day: "numeric" })
      : "Unknown date";
    let group = groupsByLabel.get(label);
    if (!group) {
      // Only a taken date's header filters by its day (date_from / date_to are taken dates).
      const dateIso = time !== null && sort === "taken_at" ? d!.toISOString().slice(0, 10) : null;
      group = { label, dateIso, assets: [], latest: time };
      groupsByLabel.set(label, group);
    }
    group.assets.push(asset);
    if (time !== null) group.latest = group.latest === null ? time : Math.max(group.latest, time);
  }
  const groups = Array.from(groupsByLabel.values());
  if (sortedBy === undefined) {
    // Not sorted by the server: newest day first, an unknown date last.
    groups.sort((a, b) => (a.latest === null ? (b.latest === null ? 0 : 1) : b.latest === null ? -1 : b.latest - a.latest));
  }
  return groups.map(({ label, dateIso, assets: inIt }) => ({ label, dateIso, assets: inIt }));
}
