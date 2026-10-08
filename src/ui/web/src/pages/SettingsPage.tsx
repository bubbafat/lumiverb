import { NavLink, Outlet } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { getAiSettings, getCurrentUser } from "../api/client";
import { AI_QUERY_KEY, aiHasProblem } from "./settings/AiSection";

interface NavItem {
  to: string;
  label: string;
  requireEditor?: boolean;
}

const NAV_ITEMS: NavItem[] = [
  { to: "/settings/account", label: "Account" },
  { to: "/settings/preferences", label: "Preferences" },
  { to: "/settings/playback", label: "Playback" },
  { to: "/settings/ai", label: "AI" },
  { to: "/settings/files", label: "Files" },
  { to: "/settings/security", label: "Security" },
  { to: "/settings/keys", label: "API Keys", requireEditor: true },
];

export default function SettingsPage() {
  const { data: user, isPending } = useQuery({
    queryKey: ["settings", "me"],
    queryFn: getCurrentUser,
  });

  // The worker couldn't use the vision model: flag the AI page until it's fixed.
  const { data: ai } = useQuery({ queryKey: AI_QUERY_KEY, queryFn: getAiSettings });
  const aiProblem = aiHasProblem(ai);

  // Don't hide editor-only items until we know the role (avoids flicker)
  const isEditorOrAbove =
    isPending || user?.role === "admin" || user?.role === "editor";

  return (
    <div className="mx-auto max-w-4xl px-6 py-6">
      <h1 className="text-2xl font-semibold text-gray-100">Settings</h1>
      <div className="mt-6 flex flex-col gap-8 sm:flex-row">
        <nav className="flex shrink-0 flex-row flex-wrap gap-1 sm:w-48 sm:flex-col sm:flex-nowrap">
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
                {item.to === "/settings/ai" && aiProblem && (
                  <span
                    role="img"
                    aria-label="Vision AI is paused"
                    className="ml-2 inline-block h-2 w-2 rounded-full bg-red-500 align-middle"
                  />
                )}
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
