import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getCurrentUser, getTenantSettings, updateTenantSettings, type TenantSettings } from "../../api/client";

const MAX_SECONDS = 86_400;

function parseSeconds(text: string): number | null {
  if (!/^\d+$/.test(text.trim())) return null;
  const n = Number(text);
  return n >= 1 && n <= MAX_SECONDS ? n : null;
}

function describe(cap: number | null): string {
  return cap == null ? "whole video" : `first ${cap} seconds`;
}

/** One audience's choice, starting from what's saved: the whole video, or the first N seconds. */
function useCapField(saved: number | null) {
  const [capped, setCapped] = useState(saved != null);
  const [text, setText] = useState(saved != null ? String(saved) : "30");
  const seconds = parseSeconds(text);
  return { capped, setCapped, text, setText, seconds, value: capped ? seconds : null, valid: !capped || seconds != null };
}

const inputClass =
  "w-24 rounded-md border border-gray-700 bg-gray-950 px-2 py-1.5 text-sm text-gray-100 disabled:opacity-50";

/** Account-wide: how much of each video plays, signed in and on public pages. Admins change it. */
export default function PlaybackSection() {
  const { data: user } = useQuery({ queryKey: ["settings", "me"], queryFn: getCurrentUser });
  const { data: settings, isLoading } = useQuery({ queryKey: ["tenant-settings"], queryFn: getTenantSettings });
  if (isLoading || !settings) {
    return <div className="h-32 rounded-lg border border-gray-700/50 bg-gray-900/50 animate-pulse" />;
  }
  // Built once the settings are here, so the choices start from them.
  return <PlaybackForm settings={settings} isAdmin={user?.role === "admin"} />;
}

function PlaybackForm({ settings, isAdmin }: { settings: TenantSettings; isAdmin: boolean }) {
  const queryClient = useQueryClient();
  const signedIn = useCapField(settings.video_preview_max_seconds);
  const pub = useCapField(settings.public_video_preview_max_seconds);
  const [saved, setSaved] = useState(false);

  const save = useMutation({
    mutationFn: (next: { video_preview_max_seconds: number | null; public_video_preview_max_seconds: number | null }) =>
      updateTenantSettings(next),
    onSuccess: (next) => {
      queryClient.setQueryData(["tenant-settings"], next);
      queryClient.invalidateQueries({ queryKey: ["playback"] });
      setSaved(true);
    },
  });

  const changed =
    signedIn.value !== settings.video_preview_max_seconds || pub.value !== settings.public_video_preview_max_seconds;
  const canSave = isAdmin && signedIn.valid && pub.valid && changed && !save.isPending;
  const edited = () => setSaved(false);
  const publicOverSignedIn = signedIn.value != null && (pub.value == null || pub.value > signedIn.value);

  return (
    <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-6 space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-gray-100">Playback</h2>
        <p className="mt-1 text-sm text-gray-400">How much of each video plays.</p>
      </div>

      {isAdmin ? (
        <form
          className="space-y-5"
          onSubmit={(e) => {
            e.preventDefault();
            if (canSave) {
              save.mutate({ video_preview_max_seconds: signedIn.value, public_video_preview_max_seconds: pub.value });
            }
          }}
        >
          <fieldset className="space-y-3">
            <legend className="text-sm font-medium text-gray-300">Signed in</legend>
            <label className="flex items-center gap-2 text-sm text-gray-200">
              <input type="radio" name="signed-in" checked={!signedIn.capped}
                onChange={() => { signedIn.setCapped(false); edited(); }} />
              Whole video
            </label>
            <div className="flex flex-wrap items-center gap-2 text-sm text-gray-200">
              <label className="flex items-center gap-2">
                <input type="radio" name="signed-in" checked={signedIn.capped}
                  onChange={() => { signedIn.setCapped(true); edited(); }} />
                First
              </label>
              <input type="text" inputMode="numeric" aria-label="Seconds" value={signedIn.text}
                disabled={!signedIn.capped} className={inputClass}
                onChange={(e) => { signedIn.setText(e.target.value); signedIn.setCapped(true); edited(); }} />
              <span>seconds</span>
            </div>
          </fieldset>

          <fieldset className="space-y-3">
            <legend className="text-sm font-medium text-gray-300">Public pages</legend>
            <div className="flex flex-wrap items-center gap-2 text-sm text-gray-200">
              <label className="flex items-center gap-2">
                <input type="radio" name="public" checked={pub.capped} aria-label="Public pages: first"
                  onChange={() => { pub.setCapped(true); edited(); }} />
                Show the first
              </label>
              <input type="text" inputMode="numeric" aria-label="Public seconds" value={pub.text}
                disabled={!pub.capped} className={inputClass}
                onChange={(e) => { pub.setText(e.target.value); pub.setCapped(true); edited(); }} />
              <span>seconds</span>
            </div>
            <label className="flex items-center gap-2 text-sm text-gray-200">
              <input type="radio" name="public" checked={!pub.capped} aria-label="Public pages: whole video"
                onChange={() => { pub.setCapped(false); edited(); }} />
              Show the whole video
            </label>
            {publicOverSignedIn && (
              <p className="text-xs text-gray-500">
                Public pages never play more than signed-in people get: {describe(signedIn.value)}.
              </p>
            )}
          </fieldset>

          {(!signedIn.valid || !pub.valid) && (
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
          <p className="text-sm text-gray-200">Signed in: {describe(settings.video_preview_max_seconds)}</p>
          <p className="text-sm text-gray-200">Public pages: {describe(settings.public_video_preview_max_seconds)}</p>
          <p className="text-sm text-gray-500">Only admins can change this.</p>
        </div>
      )}
      <p className="text-xs text-gray-500">
        Until a video's full-length proxy is ready, its 10-second preview plays.
      </p>
    </div>
  );
}
