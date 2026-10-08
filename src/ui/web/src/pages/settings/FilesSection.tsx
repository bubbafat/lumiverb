import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getCurrentUser, getTenantSettings, updateTenantSettings, type TenantSettings } from "../../api/client";

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
  return <FilesForm settings={settings} isAdmin={user?.role === "admin"} />;
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
                  aria-describedby={`follow-moves-${option.value}`}
                />
                <span>
                  <span className="font-medium">{option.title}</span>
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
            {saved && !save.isPending && <span className="text-sm text-emerald-300">Saved</span>}
            {save.isError && <span className="text-sm text-red-300">Couldn't save. Try again.</span>}
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
