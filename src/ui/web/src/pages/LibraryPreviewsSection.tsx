import { useQuery, useQueryClient } from "@tanstack/react-query";
import { getCurrentUser, getLibraryPreviews, moveLibraryPreviews, type LibraryPreviews } from "../api/client";
import { MoveChoice, Moving, STORAGE_QUERY_KEY, free } from "./settings/StorageSection";

/** Where a library's previews are: the server's folder (Settings › Storage) or
 * one of its own (a big video library on a drive of its own). Admins only;
 * moved on the machine itself. */
export default function LibraryPreviewsSection({ libraryId }: { libraryId: string }) {
  const queryClient = useQueryClient();
  const { data: user } = useQuery({ queryKey: ["settings", "me"], queryFn: getCurrentUser });
  const key = ["library-previews", libraryId];
  const { data } = useQuery({
    queryKey: key,
    queryFn: () => getLibraryPreviews(libraryId),
    enabled: user?.role === "admin",
    refetchInterval: (q) => (q.state.data?.moving ? 2_000 : 60_000),
  });
  if (!data) return null;

  const moved = (next: LibraryPreviews) => {
    queryClient.setQueryData(key, next);
    void queryClient.invalidateQueries({ queryKey: STORAGE_QUERY_KEY });
  };

  return (
    <section aria-labelledby="library-previews" className="rounded-lg border border-gray-700/50 bg-gray-900/50 px-4 py-3 space-y-2">
      <h2 id="library-previews" className="text-lg font-semibold text-gray-100">Previews</h2>
      <p className="text-sm text-gray-400">
        {data.own ? "A folder of its own:" : "In the server's previews folder (Settings › Storage):"}
      </p>
      <p className="break-all font-mono text-sm text-gray-200">{data.folder}</p>
      {!data.available ? (
        <p role="alert" className="text-sm text-red-300">
          This folder isn&apos;t there. Is its drive plugged in? Until it is, this library&apos;s previews don&apos;t show.
        </p>
      ) : (
        <p className="text-xs text-gray-500">{free(data.free, data.total)}</p>
      )}
      {data.own && data.same_drive_as_originals && (
        <p className="text-sm text-amber-200">
          On the same drive as its originals: when that drive is unplugged, the library can&apos;t be browsed.
        </p>
      )}
      {data.moving ? (
        <Moving move={data.moving} what="its previews" onTriedAgain={() => void queryClient.invalidateQueries({ queryKey: key })} />
      ) : (
        <MoveChoice
          changeableHere={data.changeable_here}
          backLabel={data.own ? "Back to the server's folder…" : null}
          ask={(to, go) => moveLibraryPreviews(libraryId, to, go)}
          onMoving={moved}
        />
      )}
    </section>
  );
}
