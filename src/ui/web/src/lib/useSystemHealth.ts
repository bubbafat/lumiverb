import { useQuery } from "@tanstack/react-query";
import { getApiKey, getSystemHealth, type HealthState, type SystemHealth } from "../api/client";

export const SYSTEM_HEALTH_KEY = ["system", "health"] as const;

/** How often the nav asks; the Admin page asks every 10 seconds while it's open. */
export const NAV_HEALTH_INTERVAL = 30_000;

/** The system's health (GET /v1/system/health), for anyone signed in. */
export function useSystemHealth(refetchInterval: number = NAV_HEALTH_INTERVAL) {
  return useQuery({
    queryKey: SYSTEM_HEALTH_KEY,
    queryFn: getSystemHealth,
    enabled: Boolean(getApiKey()),
    refetchInterval,
  });
}

/** The Admin link's dot: yellow or red when something needs a look; red when the API doesn't answer. */
export function healthAlert(health: SystemHealth | undefined, failed: boolean): Exclude<HealthState, "green"> | null {
  if (failed) return "red";
  if (!health || health.state === "green") return null;
  return health.state;
}
