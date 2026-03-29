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
  /** Select all assets in a group (by their IDs) */
  selectGroup: (assetIds: string[]) => void;
  /** Clear all selections */
  clear: () => void;
  /** Check if an asset is selected */
  has: (assetId: string) => boolean;
  /** Get selected IDs as array */
  toArray: () => string[];
}

/**
 * Selection hook for multi-select in asset grids.
 *
 * @param orderedAssetIds - All asset IDs in display order. Used for shift-range selection.
 */
export function useSelection(orderedAssetIds: string[]): UseSelectionReturn {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const lastToggledRef = useRef<string | null>(null);

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

  const selectGroup = useCallback((assetIds: string[]) => {
    setSelected((prev) => {
      const next = new Set(prev);
      // If all in group are already selected, deselect them
      const allSelected = assetIds.every((id) => next.has(id));
      if (allSelected) {
        for (const id of assetIds) next.delete(id);
      } else {
        for (const id of assetIds) next.add(id);
      }
      return next;
    });
  }, []);

  const clear = useCallback(() => {
    setSelected(new Set());
    lastToggledRef.current = null;
  }, []);

  const has = useCallback((assetId: string) => selected.has(assetId), [selected]);

  const toArray = useCallback(() => Array.from(selected), [selected]);

  return {
    selected,
    count: selected.size,
    isActive: selected.size > 0,
    toggle,
    selectGroup,
    clear,
    has,
    toArray,
  };
}
