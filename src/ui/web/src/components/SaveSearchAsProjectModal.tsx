import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { createProject, ApiError } from "../api/client";
import { Modal } from "./Modal";
import type { SavedQueryV2 } from "../lib/queryFilter";
import { savedQueryLabels } from "../lib/queryFilter";

interface Props {
  savedQuery: SavedQueryV2;
  onClose: () => void;
}

/** Save the search as a project: the clips it finds now, in an explicit list
 * (Robert, Oct 9: projects are explicit; the search is saved with Save search). */
export function SaveSearchAsProjectModal({ savedQuery, onClose }: Props) {
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: () =>
      createProject(name, { from_search: savedQuery as unknown as Record<string, unknown> }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["projects"] });
      onClose();
    },
    onError: (err: ApiError) => setError(err.message),
  });

  const labels = savedQueryLabels(savedQuery);

  return (
    <Modal isOpen={true} title="Save as project" onClose={onClose}>
      <div className="space-y-4">
        <p className="text-sm text-gray-400">
          A project of the clips this search finds now. It won&apos;t change as clips are added or edited; to
          keep the search itself, use Save search.
        </p>
        {labels.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {labels.map((l, i) => (
              <span
                key={i}
                className="inline-block rounded-full bg-gray-700 px-2.5 py-0.5 text-xs text-gray-300"
              >
                {l}
              </span>
            ))}
          </div>
        )}
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Project name"
          className="w-full rounded-md border border-gray-600 bg-gray-800 px-3 py-2 text-sm text-gray-100 placeholder:text-gray-500 focus:border-indigo-500 focus:outline-none"
          autoFocus
          onKeyDown={(e) => {
            if (e.key === "Enter" && name.trim()) mutation.mutate();
          }}
        />
        {error && <p className="text-sm text-red-400">{error}</p>}
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md px-4 py-2 text-sm text-gray-400 hover:text-gray-200"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => mutation.mutate()}
            disabled={!name.trim() || mutation.isPending}
            className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
          >
            {mutation.isPending ? "Saving..." : "Save"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
