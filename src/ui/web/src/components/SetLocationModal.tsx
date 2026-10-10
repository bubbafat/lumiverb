import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError, setLocations, type LocationReplace } from "../api/client";
import { Modal } from "./Modal";
import { clipCount } from "./ProjectUsageList";

/** "Set location…" for a selection (ADR-017): a point, or the same place as
 * one of the selected clips. Replacing a location the file or a person gave
 * asks first, as the API requires (409 location_exists). */

type Ask = { count: number; person: number; file: number };

export function parseLatLon(raw: string): { lat: number; lon: number } | null {
  const parts = raw.split(/[,\s]+/).filter(Boolean).map(Number);
  if (parts.length !== 2 || parts.some((n) => !Number.isFinite(n))) return null;
  const [lat, lon] = parts;
  if (Math.abs(lat) > 90 || Math.abs(lon) > 180 || (lat === 0 && lon === 0)) return null;
  return { lat, lon };
}

export function SetLocationModal({
  assetIds,
  nameOf,
  onClose,
  onDone,
}: {
  assetIds: string[];
  /** A clip's file name, for "Same place as". */
  nameOf: (assetId: string) => string;
  onClose: () => void;
  onDone?: () => void;
}) {
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<"point" | "same">("point");
  const [point, setPoint] = useState("");
  const [sameAs, setSameAs] = useState(assetIds[0] ?? "");
  const [ask, setAsk] = useState<Ask | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const parsed = parseLatLon(point);
  const ready = mode === "point" ? parsed !== null : sameAs !== "";

  const send = async (replace: LocationReplace) => {
    setBusy(true);
    setError(null);
    try {
      const where = mode === "point" ? parsed! : { same_as: sameAs };
      await setLocations({ asset_ids: assetIds, replace, ...where });
      void queryClient.invalidateQueries();
      onDone?.();
      onClose();
    } catch (err) {
      if (err instanceof ApiError && err.code === "location_exists" && err.details) {
        const d = err.details as Record<string, number>;
        setAsk({ count: d.count ?? 0, person: d.person ?? 0, file: d.file ?? 0 });
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setBusy(false);
    }
  };

  const input = "w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-gray-100";
  const button = "rounded-lg px-4 py-2 text-sm font-medium disabled:opacity-50";

  return (
    <Modal isOpen onClose={onClose} title={`Set location for ${clipCount(assetIds.length)}`}>
      {ask ? (
        <div className="space-y-4">
          <p className="text-sm text-gray-300">
            {clipCount(ask.count)} already {ask.count === 1 ? "has" : "have"} a location
            {ask.person > 0 && ask.file > 0
              ? ` (${ask.person} set by a person, ${ask.file} from the file).`
              : ask.file > 0 ? " from the file." : " set by a person."}
            {ask.file > 0 && " The file's stays visible."}
          </p>
          <div className="flex justify-end gap-2">
            <button type="button" onClick={() => setAsk(null)} className={`${button} border border-gray-600 text-gray-300 hover:bg-gray-800`}>
              Back
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => void send(ask.file > 0 ? "all" : "person")}
              className={`${button} bg-indigo-600 text-white hover:bg-indigo-500`}
            >
              Replace {ask.count}
            </button>
          </div>
        </div>
      ) : (
        <div className="space-y-4">
          <div className="flex gap-4 text-sm text-gray-300">
            <label className="flex items-center gap-1.5">
              <input type="radio" checked={mode === "point"} onChange={() => setMode("point")} />
              Coordinates
            </label>
            {assetIds.length > 1 && (
              <label className="flex items-center gap-1.5">
                <input type="radio" checked={mode === "same"} onChange={() => setMode("same")} />
                Same place as
              </label>
            )}
          </div>
          {mode === "point" ? (
            <input
              aria-label="Latitude, longitude"
              placeholder="48.85837, 2.29448"
              value={point}
              onChange={(e) => setPoint(e.target.value)}
              className={input}
            />
          ) : (
            <select aria-label="Same place as" value={sameAs} onChange={(e) => setSameAs(e.target.value)} className={input}>
              {assetIds.map((id) => (
                <option key={id} value={id}>{nameOf(id)}</option>
              ))}
            </select>
          )}
          {error && <p role="alert" className="text-sm text-red-300">{error}</p>}
          <div className="flex justify-end gap-2">
            <button type="button" onClick={onClose} className={`${button} border border-gray-600 text-gray-300 hover:bg-gray-800`}>
              Cancel
            </button>
            <button
              type="button"
              disabled={!ready || busy}
              onClick={() => void send("none")}
              className={`${button} bg-indigo-600 text-white hover:bg-indigo-500`}
            >
              Set location
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}
