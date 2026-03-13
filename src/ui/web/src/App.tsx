import { Link, Outlet, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { listLibraries } from "./api/client";

export default function App() {
  const { libraryId } = useParams<{ libraryId: string }>();
  const { data: libraries } = useQuery({
    queryKey: ["libraries", true],
    queryFn: () => listLibraries(true),
    enabled: !!libraryId,
  });
  const library = libraryId
    ? libraries?.find((l) => l.library_id === libraryId)
    : null;

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100">
      <nav className="border-b border-gray-800 bg-gray-900/80 px-6 py-4">
        <div className="mx-auto flex max-w-6xl items-center justify-between">
          <div className="flex items-center gap-3">
            <Link to="/" className="text-xl font-semibold tracking-tight hover:text-gray-200">
              Lumiverb
            </Link>
            {library && (
              <>
                <span className="text-gray-600">/</span>
                <span className="text-gray-300">{library.name}</span>
              </>
            )}
          </div>
          <div className="flex items-center gap-6" />
        </div>
      </nav>
      <main className="mx-auto max-w-6xl px-6 py-6">
        <Outlet />
      </main>
    </div>
  );
}
