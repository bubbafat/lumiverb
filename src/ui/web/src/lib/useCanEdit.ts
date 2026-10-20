import { useQuery } from "@tanstack/react-query";
import { getCurrentUser } from "../api/client";

function useMe(enabled: boolean) {
  const { data } = useQuery({
    queryKey: ["settings", "me"],
    queryFn: getCurrentUser,
    enabled,
    staleTime: 60_000,
  });
  return enabled ? data : undefined;
}

/** The signed-in person's role ("admin", "editor", "viewer"); undefined until known,
 * and when `enabled` is false (no request, as on public pages). */
export function useRole(enabled = true): string | undefined {
  return useMe(enabled)?.role;
}

/** Trying it as a viewer (a demo server's session, which reads only): nothing that changes
 * anything is offered, a rating, a saved search or a project's clips included. */
export function useIsDemo(enabled = true): boolean {
  return useMe(enabled)?.demo === true;
}

/** Whether the person signed in may change clips (archive, trash, restore): an editor or an admin. */
export function useCanEdit(enabled = true): boolean {
  const role = useRole(enabled);
  return role === "admin" || role === "editor";
}
