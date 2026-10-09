import type { HealthState } from "../api/client";

const COLOR: Record<HealthState, string> = {
  green: "bg-emerald-500",
  yellow: "bg-amber-400",
  red: "bg-red-500",
};

const SAYS: Record<HealthState, string> = {
  green: "OK",
  yellow: "Needs a look",
  red: "Not working",
};

/** A green, yellow or red dot, named for screen readers. */
export function HealthDot({ state, label, className = "" }: { state: HealthState; label?: string; className?: string }) {
  return (
    <span
      role="img"
      aria-label={label ?? SAYS[state]}
      title={label ?? SAYS[state]}
      className={`inline-block h-2 w-2 shrink-0 rounded-full ${COLOR[state]} ${className}`}
    />
  );
}

/** The Admin link's dot over its icon, when the system needs a look. */
export function AdminAlertDot({ alert }: { alert: "yellow" | "red" | null }) {
  if (!alert) return null;
  return (
    <HealthDot
      state={alert}
      label={alert === "red" ? "Something isn't working: see Admin" : "Something needs a look: see Admin"}
      className="absolute -right-0.5 -top-0.5 ring-2 ring-gray-950"
    />
  );
}
