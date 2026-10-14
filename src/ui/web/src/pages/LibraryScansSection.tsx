import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, getCurrentUser, getSchedulerStatus, pauseScans, resumeScans } from "../api/client";

/** Who reads a library's files and whether its scans run (ADR-018 phase 1), as Settings → Processing says it for
 * every library: its own Scans pause, or Pause all's. Admins pause and resume this library's scans. */
export default function LibraryScansSection({ libraryId }: { libraryId: string }) {
  const queryClient = useQueryClient();
  const { data: user } = useQuery({ queryKey: ["settings", "me"], queryFn: getCurrentUser });
  const { data: status } = useQuery({ queryKey: ["scheduler-status"], queryFn: getSchedulerStatus, refetchInterval: 5_000 });
  const row = status?.libraries?.find((l) => l.library_id === libraryId);
  const toggle = useMutation({
    mutationFn: (pause: boolean) => (pause ? pauseScans([libraryId]) : resumeScans([libraryId])),
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: ["scheduler-status"] }),
        queryClient.invalidateQueries({ queryKey: ["libraries"] }),
        queryClient.invalidateQueries({ queryKey: ["producers"] }),
      ]),
  });
  if (!row) return null;
  return (
    <div className="space-y-2 rounded-lg border border-gray-700/50 bg-gray-900/50 px-4 py-3" aria-label="Scans">
      <h2 className="text-lg font-semibold text-gray-100">Scans</h2>
      <p className="text-sm text-gray-400">
        {row.reader === "server" ? "This server reads its files." : `${row.reader} reads its files.`}{" "}
        {row.paused
          ? "Its scans are paused: no new or changed files are found in it. Everything else goes on."
          : "Its scans find new and changed files."}
      </p>
      {user?.role === "admin" && (
        <button
          type="button"
          disabled={toggle.isPending}
          onClick={() => toggle.mutate(!row.paused)}
          className="rounded-lg border border-gray-700 px-3 py-1 text-sm text-gray-200 hover:border-gray-500 disabled:opacity-50"
        >
          {row.paused ? "Resume scans" : "Pause scans"}
        </button>
      )}
      {toggle.error && (
        <p role="alert" className="text-sm text-red-300">
          {toggle.error instanceof ApiError ? toggle.error.message : "Couldn't reach the server. Try again."}
        </p>
      )}
    </div>
  );
}
