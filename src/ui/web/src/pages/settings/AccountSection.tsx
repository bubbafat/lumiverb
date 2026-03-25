import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { getCurrentUser } from "../../api/client";

const ROLE_COLORS: Record<string, string> = {
  admin: "bg-indigo-600/20 text-indigo-300 border-indigo-600/30",
  editor: "bg-emerald-600/20 text-emerald-300 border-emerald-600/30",
  viewer: "bg-gray-600/20 text-gray-300 border-gray-600/30",
};

function roleBadge(role: string) {
  const cls = ROLE_COLORS[role] ?? ROLE_COLORS.viewer;
  return (
    <span
      className={`inline-block rounded-full border px-2.5 py-0.5 text-xs font-medium ${cls}`}
    >
      {role.charAt(0).toUpperCase() + role.slice(1)}
    </span>
  );
}

export default function AccountSection() {
  const { data: user, isLoading } = useQuery({
    queryKey: ["settings", "me"],
    queryFn: getCurrentUser,
  });

  if (isLoading) {
    return (
      <div className="h-32 rounded-lg border border-gray-700/50 bg-gray-900/50 animate-pulse" />
    );
  }

  return (
    <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-6 space-y-5">
      <h2 className="text-lg font-semibold text-gray-100">Account</h2>

      <div className="space-y-4">
        <div>
          <label className="block text-sm font-medium text-gray-400">Email</label>
          <p className="mt-1 text-sm text-gray-200">
            {user?.email ?? "N/A (API key authentication)"}
          </p>
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-400">Role</label>
          <div className="mt-1">{roleBadge(user?.role ?? "viewer")}</div>
        </div>

        {user?.email && (
          <div>
            <Link
              to="/reset-password"
              className="text-sm font-medium text-indigo-400 hover:text-indigo-300 transition-colors"
            >
              Change password
            </Link>
          </div>
        )}
      </div>
    </div>
  );
}
