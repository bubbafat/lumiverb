import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  getCurrentUser,
  getSchedulerStatus,
  listLibraries,
  pauseScans,
  resumeScans,
  scanNow,
} from "../api/client";
import type { ReaderCheckIn } from "../api/types";
import { timeAgo } from "../lib/format";
import { QUIET_MS, remakesHeld, toUpload } from "../lib/readers";

/** Who reads a library's files and whether its scans run (ADR-018 phase 1), as Settings → Processing says it for
 * every library: its own Scans pause, or Pause all's. When its reader last checked in and how it was (phase 4).
 * Editors ask for a scan of the whole library (Scan now); admins pause and resume this library's scans. */
export default function LibraryScansSection({ libraryId }: { libraryId: string }) {
  const queryClient = useQueryClient();
  const { data: user } = useQuery({ queryKey: ["settings", "me"], queryFn: getCurrentUser });
  const { data: status } = useQuery({ queryKey: ["scheduler-status"], queryFn: getSchedulerStatus, refetchInterval: 5_000 });
  const { data: libraries } = useQuery({
    queryKey: ["libraries", false], queryFn: () => listLibraries(false), refetchInterval: 15_000 });
  const row = status?.libraries?.find((l) => l.library_id === libraryId);
  const library = libraries?.find((l) => l.library_id === libraryId);
  const refresh = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ["scheduler-status"] }),
      queryClient.invalidateQueries({ queryKey: ["libraries"] }),
      queryClient.invalidateQueries({ queryKey: ["producers"] }),
    ]);
  const toggle = useMutation({
    mutationFn: (pause: boolean) => (pause ? pauseScans([libraryId]) : resumeScans([libraryId])),
    onSuccess: refresh,
  });
  const ask = useMutation({ mutationFn: () => scanNow(libraryId), onSuccess: refresh });
  // The scheduler's status lists the libraries this server reads; a device's is said from the library itself.
  if (!row && !library) return null;
  const paused = row?.paused ?? Boolean(library?.scans_paused);
  const reader = row?.reader ?? library?.reader ?? "server";
  const asked = Boolean(library?.scan_requested_at);
  const error = toggle.error ?? ask.error;
  return (
    <div className="space-y-2 rounded-lg border border-gray-700/50 bg-gray-900/50 px-4 py-3" aria-label="Scans">
      <h2 className="text-lg font-semibold text-gray-100">Scans</h2>
      <p className="text-sm text-gray-400">
        {reader === "server" ? "This server reads its files." : `${library?.reader_name ?? "A device"} reads its files.`}{" "}
        {paused
          ? "Its scans are paused: no new or changed files are found in it. Everything else goes on."
          : "Its scans find new and changed files."}
      </p>
      {library?.checked_in && <CheckedIn seen={library.checked_in} />}
      {asked && !paused && (
        <p className="text-sm text-gray-400">A scan of the whole library is asked for: it starts within a minute.</p>
      )}
      <div className="flex flex-wrap gap-2">
        {user && user.role !== "viewer" && !paused && (
          <button
            type="button"
            disabled={ask.isPending || asked}
            onClick={() => ask.mutate()}
            className="rounded-lg border border-gray-700 px-3 py-1 text-sm text-gray-200 hover:border-gray-500 disabled:opacity-50"
          >
            {asked ? "Scan asked for" : "Scan now"}
          </button>
        )}
        {user?.role === "admin" && (
          <button
            type="button"
            disabled={toggle.isPending}
            onClick={() => toggle.mutate(!paused)}
            className="rounded-lg border border-gray-700 px-3 py-1 text-sm text-gray-200 hover:border-gray-500 disabled:opacity-50"
          >
            {paused ? "Resume scans" : "Pause scans"}
          </button>
        )}
      </div>
      {error && (
        <p role="alert" className="text-sm text-red-300">
          {error instanceof ApiError ? error.message : "Couldn't reach the server. Try again."}
        </p>
      )}
    </div>
  );
}

/** "This server checked in 1 minute ago: scanning now, 212 files (14 GB) to upload. Last full scan 3 minutes ago." */
function CheckedIn({ seen }: { seen: ReaderCheckIn }) {
  const who = seen.client === "this server" ? "This server" : seen.client;
  const quiet = Date.now() - new Date(seen.checked_in_at).getTime() > QUIET_MS;
  const how = [{
    scanning: "scanning now",
    idle: "",
    paused: "",
    away: "it can't reach the library's storage",
  }[seen.state], toUpload(seen), remakesHeld(seen)].filter(Boolean).join(", ");
  return (
    <p className={`text-sm ${quiet ? "text-amber-300" : "text-gray-400"}`}>
      {quiet
        ? `${who} hasn't checked in since ${timeAgo(seen.checked_in_at)}.`
        : `${who} checked in ${timeAgo(seen.checked_in_at)}${how ? `: ${how}` : ""}.`}
      {seen.last_full_scan_at && ` Last full scan ${timeAgo(seen.last_full_scan_at)}.`}
    </p>
  );
}
