import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, getLibrary, getLibraryPeople, setLibraryPrivate, setLibraryRole } from "../api/client";
import type { LibraryPerson, LibraryRole } from "../api/types";
import { defaultRole, howTheyHaveIt, ROLE_NAMES, runsIt } from "../lib/libraryRoles";

const CHOICES: LibraryRole[] = ["admin", "editor", "viewer", "none"];

/** Who can see and edit the library (ADR-018 decision 8): everyone in the account with their role on it. Its admin
 * gives someone a role here, above or below their account role, or makes it private. Shown to its admin only (an
 * account admin is every library's). */
export default function LibraryPeopleSection({ libraryId }: { libraryId: string }) {
  const queryClient = useQueryClient();
  const { data: library } = useQuery({ queryKey: ["library", libraryId], queryFn: () => getLibrary(libraryId) });
  const runs = runsIt(library?.role ?? undefined);
  const { data } = useQuery({
    queryKey: ["library-people", libraryId], queryFn: () => getLibraryPeople(libraryId), enabled: runs });
  const refresh = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ["library-people", libraryId] }),
      queryClient.invalidateQueries({ queryKey: ["library", libraryId] }),
      queryClient.invalidateQueries({ queryKey: ["libraries"] }),
    ]);
  const role = useMutation({
    mutationFn: ({ userId, next }: { userId: string; next: LibraryRole | null }) => setLibraryRole(libraryId, userId, next),
    onSuccess: refresh,
  });
  const privacy = useMutation({ mutationFn: (next: boolean) => setLibraryPrivate(libraryId, next), onSuccess: refresh });
  if (!runs || !data) return null;
  const error = role.error ?? privacy.error;

  return (
    <div className="space-y-3 rounded-lg border border-gray-700/50 bg-gray-900/50 px-4 py-3" aria-label="People">
      <h2 className="text-lg font-semibold text-gray-100">People</h2>
      <p className="text-sm text-gray-400">
        Who can see this library and what they can do. Each person has their account role here unless you give them
        another: an editor here can be a viewer elsewhere. Its admins run it: these settings, its folder and who reads
        it. Account admins are every library's admin.
      </p>
      <label className="flex items-start gap-2 text-sm text-gray-300">
        <input
          type="checkbox"
          checked={data.private}
          disabled={privacy.isPending}
          onChange={(e) => privacy.mutate(e.target.checked)}
          className="mt-0.5"
        />
        <span>
          Private: only account admins and the people given a role here see it. (A public link is apart: anyone with
          it sees the library either way.)
        </span>
      </label>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-700/50 text-left text-xs text-gray-500">
              <th className="py-2 pr-4 font-medium">Person</th>
              <th className="py-2 pr-4 font-medium">Role here</th>
              <th className="py-2 font-medium">Why</th>
            </tr>
          </thead>
          <tbody>
            {data.people.map((person) => (
              <PersonRow key={person.user_id} person={person} isPrivate={data.private} busy={role.isPending}
                         onChange={(next) => role.mutate({ userId: person.user_id, next })} />
            ))}
          </tbody>
        </table>
      </div>
      {error && (
        <p role="alert" className="text-sm text-red-300">
          {error instanceof ApiError ? error.message : "Couldn't reach the server. Try again."}
        </p>
      )}
    </div>
  );
}

function PersonRow({ person, isPrivate, busy, onChange }: {
  person: LibraryPerson;
  isPrivate: boolean;
  busy: boolean;
  onChange: (next: LibraryRole | null) => void;
}) {
  const accountAdmin = person.account_role === "admin";
  return (
    <tr className="border-b border-gray-800">
      <td className="py-2 pr-4 text-gray-200">{person.email}</td>
      <td className="py-2 pr-4">
        {accountAdmin ? (
          <span className="text-gray-300">Admin</span>
        ) : (
          <select
            aria-label={`${person.email}'s role here`}
            value={person.set_role ?? ""}
            disabled={busy}
            onChange={(e) => onChange(e.target.value === "" ? null : (e.target.value as LibraryRole))}
            className="rounded border border-gray-700 bg-gray-800 px-2 py-1 text-sm text-gray-200 disabled:opacity-50"
          >
            <option value="">Default ({ROLE_NAMES[defaultRole(person, isPrivate)]})</option>
            {CHOICES.map((r) => (
              <option key={r} value={r}>{ROLE_NAMES[r]}</option>
            ))}
          </select>
        )}
      </td>
      <td className="py-2 text-gray-400">{howTheyHaveIt(person, isPrivate)}</td>
    </tr>
  );
}
