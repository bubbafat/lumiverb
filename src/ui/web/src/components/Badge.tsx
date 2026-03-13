import { ReactNode } from "react";

type Variant = "idle" | "running" | "error" | "trashed";

interface BadgeProps {
  variant: Variant;
  children: ReactNode;
}

const variantClasses: Record<Variant, string> = {
  idle: "bg-gray-700/50 text-gray-300",
  running: "bg-blue-900/50 text-blue-300",
  error: "bg-red-900/30 text-red-400",
  trashed: "bg-amber-900/30 text-amber-400",
};

export function Badge({ variant, children }: BadgeProps) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ${variantClasses[variant]}`}
    >
      {variant === "running" && (
        <span
          className="h-1.5 w-1.5 rounded-full bg-blue-400 animate-pulse"
          aria-hidden
        />
      )}
      {children}
    </span>
  );
}
