import { useEffect, useRef, useState } from "react";
import { Outlet } from "react-router-dom";
import { Sidebar } from "./Sidebar";
import { ScrollContainerContext } from "../context/ScrollContainerContext";

const SIDEBAR_COLLAPSED_KEY = "lv_sidebar_collapsed";

export default function AppShell() {
  const [collapsed, setCollapsed] = useState(false);
  const mainRef = useRef<HTMLElement | null>(null);

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
      <div className={`${sidebarWidth} transition-all duration-200`}>
        <Sidebar
          collapsed={collapsed}
          onToggleCollapsed={() => setCollapsed((prev) => !prev)}
        />
      </div>
      <ScrollContainerContext.Provider value={mainRef.current}>
        <main
          ref={mainRef}
          className="flex-1 overflow-auto"
        >
          <div className="mx-auto max-w-6xl px-6 py-6">
            <Outlet />
          </div>
        </main>
      </ScrollContainerContext.Provider>
    </div>
  );
}

