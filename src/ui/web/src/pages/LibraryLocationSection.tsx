import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, getLibrary, setLibraryInferLocation } from "../api/client";
import { runsIt } from "../lib/libraryRoles";

/** Whether the library's clips without a location are placed from those taken around the same time (ADR-017's
 * Infer location, each library's). Its admin turns it on or off; turning it off removes its guesses, asked first
 * (409 guesses_exist). Everyone else who sees the library sees whether it's on. */
export default function LibraryLocationSection({ libraryId }: { libraryId: string }) {
  const queryClient = useQueryClient();
  const { data: library } = useQuery({ queryKey: ["library", libraryId], queryFn: () => getLibrary(libraryId) });
  // How many clips' guesses turning it off removes, while that's asked.
  const [asking, setAsking] = useState<number | null>(null);
  const change = useMutation({
    mutationFn: ({ on, remove = false }: { on: boolean; remove?: boolean }) =>
      setLibraryInferLocation(libraryId, on, remove),
    onSuccess: () => {
      setAsking(null);
      return Promise.all([
        queryClient.invalidateQueries({ queryKey: ["library", libraryId] }),
        queryClient.invalidateQueries({ queryKey: ["libraries"] }),
        queryClient.invalidateQueries({ queryKey: ["producers"] }),
      ]);
    },
    onError: (e) => {
      if (e instanceof ApiError && e.code === "guesses_exist") setAsking(Number(e.details?.clips ?? 0));
    },
  });
  // A server from before Infer location was each library's doesn't say.
  if (!library || library.infer_location === undefined) return null;
  const on = library.infer_location;
  const error = change.error instanceof ApiError && change.error.code === "guesses_exist" ? null : change.error;

  return (
    <div className="space-y-2 rounded-lg border border-gray-700/50 bg-gray-900/50 px-4 py-3" aria-label="Location guesses">
      <h2 className="text-lg font-semibold text-gray-100">Location guesses</h2>
      <p className="text-sm text-gray-400">
        Photos and videos without a location are placed from the ones in this library taken around the same time
        with one, such as a phone&apos;s. A guess never replaces a location from the file or one a person set, and
        shows as a guess.
      </p>
      {runsIt(library.role ?? undefined) ? (
        <label className="flex items-center gap-2 text-sm text-gray-200">
          <input
            type="checkbox"
            checked={on}
            disabled={change.isPending || asking !== null}
            onChange={(e) => change.mutate({ on: e.target.checked })}
          />
          Infer location
        </label>
      ) : (
        <p className="text-sm text-gray-300">Infer location: {on ? "On" : "Off"}</p>
      )}
      {asking !== null && (
        <div role="alert" className="space-y-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm">
          <p className="text-amber-100">
            Turning it off removes the location guesses of {asking.toLocaleString()} {asking === 1 ? "clip" : "clips"}{" "}
            in this library. A location a person set stays.
          </p>
          <div className="flex flex-wrap gap-4">
            <button
              type="button"
              disabled={change.isPending}
              onClick={() => change.mutate({ on: false, remove: true })}
              className="text-indigo-300 hover:underline disabled:opacity-50"
            >
              Remove guesses
            </button>
            <button type="button" onClick={() => setAsking(null)} className="text-indigo-300 hover:underline">
              Keep it on
            </button>
          </div>
        </div>
      )}
      {error && (
        <p role="alert" className="text-sm text-red-300">
          {error instanceof ApiError ? error.message : "Couldn't reach the server. Try again."}
        </p>
      )}
    </div>
  );
}
