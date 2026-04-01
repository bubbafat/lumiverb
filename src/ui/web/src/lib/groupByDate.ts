import type { AssetPageItem } from "../api/types";

export interface DateGroup {
  label: string; // e.g. "Tuesday, June 4, 2024" or "Unknown date"
  dateIso: string | null; // YYYY-MM-DD or null for "Unknown date"
  assets: AssetPageItem[]; // ordered assets in this group
}

export function groupAssetsByDate(assets: AssetPageItem[]): DateGroup[] {
  if (!assets.length) return [];

  type GroupAccumulator = {
    label: string;
    dateIso: string | null;
    assets: AssetPageItem[];
    latestTimestamp: number | null;
  };

  const groupsByLabel = new Map<string, GroupAccumulator>();

  for (const asset of assets) {
    const dateStr = asset.taken_at ?? asset.file_mtime;
    let label: string;
    let dateIso: string | null = null;
    let timestamp: number | null = null;

    if (!dateStr) {
      label = "Unknown date";
    } else {
      const d = new Date(dateStr);
      if (Number.isNaN(d.getTime())) {
        label = "Unknown date";
      } else {
        label = d.toLocaleDateString("en-US", {
          weekday: "long",
          year: "numeric",
          month: "long",
          day: "numeric",
        });
        dateIso = d.toISOString().slice(0, 10);
        timestamp = d.getTime();
      }
    }

    let group = groupsByLabel.get(label);
    if (!group) {
      group = { label, dateIso, assets: [], latestTimestamp: timestamp };
      groupsByLabel.set(label, group);
    }

    group.assets.push(asset);
    if (timestamp !== null) {
      group.latestTimestamp =
        group.latestTimestamp !== null
          ? Math.max(group.latestTimestamp, timestamp)
          : timestamp;
    }
  }

  const groups: GroupAccumulator[] = Array.from(groupsByLabel.values());

  groups.sort((a, b) => {
    if (a.latestTimestamp === null && b.latestTimestamp === null) return 0;
    if (a.latestTimestamp === null) return 1; // Unknown (null) goes last
    if (b.latestTimestamp === null) return -1;
    return b.latestTimestamp - a.latestTimestamp; // most recent first
  });

  return groups.map(({ label, dateIso, assets }) => ({ label, dateIso, assets }));
}

