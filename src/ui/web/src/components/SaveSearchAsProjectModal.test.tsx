import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("../api/client", async (importOriginal) => {
  const real = await importOriginal<typeof import("../api/client")>();
  return { ...real, createProject: vi.fn() };
});

import { ApiError, createProject } from "../api/client";
import type { ProjectItem } from "../api/types";
import { SaveSearchAsProjectModal } from "./SaveSearchAsProjectModal";

const create = vi.mocked(createProject);
const search = { filters: [{ type: "camera_make", value: "Canon" }], sort: "taken_at", direction: "desc" as const };

beforeEach(() => {
  create.mockReset();
});
afterEach(cleanup);

function modal() {
  const onClose = vi.fn();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <SaveSearchAsProjectModal savedQuery={search} onClose={onClose} />
    </QueryClientProvider>,
  );
  return onClose;
}

describe("SaveSearchAsProjectModal", () => {
  it("makes a project of the clips the search finds now", async () => {
    create.mockResolvedValue({ project_id: "col_1" } as ProjectItem);
    const onClose = modal();
    expect(screen.getByText(/won.t change as clips are added/)).toBeTruthy();
    fireEvent.change(screen.getByPlaceholderText("Project name"), { target: { value: "Canon" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(create).toHaveBeenCalledWith("Canon", { from_search: search }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("says when the search finds too many clips", async () => {
    create.mockRejectedValue(new ApiError(422, "More than 10,000 clips match: narrow the search to save it as a project.",
      "search_too_big"));
    const onClose = modal();
    fireEvent.change(screen.getByPlaceholderText("Project name"), { target: { value: "Everything" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/narrow the search/)).toBeTruthy();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("needs a name", () => {
    modal();
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
  });
});
