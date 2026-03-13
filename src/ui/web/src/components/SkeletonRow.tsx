export function SkeletonRow() {
  return (
    <div className="animate-pulse rounded-lg border border-gray-700/50 bg-gray-800/50 p-4">
      <div className="flex items-center justify-between gap-4">
        <div className="flex-1 space-y-2">
          <div className="h-4 w-48 rounded bg-gray-700" />
          <div className="h-3 w-64 rounded bg-gray-700/80" />
        </div>
        <div className="flex items-center gap-3">
          <div className="h-6 w-16 rounded-full bg-gray-700" />
          <div className="h-6 w-20 rounded-full bg-gray-700" />
        </div>
      </div>
    </div>
  );
}
