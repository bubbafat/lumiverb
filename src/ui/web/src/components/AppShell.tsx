import { useEffect, useState } from "react";
import { Outlet } from "react-router-dom";
import { Sidebar } from "./Sidebar";
import { BottomNav } from "./BottomNav";
import { CommandPalette } from "./CommandPalette";
import { KeyboardShortcuts } from "./KeyboardShortcuts";
import { ScrollContainerContext } from "../context/ScrollContainerContext";

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
        />
      </div>
      <ScrollContainerContext.Provider value={mainEl}>
        <main
          ref={setMainEl}
          className="flex-1 overflow-auto pb-safe-offset-16 md:pb-0"
        >
          <Outlet />
        </main>
      </ScrollContainerContext.Provider>
      <BottomNav />

      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
      <KeyboardShortcuts
        open={shortcutsOpen}
        onClose={() => setShortcutsOpen(false)}
      />
    </div>
  );
}
