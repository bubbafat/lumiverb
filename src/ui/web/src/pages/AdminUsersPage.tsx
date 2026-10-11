import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  listUsers,
  inviteUser,
  newInviteLink,
  updateUserRole,
  deleteUser,
  getNetworkAccess,
  ApiError,
} from "../api/client";
import { decodeJwtClaims } from "../api/jwt";
import type { UserItem } from "../api/types";
import { timeAgo } from "../lib/format";

const ROLES = ["admin", "editor", "viewer"] as const;
type Role = (typeof ROLES)[number];

/** A link just made: shown once, to copy and send however they like. */
type MadeLink = { email: string; url: string };

function relativeTime(iso: string | null): string {
  return iso ? timeAgo(iso) : "never";
}

function UserRow({
  user,
  currentUserId,
  onRoleChange,
  onRemove,
  onNewLink,
}: {
  user: UserItem;
  currentUserId: string | null;
  onRoleChange: (userId: string, role: string) => void;
  onRemove: (user: UserItem) => void;
  onNewLink: (user: UserItem) => void;
}) {
  const isSelf = user.user_id === currentUserId;

  return (
    <tr className="border-b border-gray-800">
      <td className="px-4 py-3 text-sm text-gray-200">{user.email}</td>
      <td className="px-4 py-3">
        <select
          value={user.role}
          disabled={isSelf}
          onChange={(e) => onRoleChange(user.user_id, e.target.value)}
          className="rounded border border-gray-700 bg-gray-800 px-2 py-1 text-sm text-gray-200 disabled:opacity-50"
        >
          {ROLES.map((r) => (
            <option key={r} value={r}>
              {r.charAt(0).toUpperCase() + r.slice(1)}
            </option>
          ))}
        </select>
      </td>
      <td className="px-4 py-3 text-sm text-gray-400">{relativeTime(user.last_login_at)}</td>
      <td className="px-4 py-3 text-right whitespace-nowrap space-x-4">
        {user.last_login_at === null && !isSelf && (
          <button onClick={() => onNewLink(user)} className="text-sm text-indigo-300 hover:text-indigo-200">
            New link
          </button>
        )}
        {!isSelf && (
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

  const roleMutation = useMutation({
    mutationFn: ({ userId, role }: { userId: string; role: string }) =>
      updateUserRole(userId, role),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin", "users"] }),
  });

  const deleteMutation = useMutation({
    mutationFn: (userId: string) => deleteUser(userId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin", "users"] }),
  });

  const inviteMutation = useMutation({
    mutationFn: ({ email, role }: { email: string; role: string }) => inviteUser(email, role),
    onSuccess: (made) => {
      qc.invalidateQueries({ queryKey: ["admin", "users"] });
      setMadeLink(made.invite_url ? { email: made.email, url: made.invite_url } : null);
      setNewEmail("");
      setNewRole("viewer");
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

  const linkMutation = useMutation({
    mutationFn: (user: UserItem) => newInviteLink(user.user_id).then((link) => ({ email: user.email, url: link.invite_url })),
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

  const [newEmail, setNewEmail] = useState("");
  const [newRole, setNewRole] = useState<Role>("viewer");
  const [createError, setCreateError] = useState<string | null>(null);
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
        if (err instanceof ApiError && err.status === 409) {
          setDeleteError("Cannot remove the last admin.");
        } else {
          setDeleteError("Failed to remove user.");
        }
      },
    });
  }

  function handleInvite(e: React.FormEvent) {
    e.preventDefault();
    setCreateError(null);
    inviteMutation.mutate({ email: newEmail.trim(), role: newRole });
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
        <p className="mt-1 text-sm text-gray-500">Who can use Lumiverb, and what they can do.</p>
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
                    onRoleChange={handleRoleChange}
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
              className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-gray-100 placeholder-gray-500 outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
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

          {createError && <p className="text-sm text-red-400">{createError}</p>}

          <button
            type="submit"
            disabled={inviteMutation.isPending || !newEmail.trim()}
            className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {inviteMutation.isPending ? "One moment…" : "Make invite link"}
          </button>
        </form>
      </section>
    </div>
  );
}

/** The link just made, to copy: it isn't shown again (a new one can be made while they haven't joined). */
function InviteLinkPanel({ link, thisMacOnly, onDone }: { link: MadeLink; thisMacOnly: boolean; onDone: () => void }) {
  const [copied, setCopied] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  async function copy() {
    try {
      await navigator.clipboard.writeText(link.url);
      setCopied(true);
    } catch {
      // No clipboard here (a page that isn't https or this machine's): select it to copy by hand.
      input.current?.select();
    }
  }

  return (
    <section className="rounded-lg border border-indigo-500/40 bg-indigo-950/30 p-5 space-y-3">
      <p className="text-sm text-gray-200">
        Send <span className="font-medium">{link.email}</span> this link. It works once, for 7 days, and they
        choose their password there.
      </p>
      <div className="flex flex-col gap-2 sm:flex-row">
        <input
          ref={input}
          readOnly
          aria-label="Invite link"
          value={link.url}
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
