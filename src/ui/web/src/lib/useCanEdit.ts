import { useQuery } from "@tanstack/react-query";
import { getCurrentUser } from "../api/client";

/** The signed-in person's role ("admin", "editor", "viewer"); undefined until known,
 * and when `enabled` is false (no request, as on public pages). */
export function useRole(enabled = true): string | undefined {
  const { data } = useQuery({
    queryKey: ["settings", "me"],
    queryFn: getCurrentUser,
    enabled,
    staleTime: 60_000,
  });
  return enabled ? data?.role : undefined;
}

/** Whether the person signed in may change clips (archive, trash, restore): an editor or an admin. */
export function useCanEdit(enabled = true): boolean {
  const role = useRole(enabled);
  return role === "admin" || role === "editor";
}
