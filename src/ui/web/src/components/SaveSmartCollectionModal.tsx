import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { createCollection, ApiError } from "../api/client";
import { Modal } from "./Modal";
import type { SavedQuery } from "../api/types";

/** Convert camelCase keys to snake_case for the server. */
function camelToSnake(key: string): string {
  return key.replace(/[A-Z]/g, (m) => "_" + m.toLowerCase());
}

/** Convert a camelCase-keyed filter object to snake_case keys.
 *  Strips null, undefined, and false values — false booleans in browseOpts
 *  are defaults (e.g., hasGps=false when no GPS filter is active), not user selections.
 *  Also strips sort/dir defaults since they're not filters.
 */
export function toSnakeCaseFilters(
  filters: Record<string, unknown> | object,
): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(filters)) {
    if (v == null || v === false) continue;
    const snakeKey = camelToSnake(k);
    // Skip sort defaults — they're not filters
    if (snakeKey === "sort" || snakeKey === "dir") continue;
    // Skip library_id — it goes at the top level of saved_query, not in filters
    if (snakeKey === "library_id") continue;
    result[snakeKey] = v;
  }
  return result;
}

/** Convert a saved query into human-readable filter labels. */
export function formatSavedQuery(
  savedQuery: { q?: string; filters: Record<string, unknown> },
): string[] {
  const labels: string[] = [];
  const f = savedQuery.filters;

  if (savedQuery.q) labels.push(`Search: "${savedQuery.q}"`);

  if (f.camera_make) {
    const cam = f.camera_model
      ? `${f.camera_make} ${f.camera_model}`
      : String(f.camera_make);
    labels.push(`Camera: ${cam}`);
  } else if (f.camera_model) {
    labels.push(`Camera: ${f.camera_model}`);
  }
  if (f.lens_model) labels.push(`Lens: ${f.lens_model}`);

  if (f.media_type === "image") labels.push("Photos only");
  else if (f.media_type === "video") labels.push("Videos only");

  if (f.favorite === true) labels.push("Favorites");

  if (f.star_min != null && f.star_max != null) {
    labels.push(
      f.star_min === f.star_max
        ? `Stars: ${f.star_min}`
        : `Stars: ${f.star_min}–${f.star_max}`,
    );
  } else if (f.star_min != null) {
    labels.push(`Stars: ${f.star_min}+`);
  } else if (f.star_max != null) {
    labels.push(`Stars: up to ${f.star_max}`);
  }

  if (f.color) labels.push(`Color: ${f.color}`);
  if (f.tag) labels.push(`Tag: ${f.tag}`);
  if (f.has_gps === true) labels.push("Has GPS");
  if (f.has_faces === true) labels.push("Has faces");
  if (f.has_rating === true) labels.push("Has rating");
  if (f.has_color === true) labels.push("Has color label");
  if (f.person_id) labels.push("Person filter");

  if (f.iso_min != null || f.iso_max != null) {
    if (f.iso_min && f.iso_max && f.iso_min === f.iso_max) {
      labels.push(`ISO ${f.iso_min}`);
    } else {
      labels.push(`ISO ${f.iso_min ?? ""}${f.iso_min && f.iso_max ? "–" : ""}${f.iso_max ?? ""}`);
    }
  }

  if (f.date_from || f.date_to) {
    if (f.date_from && f.date_to) labels.push(`Date: ${f.date_from} – ${f.date_to}`);
    else if (f.date_from) labels.push(`From: ${f.date_from}`);
    else labels.push(`Until: ${f.date_to}`);
  }

  return labels;
}

interface Props {
  savedQuery: SavedQuery;
  onClose: () => void;
}

export function SaveSmartCollectionModal({ savedQuery, onClose }: Props) {
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: () =>
      createCollection(name, {
        type: "smart",
        saved_query: savedQuery,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["collections"] });
      onClose();
    },
    onError: (err: ApiError) => setError(err.message),
  });

  return (
    <Modal isOpen={true} title="Save as Smart Collection" onClose={onClose}>
      <div className="space-y-4">
        <p className="text-sm text-gray-400">
          This collection will automatically update as matching photos change.
        </p>
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Collection name"
          className="w-full rounded-md border border-gray-600 bg-gray-800 px-3 py-2 text-sm text-gray-100 placeholder:text-gray-500 focus:border-indigo-500 focus:outline-none"
          autoFocus
          onKeyDown={(e) => {
            if (e.key === "Enter" && name.trim()) mutation.mutate();
          }}
        />
        {error && <p className="text-sm text-red-400">{error}</p>}
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md px-4 py-2 text-sm text-gray-400 hover:text-gray-200"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => mutation.mutate()}
            disabled={!name.trim() || mutation.isPending}
            className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
          >
            {mutation.isPending ? "Saving..." : "Save"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
