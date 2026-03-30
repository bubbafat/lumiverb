import { Link } from "react-router-dom";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { listPeople, getClusters } from "../api/client";
import type { PersonItem } from "../api/client";
import { useAuthenticatedImage } from "../api/useAuthenticatedImage";

function PersonCard({ person }: { person: PersonItem }) {
  const { url, isLoading } = useAuthenticatedImage(
    person.representative_asset_id ?? "",
    "thumbnail",
    { enabled: !!person.representative_asset_id },
  );

  return (
    <Link
      to={`/people/${person.person_id}`}
      className="group flex flex-col items-center gap-2 rounded-xl bg-gray-800/50 p-4 transition-colors hover:bg-gray-700/50"
    >
      <div className="h-24 w-24 overflow-hidden rounded-full bg-gray-700">
        {isLoading ? (
          <div className="h-full w-full animate-pulse bg-gray-600" />
        ) : url ? (
          <img
            src={url}
            alt={person.display_name}
            className="h-full w-full object-cover"
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-2xl text-gray-500">
            {person.display_name.charAt(0).toUpperCase()}
          </div>
        )}
      </div>
      <span className="text-sm font-medium text-gray-200 group-hover:text-white">
        {person.display_name}
      </span>
      <span className="text-xs text-gray-500">
        {person.face_count} {person.face_count === 1 ? "photo" : "photos"}
      </span>
    </Link>
  );
}

export default function PeoplePage() {
  const peopleQuery = useInfiniteQuery({
    queryKey: ["people"],
    queryFn: ({ pageParam }) => listPeople(pageParam, 50),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

  const clustersQuery = useQuery({
    queryKey: ["face-clusters-summary"],
    queryFn: () => getClusters(1, 1),
  });

  const people = peopleQuery.data?.pages.flatMap((p) => p.items) ?? [];
  const unnamedCount = clustersQuery.data?.clusters.reduce((sum, c) => sum + c.size, 0) ?? 0;
  const hasClusters = (clustersQuery.data?.clusters.length ?? 0) > 0;

  return (
    <div className="mx-auto max-w-6xl px-4 py-8">
      <h1 className="mb-6 text-2xl font-bold text-white">People</h1>

      {people.length === 0 && !peopleQuery.isLoading && (
        <p className="text-sm text-gray-500">
          No named people yet. Use face clustering to identify and name people in your photos.
        </p>
      )}

      {people.length > 0 && (
        <div className="mb-8 grid grid-cols-3 gap-4 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6">
          {people.map((person) => (
            <PersonCard key={person.person_id} person={person} />
          ))}
        </div>
      )}

      {peopleQuery.hasNextPage && (
        <button
          type="button"
          onClick={() => peopleQuery.fetchNextPage()}
          disabled={peopleQuery.isFetchingNextPage}
          className="mb-8 rounded-lg border border-gray-700 bg-gray-800/50 px-4 py-2 text-sm text-gray-300 hover:bg-gray-700"
        >
          {peopleQuery.isFetchingNextPage ? "Loading..." : "Load more"}
        </button>
      )}

      {hasClusters && (
        <div className="rounded-lg border border-gray-700 bg-gray-800/30 p-4">
          <p className="text-sm text-gray-400">
            {unnamedCount} unnamed face{unnamedCount !== 1 ? " clusters" : " cluster"} detected.
            <span className="text-gray-500"> Cluster management coming soon.</span>
          </p>
        </div>
      )}

      {peopleQuery.isLoading && (
        <div className="grid grid-cols-3 gap-4 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6">
          {Array.from({ length: 12 }).map((_, i) => (
            <div key={i} className="flex flex-col items-center gap-2 rounded-xl bg-gray-800/50 p-4">
              <div className="h-24 w-24 animate-pulse rounded-full bg-gray-700" />
              <div className="h-4 w-16 animate-pulse rounded bg-gray-700" />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
