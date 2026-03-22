import { Modal } from "./Modal";

interface KeyboardShortcutsProps {
  open: boolean;
  onClose: () => void;
}

const GROUPS = [
  {
    label: "Navigation",
    shortcuts: [
      { keys: ["⌘K", "Ctrl+K"], description: "Open library switcher" },
      { keys: ["?"], description: "Show / hide this panel" },
    ],
  },
  {
    label: "Lightbox",
    shortcuts: [
      { keys: ["←", "→"], description: "Previous / next photo" },
      { keys: ["Esc"], description: "Close lightbox" },
      { keys: ["F"], description: "Toggle fullscreen" },
      { keys: ["Space"], description: "Start / stop slideshow" },
      { keys: ["?"], description: "Toggle keyboard hints" },
    ],
  },
] as const;

export function KeyboardShortcuts({ open, onClose }: KeyboardShortcutsProps) {
  return (
    <Modal isOpen={open} onClose={onClose} title="Keyboard shortcuts">
      <div className="space-y-5">
        {GROUPS.map((group) => (
          <div key={group.label}>
            <p className="mb-2 text-xs font-medium uppercase tracking-wider text-gray-500">
              {group.label}
            </p>
            <ul className="space-y-1.5">
              {group.shortcuts.map((s) => (
                <li
                  key={s.description}
                  className="flex items-center justify-between gap-4"
                >
                  <span className="text-sm text-gray-300">{s.description}</span>
                  <span className="flex items-center gap-1 shrink-0">
                    {s.keys.map((k) => (
                      <kbd
                        key={k}
                        className="rounded border border-gray-700 bg-gray-800 px-1.5 py-0.5 font-mono text-xs text-gray-300"
                      >
                        {k}
                      </kbd>
                    ))}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </Modal>
  );
}
