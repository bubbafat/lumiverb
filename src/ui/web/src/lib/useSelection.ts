import { useState, useCallback, useRef } from "react";

export interface UseSelectionReturn {
  /** Currently selected asset IDs */
  selected: Set<string>;
  /** Number of selected items */
  count: number;
  /** Whether any items are selected */
  isActive: boolean;
  /** Toggle a single asset. With shiftKey=true, selects range from last toggled. */
  toggle: (assetId: string, opts?: { shiftKey?: boolean }) => void;
  /** Select all assets in a group (by their IDs). Pass dateIso to track for auto-select. */
  selectGroup: (assetIds: string[], dateIso?: string | null) => void;
  /** Clear all selections */
  clear: () => void;
  /** Check if an asset is selected */
  has: (assetId: string) => boolean;
  /** Get selected IDs as array */
  toArray: () => string[];
  /** Auto-select assets that match a previously selected date group.
   *  Call with new assets after a page load. */
  autoSelectForDates: (assets: { asset_id: string; taken_at?: string | null; file_mtime?: string | null }[]) => void;
}

/**
 * Selection hook for multi-select in asset grids.
 *
 * @param orderedAssetIds - All asset IDs in display order. Used for shift-range selection.
 */
export function useSelection(orderedAssetIds: string[]): UseSelectionReturn {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const lastToggledRef = useRef<string | null>(null);
  /** Date keys (YYYY-MM-DD) with active group selections. */
  const selectedDateKeysRef = useRef<Set<string>>(new Set());

  const toggle = useCallback(
    (assetId: string, opts?: { shiftKey?: boolean }) => {
      setSelected((prev) => {
        const next = new Set(prev);

        if (opts?.shiftKey && lastToggledRef.current) {
          // Range select: from last toggled to current
          const fromIdx = orderedAssetIds.indexOf(lastToggledRef.current);
          const toIdx = orderedAssetIds.indexOf(assetId);
          if (fromIdx !== -1 && toIdx !== -1) {
            const start = Math.min(fromIdx, toIdx);
            const end = Math.max(fromIdx, toIdx);
            for (let i = start; i <= end; i++) {
              next.add(orderedAssetIds[i]);
            }
            lastToggledRef.current = assetId;
            return next;
          }
        }

        // Individual toggle
        if (next.has(assetId)) {
          next.delete(assetId);
        } else {
          next.add(assetId);
        }
        lastToggledRef.current = assetId;
        return next;
      });
    },
    [orderedAssetIds],
  );

  const selectGroup = useCallback((assetIds: string[], dateIso?: string | null) => {
    setSelected((prev) => {
      const next = new Set(prev);
      // If all in group are already selected, deselect them
      const allSelected = assetIds.every((id) => next.has(id));
      if (allSelected) {
        for (const id of assetIds) next.delete(id);
        if (dateIso) selectedDateKeysRef.current.delete(dateIso);
      } else {
        for (const id of assetIds) next.add(id);
        if (dateIso) selectedDateKeysRef.current.add(dateIso);
      }
      return next;
    });
  }, []);

  const clear = useCallback(() => {
    setSelected(new Set());
    selectedDateKeysRef.current.clear();
    lastToggledRef.current = null;
  }, []);

  const has = useCallback((assetId: string) => selected.has(assetId), [selected]);

  const toArray = useCallback(() => Array.from(selected), [selected]);

  const autoSelectForDates = useCallback(
    (assets: { asset_id: string; taken_at?: string | null; file_mtime?: string | null }[]) => {
      if (selectedDateKeysRef.current.size === 0) return;
      const toAdd: string[] = [];
      for (const asset of assets) {
        const dateStr = asset.taken_at ?? asset.file_mtime;
        if (!dateStr) continue;
        const d = new Date(dateStr);
        if (Number.isNaN(d.getTime())) continue;
        const dateKey = d.toISOString().slice(0, 10);
        if (selectedDateKeysRef.current.has(dateKey)) {
          toAdd.push(asset.asset_id);
        }
      }
      if (toAdd.length > 0) {
        setSelected((prev) => {
          const next = new Set(prev);
          for (const id of toAdd) next.add(id);
          return next;
        });
      }
    },
    [],
  );

  return {
    selected,
    count: selected.size,
    isActive: selected.size > 0,
    toggle,
    selectGroup,
    clear,
    has,
    toArray,
    autoSelectForDates,
  };
}
