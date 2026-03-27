import { useCallback, useEffect, useRef, useState } from "react";
import type { FacetsResponse } from "../api/types";
import { formatExposure } from "../lib/format";

interface DatePreset {
  label: string;
  from: string;
  to: string;
}

function getDatePresets(): DatePreset[] {
  const today = new Date();
  const fmt = (d: Date) => d.toISOString().slice(0, 10);
  const startOf = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate());

  const todayStr = fmt(startOf(today));
  const yday = new Date(today);
  yday.setDate(today.getDate() - 1);
  const weekStart = new Date(today);
  weekStart.setDate(today.getDate() - today.getDay());
  const monthStart = new Date(today.getFullYear(), today.getMonth(), 1);
  const yearStart = new Date(today.getFullYear(), 0, 1);

  return [
    { label: "Today", from: todayStr, to: todayStr },
    { label: "Yesterday", from: fmt(startOf(yday)), to: fmt(startOf(yday)) },
    { label: "This week", from: fmt(weekStart), to: todayStr },
    { label: "This month", from: fmt(monthStart), to: todayStr },
    { label: "This year", from: fmt(yearStart), to: todayStr },
  ];
}

function formatDateChiclet(from: string, to: string): string {
  const parseLocal = (s: string) => new Date(s + "T00:00:00");
  const fmtShort = (d: Date) =>
    d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  const fmtFull = (d: Date) =>
    d.toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
      year: "numeric",
    });

  if (from === to) return fmtFull(parseLocal(from));

  const df = parseLocal(from);
  const dt = parseLocal(to);
  if (df.getFullYear() === dt.getFullYear()) {
    if (df.getMonth() === dt.getMonth()) {
      return `${fmtShort(df)} – ${dt.getDate()}, ${df.getFullYear()}`;
    }
    return `${fmtShort(df)} – ${fmtShort(dt)}, ${df.getFullYear()}`;
  }
  return `${fmtFull(df)} – ${fmtFull(dt)}`;
}

const SORT_OPTIONS = [
  { value: "taken_at", label: "Date Taken" },
  { value: "created_at", label: "Date Added" },
  { value: "file_size", label: "File Size" },
  { value: "iso", label: "ISO" },
  { value: "aperture", label: "Aperture" },
  { value: "focal_length", label: "Focal Length" },
  { value: "rel_path", label: "Filename" },
] as const;

interface FilterBarProps {
  q: string | null;
  tag: string | null;
  path: string | null;
  dateFrom: string | null;
  dateTo: string | null;
  onChangeQ: (q: string | null) => void;
  onChangeTag: (tag: string | null) => void;
  onChangePath: (path: string | null) => void;
  onChangeDateRange: (from: string | null, to: string | null) => void;
  // Sort/filter state
  sort: string;
  dir: "asc" | "desc";
  mediaType: string | null;
  cameraMake: string | null;
  cameraModel: string | null;
  lensModel: string | null;
  isoMin: string | null;
  isoMax: string | null;
  exposureMinUs: string | null;
  exposureMaxUs: string | null;
  apertureMin: string | null;
  apertureMax: string | null;
  focalLengthMin: string | null;
  focalLengthMax: string | null;
  hasExposure: boolean | null;
  hasGps: boolean;
  nearLat: string | null;
  nearLon: string | null;
  nearRadiusKm: string | null;
  onChangeFilter: (key: string, value: string | null) => void;
  facets: FacetsResponse | null;
}

export function FilterBar({
  q,
  tag,
  path,
  dateFrom,
  dateTo,
  onChangeQ,
  onChangeTag,
  onChangePath,
  onChangeDateRange,
  sort,
  dir,
  mediaType,
  cameraMake,
  cameraModel,
  lensModel,
  isoMin,
  isoMax,
  exposureMinUs,
  exposureMaxUs,
  apertureMin,
  apertureMax,
  focalLengthMin,
  focalLengthMax,
  hasExposure,
  hasGps,
  nearLat,
  nearLon,
  nearRadiusKm,
  onChangeFilter,
  facets,
}: FilterBarProps) {
  const [inputValue, setInputValue] = useState(q ?? "");
  const [showDateRow, setShowDateRow] = useState(Boolean(dateFrom));
  const [customFrom, setCustomFrom] = useState(dateFrom ?? "");
  const [customTo, setCustomTo] = useState(dateTo ?? "");
  const [showCustom, setShowCustom] = useState(false);
  const customFromRef = useRef<HTMLInputElement>(null);
  const [showFilters, setShowFilters] = useState(() => {
    try {
      return localStorage.getItem("lv_filters_open") === "true";
    } catch {
      return false;
    }
  });

  // Keep input in sync when external q changes
  useEffect(() => {
    setInputValue(q ?? "");
  }, [q]);

  // Sync custom fields when external date changes
  useEffect(() => {
    setCustomFrom(dateFrom ?? "");
    setCustomTo(dateTo ?? "");
    if (dateFrom) setShowDateRow(true);
  }, [dateFrom, dateTo]);

  const applySearch = useCallback(
    (value: string) => {
      const trimmed = value.trim();
      onChangeQ(trimmed.length > 0 ? trimmed : null);
    },
    [onChangeQ],
  );

  useEffect(() => {
    const handle = window.setTimeout(() => {
      applySearch(inputValue);
    }, 500);
    return () => window.clearTimeout(handle);
  }, [inputValue, applySearch]);

  const handleKeyDown: React.KeyboardEventHandler<HTMLInputElement> = (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      applySearch(inputValue);
    }
  };

  const handleClear = () => {
    setInputValue("");
    onChangeQ(null);
  };

  const applyPreset = (preset: DatePreset) => {
    setCustomFrom(preset.from);
    setCustomTo(preset.to);
    setShowCustom(false);
    onChangeDateRange(preset.from, preset.to);
  };

  const applyCustom = () => {
    if (customFrom && customTo) {
      onChangeDateRange(customFrom, customTo);
    } else if (customFrom) {
      onChangeDateRange(customFrom, customFrom);
    }
  };

  const clearDate = () => {
    setCustomFrom("");
    setCustomTo("");
    setShowCustom(false);
    onChangeDateRange(null, null);
  };

  const toggleDateRow = () => {
    const next = !showDateRow;
    setShowDateRow(next);
    if (!next) clearDate();
  };

  const toggleFilters = () => {
    const next = !showFilters;
    setShowFilters(next);
    try {
      localStorage.setItem("lv_filters_open", String(next));
    } catch { /* ignore */ }
  };

  const hasDateFilter = Boolean(dateFrom);
  const hasActiveFilters = !!(
    mediaType || cameraMake || cameraModel || lensModel ||
    isoMin || isoMax || exposureMinUs || exposureMaxUs || apertureMin || apertureMax ||
    focalLengthMin || focalLengthMax || hasExposure != null || hasGps || nearLat
  );

  const showQChiclet = q !== null && q.length > 0;
  const showTagChiclet = tag !== null && tag.length > 0;
  const showPathChiclet = path !== null && path.length > 0;
  const presets = getDatePresets();

  const selectCls = "rounded-md border border-gray-700 bg-gray-800 px-2 py-1.5 text-xs text-gray-200 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500";

  return (
    <div className="flex flex-col gap-2">
      {/* Row 1: search input + controls + chiclets */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative flex-1 min-w-[200px] max-w-xl">
          <span className="pointer-events-none absolute inset-y-0 left-3 flex items-center text-gray-500">
            <svg
              className="h-4 w-4"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden
            >
              <circle cx="11" cy="11" r="7" />
              <line x1="16.65" y1="16.65" x2="21" y2="21" />
            </svg>
          </span>
          <input
            type="search"
            className="w-full rounded-lg border border-gray-700 bg-gray-800 pl-9 pr-8 py-2 text-sm text-gray-100 placeholder:text-gray-500 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            placeholder="Search ..."
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyDown={handleKeyDown}
          />
          {inputValue && (
            <button
              type="button"
              onClick={handleClear}
              className="absolute inset-y-0 right-2 flex items-center text-gray-500 hover:text-gray-200"
              aria-label="Clear search"
            >
              x
            </button>
          )}
        </div>

        {/* Media type toggle */}
        <div className="flex rounded-lg border border-gray-700 overflow-hidden text-xs">
          {(["all", "image", "video"] as const).map((mt) => {
            const isActive = mt === "all" ? !mediaType : mediaType === mt;
            return (
              <button
                key={mt}
                type="button"
                onClick={() => onChangeFilter("media_type", mt === "all" ? null : mt)}
                className={`px-3 py-1.5 transition-colors ${
                  isActive
                    ? "bg-indigo-600 text-white"
                    : "bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-gray-200"
                }`}
              >
                {mt === "all" ? "All" : mt === "image" ? "Photos" : "Videos"}
              </button>
            );
          })}
        </div>

        {/* Sort dropdown */}
        <div className="flex items-center gap-1">
          <select
            value={sort}
            onChange={(e) => onChangeFilter("sort", e.target.value)}
            className={selectCls}
          >
            {SORT_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={() => onChangeFilter("dir", dir === "asc" ? "desc" : "asc")}
            className="flex h-7 w-7 items-center justify-center rounded-md border border-gray-700 bg-gray-800 text-gray-400 hover:border-gray-600 hover:text-gray-200 text-xs"
            title={dir === "asc" ? "Ascending" : "Descending"}
          >
            {dir === "asc" ? "\u2191" : "\u2193"}
          </button>
        </div>

        {/* Date filter toggle */}
        <button
          type="button"
          onClick={toggleDateRow}
          title={showDateRow ? "Hide date filter" : "Filter by date"}
          aria-label="Toggle date filter"
          className={`relative flex h-9 w-9 items-center justify-center rounded-lg border transition-colors ${
            showDateRow
              ? "border-indigo-500 bg-indigo-600/20 text-indigo-300"
              : "border-gray-700 bg-gray-800 text-gray-400 hover:border-gray-600 hover:text-gray-200"
          }`}
        >
          <svg
            className="h-4 w-4"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden
          >
            <rect x="3" y="4" width="18" height="18" rx="2" />
            <line x1="16" y1="2" x2="16" y2="6" />
            <line x1="8" y1="2" x2="8" y2="6" />
            <line x1="3" y1="10" x2="21" y2="10" />
          </svg>
          {hasDateFilter && (
            <span className="absolute -top-0.5 -right-0.5 h-2 w-2 rounded-full bg-indigo-500" />
          )}
        </button>

        {/* Filters toggle */}
        <button
          type="button"
          onClick={toggleFilters}
          title={showFilters ? "Hide filters" : "Show filters"}
          className={`relative flex h-9 items-center gap-1 rounded-lg border px-2.5 text-xs font-medium transition-colors ${
            showFilters || hasActiveFilters
              ? "border-indigo-500 bg-indigo-600/20 text-indigo-300"
              : "border-gray-700 bg-gray-800 text-gray-400 hover:border-gray-600 hover:text-gray-200"
          }`}
        >
          <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
            <line x1="4" y1="6" x2="20" y2="6" />
            <line x1="7" y1="12" x2="17" y2="12" />
            <line x1="10" y1="18" x2="14" y2="18" />
          </svg>
          Filters
          {hasActiveFilters && (
            <span className="absolute -top-0.5 -right-0.5 h-2 w-2 rounded-full bg-indigo-500" />
          )}
        </button>

        {/* Active chiclets */}
        <div className="flex flex-wrap items-center gap-2">
          {showPathChiclet && (
            <Chiclet label={`/${path}`} onClear={() => onChangePath(null)} />
          )}
          {showQChiclet && (
            <Chiclet label={`"${q}"`} onClear={() => onChangeQ(null)} />
          )}
          {showTagChiclet && (
            <Chiclet label={`#${tag}`} onClear={() => onChangeTag(null)} />
          )}
          {hasDateFilter && dateFrom && dateTo && (
            <Chiclet label={formatDateChiclet(dateFrom, dateTo)} onClear={clearDate} />
          )}
          {mediaType && (
            <Chiclet
              label={mediaType === "image" ? "Photos only" : "Videos only"}
              onClear={() => onChangeFilter("media_type", null)}
            />
          )}
          {cameraMake && (
            <Chiclet label={cameraMake} onClear={() => { onChangeFilter("camera_make", null); onChangeFilter("camera_model", null); }} />
          )}
          {cameraModel && (
            <Chiclet label={cameraModel} onClear={() => onChangeFilter("camera_model", null)} />
          )}
          {lensModel && (
            <Chiclet label={lensModel} onClear={() => onChangeFilter("lens_model", null)} />
          )}
          {(isoMin || isoMax) && (
            <Chiclet
              label={`ISO ${isoMin ?? ""}${isoMin && isoMax ? "-" : ""}${isoMax ?? ""}`}
              onClear={() => { onChangeFilter("iso_min", null); onChangeFilter("iso_max", null); }}
            />
          )}
          {(exposureMinUs || exposureMaxUs) && (() => {
            const minLabel = exposureMinUs ? formatExposure(Number(exposureMinUs)) : null;
            const maxLabel = exposureMaxUs ? formatExposure(Number(exposureMaxUs)) : null;
            const label = minLabel && maxLabel && minLabel === maxLabel
              ? minLabel
              : `${minLabel ?? ""}${minLabel && maxLabel ? " – " : ""}${maxLabel ?? ""}`;
            return (
              <Chiclet
                label={label}
                onClear={() => { onChangeFilter("exposure_min_us", null); onChangeFilter("exposure_max_us", null); }}
              />
            );
          })()}
          {(apertureMin || apertureMax) && (
            <Chiclet
              label={`f/${apertureMin ?? ""}${apertureMin && apertureMax ? "-" : ""}${apertureMax ?? ""}`}
              onClear={() => { onChangeFilter("aperture_min", null); onChangeFilter("aperture_max", null); }}
            />
          )}
          {(focalLengthMin || focalLengthMax) && (
            <Chiclet
              label={`${focalLengthMin ?? ""}${focalLengthMin && focalLengthMax ? "-" : ""}${focalLengthMax ?? ""}mm`}
              onClear={() => { onChangeFilter("focal_length_min", null); onChangeFilter("focal_length_max", null); }}
            />
          )}
          {hasExposure === false && (
            <Chiclet label="No exposure data" onClear={() => onChangeFilter("has_exposure", null)} />
          )}
          {hasExposure === true && (
            <Chiclet label="Has exposure data" onClear={() => onChangeFilter("has_exposure", null)} />
          )}
          {hasGps && !nearLat && (
            <Chiclet label="Has location" onClear={() => onChangeFilter("has_gps", null)} />
          )}
          {nearLat && nearLon && (
            <Chiclet
              label={`Within ${nearRadiusKm ?? "1"}km`}
              onClear={() => {
                onChangeFilter("near_lat", null);
                onChangeFilter("near_lon", null);
                onChangeFilter("near_radius_km", null);
              }}
            />
          )}
        </div>
      </div>

      {/* Row 2: date presets */}
      {showDateRow && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-gray-700/50 bg-gray-900/60 px-3 py-2">
          {presets.map((p) => {
            const isActive = dateFrom === p.from && dateTo === p.to;
            return (
              <button
                key={p.label}
                type="button"
                onClick={() => applyPreset(p)}
                className={`rounded-full px-3 py-1 text-xs font-medium transition-colors ${
                  isActive
                    ? "bg-indigo-600 text-white"
                    : "bg-gray-800 text-gray-300 hover:bg-gray-700 hover:text-gray-100"
                }`}
              >
                {p.label}
              </button>
            );
          })}

          <button
            type="button"
            onClick={() => {
              setShowCustom((v) => !v);
              if (!showCustom) setTimeout(() => customFromRef.current?.focus(), 0);
            }}
            className={`rounded-full px-3 py-1 text-xs font-medium transition-colors ${
              showCustom
                ? "bg-gray-700 text-gray-100"
                : "bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-gray-200"
            }`}
          >
            Custom
          </button>

          {showCustom && (
            <div className="flex items-center gap-2 mt-1 w-full">
              <input
                ref={customFromRef}
                type="date"
                value={customFrom}
                onChange={(e) => setCustomFrom(e.target.value)}
                className="rounded-md border border-gray-700 bg-gray-800 px-2 py-1 text-xs text-gray-200 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 [color-scheme:dark]"
                aria-label="Date from"
              />
              <span className="text-xs text-gray-500">to</span>
              <input
                type="date"
                value={customTo}
                onChange={(e) => setCustomTo(e.target.value)}
                min={customFrom}
                className="rounded-md border border-gray-700 bg-gray-800 px-2 py-1 text-xs text-gray-200 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 [color-scheme:dark]"
                aria-label="Date to"
              />
              <button
                type="button"
                onClick={applyCustom}
                disabled={!customFrom}
                className="rounded-md bg-indigo-600 px-3 py-1 text-xs font-medium text-white hover:bg-indigo-500 disabled:opacity-40"
              >
                Apply
              </button>
            </div>
          )}

          {hasDateFilter && (
            <button
              type="button"
              onClick={clearDate}
              className="ml-auto text-xs text-gray-500 hover:text-gray-300"
            >
              Clear date
            </button>
          )}
        </div>
      )}

      {/* Row 3: expanded filters panel */}
      {showFilters && (
        <div className="flex flex-wrap items-end gap-4 rounded-lg border border-gray-700/50 bg-gray-900/60 px-3 py-3">
          {/* Camera make */}
          {facets && facets.camera_makes.length > 0 && (
            <FilterSelect
              label="Camera"
              value={cameraMake ?? ""}
              options={facets.camera_makes}
              onChange={(v) => {
                onChangeFilter("camera_make", v || null);
                if (!v) onChangeFilter("camera_model", null);
              }}
            />
          )}

          {/* Camera model */}
          {cameraMake && facets && facets.camera_models.length > 0 && (
            <FilterSelect
              label="Model"
              value={cameraModel ?? ""}
              options={facets.camera_models.filter(
                (m) => !cameraMake || m.toLowerCase().includes(cameraMake.toLowerCase()),
              )}
              onChange={(v) => onChangeFilter("camera_model", v || null)}
            />
          )}

          {/* Lens */}
          {facets && facets.lens_models.length > 0 && (
            <FilterSelect
              label="Lens"
              value={lensModel ?? ""}
              options={facets.lens_models}
              onChange={(v) => onChangeFilter("lens_model", v || null)}
            />
          )}

          {/* ISO range */}
          {facets && facets.iso_range[0] != null && (
            <RangeInputs
              label="ISO"
              min={isoMin ?? ""}
              max={isoMax ?? ""}
              placeholderMin={String(facets.iso_range[0] ?? "")}
              placeholderMax={String(facets.iso_range[1] ?? "")}
              onChangeMin={(v) => onChangeFilter("iso_min", v || null)}
              onChangeMax={(v) => onChangeFilter("iso_max", v || null)}
            />
          )}

          {/* Aperture range */}
          {facets && facets.aperture_range[0] != null && (
            <RangeInputs
              label="Aperture (f/)"
              min={apertureMin ?? ""}
              max={apertureMax ?? ""}
              placeholderMin={String(facets.aperture_range[0] ?? "")}
              placeholderMax={String(facets.aperture_range[1] ?? "")}
              onChangeMin={(v) => onChangeFilter("aperture_min", v || null)}
              onChangeMax={(v) => onChangeFilter("aperture_max", v || null)}
            />
          )}

          {/* Focal length range */}
          {facets && facets.focal_length_range[0] != null && (
            <RangeInputs
              label="Focal (mm)"
              min={focalLengthMin ?? ""}
              max={focalLengthMax ?? ""}
              placeholderMin={String(facets.focal_length_range[0] ?? "")}
              placeholderMax={String(facets.focal_length_range[1] ?? "")}
              onChangeMin={(v) => onChangeFilter("focal_length_min", v || null)}
              onChangeMax={(v) => onChangeFilter("focal_length_max", v || null)}
            />
          )}

          {/* GPS toggle */}
          {facets && facets.has_gps_count > 0 && (
            <label className="flex items-center gap-1.5 text-xs text-gray-300 cursor-pointer">
              <input
                type="checkbox"
                checked={hasGps}
                onChange={(e) => onChangeFilter("has_gps", e.target.checked ? "true" : null)}
                className="rounded border-gray-700 bg-gray-800 text-indigo-600 focus:ring-indigo-500"
              />
              Has location ({facets.has_gps_count})
            </label>
          )}

          {/* Geo-proximity radius selector (only when near_lat is set) */}
          {nearLat && nearLon && (
            <div className="flex items-center gap-1.5">
              <span className="text-xs text-gray-400">Radius:</span>
              <select
                value={nearRadiusKm ?? "1"}
                onChange={(e) => onChangeFilter("near_radius_km", e.target.value)}
                className={selectCls}
              >
                {[0.5, 1, 5, 10, 50].map((r) => (
                  <option key={r} value={String(r)}>
                    {r}km
                  </option>
                ))}
              </select>
            </div>
          )}

          {hasActiveFilters && (
            <button
              type="button"
              onClick={() => {
                for (const key of [
                  "media_type", "camera_make", "camera_model", "lens_model",
                  "iso_min", "iso_max", "aperture_min", "aperture_max",
                  "focal_length_min", "focal_length_max", "has_gps",
                  "near_lat", "near_lon", "near_radius_km",
                ]) {
                  onChangeFilter(key, null);
                }
              }}
              className="ml-auto text-xs text-gray-500 hover:text-gray-300"
            >
              Clear all filters
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function Chiclet({ label, onClear }: { label: string; onClear: () => void }) {
  return (
    <button
      type="button"
      onClick={onClear}
      className="inline-flex items-center gap-1 rounded-full bg-gray-700 px-3 py-1 text-sm text-gray-200"
    >
      <span>{label}</span>
      <span className="ml-1 text-gray-400 hover:text-gray-100">x</span>
    </button>
  );
}

function FilterSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (v: string) => void;
}) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-[10px] font-medium uppercase tracking-wider text-gray-500">
        {label}
      </span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="rounded-md border border-gray-700 bg-gray-800 px-2 py-1.5 text-xs text-gray-200 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 max-w-[200px]"
      >
        <option value="">Any</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </div>
  );
}

function RangeInputs({
  label,
  min,
  max,
  placeholderMin,
  placeholderMax,
  onChangeMin,
  onChangeMax,
}: {
  label: string;
  min: string;
  max: string;
  placeholderMin: string;
  placeholderMax: string;
  onChangeMin: (v: string) => void;
  onChangeMax: (v: string) => void;
}) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-[10px] font-medium uppercase tracking-wider text-gray-500">
        {label}
      </span>
      <div className="flex items-center gap-1">
        <input
          type="number"
          value={min}
          placeholder={placeholderMin}
          onChange={(e) => onChangeMin(e.target.value)}
          className="w-20 rounded-md border border-gray-700 bg-gray-800 px-2 py-1.5 text-xs text-gray-200 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
        />
        <span className="text-xs text-gray-500">-</span>
        <input
          type="number"
          value={max}
          placeholder={placeholderMax}
          onChange={(e) => onChangeMax(e.target.value)}
          className="w-20 rounded-md border border-gray-700 bg-gray-800 px-2 py-1.5 text-xs text-gray-200 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
        />
      </div>
    </div>
  );
}
