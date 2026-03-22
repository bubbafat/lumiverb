import { useEffect, useState } from "react";
import { Outlet } from "react-router-dom";
import { Sidebar } from "./Sidebar";
import { BottomNav } from "./BottomNav";
import { ScrollContainerContext } from "../context/ScrollContainerContext";

const SIDEBAR_COLLAPSED_KEY = "lv_sidebar_collapsed";

export default function AppShell() {
  const [collapsed, setCollapsed] = useState(false);
  const [mainEl, setMainEl] = useState<HTMLElement | null>(null);

  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY);
      if (stored === "true") setCollapsed(true);
      if (stored === "false") setCollapsed(false);
    } catch {
      // ignore
    }
  }, []);

  const sidebarWidth = collapsed ? "w-12" : "w-64";

  return (
    <div className="flex h-screen overflow-hidden bg-gray-950 text-gray-100">
      {/* Sidebar — hidden on mobile, visible on md+ */}
      <div className={`hidden md:block ${sidebarWidth} transition-all duration-200 motion-reduce:transition-none`}>
        <Sidebar
          collapsed={collapsed}
          onToggleCollapsed={() => setCollapsed((prev) => !prev)}
        />
      </div>
      <ScrollContainerContext.Provider value={mainEl}>
        {/* pb-safe-offset-16 — nav height (64px) + env(safe-area-inset-bottom).
             Plain pb-16 undershoots on notched phones where the nav bar itself
             also consumes the safe-area inset, hiding the last content row. */}
        <main
          ref={setMainEl}
          className="flex-1 overflow-auto pb-safe-offset-16 md:pb-0"
        >
          <Outlet />
        </main>
      </ScrollContainerContext.Provider>
      <BottomNav />
    </div>
  );
}

