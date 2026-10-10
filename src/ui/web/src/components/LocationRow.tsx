import { useState, type ReactNode } from "react";
import type { AssetLocation } from "../api/types";

/** Where a clip was shot (ADR-017): a person's, else the file's, else a
 * guess; a suggestion asks. Shown in the Lightbox's details. */

const coords = (lat: number, lon: number) => `${lat.toFixed(5)}, ${lon.toFixed(5)}`;

export function about(radiusM: number): string {
  if (radiusM < 1000) return `About ${Math.max(10, Math.round(radiusM / 10) * 10)} m`;
  return `About ${Math.round(radiusM / 1000)} km`;
}

function MapLink({ lat, lon, children }: { lat: number; lon: number; children?: ReactNode }) {
  return (
    <a
      href={`https://maps.google.com/?q=${lat},${lon}`}
      target="_blank"
      rel="noopener noreferrer"
      title={coords(lat, lon)}
      className="text-indigo-400 hover:text-indigo-300 hover:underline transition-colors"
    >
      {children ?? coords(lat, lon)}
    </a>
  );
}

const small = "text-xs text-indigo-400 hover:text-indigo-300 hover:underline transition-colors disabled:opacity-50";

export interface LocationRowProps {
  gpsLat: number | null | undefined;
  gpsLon: number | null | undefined;
  location?: AssetLocation | null;
  /** An editor or admin: shows Clear, Use this and No. */
  canEdit?: boolean;
  busy?: boolean;
  /** A change that failed, said under the row. */
  error?: string | null;
  onNearbyClick?: (lat: number, lon: number) => void;
  onClear?: () => void;
  onAccept?: () => void;
  onReject?: () => void;
}

export function LocationRow({
  gpsLat, gpsLon, location, canEdit = false, busy = false, error, onNearbyClick, onClear, onAccept, onReject,
}: LocationRowProps) {
  const [showBasis, setShowBasis] = useState(false);
  const file = gpsLat != null && gpsLon != null ? { lat: gpsLat, lon: gpsLon } : null;
  const person = location && location.source === "person" ? location : null;
  const guess = location && location.status === "applied" && location.source !== "person" ? location : null;
  const suggestion = location && location.status === "suggested" ? location : null;
  const shown = person ?? file ?? guess;
  if (!shown && !suggestion) return null;

  return (
    <>
      <div className="flex">
        <dt className="w-2/5 text-xs text-gray-500">Location</dt>
        <dd className="w-3/5 space-y-0.5 text-sm">
          {person && (
            <>
              <div><MapLink lat={person.lat} lon={person.lon} /></div>
              <div className="text-xs text-gray-400">
                {person.set_by ? `Set by ${person.set_by}` : "Set by hand"}
                {person.basis_summary ? ` · ${person.basis_summary}` : ""}
                {canEdit && onClear && (
                  <>
                    {" · "}
                    <button type="button" disabled={busy} onClick={onClear} className={small}>Clear</button>
                  </>
                )}
              </div>
              {file && <div className="text-xs text-gray-500">From the file: {coords(file.lat, file.lon)}</div>}
            </>
          )}
          {!person && file && <div><MapLink lat={file.lat} lon={file.lon} /></div>}
          {!person && !file && guess && (
            <>
              <div className="flex items-center gap-2">
                <MapLink lat={guess.lat} lon={guess.lon} />
                <button
                  type="button"
                  aria-expanded={showBasis}
                  onClick={() => setShowBasis((v) => !v)}
                  className="rounded bg-amber-900/50 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-amber-300 hover:bg-amber-900/80"
                >
                  Guess
                </button>
              </div>
              {showBasis && (
                <div className="text-xs text-gray-400">
                  {about(guess.radius_m)}
                  {guess.basis_summary ? ` · ${guess.basis_summary}` : ""}
                  {guess.basis_gone ? " · source removed" : ""}
                </div>
              )}
            </>
          )}
          {!person && suggestion && (
            <div className="text-xs text-gray-300">
              Maybe: {suggestion.basis_summary || coords(suggestion.lat, suggestion.lon)}
              {canEdit && onAccept && (
                <>
                  {" · "}
                  <button type="button" disabled={busy} onClick={onAccept} className={small}>Use this</button>
                </>
              )}
              {canEdit && onReject && (
                <>
                  {" · "}
                  <button type="button" disabled={busy} onClick={onReject} className={small}>No</button>
                </>
              )}
            </div>
          )}
          {error && <div role="alert" className="text-xs text-red-300">{error}</div>}
        </dd>
      </div>
      {onNearbyClick && shown && (
        <div className="flex">
          <dt className="w-2/5" />
          <dd className="w-3/5">
            <button type="button" onClick={() => onNearbyClick(shown.lat, shown.lon)} className={small}>
              Photos nearby
            </button>
          </dd>
        </div>
      )}
    </>
  );
}
