import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { FacetsResponse } from "../api/types";
import { RATING_COLORS, COLOR_HEX } from "../api/types";
// formatExposure available if exposure filter UI is restored
import { searchPeople, getPerson } from "../api/client";
import { FaceCropImage } from "./FaceCropImage";
import type { LeafFilter } from "../lib/queryFilter";
import {
  getFilterValue,
  filterLabel,
  parseRange,
  composeRange,
  parseNear,
  composeNear,
  parseDate,
  composeDate,
} from "../lib/queryFilter";

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

// ---------------------------------------------------------------------------
// New filter algebra interface
// ---------------------------------------------------------------------------

interface FilterBarProps {
  /** Active filters from the URL. */
  filters: LeafFilter[];
  sort: string;
  dir: "asc" | "desc";
  /** Set or remove a single filter. value=null removes it. */
  onSetFilter: (type: string, value: string | null) => void;
  /** Set sort + direction. */
  onSetSort: (sort: string, dir: "asc" | "desc") => void;
  /** Clear all filters. */
  onClearAll: () => void;
  /** Facets for populating dropdowns. */
  facets: FacetsResponse | null;
  /** Called when user clicks "Save as Smart Collection". */
  onSaveSmartCollection?: () => void;
}

function PersonChiclet({ personId, onClear }: { personId: string; onClear: () => void }) {
  const { data } = useQuery({
    queryKey: ["person", personId],
    queryFn: () => getPerson(personId),
  });
  return <Chiclet label={`Person: ${data?.display_name ?? "..."}`} onClear={onClear} />;
}

function PersonFilterDropdown({
  personId,
  onSelect,
}: {
  personId: string | null;
  onSelect: (personId: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const ref = useRef<HTMLDivElement>(null);

  const { data } = useQuery({
    queryKey: ["people-search", q],
    queryFn: () => searchPeople(q || "", 10),
    enabled: open,
  });

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  if (personId) return null; // already filtered, shown as chiclet

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="text-xs text-gray-400 hover:text-gray-200"
      >
        Person...
      </button>
      {open && (
        <div className="absolute left-0 top-full z-50 mt-1 w-56 rounded-lg border border-gray-600 bg-gray-800 p-2 shadow-xl">
          <input
            type="text"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search people..."
            className="w-full rounded border border-gray-600 bg-gray-900 px-2 py-1 text-xs text-white focus:border-indigo-500 focus:outline-none"
            autoFocus
          />
          <div className="mt-1 max-h-40 overflow-y-auto">
            {(data?.items ?? []).map((p) => (
              <button
                key={p.person_id}
                type="button"
                onClick={() => {
                  onSelect(p.person_id);
                  setOpen(false);
                  setQ("");
                }}
                className="flex w-full items-center gap-2 rounded px-2 py-1 text-left text-xs text-gray-200 hover:bg-gray-700"
              >
                {p.representative_face_id && (
                  <FaceCropImage faceId={p.representative_face_id} size={24} />
                )}
                <span className="flex-1">
                  {p.display_name} <span className="text-gray-500">({p.face_count})</span>
                </span>
              </button>
            ))}
            {data && data.items.length === 0 && q && (
              <p className="px-2 py-1 text-xs text-gray-500">No people found</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export function FilterBar({
  filters,
  sort,
  dir,
  onSetFilter,
  onSetSort,
  onClearAll,
  facets,
  onSaveSmartCollection,
}: FilterBarProps) {
  // --- Read individual values from filter array ---
  const q = getFilterValue(filters, "query") ?? null;
  const tag = getFilterValue(filters, "tag") ?? null;
  const mediaType = getFilterValue(filters, "media") ?? null;
  const cameraMake = getFilterValue(filters, "camera_make") ?? null;
  const cameraModel = getFilterValue(filters, "camera_model") ?? null;
  const lensModel = getFilterValue(filters, "lens") ?? null;
  const isoRange = parseRange(getFilterValue(filters, "iso"));
  const apertureRange = parseRange(getFilterValue(filters, "aperture"));
  const focalLengthRange = parseRange(getFilterValue(filters, "focal_length"));
  const _exposureRange = parseRange(getFilterValue(filters, "exposure"));
  const _hasExposureVal = getFilterValue(filters, "has_exposure");
  void _exposureRange; void _hasExposureVal; // available for future exposure filter UI
  const hasGps = getFilterValue(filters, "has_gps") === "yes";
  const hasFaces = getFilterValue(filters, "has_faces") === "yes";
  const personId = getFilterValue(filters, "person") ?? null;
  const nearVal = parseNear(getFilterValue(filters, "near"));
  const dateVal = parseDate(getFilterValue(filters, "date"));
  const favoriteVal = getFilterValue(filters, "favorite");
  const favorite = favoriteVal === "yes" ? true : favoriteVal === "no" ? false : null;
  const starVal = parseRange(getFilterValue(filters, "stars"));
  const colorFilter = getFilterValue(filters, "color") ?? null;

  const [inputValue, setInputValue] = useState(q ?? "");
  const [searchFocused, setSearchFocused] = useState(false);
  const searchContainerRef = useRef<HTMLDivElement>(null);
  const [showDateRow, setShowDateRow] = useState(Boolean(dateVal.from));
  const [customFrom, setCustomFrom] = useState(dateVal.from ?? "");
  const [customTo, setCustomTo] = useState(dateVal.to ?? "");
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
    setCustomFrom(dateVal.from ?? "");
    setCustomTo(dateVal.to ?? "");
    if (dateVal.from) setShowDateRow(true);
  }, [dateVal.from, dateVal.to]);

  // Person suggestions in main search bar
  const showPersonSuggestions = searchFocused && inputValue.trim().length >= 2 && !personId;
  const { data: searchPeopleData } = useQuery({
    queryKey: ["people-search-bar", inputValue.trim()],
    queryFn: () => searchPeople(inputValue.trim(), 5),
    enabled: showPersonSuggestions,
  });
  const personSuggestions = showPersonSuggestions ? (searchPeopleData?.items ?? []) : [];

  // Close search suggestions on click outside
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (searchContainerRef.current && !searchContainerRef.current.contains(e.target as Node)) {
        setSearchFocused(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const applySearch = useCallback(
    (value: string) => {
      const trimmed = value.trim();
      onSetFilter("query", trimmed.length > 0 ? trimmed : null);
    },
    [onSetFilter],
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
    onSetFilter("query", null);
  };

  const applyPreset = (preset: DatePreset) => {
    setCustomFrom(preset.from);
    setCustomTo(preset.to);
    setShowCustom(false);
    onSetFilter("date", composeDate(preset.from, preset.to));
  };

  const applyCustom = () => {
    if (customFrom && customTo) {
      onSetFilter("date", composeDate(customFrom, customTo));
    } else if (customFrom) {
      onSetFilter("date", composeDate(customFrom, customFrom));
    }
  };

  const clearDate = () => {
    setCustomFrom("");
    setCustomTo("");
    setShowCustom(false);
    onSetFilter("date", null);
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

  const hasDateFilter = Boolean(dateVal.from);
  // Filters excluding query/tag/date/library (those have their own chiclets or are scope)
  const structuralFilters = filters.filter(
    (f) => !["query", "tag", "date", "library", "path"].includes(f.type),
  );
  const hasActiveFilters = structuralFilters.length > 0;

  const showQChiclet = q !== null && q.length > 0;
  const showTagChiclet = tag !== null && tag.length > 0;
  const hasActiveChiclets = showQChiclet || showTagChiclet ||
    hasDateFilter || hasActiveFilters;
  const presets = getDatePresets();

  const selectCls = "rounded-md border border-gray-700 bg-gray-800 px-2 py-1.5 text-xs text-gray-200 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500";

  return (
    <div className="flex flex-col gap-2">
      {/* Row 1: search input + controls + chiclets */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative flex-1 min-w-[200px] max-w-xl" ref={searchContainerRef}>
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
            onFocus={() => setSearchFocused(true)}
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
          {personSuggestions.length > 0 && (
            <div className="absolute left-0 top-full z-50 mt-1 w-full rounded-lg border border-gray-600 bg-gray-800 p-1 shadow-xl">
              <div className="px-2 py-1 text-[10px] font-medium uppercase tracking-wider text-gray-500">
                People
              </div>
              {personSuggestions.map((p) => (
                <button
                  key={p.person_id}
                  type="button"
                  onClick={() => {
                    onSetFilter("person", p.person_id);
                    setInputValue("");
                    onSetFilter("query", null);
                    setSearchFocused(false);
                  }}
                  className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm text-gray-200 hover:bg-gray-700"
                >
                  {p.representative_face_id && (
                    <FaceCropImage faceId={p.representative_face_id} size={28} />
                  )}
                  <span className="flex-1">
                    {p.display_name}
                    <span className="ml-1.5 text-xs text-gray-500">
                      {p.face_count} {p.face_count === 1 ? "photo" : "photos"}
                    </span>
                  </span>
                </button>
              ))}
            </div>
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
                onClick={() => onSetFilter("media", mt === "all" ? null : mt)}
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
            onChange={(e) => onSetSort(e.target.value, dir)}
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
            onClick={() => onSetSort(sort, dir === "asc" ? "desc" : "asc")}
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

        {/* Active chiclets — rendered generically from filter array */}
        <div className="flex flex-wrap items-center gap-2">
          {filters
            .filter((f) => f.type !== "library") // don't show library scope as chiclet
            .map((f) => {
              // Special handling for certain filter types
              if (f.type === "person") {
                return <PersonChiclet key={f.type} personId={f.value} onClear={() => onSetFilter("person", null)} />;
              }
              if (f.type === "date" && dateVal.from && dateVal.to) {
                return <Chiclet key={f.type} label={formatDateChiclet(dateVal.from, dateVal.to)} onClear={clearDate} />;
              }
              if (f.type === "near" && nearVal) {
                return <Chiclet key={f.type} label={`Within ${nearVal.radius}km`} onClear={() => onSetFilter("near", null)} />;
              }
              return (
                <Chiclet
                  key={`${f.type}:${f.value}`}
                  label={filterLabel(f)}
                  onClear={() => onSetFilter(f.type, null)}
                />
              );
            })}
          {hasActiveChiclets && (
            <>
              <button
                type="button"
                onClick={onClearAll}
                className="text-xs text-gray-500 hover:text-gray-300 whitespace-nowrap"
              >
                Clear filters
              </button>
              {onSaveSmartCollection && (
                <button
                  type="button"
                  onClick={onSaveSmartCollection}
                  className="text-xs text-indigo-400 hover:text-indigo-300 whitespace-nowrap"
                >
                  Save collection
                </button>
              )}
            </>
          )}
        </div>
      </div>

      {/* Row 2: date presets */}
      {showDateRow && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-gray-700/50 bg-gray-900/60 px-3 py-2">
          {presets.map((p) => {
            const isActive = dateVal.from === p.from && dateVal.to === p.to;
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
                onSetFilter("camera_make", v || null);
                if (!v) onSetFilter("camera_model", null);
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
              onChange={(v) => onSetFilter("camera_model", v || null)}
            />
          )}

          {/* Lens */}
          {facets && facets.lens_models.length > 0 && (
            <FilterSelect
              label="Lens"
              value={lensModel ?? ""}
              options={facets.lens_models}
              onChange={(v) => onSetFilter("lens", v || null)}
            />
          )}

          {/* ISO range */}
          {facets && facets.iso_range[0] != null && (
            <RangeInputs
              label="ISO"
              min={isoRange.min ?? ""}
              max={isoRange.max ?? ""}
              placeholderMin={String(facets.iso_range[0] ?? "")}
              placeholderMax={String(facets.iso_range[1] ?? "")}
              onChangeMin={(v) => onSetFilter("iso", composeRange(v || null, isoRange.max))}
              onChangeMax={(v) => onSetFilter("iso", composeRange(isoRange.min, v || null))}
            />
          )}

          {/* Aperture range */}
          {facets && facets.aperture_range[0] != null && (
            <RangeInputs
              label="Aperture (f/)"
              min={apertureRange.min ?? ""}
              max={apertureRange.max ?? ""}
              placeholderMin={String(facets.aperture_range[0] ?? "")}
              placeholderMax={String(facets.aperture_range[1] ?? "")}
              onChangeMin={(v) => onSetFilter("aperture", composeRange(v || null, apertureRange.max))}
              onChangeMax={(v) => onSetFilter("aperture", composeRange(apertureRange.min, v || null))}
            />
          )}

          {/* Focal length range */}
          {facets && facets.focal_length_range[0] != null && (
            <RangeInputs
              label="Focal (mm)"
              min={focalLengthRange.min ?? ""}
              max={focalLengthRange.max ?? ""}
              placeholderMin={String(facets.focal_length_range[0] ?? "")}
              placeholderMax={String(facets.focal_length_range[1] ?? "")}
              onChangeMin={(v) => onSetFilter("focal_length", composeRange(v || null, focalLengthRange.max))}
              onChangeMax={(v) => onSetFilter("focal_length", composeRange(focalLengthRange.min, v || null))}
            />
          )}

          {/* GPS toggle */}
          {facets && facets.has_gps_count > 0 && (
            <label className="flex items-center gap-1.5 text-xs text-gray-300 cursor-pointer">
              <input
                type="checkbox"
                checked={hasGps}
                onChange={(e) => onSetFilter("has_gps", e.target.checked ? "yes" : null)}
                className="rounded border-gray-700 bg-gray-800 text-indigo-600 focus:ring-indigo-500"
              />
              Has location ({facets.has_gps_count})
            </label>
          )}

          {/* Faces toggle */}
          {facets && facets.has_face_count > 0 && (
            <label className="flex items-center gap-1.5 text-xs text-gray-300 cursor-pointer">
              <input
                type="checkbox"
                checked={hasFaces}
                onChange={(e) => onSetFilter("has_faces", e.target.checked ? "yes" : null)}
                className="rounded border-gray-700 bg-gray-800 text-indigo-600 focus:ring-indigo-500"
              />
              Has faces ({facets.has_face_count})
            </label>
          )}

          {/* Person filter */}
          <PersonFilterDropdown
            personId={personId}
            onSelect={(pid) => onSetFilter("person", pid)}
          />

          {/* Geo-proximity radius selector (only when near filter is set) */}
          {nearVal && (
            <div className="flex items-center gap-1.5">
              <span className="text-xs text-gray-400">Radius:</span>
              <select
                value={nearVal.radius}
                onChange={(e) => onSetFilter("near", composeNear(nearVal.lat, nearVal.lon, e.target.value))}
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

          {/* Rating filters */}
          <div className="flex flex-wrap items-center gap-3">
            <span className="text-xs font-medium text-gray-400">Rating</span>
            <button
              type="button"
              onClick={() => onSetFilter("favorite", favorite === true ? null : "yes")}
              className={`flex items-center gap-1 rounded-md border px-2 py-1 text-xs transition-colors ${
                favorite === true
                  ? "border-red-500/50 bg-red-900/20 text-red-400"
                  : "border-gray-700 text-gray-400 hover:border-gray-600"
              }`}
            >
              <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill={favorite === true ? "currentColor" : "none"} stroke="currentColor" strokeWidth="2" aria-hidden>
                <path strokeLinecap="round" strokeLinejoin="round" d="M20.84 4.61a5.5 5.5 0 00-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 00-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 000-7.78z" />
              </svg>
              Favorites
            </button>
            <div className="flex items-center gap-1">
              <span className="text-xs text-gray-500">Stars</span>
              {[1, 2, 3, 4, 5].map((n) => {
                const min = starVal.min != null ? Number(starVal.min) : null;
                const max = starVal.max != null ? Number(starVal.max) : null;
                const isActive = min != null && max != null && n >= min && n <= max;
                return (
                  <button
                    key={n}
                    type="button"
                    onClick={() => {
                      let newMin: string | null;
                      let newMax: string | null;
                      if (min == null || max == null) {
                        newMin = String(n); newMax = String(n);
                      } else if (n < min) {
                        newMin = String(n); newMax = String(max);
                      } else if (n > max) {
                        newMin = String(min); newMax = String(n);
                      } else if (n === min && n === max) {
                        newMin = null; newMax = null;
                      } else if (n === min) {
                        newMin = String(n + 1); newMax = String(max);
                      } else if (n === max) {
                        newMin = String(min); newMax = String(n - 1);
                      } else {
                        newMin = String(n); newMax = String(max);
                      }
                      onSetFilter("stars", composeRange(newMin, newMax));
                    }}
                    className={`transition-colors ${isActive ? "text-amber-400" : "text-gray-600 hover:text-amber-300"}`}
                    title={`${n} stars`}
                  >
                    <svg className="h-4 w-4" viewBox="0 0 24 24" fill={isActive ? "currentColor" : "none"} stroke="currentColor" strokeWidth="2" aria-hidden>
                      <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z" />
                    </svg>
                  </button>
                );
              })}
            </div>
            <div className="flex items-center gap-1">
              <span className="text-xs text-gray-500">Color</span>
              {RATING_COLORS.map((c) => (
                <button
                  key={c}
                  type="button"
                  onClick={() => onSetFilter("color", colorFilter === c ? null : c)}
                  className={`h-4 w-4 rounded-full transition-all ${
                    colorFilter === c
                      ? "ring-2 ring-white/70 ring-offset-1 ring-offset-gray-900"
                      : "hover:ring-1 hover:ring-white/30 hover:ring-offset-1 hover:ring-offset-gray-900"
                  }`}
                  style={{ backgroundColor: COLOR_HEX[c] }}
                  title={c.charAt(0).toUpperCase() + c.slice(1)}
                />
              ))}
            </div>
          </div>

          {hasActiveFilters && (
            <FilterMenu
              onClearAll={onClearAll}
              onSaveSmartCollection={onSaveSmartCollection}
            />
          )}
        </div>
      )}
    </div>
  );
}

function FilterMenu({
  onClearAll,
  onSaveSmartCollection,
}: {
  onClearAll: () => void;
  onSaveSmartCollection?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  return (
    <div ref={ref} className="relative ml-auto">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="rounded px-2 py-1 text-xs text-gray-500 hover:bg-gray-700 hover:text-gray-300"
        title="Filter actions"
      >
        ...
      </button>
      {open && (
        <div className="absolute right-0 top-full z-50 mt-1 min-w-[200px] rounded-md border border-gray-700 bg-gray-800 py-1 shadow-lg">
          <button
            type="button"
            onClick={() => { onClearAll(); setOpen(false); }}
            className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-gray-300 hover:bg-gray-700"
          >
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden>
              <path d="M18.36 5.64l-12.72 12.72M5.64 5.64l12.72 12.72" />
            </svg>
            Clear all filters
          </button>
          {onSaveSmartCollection && (
            <button
              type="button"
              onClick={() => { onSaveSmartCollection(); setOpen(false); }}
              className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-gray-300 hover:bg-gray-700"
            >
              <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden>
                <path d="M12 5v14M5 12h14" />
              </svg>
              Save as Smart Collection
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
          onChange={(e) => onChangeMin(e.target.value)}
          placeholder={placeholderMin}
          className="w-16 rounded-md border border-gray-700 bg-gray-800 px-2 py-1.5 text-xs text-gray-200 placeholder:text-gray-600 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
        />
        <span className="text-xs text-gray-500">–</span>
        <input
          type="number"
          value={max}
          onChange={(e) => onChangeMax(e.target.value)}
          placeholder={placeholderMax}
          className="w-16 rounded-md border border-gray-700 bg-gray-800 px-2 py-1.5 text-xs text-gray-200 placeholder:text-gray-600 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
        />
      </div>
    </div>
  );
}
