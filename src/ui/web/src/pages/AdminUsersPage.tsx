import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  listUsers,
  inviteUser,
  newInviteLink,
  updateUserRole,
  setUserEveryLibrary,
  deleteUser,
  getCurrentUser,
  getNetworkAccess,
  ApiError,
} from "../api/client";
import { decodeJwtClaims } from "../api/jwt";
import type { UserItem } from "../api/types";
import { timeAgo } from "../lib/format";
import { copyText } from "../lib/copyText";

const ROLES = ["admin", "editor", "viewer"] as const;
type Role = (typeof ROLES)[number];

/** A link just made, at each address (the likeliest to open first): shown once, to copy and send. */
export type MadeLink = { email: string; urls: string[] };

function relativeTime(iso: string | null): string {
  return iso ? timeAgo(iso) : "never";
}

/** Said instead of a server admin's controls to someone who isn't one: the API refuses them (403 server_admin_user). */
const SERVER_ADMINS_OWN ="Only a server admin changes a server admin.";

function UserRow({
  user,
  currentUserId,
  viewerIsServerAdmin,
  onRoleChange,
  onLibrariesChange,
  onRemove,
  onNewLink,
}: {
  user: UserItem;
  currentUserId: string | null;
  viewerIsServerAdmin: boolean;
  onRoleChange: (userId: string, role: string) => void;
  onLibrariesChange: (userId: string, everyLibrary: boolean) => void;
  onRemove: (user: UserItem) => void;
  onNewLink: (user: UserItem) => void;
}) {
  const isSelf = user.user_id === currentUserId;
  // Whoever holds a server admin's password has the whole install: they're out of an account admin's reach.
  const outOfReach = !!user.server_admin && !viewerIsServerAdmin;

  return (
    <tr className="border-b border-gray-800">
      <td className="px-4 py-3 text-sm text-gray-200">
        {user.email}
        {user.server_admin && <span className="ml-2 text-xs text-gray-500">Server admin</span>}
      </td>
      <td className="px-4 py-3">
        <select
          aria-label={`Role of ${user.email}`}
          value={user.role}
          disabled={isSelf || outOfReach}
          title={outOfReach ? SERVER_ADMINS_OWN : undefined}
          onChange={(e) => onRoleChange(user.user_id, e.target.value)}
          className="rounded-sm border border-gray-700 bg-gray-800 px-2 py-1 text-sm text-gray-200 disabled:opacity-50"
        >
          {ROLES.map((r) => (
            <option key={r} value={r}>
              {r.charAt(0).toUpperCase() + r.slice(1)}
            </option>
          ))}
        </select>
      </td>
      <td className="px-4 py-3">
        {user.role === "admin" ? (
          <span className="text-sm text-gray-400">Every library</span>
        ) : (
          <select
            aria-label={`Libraries ${user.email} sees`}
            value={user.every_library === false ? "given" : "every"}
            disabled={outOfReach}
            title={outOfReach ? SERVER_ADMINS_OWN : undefined}
            onChange={(e) => onLibrariesChange(user.user_id, e.target.value === "every")}
            className="rounded-sm border border-gray-700 bg-gray-800 px-2 py-1 text-sm text-gray-200 disabled:opacity-50"
          >
            <option value="every">Every library</option>
            <option value="given">Only those given</option>
          </select>
        )}
      </td>
      <td className="px-4 py-3 text-sm text-gray-400">{relativeTime(user.last_login_at)}</td>
      <td className="px-4 py-3 text-right whitespace-nowrap space-x-4">
        {outOfReach && <span className="text-xs text-gray-500">{SERVER_ADMINS_OWN}</span>}
        {user.last_login_at === null && !isSelf && !outOfReach && (
          <button onClick={() => onNewLink(user)} className="text-sm text-indigo-300 hover:text-indigo-200">
            New link
          </button>
        )}
        {!isSelf && !outOfReach && (
          <button
            onClick={() => onRemove(user)}
            className="text-sm text-red-400 hover:text-red-300"
          >
            Remove
          </button>
        )}
      </td>
    </tr>
  );
}

export default function AdminUsersPage() {
  const qc = useQueryClient();
  const currentUserId = decodeJwtClaims()?.sub ?? null;

  const { data: users, isLoading, error } = useQuery({
    queryKey: ["admin", "users"],
    queryFn: listUsers,
  });
  // A server admin's row is theirs alone to change (the API's 403 server_admin_user).
  const { data: me } = useQuery({ queryKey: ["settings", "me"], queryFn: getCurrentUser });
  const viewerIsServerAdmin = me?.server_admin === true;

  const roleMutation = useMutation({
    mutationFn: ({ userId, role }: { userId: string; role: string }) =>
      updateUserRole(userId, role),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin", "users"] }),
  });

  const librariesMutation = useMutation({
    mutationFn: ({ userId, everyLibrary }: { userId: string; everyLibrary: boolean }) =>
      setUserEveryLibrary(userId, everyLibrary),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin", "users"] }),
    onError: (err) => {
      setRoleError(err instanceof ApiError && err.status === 403 ? err.message : "Failed to update which libraries they see.");
      qc.invalidateQueries({ queryKey: ["admin", "users"] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (userId: string) => deleteUser(userId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin", "users"] }),
  });

  const linkMutation = useMutation({
    mutationFn: (user: UserItem) => newInviteLink(user.user_id).then((link) => ({ email: user.email, urls: link.invite_urls })),
    onSuccess: (made) => {
      setMadeLink(made);
      setLinkError(null);
    },
    onError: (err) => setLinkError(err instanceof ApiError ? err.message : "Couldn't make a new link. Try again."),
  });

  // An invite link works only where other people can open Lumiverb: a Mac's
  // is this Mac only until Settings › Network says otherwise.
  const { data: network } = useQuery({ queryKey: ["system", "network"], queryFn: getNetworkAccess });
  const thisMacOnly = !!network?.available && !network.other_devices;

  const [madeLink, setMadeLink] = useState<MadeLink | null>(null);
  const [linkError, setLinkError] = useState<string | null>(null);

  const [roleError, setRoleError] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  function handleRoleChange(userId: string, role: string) {
    setRoleError(null);
    roleMutation.mutate(
      { userId, role },
      {
        onError: (err) => {
          if (err instanceof ApiError && err.status === 409) {
            setRoleError("Cannot demote the last admin.");
          } else if (err instanceof ApiError && err.status === 403) {
            setRoleError(err.message);
          } else {
            setRoleError("Failed to update role.");
          }
          qc.invalidateQueries({ queryKey: ["admin", "users"] });
        },
      },
    );
  }

  function handleRemove(user: UserItem) {
    setDeleteError(null);
    if (!window.confirm(`Remove ${user.email}? They will lose access immediately.`)) return;
    deleteMutation.mutate(user.user_id, {
      onError: (err) => {
        if (err instanceof ApiError && err.code === "last_admin") {
          setDeleteError("Cannot remove the last admin.");
        } else if (err instanceof ApiError && (err.status === 403 || err.status === 409)) {
          setDeleteError(err.message); // a server admin's, or the last one
        } else {
          setDeleteError("Failed to remove user.");
        }
      },
    });
  }

  function handleNewLink(user: UserItem) {
    setLinkError(null);
    if (!window.confirm(`Make a new link for ${user.email}? The one they have stops working.`)) return;
    linkMutation.mutate(user);
  }

  if (error instanceof ApiError && error.status === 403) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-6">
        <p className="text-sm text-red-400">Admin access required.</p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-6 space-y-10">
      <div>
        <h1 className="text-2xl font-semibold text-gray-100">Users</h1>
        <p className="mt-1 text-sm text-gray-500">
          Who can use Lumiverb, and what they can do. A role is theirs on every library unless a library's People
          (Library settings) says otherwise; someone who sees only the libraries they're given sees none until one
          gives them a role.
        </p>
      </div>

      {madeLink && <InviteLinkPanel link={madeLink} thisMacOnly={thisMacOnly} onDone={() => setMadeLink(null)} />}

      {/* User table */}
      <section>
        {roleError && <p className="mb-3 text-sm text-red-400">{roleError}</p>}
        {deleteError && <p className="mb-3 text-sm text-red-400">{deleteError}</p>}
        {linkError && <p className="mb-3 text-sm text-red-400">{linkError}</p>}

        {isLoading ? (
          <div className="h-24 rounded-lg border border-gray-700/50 bg-gray-900/50 animate-pulse" />
        ) : users && users.length > 0 ? (
          <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 overflow-hidden">
            <table className="w-full">
              <thead>
                <tr className="border-b border-gray-700/50">
                  <th className="px-4 py-2 text-left text-xs font-medium text-gray-500">Email</th>
                  <th className="px-4 py-2 text-left text-xs font-medium text-gray-500">Role</th>
                  <th className="px-4 py-2 text-left text-xs font-medium text-gray-500">Libraries</th>
                  <th className="px-4 py-2 text-left text-xs font-medium text-gray-500">Last login</th>
                  <th className="px-4 py-2" />
                </tr>
              </thead>
              <tbody>
                {users.map((u) => (
                  <UserRow
                    key={u.user_id}
                    user={u}
                    currentUserId={currentUserId}
                    viewerIsServerAdmin={viewerIsServerAdmin}
                    onRoleChange={handleRoleChange}
                    onLibrariesChange={(userId, everyLibrary) => {
                      setRoleError(null);
                      librariesMutation.mutate({ userId, everyLibrary });
                    }}
                    onRemove={handleRemove}
                    onNewLink={handleNewLink}
                  />
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-gray-500 italic">No users yet.</p>
        )}
      </section>

      {/* Invite form */}
      <section>
        <h2 className="mb-1 text-lg font-semibold text-gray-100">Invite someone</h2>
        <p className="mb-4 text-sm text-gray-500">
          You get a link to send them. They choose their own password there.
        </p>
        <InviteForm
          onMade={(link) => {
            qc.invalidateQueries({ queryKey: ["admin", "users"] });
            setMadeLink(link);
          }}
        />
      </section>
    </div>
  );
}

/** Someone's email and role: an invite link for them (onMade), to copy and send. */
export function InviteForm({ onMade }: { onMade: (link: MadeLink | null) => void }) {
  const [newEmail, setNewEmail] = useState("");
  const [newRole, setNewRole] = useState<Role>("viewer");
  const [onlyGiven, setOnlyGiven] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const inviteMutation = useMutation({
    mutationFn: ({ email, role, everyLibrary }: { email: string; role: string; everyLibrary: boolean }) =>
      inviteUser(email, role, everyLibrary),
    onSuccess: (made) => {
      onMade(made.invite_urls?.length ? { email: made.email, urls: made.invite_urls } : null);
      setNewEmail("");
      setNewRole("viewer");
      setOnlyGiven(false);
      setCreateError(null);
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) {
        setCreateError("Someone with that email is already here.");
      } else {
        setCreateError(err instanceof ApiError ? err.message : "Couldn't make the invite. Try again.");
      }
    },
  });

  function handleInvite(e: React.FormEvent) {
    e.preventDefault();
    setCreateError(null);
    inviteMutation.mutate({ email: newEmail.trim(), role: newRole, everyLibrary: newRole === "admin" || !onlyGiven });
  }

  return (
    <form onSubmit={handleInvite} className="space-y-3 max-w-sm">
      <div>
        <label htmlFor="invite-email" className="mb-1 block text-sm font-medium text-gray-300">Email</label>
        <input
          id="invite-email"
          type="email"
          value={newEmail}
          onChange={(e) => setNewEmail(e.target.value)}
          placeholder="them@example.com"
          required
          className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-gray-100 placeholder-gray-500 outline-hidden focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
        />
      </div>
      <div>
        <label htmlFor="invite-role" className="mb-1 block text-sm font-medium text-gray-300">Role</label>
        <select
          id="invite-role"
          value={newRole}
          onChange={(e) => setNewRole(e.target.value as Role)}
          className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-gray-200"
        >
          {ROLES.map((r) => (
            <option key={r} value={r}>
              {r.charAt(0).toUpperCase() + r.slice(1)}
            </option>
          ))}
        </select>
      </div>

      {newRole !== "admin" && (
        <label className="flex items-start gap-2 text-sm text-gray-300">
          <input type="checkbox" checked={onlyGiven} onChange={(e) => setOnlyGiven(e.target.checked)} className="mt-0.5" />
          <span>Only the libraries they're given a role on (a library's People, in its settings)</span>
        </label>
      )}

      {createError && <p className="text-sm text-red-400">{createError}</p>}

      <button
        type="submit"
        disabled={inviteMutation.isPending || !newEmail.trim()}
        className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed"
      >
        {inviteMutation.isPending ? "One moment…" : "Make invite link"}
      </button>
    </form>
  );
}

/** The link just made, to copy: it isn't shown again (a new one can be made while they haven't joined). */
export function InviteLinkPanel({ link, thisMacOnly, onDone }: { link: MadeLink; thisMacOnly: boolean; onDone: () => void }) {
  const [first, ...others] = link.urls;
  return (
    <section className="rounded-lg border border-indigo-500/40 bg-indigo-950/30 p-5 space-y-3">
      <p className="text-sm text-gray-200">
        Send <span className="font-medium">{link.email}</span> this link. It works once, for 7 days, and they
        choose their password there.
      </p>
      <CopyableLink url={first} label="Invite link" />
      {others.map((url) => (
        <div key={url} className="space-y-2">
          <p className="text-sm text-gray-400">If that one doesn't open on their device, send this one instead:</p>
          <CopyableLink url={url} label="Invite link by address" />
        </div>
      ))}
      {thisMacOnly && (
        <p className="text-sm text-amber-300">
          Only this Mac can open it until{" "}
          <Link to="/settings/network" className="underline">Settings › Network</Link> lets other devices use
          Lumiverb.
        </p>
      )}
      <button onClick={onDone} className="text-sm text-gray-400 hover:text-gray-200">
        Done
      </button>
    </section>
  );
}

function CopyableLink({ url, label }: { url: string; label: string }) {
  const [copied, setCopied] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  async function copy() {
    if (await copyText(url)) {
      setCopied(true);
    } else {
      input.current?.select();  // this browser won't copy from here: selected to copy by hand
    }
  }

  return (
    <div className="flex flex-col gap-2 sm:flex-row">
      <input
        ref={input}
        readOnly
        aria-label={label}
        value={url}
        onFocus={(e) => e.target.select()}
        className="min-w-0 flex-1 rounded-lg border border-gray-700 bg-gray-900 px-3 py-2 font-mono text-xs text-gray-100"
      />
      <button
        onClick={() => void copy()}
        className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-500"
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}
