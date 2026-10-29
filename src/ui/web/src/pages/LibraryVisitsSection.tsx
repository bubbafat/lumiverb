import { useQuery } from "@tanstack/react-query";
import { getCurrentUser, getLibrary, getLibraryVisits } from "../api/client";
import { VisitsPanel } from "../components/VisitsPanel";
import { roleOn, runsIt } from "../lib/libraryRoles";

/** A public library's visits (ADR-025): its admins'. Shown while it's public. */
export default function LibraryVisitsSection({ libraryId }: { libraryId: string }) {
  const { data: user } = useQuery({ queryKey: ["settings", "me"], queryFn: getCurrentUser });
  const { data: library } = useQuery({ queryKey: ["library", libraryId], queryFn: () => getLibrary(libraryId) });
  if (!library || !library.is_public || !runsIt(roleOn(library, user?.role))) return null;
  return (
    <div className="space-y-2 rounded-lg border border-gray-700/50 bg-gray-900/50 px-4 py-3" aria-label="Visits">
      <h2 className="text-lg font-semibold text-gray-100">Visits</h2>
      <VisitsPanel queryKey={["library-visits", libraryId]} load={(days) => getLibraryVisits(libraryId, days)} />
    </div>
  );
}
