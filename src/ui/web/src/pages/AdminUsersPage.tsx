import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { listUsers, createUser, updateUserRole, deleteUser, ApiError } from "../api/client";
import { decodeJwtClaims } from "../api/jwt";
import type { UserItem } from "../api/types";

const ROLES = ["admin", "editor", "viewer"] as const;
const MIN_PASSWORD_LENGTH = 12;
type Role = (typeof ROLES)[number];

function relativeTime(iso: string | null): string {
  if (!iso) return "never";
  const ms = Date.now() - new Date(iso).getTime();
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function UserRow({
  user,
  currentUserId,
  onRoleChange,
  onRemove,
}: {
  user: UserItem;
  currentUserId: string | null;
  onRoleChange: (userId: string, role: string) => void;
  onRemove: (user: UserItem) => void;
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
      <td className="px-4 py-3 text-right">
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

  const createMutation = useMutation({
    mutationFn: ({ email, password, role }: { email: string; password: string; role: string }) =>
      createUser(email, password, role),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin", "users"] });
      setNewEmail("");
      setNewPassword("");
      setNewRole("viewer");
      setCreateError(null);
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) {
        setCreateError("Email already registered.");
      } else {
        setCreateError("Failed to create user.");
      }
    },
  });

  const [newEmail, setNewEmail] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [newRole, setNewRole] = useState<Role>("viewer");
  const [createError, setCreateError] = useState<string | null>(null);

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

  function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setCreateError(null);
    if (newPassword.length < MIN_PASSWORD_LENGTH) {
      setCreateError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters`);
      return;
    }
    createMutation.mutate({ email: newEmail.trim(), password: newPassword, role: newRole });
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
        <p className="mt-1 text-sm text-gray-500">Manage user accounts and roles.</p>
      </div>

      {/* User table */}
      <section>
        {roleError && <p className="mb-3 text-sm text-red-400">{roleError}</p>}
        {deleteError && <p className="mb-3 text-sm text-red-400">{deleteError}</p>}

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
                  />
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-gray-500 italic">No users yet.</p>
        )}
      </section>

      {/* Create user form */}
      <section>
        <h2 className="mb-4 text-lg font-semibold text-gray-100">Add user</h2>
        <form onSubmit={handleCreate} className="space-y-3 max-w-sm">
          <div>
            <label className="mb-1 block text-sm font-medium text-gray-300">Email</label>
            <input
              type="email"
              value={newEmail}
              onChange={(e) => setNewEmail(e.target.value)}
              placeholder="user@example.com"
              required
              className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-gray-100 placeholder-gray-500 outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
            />
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium text-gray-300">Password</label>
            <input
              type="password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              placeholder="••••••••"
              required
              className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-gray-100 placeholder-gray-500 outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
            />
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium text-gray-300">Role</label>
            <select
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
            disabled={createMutation.isPending || !newEmail.trim() || !newPassword}
            className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {createMutation.isPending ? "Creating…" : "Create user"}
          </button>
        </form>
      </section>
    </div>
  );
}
