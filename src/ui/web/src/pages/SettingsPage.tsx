import { NavLink, Outlet } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { getCurrentUser } from "../api/client";

interface NavItem {
  to: string;
  label: string;
  requireEditor?: boolean;
}

const NAV_ITEMS: NavItem[] = [
  { to: "/settings/account", label: "Account" },
  { to: "/settings/preferences", label: "Preferences" },
  { to: "/settings/security", label: "Security" },
  { to: "/settings/keys", label: "API Keys", requireEditor: true },
];

export default function SettingsPage() {
  const { data: user, isPending } = useQuery({
    queryKey: ["settings", "me"],
    queryFn: getCurrentUser,
  });

  // Don't hide editor-only items until we know the role (avoids flicker)
  const isEditorOrAbove =
    isPending || user?.role === "admin" || user?.role === "editor";

  return (
    <div className="mx-auto max-w-4xl px-6 py-6">
      <h1 className="text-2xl font-semibold text-gray-100">Settings</h1>
      <div className="mt-6 flex flex-col gap-8 sm:flex-row">
        <nav className="flex shrink-0 flex-row gap-1 sm:w-48 sm:flex-col">
          {NAV_ITEMS.map((item) => {
            if (item.requireEditor && !isEditorOrAbove) return null;
            return (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) =>
                  `rounded-lg px-3 py-2 text-sm font-medium transition-colors duration-150 ${
                    isActive
                      ? "bg-indigo-600/30 text-indigo-200"
                      : "text-gray-400 hover:bg-gray-800/80 hover:text-gray-200"
                  }`
                }
              >
                {item.label}
              </NavLink>
            );
          })}
        </nav>
        <div className="min-w-0 flex-1">
          <Outlet />
        </div>
      </div>
    </div>
  );
}
