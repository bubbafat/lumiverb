import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getCurrentUser, getTenantSettings, updateTenantSettings } from "../../api/client";

const MAX_SECONDS = 86_400;

function parseSeconds(text: string): number | null {
  if (!/^\d+$/.test(text.trim())) return null;
  const n = Number(text);
  return n >= 1 && n <= MAX_SECONDS ? n : null;
}

/** Account-wide: how much of each video plays. Admins change it; everyone sees it. */
export default function PlaybackSection() {
  const queryClient = useQueryClient();
  const { data: user } = useQuery({ queryKey: ["settings", "me"], queryFn: getCurrentUser });
  const { data: settings, isLoading } = useQuery({ queryKey: ["tenant-settings"], queryFn: getTenantSettings });
  const [capped, setCapped] = useState(false);
  const [secondsText, setSecondsText] = useState("30");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (!settings) return;
    setCapped(settings.video_preview_max_seconds != null);
    if (settings.video_preview_max_seconds != null) setSecondsText(String(settings.video_preview_max_seconds));
  }, [settings]);

  const save = useMutation({
    mutationFn: (value: number | null) => updateTenantSettings({ video_preview_max_seconds: value }),
    onSuccess: (next) => {
      queryClient.setQueryData(["tenant-settings"], next);
      queryClient.invalidateQueries({ queryKey: ["playback"] });
      setSaved(true);
    },
  });

  if (isLoading || !settings) {
    return <div className="h-32 rounded-lg border border-gray-700/50 bg-gray-900/50 animate-pulse" />;
  }

  const current = settings.video_preview_max_seconds;
  const isAdmin = user?.role === "admin";
  const seconds = parseSeconds(secondsText);
  const value = capped ? seconds : null;
  const canSave = isAdmin && (!capped || seconds != null) && value !== current && !save.isPending;

  return (
    <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-6 space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-gray-100">Playback</h2>
        <p className="mt-1 text-sm text-gray-400">
          How much of each video plays, for everyone in this account and on public pages.
        </p>
      </div>

      {isAdmin ? (
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (canSave) save.mutate(value);
          }}
        >
          <label className="flex items-center gap-2 text-sm text-gray-200">
            <input
              type="radio"
              name="video-length"
              checked={!capped}
              onChange={() => {
                setCapped(false);
                setSaved(false);
              }}
            />
            Whole video
          </label>
          <div className="flex flex-wrap items-center gap-2 text-sm text-gray-200">
            <label className="flex items-center gap-2">
              <input
                type="radio"
                name="video-length"
                checked={capped}
                onChange={() => {
                  setCapped(true);
                  setSaved(false);
                }}
              />
              First
            </label>
            <input
              type="text"
              inputMode="numeric"
              aria-label="Seconds"
              value={secondsText}
              disabled={!capped}
              onChange={(e) => {
                setSecondsText(e.target.value);
                setCapped(true);
                setSaved(false);
              }}
              className="w-24 rounded-md border border-gray-700 bg-gray-950 px-2 py-1.5 text-sm text-gray-100 disabled:opacity-50"
            />
            <span>seconds</span>
          </div>
          {capped && seconds == null && (
            <p className="text-sm text-amber-300">Enter a whole number of seconds, from 1 to {MAX_SECONDS.toLocaleString()}.</p>
          )}
          <div className="flex items-center gap-3">
            <button
              type="submit"
              disabled={!canSave}
              className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Save
            </button>
            {saved && !save.isPending && <span className="text-sm text-emerald-300">Saved</span>}
            {save.isError && <span className="text-sm text-red-300">Couldn't save. Try again.</span>}
          </div>
        </form>
      ) : (
        <div className="space-y-1">
          <p className="text-sm text-gray-200">{current == null ? "Whole video" : `First ${current} seconds`}</p>
          <p className="text-sm text-gray-500">Only admins can change this.</p>
        </div>
      )}
      <p className="text-xs text-gray-500">
        Until a video's full-length proxy is ready, its 10-second preview plays.
      </p>
    </div>
  );
}
