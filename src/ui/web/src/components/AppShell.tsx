import { useEffect, useState } from "react";
import { Outlet } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { getApiKey, getCurrentUser } from "../api/client";
import { Sidebar } from "./Sidebar";
import { BottomNav } from "./BottomNav";
import { CommandPalette } from "./CommandPalette";
import { KeyboardShortcuts } from "./KeyboardShortcuts";
import { ScrollContainerContext } from "../context/ScrollContainerContext";
import { healthAlert, useSystemHealth } from "../lib/useSystemHealth";

const SIDEBAR_COLLAPSED_KEY = "lv_sidebar_collapsed";

export default function AppShell() {
  const [collapsed, setCollapsed] = useState(false);
  const [mainEl, setMainEl] = useState<HTMLElement | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);

  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY);
      if (stored === "true") setCollapsed(true);
      if (stored === "false") setCollapsed(false);
    } catch {
      // ignore
    }
  }, []);

  // Global keyboard shortcuts
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      // ⌘K / Ctrl+K — library switcher
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setPaletteOpen((o) => !o);
        return;
      }
      // ? — keyboard reference (not in an input, not when lightbox is open)
      if (e.key === "?") {
        const tag = document.activeElement?.tagName;
        const isInput =
          tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
        const lightboxOpen = Boolean(document.querySelector('[data-lightbox="true"]'));
        if (!isInput && !lightboxOpen) {
          setShortcutsOpen((o) => !o);
        }
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, []);

  // Admin, and its dot (the system's health), for admins only. Hidden until the
  // role is known: never shown to someone it isn't for. Asked again until it's
  // known: a page opened while the server restarts (an upgrade) would otherwise
  // go on without the role, hiding every admin's control.
  const { data: me } = useQuery({
    queryKey: ["settings", "me"],
    queryFn: getCurrentUser,
    enabled: Boolean(getApiKey()),
    refetchInterval: (query) => (query.state.status === "error" ? 5_000 : false),
  });
  const isAdmin = me?.role === "admin";
  const health = useSystemHealth(undefined, isAdmin);
  const adminAlert = isAdmin ? healthAlert(health.data, health.isError) : null;

  const sidebarWidth = collapsed ? "w-12" : "w-64";

  return (
    <div className="flex h-screen overflow-hidden bg-gray-950 text-gray-100">
      {/* Sidebar — hidden on mobile, visible on md+ */}
      <div
        className={`hidden md:block ${sidebarWidth} transition-all duration-200 motion-reduce:transition-none`}
      >
        <Sidebar
          collapsed={collapsed}
          onToggleCollapsed={() => setCollapsed((prev) => !prev)}
          onOpenPalette={() => setPaletteOpen(true)}
          adminAlert={adminAlert}
          showAdmin={isAdmin}
        />
      </div>
      <ScrollContainerContext.Provider value={mainEl}>
        <main
          ref={setMainEl}
          className="flex-1 overflow-auto pb-safe-offset-16 md:pb-0"
        >
          {me?.demo && (
            <p role="status" className="border-b border-indigo-900/60 bg-indigo-950/60 px-4 py-2 text-center text-sm text-indigo-200">
              You're trying Lumiverb as a viewer: search, browse and open anything. Nothing here can be changed.
            </p>
          )}
          <Outlet />
        </main>
      </ScrollContainerContext.Provider>
      {/* Projects are an editor's (the API's require_editor): never a viewer's, the demo's included. */}
      <BottomNav adminAlert={adminAlert} showAdmin={isAdmin}
                 showProjects={(me?.role === "admin" || me?.role === "editor") && !me?.demo} />

      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
      <KeyboardShortcuts
        open={shortcutsOpen}
        onClose={() => setShortcutsOpen(false)}
      />
    </div>
  );
}
