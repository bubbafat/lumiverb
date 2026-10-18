import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import LibraryLocationSection from "./LibraryLocationSection";

const fetchMock = vi.fn();
let role = "admin";
let infer: boolean | undefined = false;
let guesses = 0;
const sent: { method: string; path: string; body: unknown }[] = [];

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

function library() {
  return { library_id: "lib_1", name: "Trips", root_path: "/t", is_public: false, role,
           ...(infer === undefined ? {} : { infer_location: infer }) };
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    const path = new URL(url, "http://x").pathname.replace(/^\/v1/, "");
    sent.push({ method, path, body });
    if (path === "/libraries/lib_1" && method === "GET") return json(library());
    if (path === "/libraries/lib_1" && method === "PATCH") {
      const { infer_location: on, remove_guesses: remove } = body as { infer_location: boolean; remove_guesses?: boolean };
      if (!on && guesses > 0 && !remove) {
        return json({ error: { code: "guesses_exist", message: "Turning Infer location off removes…",
                               details: { clips: guesses } } }, 409);
      }
      infer = on;
      if (!on) guesses = 0;
      return json(library());
    }
    return json({}, 404);
  });
});

afterEach(() => {
  cleanup();
  fetchMock.mockReset();
  vi.unstubAllGlobals();
  role = "admin";
  infer = false;
  guesses = 0;
  sent.length = 0;
});

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <LibraryLocationSection libraryId="lib_1" />
    </QueryClientProvider>,
  );
}

const patches = () => sent.filter((s) => s.method === "PATCH").map((s) => s.body);

describe("LibraryLocationSection", () => {
  it("turns Infer location on for the library", async () => {
    renderSection();
    const box = (await screen.findByRole("checkbox", { name: "Infer location" })) as HTMLInputElement;
    expect(box.checked).toBe(false);
    fireEvent.click(box);
    await waitFor(() => expect(patches()).toEqual([{ infer_location: true }]));
    await waitFor(() => expect((screen.getByRole("checkbox", { name: "Infer location" }) as HTMLInputElement).checked)
      .toBe(true));
  });

  it("asks before turning it off removes guesses, and removes them on yes", async () => {
    infer = true;
    guesses = 12;
    renderSection();
    fireEvent.click(await screen.findByRole("checkbox", { name: "Infer location" }));
    expect((await screen.findByRole("alert")).textContent).toContain("removes the location guesses of 12 clips");
    expect(infer).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Remove guesses" }));
    await waitFor(() => expect(patches()).toEqual([{ infer_location: false },
                                                   { infer_location: false, remove_guesses: true }]));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    expect((screen.getByRole("checkbox", { name: "Infer location" }) as HTMLInputElement).checked).toBe(false);
  });

  it("keeps it on when the question is cancelled, sending nothing more", async () => {
    infer = true;
    guesses = 1;
    renderSection();
    fireEvent.click(await screen.findByRole("checkbox", { name: "Infer location" }));
    expect((await screen.findByRole("alert")).textContent).toContain("guesses of 1 clip in");
    fireEvent.click(screen.getByRole("button", { name: "Keep it on" }));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(patches()).toEqual([{ infer_location: false }]);
    expect((screen.getByRole("checkbox", { name: "Infer location" }) as HTMLInputElement).checked).toBe(true);
  });

  it("shows someone who doesn't run the library whether it's on, with nothing to change", async () => {
    role = "editor";
    infer = true;
    renderSection();
    expect(await screen.findByText("Infer location: On")).toBeTruthy();
    expect(screen.queryByRole("checkbox")).toBeNull();
  });

  it("isn't shown by a server from before it was each library's", async () => {
    infer = undefined;
    const { container } = renderSection();
    await waitFor(() => expect(sent.some((s) => s.path === "/libraries/lib_1")).toBe(true));
    expect(container.textContent).toBe("");
  });
});
