import type { ReactNode } from "react";

interface DrawerOverlayProps {
  open: boolean;
  onClose: () => void;
  children: ReactNode;
}

export function DrawerOverlay({ open, onClose, children }: DrawerOverlayProps) {
  return (
    <>
      {/* Backdrop — z-50 so it sits above BottomNav (z-40) */}
      <div
        className={`fixed inset-0 z-50 bg-black/50 transition-opacity duration-200 md:hidden ${
          open ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
        onClick={onClose}
        aria-hidden
      />
      {/* Slide-over panel — z-50, same level as backdrop */}
      <div
        className={`fixed inset-y-0 left-0 z-50 w-64 overflow-y-auto bg-gray-900 transition-transform duration-200 motion-reduce:transition-none md:hidden ${
          open ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        {children}
      </div>
    </>
  );
}
