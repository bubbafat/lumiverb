import type { ProjectUsage } from "../api/client";

export const clipCount = (n: number) => (n === 1 ? "1 clip" : `${n.toLocaleString()} clips`);

/** The projects a 409 in_projects named: which ones would lose clips, and how many. */
export function ProjectUsageList({ usage, children }: { usage: ProjectUsage; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-amber-800/50 bg-amber-950/30 p-3 text-sm text-amber-100/90">
      <p className="mb-2">{children}</p>
      <ul className="space-y-0.5 text-amber-200/80">
        {usage.projects.map((p) => (
          <li key={p.project_id}>
            {p.name}: {clipCount(p.clips)}
            {p.status === "archived" ? " (archived)" : ""}
            {p.in_trash ? " (in the trash)" : ""}
          </li>
        ))}
        {usage.other_projects > 0 && <li>and {usage.other_projects} more you can&apos;t see</li>}
      </ul>
    </div>
  );
}
