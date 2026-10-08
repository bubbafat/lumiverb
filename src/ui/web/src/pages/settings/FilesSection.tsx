import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, getCurrentUser, getTenantSettings, updateTenantSettings, type TenantSettings } from "../../api/client";

const ON_TITLE = "Follow moves and renames";
const ON_TEXT =
  "The same file is the same asset wherever it goes. A file you move or rename keeps its notes, ratings and " +
  "projects; so does the copy left when you copy a file and delete the original.";
const OFF_TITLE = "Every path is its own file";
const OFF_TEXT =
  "A file at a new path starts fresh, without the notes, ratings or projects it had. Its old asset is " +
  "archived, and comes back only if the file returns to its old path.";

/** Account-wide: whether the same content is the same asset. Admins change it. */
export default function FilesSection() {
  const { data: user } = useQuery({ queryKey: ["settings", "me"], queryFn: getCurrentUser });
  const { data: settings, isLoading } = useQuery({ queryKey: ["tenant-settings"], queryFn: getTenantSettings });
  if (isLoading || !settings) {
    return <div className="h-32 rounded-lg border border-gray-700/50 bg-gray-900/50 animate-pulse" />;
  }
  // Built once the settings are here, so the choice starts from them.
  return (
    <div className="space-y-6">
      <FilesForm settings={settings} isAdmin={user?.role === "admin"} />
      <TrashForm settings={settings} isAdmin={user?.role === "admin"} />
    </div>
  );
}

const MAX_TRASH_DAYS = 3650;

/** How long the trash keeps clips, libraries and projects before deleting them for good. */
function TrashForm({ settings, isAdmin }: { settings: TenantSettings; isAdmin: boolean }) {
  const queryClient = useQueryClient();
  const savedDays = settings.trash_days === undefined ? 30 : settings.trash_days; // older servers: 30
  const [auto, setAuto] = useState(savedDays !== null);
  const [days, setDays] = useState(String(savedDays ?? 30));
  const [saved, setSaved] = useState(false);
  // Fewer days would delete these at once: the server asked first.
  const [ask, setAsk] = useState<{ clips: number; libraries: number; projects: number } | null>(null);

  const save = useMutation({
    mutationFn: ({ value, confirm }: { value: number | null; confirm: boolean }) =>
      updateTenantSettings({ trash_days: value, ...(confirm ? { confirm_purge: true } : {}) }),
    onSuccess: (next) => {
      queryClient.setQueryData(["tenant-settings"], next);
      setAsk(null);
      setSaved(true);
    },
    onError: (err) => {
      if (err instanceof ApiError && err.code === "trash_days_shortened" && err.details) {
        const d = err.details as Record<string, number>;
        setAsk({ clips: d.clips ?? 0, libraries: d.libraries ?? 0, projects: d.projects ?? 0 });
      }
    },
  });

  const parsed = /^[0-9]+$/.test(days.trim()) ? Number(days.trim()) : NaN;
  const valid = !auto || (parsed >= 1 && parsed <= MAX_TRASH_DAYS);
  const value = auto ? parsed : null;
  const canSave = isAdmin && valid && value !== savedDays && !save.isPending;
  const describe = (d: number | null) =>
    d === null ? "Kept until someone deletes it for good." : `Deleted for good after ${d} ${d === 1 ? "day" : "days"}.`;
  const what = ask
    ? [
        ask.clips ? (ask.clips === 1 ? "1 clip" : `${ask.clips.toLocaleString()} clips`) : "",
        ask.libraries ? (ask.libraries === 1 ? "1 library" : `${ask.libraries} libraries`) : "",
        ask.projects ? (ask.projects === 1 ? "1 project" : `${ask.projects} projects`) : "",
      ].filter(Boolean).join(", ")
    : "";

  return (
    <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-6 space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-gray-100">Trash</h2>
        <p className="mt-1 text-sm text-gray-400">
          What happens to clips, libraries and projects in the trash. Archived clips aren&apos;t deleted on their
          own; they go only with their library.
        </p>
      </div>
      {isAdmin ? (
        <form
          aria-label="Trash"
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (canSave) save.mutate({ value, confirm: false });
          }}
        >
          <fieldset className="space-y-3">
            <legend className="sr-only">Emptying the trash</legend>
            <label className="flex items-start gap-3 text-sm text-gray-200">
              <input
                type="radio"
                name="trash-days"
                className="mt-1"
                checked={auto}
                onChange={() => {
                  setAuto(true);
                  setSaved(false);
                  setAsk(null);
                }}
                aria-labelledby="trash-auto-title"
              />
              <span>
                <span id="trash-auto-title" className="font-medium">Delete for good after</span>{" "}
                <input
                  type="text"
                  inputMode="numeric"
                  aria-label="Days in the trash"
                  value={days}
                  disabled={!auto}
                  onChange={(e) => {
                    setDays(e.target.value);
                    setSaved(false);
                    setAsk(null);
                  }}
                  className="mx-1 w-16 rounded border border-gray-700 bg-gray-800 px-2 py-0.5 text-gray-100 disabled:opacity-50"
                />{" "}
                days <span className="text-gray-500">(30 by default)</span>
                <span className="mt-0.5 block text-gray-400">
                  Anything in the trash longer is deleted for good, with its previews and analysis. Until then it
                  can be restored.
                </span>
              </span>
            </label>
            <label className="flex items-start gap-3 text-sm text-gray-200">
              <input
                type="radio"
                name="trash-days"
                className="mt-1"
                checked={!auto}
                onChange={() => {
                  setAuto(false);
                  setSaved(false);
                }}
                aria-labelledby="trash-manual-title"
                aria-describedby="trash-manual"
              />
              <span>
                <span id="trash-manual-title" className="font-medium">Only when emptied by hand</span>
                <span id="trash-manual" className="mt-0.5 block text-gray-400">
                  The trash keeps everything until someone deletes it for good.
                </span>
              </span>
            </label>
          </fieldset>
          {auto && !valid && (
            <p role="alert" className="text-sm text-red-300">
              Give a whole number of days from 1 to {MAX_TRASH_DAYS.toLocaleString()}.
            </p>
          )}
          {ask && (
            <div role="alertdialog" aria-label="Delete them now?" className="rounded-lg border border-amber-800/50 bg-amber-950/30 p-3 text-sm text-amber-100/90">
              <p>
                {what} {ask.clips + ask.libraries + ask.projects === 1 ? "has" : "have"} been in the trash longer
                than {parsed} {parsed === 1 ? "day" : "days"}, and would be deleted for good within minutes.
              </p>
              <div className="mt-3 flex gap-2">
                <button
                  type="button"
                  disabled={save.isPending}
                  onClick={() => save.mutate({ value, confirm: true })}
                  className="rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-500 disabled:opacity-50"
                >
                  Save and delete them
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setAsk(null);
                    save.reset();
                  }}
                  className="rounded-md px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-800"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
          <div className="flex items-center gap-3">
            <button
              type="submit"
              disabled={!canSave}
              className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Save
            </button>
            <span role="status" className="text-sm">
              {saved && !save.isPending && <span className="text-emerald-300">Saved</span>}
              {save.isError && !ask && <span className="text-red-300">Couldn&apos;t save. Try again.</span>}
            </span>
          </div>
        </form>
      ) : (
        <div className="space-y-1">
          <p className="text-sm text-gray-200">{describe(savedDays)}</p>
          <p className="text-sm text-gray-500">Only admins can change this.</p>
        </div>
      )}
    </div>
  );
}

function FilesForm({ settings, isAdmin }: { settings: TenantSettings; isAdmin: boolean }) {
  const queryClient = useQueryClient();
  const savedValue = settings.follow_moves !== false; // a server that doesn't say: on
  const [follow, setFollow] = useState(savedValue);
  const [saved, setSaved] = useState(false);

  const save = useMutation({
    mutationFn: (followMoves: boolean) => updateTenantSettings({ follow_moves: followMoves }),
    onSuccess: (next) => {
      queryClient.setQueryData(["tenant-settings"], next);
      setSaved(true);
    },
  });

  const canSave = isAdmin && follow !== savedValue && !save.isPending;
  const choose = (value: boolean) => {
    setFollow(value);
    setSaved(false);
  };

  return (
    <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-6 space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-gray-100">Files</h2>
        <p className="mt-1 text-sm text-gray-400">What happens when a file moves, is renamed or is copied.</p>
      </div>

      {isAdmin ? (
        <form
          aria-label="Moves and renames"
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (canSave) save.mutate(follow);
          }}
        >
          <fieldset className="space-y-3">
            <legend className="sr-only">Moves and renames</legend>
            {[
              { value: true, title: ON_TITLE, text: ON_TEXT },
              { value: false, title: OFF_TITLE, text: OFF_TEXT },
            ].map((option) => (
              <label key={option.title} className="flex items-start gap-3 text-sm text-gray-200">
                <input
                  type="radio"
                  name="follow-moves"
                  className="mt-1"
                  checked={follow === option.value}
                  onChange={() => choose(option.value)}
                  aria-labelledby={`follow-moves-${option.value}-title`}
                  aria-describedby={`follow-moves-${option.value}`}
                />
                <span>
                  <span id={`follow-moves-${option.value}-title`} className="font-medium">{option.title}</span>
                  {option.value && <span className="text-gray-500"> (default)</span>}
                  <span id={`follow-moves-${option.value}`} className="mt-0.5 block text-gray-400">
                    {option.text}
                  </span>
                </span>
              </label>
            ))}
          </fieldset>
          <div className="flex items-center gap-3">
            <button
              type="submit"
              disabled={!canSave}
              className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Save
            </button>
            <span role="status" className="text-sm">
              {saved && !save.isPending && <span className="text-emerald-300">Saved</span>}
              {save.isError && <span className="text-red-300">Couldn't save. Try again.</span>}
            </span>
          </div>
        </form>
      ) : (
        <div className="space-y-1">
          <p className="text-sm font-medium text-gray-200">{savedValue ? ON_TITLE : OFF_TITLE}</p>
          <p className="text-sm text-gray-400">{savedValue ? ON_TEXT : OFF_TEXT}</p>
          <p className="text-sm text-gray-500">Only admins can change this.</p>
        </div>
      )}
    </div>
  );
}
